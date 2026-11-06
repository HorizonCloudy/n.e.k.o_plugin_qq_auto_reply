from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from main_logic.tool_calling import ToolResult
from utils.llm_client import AIMessage, SystemMessage, create_chat_llm_async
from utils.token_tracker import set_call_type

from .companion_context_service import consent_enabled
from .gallery_service import AVATAR_TOOL_NAME, BROWSE_TOOL_NAME
from .memory_tool_service import RECALL_TOOL_HTTP_TIMEOUT_SECONDS, RECALL_TOOL_NAME
from .pipeline_models import (
    QQModelResult,
    QQPipelineStageTrace,
    QQReplyContext,
    is_synthetic_source,
)
from .plugin_tool_service import emit_bridge_log
from .plugin_tool_turn_guard import (
    TOOL_TURN_INSTRUCTION,
    UNEXECUTED_SEARCH_REPLY,
    unexecuted_search_promise,
)
from .session_bootstrap_service import MAX_TOOL_ITERATIONS

#: 表情包收集工具名。与 ``sticker_lab_service.build_save_definition`` 里的一致。
STICKER_SAVE_TOOL_NAME = "save_sticker"

# 工具轮超时预算里，LLM 流那部分最多折算几段。
#
# 额度是 MAX_TOOL_ITERATIONS，理论上最坏是 1 + 5 段流。但一段流正常只有
# 10~20 秒，六段也就两分钟；真按 6×单轮预算给，卡死的那一轮会把会话锁占满
# 七分钟（群里就是全群停摆），比直接判超时丢一轮更难收拾。所以只折算到这个
# 上限为止——撞上它说明模型在空转，不是工具慢。
TOOL_TURN_STREAM_CAP = 4

#: 挂了表情包收集工具时，额外留给"下载 + 看图"的时间。
#
# 这条路径有**两次网络往返**，都不在 LLM 流的预算里：把图拉下来（15 秒超时），
# 再让看图模型说一句（内层 15 秒 + 预处理余量）。按「一次最多收 2 张」算，
# 2 × (15 + 18) = 66 秒，这里给 70 秒。
#
# 只在这轮真的收到过图时才加（``QQStickerLab.should_offer_save``）。常时加会让
# 所有轮次的超时上限一起变长，卡死的时候用户白等多一分钟。
BUILTIN_TOOL_BUDGET_SECONDS = 70.0


class QQReplyGenerationService:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    async def generate_reply_fallback_direct_llm(
        self,
        *,
        context: QQReplyContext,
    ) -> Optional[str]:
        try:
            from utils.config_manager import get_config_manager

            if self.plugin._should_skip_direct_llm_fallback_for_images(message=context.message, attachments=context.attachments):
                self.plugin.logger.warning("QQ 图片消息跳过纯文本 fallback，避免假装已看图")
                return None
            model_config = get_config_manager().get_model_api_config("conversation")
            base_url = str(model_config.get("base_url") or "").strip()
            model = str(model_config.get("model") or "").strip()
            api_key = str(model_config.get("api_key") or "").strip()
            if not base_url or not model:
                self.plugin.logger.warning("Fallback 生成跳过：agent 模型未配置")
                return None
            llm = await create_chat_llm_async(
                model=model,
                base_url=base_url,
                api_key=api_key,
                max_completion_tokens=120,
                timeout=float(self.plugin._ai_turn_timeout_seconds or 60.0) + 0.5,
                provider_type=model_config.get("provider_type"),
            )
            try:
                set_call_type("conversation")
                fb_prompt, fb_recalled = self._sanitize_for_live_consent(
                    context, context.system_prompt, context.recalled_memory_text,
                )
                # 与主会话路径对偶：清洗只保证"调用发起时"的授权，调用期间
                # 撤销的话返回文本里仍带着那些内容。
                consent_before = self._consent_dependency_snapshot(context)
                self._store_consent_snapshot(context, consent_before)
                response = await llm.ainvoke([
                    {"role": "system", "content": self._compose_turn_instructions(fb_prompt, fb_recalled)},
                    {"role": "user", "content": context.prompt_message},
                ])
                fallback_reply = getattr(response, "content", "") or ""
                if fallback_reply and self._consent_dependency_revoked(
                    context, consent_before,
                ):
                    self.plugin.logger.warning(
                        "生成期间记忆授权被撤销，丢弃 fallback 回复"
                    )
                    return None
                if fallback_reply:
                    self.plugin.logger.info(f"Fallback 直连 LLM 生成成功 (length: {len(fallback_reply)})")
                    return fallback_reply
                self.plugin.logger.warning("Fallback 直连 LLM 未生成内容")
                return None
            finally:
                aclose = getattr(llm, "aclose", None)
                if callable(aclose):
                    try:
                        await aclose()
                    except Exception:
                        pass
        except Exception as e:
            self.plugin.logger.warning(f"Fallback 直连 LLM 生成失败: {e}")
            return None

    async def generate_fallback_from_context(self, context: QQReplyContext) -> Optional[str]:
        return await self.generate_reply_fallback_direct_llm(context=context)

    async def writeback_deepchat(self, context: QQReplyContext) -> None:
        """把一轮**成功**回复写回 DeepChat（情绪 / 关系 / 表达学习）。

        所有成功路径（host session 主路径、direct 模式、两条 fallback）都必须
        调用且只调用一次：primary 成功会在 ``run_primary_session_call`` 内部写回
        并提前 return，所以走 fallback 时这里才补上。

        合成控制轮（synthetic source）不写回——它不计入真实互动，写进去会把
        关系曲线和表达学习一起带偏。

        写回是**同步 SQLite + 纯 CPU** 的活儿，所以丢到线程里跑：直接在事件循环
        里调用会阻塞同一 loop 上的心跳/定时任务（``decay_all`` 之类的批量结算
        一旦变慢就更容易看出）。``to_thread`` 不改变语义，只把阻塞挪出 loop。
        """
        deepchat = getattr(self.plugin, "deepchat_service", None)
        if deepchat is None or is_synthetic_source(getattr(context, "source_kind", "")):
            return
        try:
            await asyncio.to_thread(
                deepchat.writeback,
                sender_id=context.sender_id,
                text=context.message,
                is_group=context.is_group,
                group_id=context.group_id,
            )
        except Exception as deepchat_error:  # noqa: BLE001 —— 写回失败不得影响回复
            self.plugin.logger.warning(f"[DeepChat] 情绪/关系写回失败: {deepchat_error}")

    async def run_primary_session_call(self, context: QQReplyContext) -> QQModelResult:
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        stage_trace = QQPipelineStageTrace(
            stage="model_primary",
            status="started",
            metadata={
                "session_key": session_key,
                "is_group": context.is_group,
                "group_id": str(context.group_id or ""),
                "ephemeral_session": context.ephemeral_session,
                "group_scene_mode": context.group_scene_mode,
            },
        )
        synthetic_hist_before = None
        try:
            user_data = await self.plugin.session_bootstrap_service.ensure_generation_session(context, session_key)
            if not user_data:
                stage_trace.status = "no_session"
                return QQModelResult(reply_text=None, source="none", traces=[stage_trace])

            # This evidence belongs to exactly one generation. Clear a
            # previous turn before prime/streaming can fail and leave a stale
            # truthy value for the delivery buffer to consume.
            user_data["human_row_materialized"] = False

            user_session, reply_chunks = self.plugin.session_runtime_service.prime_generation_session_state(
                user_data,
                session_key=session_key,
                context=context,
            )

            # 合成轮的 prompt 行在生成过程中进入历史：超时 salvage（下面
            # except 里的 discard→finalize）可能在 pipeline 的 finally 记录
            # 排除之前就把历史结算掉——生成前先记长度，超时时先排除再丢。
            synthetic_hist_before = (
                len(getattr(user_session, "_conversation_history", []) or [])
                if is_synthetic_source(getattr(context, "source_kind", ""))
                else None
            )

            try:
                ai_reply = await self._run_session_generation(
                    context=context,
                    session_key=session_key,
                    user_data=user_data,
                    user_session=user_session,
                    reply_chunks=reply_chunks,
                )
                pre_tool_text = str(
                    user_data.pop("current_pre_tool_text", "") or ""
                )
            finally:
                # 成员发言的收集绑定"会话已接受该 human 行"（stream_text 在
                # 发起网络流之前就把它追加进历史），不绑回复非空、也不绑
                # 生成成功：空回复轮与流异常/超时轮里成员的话都已进共享
                # 历史、会进群 digest，却会从 participant bucket 永久缺席。
                # 单点记录（成功钩子不再重复记）；recorder 自身按 sender
                # 追加，重复调用会重复入桶，故只此一处。
                if user_data.get("memory_enabled") and user_data.pop(
                    "human_row_accepted", False,
                ):
                    try:
                        self.plugin.session_memory_service.record_group_member_turn(
                            user_data, context,
                        )
                    except Exception as record_error:
                        # 绝不掩盖原始异常（finally 里抛出会替换掉
                        # TimeoutError，超时抢救与 trace 全部走偏）。
                        self.plugin.logger.warning(
                            f"成员发言记录失败: {record_error}"
                        )
            stage_trace.metadata["recalled_memory_used"] = context.recalled_memory_used
            stage_trace.metadata["recalled_memory_length"] = len(context.recalled_memory_text)
            if synthetic_hist_before is not None:
                # 合成控制轮（主动搭话等）的 human 行是捏造的指令，不是任何
                # 人真说过的话。写记忆之前先登记进排除名单——否则私聊 /cache
                # 会把"[系统] 现在你去主动找他说话……"整段当成主人说的话落库，
                # 下一轮她自己会接着念这段指令。超时分支另有一处调用（抢救要
                # 先排除再结算），两处都按对象身份去重，重复调用无副作用。
                try:
                    self.plugin.session_memory_service.record_synthetic_prompt_rows(
                        session_key, synthetic_hist_before,
                    )
                except Exception as exclude_error:
                    self.plugin.logger.warning(
                        f"合成轮 prompt 行排除失败（不影响回复）: {exclude_error}"
                    )
            if not ai_reply:
                self.plugin.logger.warning("AI 未生成回复，准备进入 fallback")
                stage_trace.status = "empty"
                stage_trace.metadata["reply_length"] = 0
                # 静默轮也要跑记忆管家：排空调度挂在这条路径上，一个模型
                # 一直选择沉默（或 fallback 也为空）的活跃群，否则群积压
                # 与成员队列永远不会被排空——队列到硬顶开始丢，历史被复读
                # 守卫重置时也没人抢救过。
                await self._run_memory_housekeeping(session_key, user_data)
                return QQModelResult(reply_text=None, source="session", allow_fallback=True, traces=[stage_trace])

            await self._sync_memory_after_success(
                session_key=session_key, user_data=user_data, context=context,
                reply_text=ai_reply,
            )
            await self.writeback_deepchat(context)
            self.plugin.logger.info(f"AI 生成回复完成 (会话: {session_key}, length: {len(ai_reply)})")
            stage_trace.status = "success"
            stage_trace.metadata["reply_length"] = len(ai_reply)
            return QQModelResult(
                reply_text=ai_reply,
                pre_tool_text=pre_tool_text,
                source="session",
                history_ai_row=user_data.get("current_turn_ai_row"),
                traces=[stage_trace],
            )

        except asyncio.TimeoutError:
            # discard_session 内部会先结算群 scoped 缓冲再丢弃（集中抢救）。
            self.plugin.logger.warning(f"会话 {session_key} 处理超时，关闭并丢弃该会话")
            if synthetic_hist_before is not None:
                # 抢救会立即 finalize：合成控制 prompt 行必须先进排除名单，
                # 否则 pipeline 层跑完后的记录来不及、控制指令被提取成
                # 参与者历史。
                try:
                    self.plugin.session_memory_service.record_synthetic_prompt_rows(
                        session_key, synthetic_hist_before,
                    )
                except Exception as salvage_error:
                    # 抢救标记失败不能连累丢弃：会话刚被强制取消，留着它
                    # 下一轮必再超时，且未结算状态会一直挂着。
                    self.plugin.logger.warning(
                        f"超时轮合成 prompt 行标记失败: {salvage_error}"
                    )
            discarded = await self.plugin.session_runtime_service.discard_session(session_key, reason="generation_timeout")
            if discarded is False:
                # 结算失败被有意保留：但本会话的 stream 刚被 wait_for 强制
                # 取消，直接复用会再次超时、陷入死循环。打粘性标记让下轮
                # bootstrap 先重试 discard（含集中抢救），与登录身份变化
                # 的 pending_identity_discard 模式对齐。
                kept = self.plugin._user_sessions.get(session_key)
                if kept is not None:
                    kept["pending_identity_discard"] = True
            stage_trace.status = "timeout"
            # 超时必须**允许**直连兜底：主会话已被强制取消，这一轮不可能再产出
            # 回复，而下游 reply_model_node 只在 allow_fallback 为真时才跑兜底。
            # 此前这里返回的 allow_fallback 留在默认 False，于是"供应商超时"
            # 等于静默不回——用户看到猫娘彻底没反应，日志里也只有一条 warning。
            return QQModelResult(
                reply_text=None, source="session", timed_out=True,
                allow_fallback=True, fallback_reason="session_timeout",
                traces=[stage_trace],
            )
        except Exception as e:
            self.plugin.logger.exception(f"AI 生成回复失败: {e}")
            stage_trace.status = "error"
            stage_trace.detail = str(e)
            # 同上：供应商报错/网络失败同样要走兜底，否则整轮静默。
            return QQModelResult(
                reply_text=None, source="none",
                allow_fallback=True, fallback_reason="session_error",
                traces=[stage_trace],
            )
        finally:
            if context.ephemeral_session:
                await self.plugin.session_runtime_service.discard_session(session_key, reason="ephemeral_cleanup")

    def _compose_turn_instructions(self, system_prompt: str, recalled_memory_text: str) -> str:
        return "\n\n".join(part for part in [system_prompt, recalled_memory_text] if part)

    async def _run_session_generation(
        self,
        *,
        context: QQReplyContext,
        session_key: str,
        user_data: dict[str, Any],
        user_session: Any,
        reply_chunks: list[str],
    ) -> str | None:
        async with user_data["lock"]:
            reply_chunks.clear()
            user_data["current_pre_tool_text"] = ""

            queued_images = await self.plugin._queue_attachment_images(user_session, context.attachments)
            self.plugin.logger.info(f"发送消息到 AI (会话: {session_key}, length: {len(context.prompt_message)}, images: {queued_images})")
            # 群会话是全群共享的：创建时烙进 system prompt 的是首个发言者的
            # member persona / 身份行。群轮必须无条件换上本轮刚构建好的
            # prompt（含当前发言人的 scoped persona），否则召回为空的轮次
            # （早期常态）会一直用创建者快照回答所有人。
            # 生成前最后一道复检（集中在一处，读点/构建后/锁内三段窗口
            # 共用同一判据）：共享会话锁与附件排队可能让本轮等很久，其间
            # 任一授权被撤销，已注入 prompt 的对应段都不得用于生成。
            turn_system_prompt, turn_recalled_text = self._sanitize_for_live_consent(
                context, context.system_prompt, context.recalled_memory_text,
            )
            # 生成前的依赖快照：模型已经读到 scoped/跨群内容后，撤销才落
            # 下的话，回复本身仍带着那些内容——生成结束要再比一次。
            consent_before = self._consent_dependency_snapshot(context)
            self._store_consent_snapshot(context, consent_before)
            history_before = len(
                getattr(user_session, "_conversation_history", []) or []
            )
            # 成员发言的收集绑定"共享历史真的收下了这条 human 行"。锁等待
            # 与附件排队都在 stream_text 之前，它们异常/取消/超时时这条消息
            # 根本没进历史——此时入 participant bucket 会造出会话里不存在的
            # 成员记忆。
            user_data["human_row_accepted"] = False
            user_data["human_row_materialized"] = False
            reply_attempt_state = user_data.setdefault(
                "reply_attempt_state", {"discard_epoch": 0},
            )
            tool_round_epoch: int | None = None
            raw_pre_tool_text = ""

            async def _capture_tool_round_start() -> None:
                nonlocal tool_round_epoch, raw_pre_tool_text
                emit_bridge_log(self.plugin, "INFO", f"[PluginTool] 模型进入工具轮 session={session_key}")
                current_epoch = int(
                    reply_attempt_state.get("discard_epoch", 0) or 0
                )
                if tool_round_epoch != current_epoch:
                    tool_round_epoch = current_epoch
                    raw_pre_tool_text = "".join(reply_chunks)

            restore_session_prompt = self._apply_turn_memory_context(
                user_session, turn_system_prompt, turn_recalled_text,
                # 私聊 participant 会话的 prompt 也是建会话时烙进去的：
                # 当前轮召回为空不代表旧 prompt 里没有 scoped memory。
                # 每轮都换成刚构建的 prompt，才能让生成内容与本轮依赖快照
                # 对齐；restore 保证持久会话仍保留创建时的原始 system 行。
                always_refresh=(
                    context.is_group
                    or bool(getattr(context, "companion_section", ""))
                    or bool(getattr(context, "cross_session_section", ""))
                    or user_data.get("private_memory_mode") == "participant"
                    or not bool(
                        (getattr(self.plugin, "_qq_settings", {}) or {}).get(
                            "allow_cross_group_context", False,
                        )
                    )
                ),
            )
            # recall_memory 工具按轮挂载：群会话是全群共享一个 client，而
            # participant subject 随发言人变——handler 闭包必须在会话锁内按
            # 本轮 context 重建，绝不能在建会话时冻结（那会让所有人都用首
            # 个发言者的 subject）。consent_before 传给闭包：工具读发生在
            # 生成中途，运行时记录要能被生成结束的撤销比对看到。
            armed_recall_tool, armed_bridge_tools, armed_builtin_tools = await self._arm_turn_tools(
                context=context,
                session_key=session_key,
                user_session=user_session,
                consent_before=consent_before,
                on_tool_round_start=_capture_tool_round_start,
            )
            generation_completed = False
            try:
                turn_timeout = self.plugin._ai_turn_timeout_seconds
                if armed_recall_tool:
                    # 工具轮的最坏路径是「初始流 + 每次工具迭代各一段流」，
                    # 额度 MAX_TOOL_ITERATIONS 时理论上是 6 段，再加一次召回
                    # HTTP。沿用单流预算会把"慢但会成功"的工具轮变成超时——
                    # 而这条路径超时的代价不是丢一轮，是丢弃整个共享群会话
                    # 再打粘性标记。LLM 那部分按 TOOL_TURN_STREAM_CAP 折算，
                    # 理由见该常量上的说明。
                    turn_timeout = (
                        turn_timeout
                        * min(MAX_TOOL_ITERATIONS + 1, TOOL_TURN_STREAM_CAP)
                        + RECALL_TOOL_HTTP_TIMEOUT_SECONDS
                    )
                if armed_bridge_tools:
                    # 插件工具那条更慢：跨插件是一次 IPC 往返，而且结果多半还要
                    # 再进一轮 LLM。按调用超时加预算，别让"慢但会成功"变成超时
                    # （超时会丢弃共享群会话，代价同上）。
                    #
                    # 按两次调用给预算，不是按 MAX_TOOL_ITERATIONS：一次交互里
                    # 连查两三个跨插件工具很正常（先列列表、再取详情），但连
                    # 调五次以上基本是模型在空转，给满会让最坏路径白拉长 100 秒。
                    from .plugin_tool_service import CALL_TIMEOUT_SECONDS

                    turn_timeout += CALL_TIMEOUT_SECONDS * 2
                if armed_builtin_tools:
                    # 收表情包是"下载 + 看图"两次网络往返，都不在 LLM 流的预算里。
                    # 不单独加的话，慢一点就会撞上超时——而这条路径超时的代价是
                    # 丢弃整个共享群会话，不是丢一张表情包。
                    turn_timeout += BUILTIN_TOOL_BUDGET_SECONDS
                turn_message = context.prompt_message
                if armed_bridge_tools:
                    turn_message += TOOL_TURN_INSTRUCTION
                    self._log_tool_transport_state(user_session, session_key)
                await asyncio.wait_for(
                    user_session.stream_text(turn_message),
                    timeout=turn_timeout,
                )

                completed = await self.plugin._wait_session_response_complete(user_session)
                if armed_bridge_tools:
                    self._finish_tool_audit(user_session, session_key, reply_chunks, history_before)
                if self._consent_dependency_revoked(context, consent_before):
                    # 生成期间授权被撤销：这条回复的 prompt 里带着已撤销的
                    # 内容，不能送出——清空出站文本只挡住了发送，stream_text
                    # 早已把 ai 行写进共享历史，留着它等于让被撤销的内容既
                    # 进 digest 又进后续轮次的上下文。本轮追加的 ai 行与
                    # tool 轮的裸 dict 行（assistant tool_calls / role=tool，
                    # content 里是召回原文）一并摘掉（human 行是用户自己的
                    # 发言，保留）。
                    self.plugin.logger.warning(
                        f"生成期间记忆授权被撤销，丢弃本轮回复 ({session_key})"
                    )
                    reply_chunks.clear()
                    history = getattr(user_session, "_conversation_history", None)
                    if isinstance(history, list):
                        while (
                            len(history) > history_before
                            and (
                                getattr(history[-1], "type", "") == "ai"
                                or self._is_tool_round_row(history[-1])
                            )
                        ):
                            history.pop()
                if not completed:
                    # 只 raise 不在这里 discard：外层 except TimeoutError 会
                    # 统一走"先抢救群缓冲再丢弃"，这里先 pop 会让 user_data
                    # 在抢救前就没了（原本也是双重 discard）。
                    self.plugin.logger.warning(f"会话 {session_key} 响应超时，关闭并丢弃该会话")
                    raise asyncio.TimeoutError
                generation_completed = True
            finally:
                if armed_recall_tool:
                    # 按轮挂载的对偶收尾：工具与 handler 不得越轮存活——
                    # 同一 client 上的其他生成路径（proactive 的
                    # prompt_ephemeral 等）绝不能带着本轮的 subject 闭包
                    # 发起召回。
                    for clear_slot in (
                        user_session.set_tools,
                        user_session.set_tool_call_handler,
                        getattr(
                            user_session,
                            "set_tool_round_start_callback",
                            None,
                        ),
                    ):
                        if not callable(clear_slot):
                            continue
                        try:
                            clear_slot(None)
                        except Exception:
                            # 单个卸载失败不能阻止另一槽位复位，也不能连累
                            # 下面的历史清理与成员轮记录。
                            pass
                restore_session_prompt()
                history_now = getattr(user_session, "_conversation_history", []) or []
                if isinstance(history_now, list):
                    # tool 轮写进共享历史的裸 dict 行随轮清理：召回原文是按
                    # consent 域临时授权给本轮的，语义与旧管线的"prompt 注入
                    # + restore"一致——留在共享历史里会进 digest、进后续每轮
                    # 的上下文，member 撤销后也无法再摘除。assistant tool-call
                    # 行里的 pre-tool 可见文本先折叠进最终 ai 行，再删掉携带
                    # tool metadata 的裸 dict，保证用户看到的文本与后续上下文
                    # 一致。
                    _, current_pre_tool_text = self._strip_tool_round_rows(
                        history_now,
                        history_before,
                        create_missing_ai_row=generation_completed,
                        outbound_text="".join(reply_chunks),
                        raw_pre_tool_text=(
                            raw_pre_tool_text
                            if tool_round_epoch == int(
                                reply_attempt_state.get(
                                    "discard_epoch", 0,
                                ) or 0
                            )
                            else None
                        ),
                    )
                    user_data["current_pre_tool_text"] = current_pre_tool_text
                appended = list(history_now)[history_before:]
                human_row_materialized = any(
                    getattr(row, "type", "") == "human" for row in appended
                )
                user_data["human_row_accepted"] = human_row_materialized
                # run_primary_session_call consumes human_row_accepted while
                # recording group-member turns. Delivery happens afterward,
                # so preserve separate evidence for reply buffering.
                user_data["human_row_materialized"] = human_row_materialized
                # 本轮真正写进历史的那条 ai 行（没有就是 None）。未投递打标
                # 按它的身份来：用"raw 输出非空"去推断历史里有行，是推断而
                # 不是证据——推断错了就会把上一条**已投递**的回复标成未投递，
                # 那条回复从此再也进不了 digest。
                user_data["current_turn_ai_row"] = next(
                    (row for row in reversed(appended)
                     if getattr(row, "type", "") == "ai"),
                    None,
                )
                self._stamp_nonconsent_boundary(user_data, user_session)

            return "".join(reply_chunks)

    def _log_tool_transport_state(self, session: Any, session_key: str) -> None:
        # This is the host's prepared payload, not proof of a wire request.
        payload = getattr(session, "_openai_tools_payload", None)
        count: Any = "unknown"
        if callable(payload):
            try:
                count = len(payload() or [])
            except Exception:
                count = "error"
        emit_bridge_log(
            self.plugin, "INFO",
            f"[PluginTool] 请求前诊断 session={session_key} "
            f"client={type(session).__name__} prepared_openai_tools={count} "
            f"unsupported={getattr(session, '_openai_tools_unsupported', False)} "
            f"unsupported_with_images={getattr(session, '_openai_tools_unsupported_with_images', False)}",
        )

    def _finish_tool_audit(self, session: Any, session_key: str, chunks: list,
                           history_before: int) -> None:
        audit = getattr(session, "_qq_tool_audit", {})
        started = audit.get("started", 0)
        history = getattr(session, "_conversation_history", [])
        recorded_calls = sum(
            len(row.get("tool_calls") or []) for row in history[history_before:]
            if isinstance(row, dict) and row.get("role") == "assistant"
        )
        emit_bridge_log(self.plugin, "INFO", f"[PluginTool] 本轮结束 session={session_key} "
                        f"calls_started={started} calls_returned={audit.get('returned', 0)} "
                        f"history_tool_calls={recorded_calls}")
        if started or not unexecuted_search_promise("".join(chunks)):
            return
        chunks[:] = [UNEXECUTED_SEARCH_REPLY]
        # Keep history aligned with what is actually sent; do not leave the
        # false promise for subsequent turns or memory extraction to reuse.
        for row in history[history_before:]:
            if getattr(row, "type", "") == "ai" and unexecuted_search_promise(row.content):
                row.content = UNEXECUTED_SEARCH_REPLY
            elif isinstance(row, dict) and row.get("role") == "assistant" and not row.get("tool_calls"):
                if unexecuted_search_promise(row.get("content", "")):
                    row["content"] = UNEXECUTED_SEARCH_REPLY
        emit_bridge_log(self.plugin, "WARN", f"[PluginTool] 拦截未执行的搜索承诺 session={session_key}")

    @staticmethod
    def _stamp_nonconsent_boundary(user_data: dict, user_session: Any) -> None:
        """未授权边界在生成轮 finally 记（调用点在 run_primary_session_call）。

        异常/空回复的 human 行也已进历史，只在成功路径记会漏（超时路径
        会话随后被弃，多记无害）。私聊轮同样记（此前限定 is_group）：
        participant 结算分支拿它当 digest 起点地板，OFF 时代的私聊行在
        开关翻 ON 后绝不回溯入库；legacy admin 路径不读该字段，多记无害。
        """
        if not user_data.get("memory_enabled"):
            user_data["nonconsent_history_end"] = len(
                getattr(user_session, "_conversation_history", []) or []
            )

    async def _arm_turn_tools(
        self,
        *,
        context: Any,
        user_session: Any,
        consent_before: dict,
        session_key: str = "",
        on_tool_round_start: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[bool, bool, bool]:
        """本轮挂到会话上的工具。

        返回 ``(挂了任何工具, 挂了插件工具, 挂了内置工具)``。三个值各有各的
        用处，别合并：第二个决定要不要注入 ``TOOL_TURN_INSTRUCTION``（那段讲的是
        搜索/跨插件任务，对收表情包完全不适用），第三个决定要不要为"下载 + 看图"
        额外留超时预算。

        **没有配置任何插件工具、也没收到新图时，这条路径退化成原来的
        `_arm_recall_tool`** —— 功能默认关闭，未启用的人拿到的行为与之前逐字节一致。

        插件工具（把别的插件的能力给模型用）**只在开放平台挂**：使用者定的范围。
        NapCat 那边是"群里什么样的人都有"的通道，多给一层工具面就多一层风险；
        等这边跑顺了再谈。
        """
        user_session._qq_tool_audit = {"started": 0, "returned": 0, "names": []}
        session_key = session_key or str(getattr(context, "session_key", "") or "")
        # 表情包：跟 recall / 插件工具是两条独立的线，但**不是常挂**——
        # 只在"闸没关、并且最近真的收到过图"时才给。常挂的代价是每一轮都往
        # prompt 里追加工具轮指令、每一轮都多留一份超时预算；而这些成本没有
        # 任何一张新图的时候什么也换不来（收表情包的前提是得先有人发图）。
        # 判据统一在 ``should_offer_save`` 里，这里不重复判策略。
        sticker_tools: list[Any] = []
        sticker_handlers: dict[str, Any] = {}
        sticker_lab = getattr(self.plugin, "sticker_lab", None)
        if sticker_lab is not None and sticker_lab.should_offer_save():
            try:
                sticker_tools.append(sticker_lab.build_save_definition())
                sticker_handlers[STICKER_SAVE_TOOL_NAME] = (
                    self._build_sticker_save_handler(sticker_lab, context)
                )
            except Exception as exc:
                self.plugin.logger.warning(
                    f"save_sticker 工具构建失败（这轮收不了表情包）: {exc}"
                )
                sticker_tools, sticker_handlers = [], {}
        bridge = getattr(self.plugin, "plugin_tool_service", None)
        bridge_tools: list[Any] = []
        mounted: list[tuple[dict[str, Any], str]] = []
        if bridge is not None:
            try:
                mounted, bridge_tools = await self._build_bridge_tools(context)
            except Exception:
                self.plugin.logger.warning("插件工具桥构建失败（本轮不挂插件工具）", exc_info=True)
                mounted, bridge_tools = [], []

        # 图库两条工具跟 recall / 插件工具是三条独立线：不管有没有插件工具，
        # 只要图库在，她就该能翻自己的图。所以先构造，再决定走哪条路。
        gallery_tools: list[Any] = []
        gallery_handlers: dict[str, Any] = {}
        gallery = getattr(self.plugin, "gallery_service", None)
        if gallery is not None and self._gallery_tools_allowed(context):
            try:
                gallery_handlers = self._build_gallery_handlers(gallery, context)
                gallery_tools = [
                    gallery.build_browse_definition(),
                    gallery.build_avatar_definition(),
                ]
            except Exception as exc:
                self.plugin.logger.warning(f"图库工具构建失败（这轮没有图库）: {exc}")
                gallery_tools, gallery_handlers = [], {}

        if not bridge_tools and not gallery_tools and not sticker_tools:
            armed = self._arm_recall_tool(
                context=context,
                user_session=user_session,
                consent_before=consent_before,
                on_tool_round_start=on_tool_round_start,
            )
            return armed, False, False

        # 合并挂载：recall（如果本轮该有）+ 插件工具，走一个按名字分发的 handler。
        set_tools = getattr(user_session, "set_tools", None)
        set_handler = getattr(user_session, "set_tool_call_handler", None)
        set_round_start = getattr(user_session, "set_tool_round_start_callback", None)
        if not callable(set_tools) or not callable(set_handler):
            return False, False, False

        recall_enabled = bool(getattr(context, "use_memory_context", False))
        definitions: list[Any] = []
        recall_handler = None
        if recall_enabled:
            definitions.append(self.plugin.memory_tool_service.build_recall_tool_definition())
            recall_handler = self._build_recall_tool_handler(
                context=context, consent_before=consent_before,
            )
        definitions.extend(sticker_tools)
        definitions.extend(bridge_tools)
        if gallery_tools:
            definitions.extend(gallery_tools)
        bridge_handler = None
        if bridge_tools and bridge is not None:
            bridge_handler = bridge.build_handler(
                mounted,
                session_key=session_key,
                conversation=self._bridge_conversation(context),
            )

        async def _dispatch(tool_call: Any) -> Any:
            name = str(getattr(tool_call, "name", "") or "")
            if recall_handler is not None and name == RECALL_TOOL_NAME:
                return await recall_handler(tool_call)
            audit = user_session._qq_tool_audit
            audit["started"] += 1
            audit["names"].append(name)
            builtin_handler = gallery_handlers.get(name) or sticker_handlers.get(name)
            if builtin_handler is not None:
                # 内置工具，不走插件工具桥那套日志（"调用开始 tool=xxx" 是给
                # 别的插件的工具留的痕迹）。异常直接冒到生成层，那里会兜成
                # "这张没存下来"，不会把这轮的回复整个带崩。
                result = await builtin_handler(tool_call)
                audit["returned"] += 1
                return result
            if bridge_handler is None:
                # 只挂了表情包工具、没挂插件工具时会有这个名字之外的东西：
                # 说明模型自己编了个工具名。明确报错，别静默吞掉。
                raise RuntimeError(f"本轮没有挂载名为 {name} 的工具")
            emit_bridge_log(self.plugin, "INFO", f"[PluginTool] 调用开始 session={session_key} tool={name}")
            try:
                result = await bridge_handler(tool_call)
            except Exception:
                emit_bridge_log(self.plugin, "WARN", f"[PluginTool] 处理器异常 tool={name}")
                raise
            audit["returned"] += 1
            return result

        try:
            set_tools(definitions)
            set_handler(_dispatch)
            if on_tool_round_start is not None and callable(set_round_start):
                set_round_start(on_tool_round_start)
        except Exception as exc:
            self.plugin.logger.warning(f"本轮工具挂载失败（本轮无工具）: {exc}")
            for clear_slot in (set_tools, set_handler):
                try:
                    clear_slot(None)
                except Exception:
                    pass
            return False, False, False
        emit_bridge_log(self.plugin, 
            "INFO",
            f"[PluginTool] 本轮挂载 {len(bridge_tools)} 个插件工具"
            f" + {len(gallery_tools) + len(sticker_tools)} 个内置工具"
            f"（权限 {getattr(context, 'permission_level', '') or 'unknown'}）: "
            + "、".join(row["plugin_id"] for row, _tier in mounted),
        )
        return True, bool(bridge_tools), bool(sticker_tools or gallery_tools)

    @staticmethod
    def _bridge_conversation(context: Any) -> dict[str, Any]:
        """这一轮的**会话身份**：异步任务的结果要回到同一场对话（见
        `plugin_tool_followup_service`）。记忆策略也一起带走 —— 回投是同一场对话的
        下一轮，不是另起一个临时会话。"""
        return {
            "sender_id": str(getattr(context, "sender_id", "") or ""),
            "is_group": bool(getattr(context, "is_group", False)),
            "group_id": str(getattr(context, "group_id", "") or ""),
            "user_nickname": str(getattr(context, "user_nickname", "") or ""),
            "permission_level": str(getattr(context, "permission_level", "") or ""),
            "use_memory_context": getattr(context, "use_memory_context", None),
            "persist_memory": getattr(context, "persist_memory", None),
            "ephemeral_session": bool(getattr(context, "ephemeral_session", False)),
            "group_scene_mode": str(getattr(context, "group_scene_mode", "") or ""),
        }

    async def _build_bridge_tools(self, context: Any) -> tuple[list[tuple[dict[str, Any], str]], list[Any]]:
        """把「白名单 ∧ 已启动 ∧ 这个人有权用」的插件变成工具定义。"""
        from .pipeline_models import KIND_PLUGIN_TOOL_RESULT
        from .plugin_tool_service import (
            QQPluginToolService,
            channel_allows_plugin_tools,
        )

        if str(getattr(context, "source_kind", "") or "") == KIND_PLUGIN_TOOL_RESULT:
            # 结果回投那一轮**不再挂插件工具**：这一轮的任务是把已经到手的结果说出去，
            # 而挂上工具就等于"一个异步结果可以生出下一个异步任务"，会跑成环。
            return [], []
        client = getattr(self.plugin, "qq_client", None)
        if not channel_allows_plugin_tools(client):
            return [], []
        service = self.plugin.plugin_tool_service
        tiers = service.tiers()
        if not tiers:
            return [], []
        allowed = service.allowed_tiers_for(getattr(context, "permission_level", ""))
        if not allowed:
            # 认不出来的人（权限 none）：一个插件工具都不给。
            emit_bridge_log(self.plugin, "INFO", "[PluginTool] 说话人不在名册里，本轮不挂插件工具")
            return [], []
        # 候选表是**全量非 QQ 插件**（界面也是它）；挂不挂由 select_mounted 的启动闸决定。
        candidates = await service.list_candidates()
        mounted = service.select_mounted(candidates, allowed_tiers=allowed)
        skipped = sorted(set(tiers) - {row["plugin_id"] for row, _tier in mounted})
        if skipped:
            # 留痕：配了但没挂上，通常是"那个插件此刻没在跑"或"档位不对这个人开放"。
            emit_bridge_log(self.plugin, 
                "INFO", f"[PluginTool] 本次未挂载: {'、'.join(skipped)}（未启动 / 档位不符）",
            )
        return mounted, [
            QQPluginToolService.build_tool_definition(self.plugin.plugin_tool_service, row, tier)
            for row, tier in mounted
        ]

    def _build_sticker_save_handler(self, sticker_lab: Any, context: Any):
        """本轮的表情包收集闭包。

        "收哪一张"不从模型拿编号——她看不见序号，只会瞎猜。默认收**这条会话里
        最近收到的那张**，覆盖绝大多数「这个好玩，存了」的场面。

        跨群能不能收不在这一层判：session 的 group_id 原样交给
        ``execute_save``，由 ``sticker_collect_policy`` 那一档决定（见
        ``sticker_lab_service.check``）。放这里判的话，私聊里 group_id 是空串，
        会把"哪个群都行"当成默认，闸就形同虚设。
        """

        async def _handle_save_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or STICKER_SAVE_TOOL_NAME
            try:
                output = await sticker_lab.execute_save(
                    getattr(tool_call, "arguments", None) or {},
                    sender_id=str(getattr(context, "sender_id", "") or ""),
                    group_id=str(getattr(context, "group_id", "") or ""),
                )
            except Exception as exc:
                self.plugin.logger.warning(f"save_sticker 执行失败: {exc}")
                output = "（这张没存下来。别跟对方说你存了。）"
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_save_tool

    def _arm_recall_tool(
        self,
        *,
        context: Any,
        user_session: Any,
        consent_before: dict,
        on_tool_round_start: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Install this turn's recall_memory tool + handler on the client.

        Returns whether the tool is actually armed. This is the ONLY
        recall channel: when arming fails the turn simply has no recall.
        There is deliberately no build-time fallback recall to catch it —
        that path fired a retrieval round-trip on every single turn, even
        the ones whose reply never needed memory.
        """
        if not getattr(context, "use_memory_context", False):
            return False
        set_tools = getattr(user_session, "set_tools", None)
        set_handler = getattr(user_session, "set_tool_call_handler", None)
        set_round_start = getattr(
            user_session, "set_tool_round_start_callback", None,
        )
        if not callable(set_tools) or not callable(set_handler):
            return False
        try:
            set_tools([
                self.plugin.memory_tool_service.build_recall_tool_definition()
            ])
            set_handler(self._build_recall_tool_handler(
                context=context,
                consent_before=consent_before,
            ))
            if on_tool_round_start is not None and callable(set_round_start):
                set_round_start(on_tool_round_start)
            return True
        except Exception as exc:
            self.plugin.logger.warning(
                f"recall_memory 工具挂载失败（本轮无召回）: {exc}"
            )
            # 各槽位独立做 best-effort 清理：任一个卸载失败都不能阻止
            # 其他槽位复位，否则返回 False 后可能把半挂载状态带到下一轮。
            clear_slots = [set_tools, set_handler]
            if on_tool_round_start is not None and callable(set_round_start):
                clear_slots.append(set_round_start)
            for clear_slot in clear_slots:
                try:
                    clear_slot(None)
                except Exception:
                    # 这里只能 best-effort：原始挂载异常必须保持为本轮降级，
                    # 单个槽位拒绝复位也不能阻止另一槽位继续清理。
                    pass
            return False

    def _gallery_tools_allowed(self, context: Any) -> bool:
        """Global images and account mutations are reserved for account admins.

        Re-read the roster at execution time, so a tool mounted before a role
        revocation cannot still mutate the account. Group privileges alone are
        not account-administrator privileges.
        """
        sender = str(getattr(context, "sender_id", "") or "").strip()
        if not sender:
            return False
        manager = getattr(self.plugin, "permission_mgr", None)
        if manager is not None:
            try:
                level = manager.get_permission_level(sender)
            except Exception:
                return False
            if level == "blacklist":
                return False
            return level == "admin" or sender == str(getattr(self.plugin, "_admin_qq", "") or "")
        return sender == str(getattr(self.plugin, "_admin_qq", "") or "")

    def _build_gallery_handlers(self, gallery: Any, context: Any) -> dict[str, Any]:
        """图库两个工具的闭包：``browse_gallery`` / ``set_avatar``。

        两个工具是**两步走**（先看有什么，再挑一个换上），所以这一轮必须给够
        工具轮数——`max_tool_iterations` 是 1 的时候，她只能翻到一半，
        然后把 ``set_avatar`` 当成普通文字吐出来（实测就是这样：她自己编了
        一个 index 参数，头像当然没换）。
        """

        async def _handle_browse(tool_call: Any) -> ToolResult:
            if not self._gallery_tools_allowed(context):
                return ToolResult(
                    call_id=getattr(tool_call, "call_id", "") or "",
                    name=BROWSE_TOOL_NAME, output="只有账号管理员可以查看全局图库或修改 QQ 头像。",
                )
            arguments = getattr(tool_call, "arguments", None) or {}
            query = str(arguments.get("query") or "").strip()
            try:
                limit = int(arguments.get("limit") or 8)
            except (TypeError, ValueError):
                limit = 8
            limit = max(1, min(limit, 20))
            try:
                await gallery.start()
                items = await asyncio.to_thread(
                    gallery.list_items, query=query, limit=limit
                )
            except Exception:
                self.plugin.logger.warning("浏览图库失败", exc_info=True)
                items = []
            if not items:
                output = (
                    "图库里没有符合条件的图。" if query
                    else "图库还是空的：她还没收过图，也没人往里放过。"
                )
            else:
                lines = [
                    f"{item.get('id')}　{item.get('desc') or '（这张还没描述）'}"
                    for item in items
                ]
                output = "图库里有这些（编号 描述）：\n" + "\n".join(lines)
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=getattr(tool_call, "name", "") or BROWSE_TOOL_NAME,
                output=output,
            )

        async def _handle_avatar(tool_call: Any) -> ToolResult:
            if not self._gallery_tools_allowed(context):
                return ToolResult(
                    call_id=getattr(tool_call, "call_id", "") or "",
                    name=AVATAR_TOOL_NAME, output="只有账号管理员可以查看全局图库或修改 QQ 头像。",
                )
            arguments = getattr(tool_call, "arguments", None) or {}
            key = str(arguments.get("id") or "").strip()
            if not key:
                output = "没给编号。先用 browse_gallery 看一眼有哪些图。"
            else:
                try:
                    result = await gallery.set_avatar(key)
                except Exception:
                    self.plugin.logger.warning("换头像失败", exc_info=True)
                    result = {"ok": False, "reason": "调用出错了"}
                if not result.get("ok"):
                    output = (
                        "没换成："
                        + str(result.get("reason") or result.get("message") or "原因不明")
                    )
                else:
                    verified = result.get("verified")
                    if verified is True:
                        tail = "，已经确认换上了"
                    elif verified is False:
                        # 回执成功但反查没变：说清楚，别让她以为换成了还去邀功
                        tail = "，但重新拉头像看还是原来那张，可能过一会儿才生效"
                    else:
                        tail = "（回执成功，但这会儿查不到头像，没法确认）"
                    output = f"头像换成 {result.get('id')} 了{tail}"
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=getattr(tool_call, "name", "") or AVATAR_TOOL_NAME,
                output=output,
            )

        return {BROWSE_TOOL_NAME: _handle_browse, AVATAR_TOOL_NAME: _handle_avatar}

    def _build_recall_tool_handler(
        self, *, context: Any, consent_before: dict,
    ):
        """This turn's recall_memory execution closure.

        Subjects never come from the model: ``execute_recall`` derives
        them from the turn context (the server reads an omitted subjects
        field as the legacy PRIVATE corpus). The closure also owns the
        runtime consent record. Model-authored pre-tool text is ordinary
        assistant output and remains in the outbound buffer.
        """

        # 一轮一次召回的闸在 handler 层：max_tool_iterations=1 只限 LLM/
        # tool 循环轮数，模型在同一个 assistant 回复里可以并排发多个
        # recall_memory 调用（客户端会逐个执行），流内重试也会再次进
        # tool 轮——每次都是一段 5s HTTP，会击穿超时预算里"一次召回"的
        # 假设，而这条路径超时的代价是丢弃整个共享群会话。空参试探
        # （execute_recall 本就不发 HTTP）不烧额度。
        recall_executed = [False]

        async def _handle_recall_tool(tool_call: Any) -> ToolResult:
            tool_service = self.plugin.memory_tool_service
            arguments = getattr(tool_call, "arguments", None) or {}
            substantive = tool_service.has_recall_arguments(arguments)
            if substantive and recall_executed[0]:
                self.plugin.logger.info(
                    "recall_memory 本轮已执行过，追加调用返回空结果"
                )
                return ToolResult(
                    call_id=getattr(tool_call, "call_id", "") or "",
                    name=getattr(tool_call, "name", "") or "recall_memory",
                    output=tool_service.no_result_text(),
                )
            if substantive:
                recall_executed[0] = True
            output, consumed = await tool_service.execute_recall(
                context=context,
                arguments=arguments,
            )
            if consumed:
                # consent 判据从"prompt 里有没有那段字"换成"运行时有没有
                # 真的发生这次读"：写进本轮的 consent_before（生成结束的
                # 撤销比对读它），并合入 context.consent_snapshot（发送前
                # 与 buffer 的撤销闸读它）。recalled_memory_used 不在这里
                # 记——它跟的是"召回内容被消费"而非"消费了群授权"，私聊
                # legacy 召回 consumed 恒空，由 execute_recall 在回填点记。
                for key, was_enabled in consumed.items():
                    consent_before[key] = (
                        bool(consent_before.get(key)) or bool(was_enabled)
                    )
                self._store_consent_snapshot(context, consumed)
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=getattr(tool_call, "name", "") or "recall_memory",
                output=output,
            )

        return _handle_recall_tool

    @staticmethod
    def _is_tool_round_row(row: Any) -> bool:
        """A bare dict row the client's tool loop appended to history.

        Two shapes (OpenAI-compat and genai paths both append these):
        the assistant turn announcing tool_calls, and the role=tool
        result row carrying the recalled text.
        """
        if not isinstance(row, dict):
            return False
        role = row.get("role")
        return role == "tool" or (
            role == "assistant" and bool(row.get("tool_calls"))
        )

    def _strip_tool_round_rows(
        self,
        history: list,
        start_index: int,
        *,
        create_missing_ai_row: bool = False,
        outbound_text: str = "",
        raw_pre_tool_text: str | None = None,
    ) -> tuple[int, str]:
        """Fold visible pre-tool text into the final AI row, then remove tool rows.

        Only rows appended at or after ``start_index`` are considered —
        the rows sit BETWEEN the human row and the final ai row, so this
        scans by index instead of popping from the tail. A repetition
        guard can reset the history to shorter than ``start_index``; the
        range is then empty and nothing is touched.
        """
        start = max(start_index, 0)
        assistant_tool_rows = [
            row for row in history[start:]
            if (
                isinstance(row, dict)
                and row.get("role") == "assistant"
                and row.get("tool_calls")
                and isinstance(row.get("content"), str)
            )
        ]
        persisted_pre_tool_text = "".join(
            str(row.get("content") or "") for row in assistant_tool_rows
        )
        final_ai_row = next(
            (
                row for row in reversed(history[start:])
                if getattr(row, "type", "") == "ai"
            ),
            None,
        )
        final_content = getattr(final_ai_row, "content", None)
        structural_boundary = (
            raw_pre_tool_text is not None or bool(assistant_tool_rows)
        )
        boundary_source = (
            raw_pre_tool_text
            if raw_pre_tool_text is not None
            else persisted_pre_tool_text
        )
        if (
            raw_pre_tool_text is not None
            and len(persisted_pre_tool_text) > len(raw_pre_tool_text)
            and persisted_pre_tool_text.startswith(raw_pre_tool_text)
            and outbound_text.startswith(persisted_pre_tool_text)
        ):
            # Focus 的 thinking stripper 会在 core 发出 tool sentinel 后才
            # flush 被延迟的可见 residual；round-start callback 因而可能只
            # 捕获到真实 prefix 的前半段（最窄情况是空串）。provider 已将
            # 完整 prefix 写入 assistant tool row，可与真实 outbound 对齐时
            # 用它补齐结构边界，仍不修改或丢弃任何模型文本。
            boundary_source = persisted_pre_tool_text
        raw_final_text = ""
        if (
            boundary_source is not None
            and outbound_text.startswith(boundary_source)
        ):
            raw_final_text = outbound_text[len(boundary_source):]
        elif (
            isinstance(final_content, str)
            and final_content
            and outbound_text.endswith(final_content)
        ):
            raw_final_text = final_content

        # QQ 的 sanitizer 对 dangling thinking close tag 是整轮上下文相关的：
        # 单独清洗 prefix 会把本应随 close tag 一起丢弃的内容重新泄进 history。
        # 因此先清洗真实完整出站，再减去同规则清洗后的最终段，所得才是
        # 用户实际可见的结构前缀；内部空白也会原样保留为段间分隔符。
        sanitize = getattr(self.plugin, "_sanitize_generated_reply", None)
        if callable(sanitize):
            visible_outbound = str(sanitize(outbound_text) or "")
            visible_final = str(sanitize(raw_final_text) or "")
        else:
            visible_outbound = outbound_text
            visible_final = raw_final_text
        if visible_final and visible_outbound.endswith(visible_final):
            boundary_pre_tool_text = visible_outbound[:-len(visible_final)]
        elif not visible_final:
            boundary_pre_tool_text = visible_outbound
        else:
            boundary_pre_tool_text = ""

        # 共享历史以 QQ 最终可见的整轮文本为准；只有纯空白时不凭空合成
        # 一条用户没有看到的 AI 行。
        if final_ai_row is not None and not visible_outbound.strip():
            # terminal truncation callback 会先把 raw recovery 写入 history；
            # 若整轮经 QQ sanitizer 后没有任何可见文本，这行既未投递也不
            # 能进入后续上下文/记忆。按对象身份删除，避免误删等值旧行。
            for index in range(len(history) - 1, start - 1, -1):
                if history[index] is final_ai_row:
                    del history[index]
                    break
        elif visible_outbound.strip():
            if final_ai_row is None and create_missing_ai_row:
                history.append(AIMessage(content=visible_outbound))
            elif final_ai_row is not None:
                final_ai_row.content = visible_outbound

        removed = 0
        for index in range(len(history) - 1, start - 1, -1):
            if self._is_tool_round_row(history[index]):
                del history[index]
                removed += 1
        return (
            removed,
            boundary_pre_tool_text
            if structural_boundary and boundary_pre_tool_text.strip()
            else "",
        )

    async def _run_memory_housekeeping(
        self, session_key: str, user_data: dict[str, Any],
    ) -> None:
        """Schedule the backlog / member-bucket drains for this session.

        Shared by the success path and the silent-turn path: the drains are
        the only thing standing between an always-busy group and a queue
        that discards at its hard limit."""
        try:
            await self.plugin._cache_session_delta(session_key, user_data)
        except Exception as exc:
            self.plugin.logger.warning(f"记忆管家调度失败（忽略）: {exc}")

    def _consent_dependency_snapshot(self, context: Any) -> dict:
        """Which consent switches this turn's prompt actually depends on.

        Not group-only: with cross-group consent on, a PRIVATE reply's
        sessions block can name other groups and contacts, so that turn
        depends on the switch too."""
        settings = getattr(self.plugin, "_qq_settings", {}) or {}
        snapshot: dict = {}
        token = getattr(context, "companion_token", "")
        if token and getattr(context, "companion_section", ""):
            snapshot[token] = True
        if getattr(context, "cross_session_section", ""):
            snapshot["allow_cross_group_context"] = bool(
                settings.get("allow_cross_group_context", False)
            )
        if not getattr(context, "is_group", False):
            if getattr(context, "participant_memory_enabled", False) and (
                getattr(context, "core_memory_text", "")
                or getattr(context, "recalled_memory_text", "")
            ):
                # 私聊 participant 轮的 prompt 依赖该开关（对偶群轮的
                # group_memory_enabled 记账）：发送前撤销复检要覆盖它。
                snapshot["private_participant_memory_enabled"] = bool(
                    settings.get("private_participant_memory_enabled", False)
                )
            return snapshot
        if getattr(context, "core_memory_text", "") or getattr(
            context, "recalled_memory_text", "",
        ):
            snapshot["group_memory_enabled"] = bool(
                settings.get("group_memory_enabled", False)
            )
            if getattr(context, "used_member_subject", False):
                snapshot["group_member_memory_enabled"] = bool(
                    settings.get("group_member_memory_enabled", False)
                )
        if getattr(context, "cross_group_section", ""):
            snapshot["allow_cross_group_context"] = bool(
                settings.get("allow_cross_group_context", False)
            )
        return snapshot

    def _store_consent_snapshot(self, context: Any, snapshot: dict) -> None:
        """Carry the generation-time snapshot to the pre-send gate.

        Unions with whatever the context already carries: a nested
        synthetic turn inherits the buffered drafts' dependencies, and its
        own (clean) prompt must not erase them."""
        try:
            merged = dict(getattr(context, "consent_snapshot", None) or {})
            for key, was_enabled in (snapshot or {}).items():
                merged[key] = bool(merged.get(key)) or bool(was_enabled)
            context.consent_snapshot = merged
        except Exception:
            # 合成调用方可能传的是轻量对象：拿不到就退回"发送前不复检"，
            # 生成后的复检仍在。
            pass

    def _consent_dependency_revoked(self, context: Any, before: dict) -> bool:
        """True when a switch this prompt relied on went off since `before`."""
        if not before:
            return False
        return any(
            was_enabled and not consent_enabled(self.plugin, key)
            for key, was_enabled in before.items()
        )

    def _sanitize_for_live_consent(
        self, context: Any, system_prompt: str, recalled_text: str,
    ) -> tuple[str, str]:
        """Drop prompt sections whose consent is no longer live.

        One place for all three switches so every generation path (primary
        session call, direct fallback) enforces the same boundary — the
        per-path rechecks kept diverging as new paths appeared."""
        settings = getattr(self.plugin, "_qq_settings", {}) or {}
        token = getattr(context, "companion_token", "")
        if token and not consent_enabled(self.plugin, token):
            system_prompt = self._strip_section_text(system_prompt, getattr(context, "companion_section", ""))
            context.system_prompt = self._strip_section_text(
                context.system_prompt, getattr(context, "companion_section", ""),
            )
            context.companion_section = ""
            context.companion_token = ""
        if not settings.get("allow_cross_group_context", False):
            # 私聊轮也可能带会话清单段（跨群开关打开时它会列出其他群与
            # 联系人）：非群轮不能在这里直接返回，否则那段撤不掉。
            system_prompt = self._strip_section_text(
                system_prompt, getattr(context, "cross_session_section", "") or "",
            )
        if not getattr(context, "is_group", False):
            if getattr(
                context, "participant_memory_enabled", False,
            ) and not settings.get("private_participant_memory_enabled", False):
                # 私聊 participant 轮的授权在生成前被撤销：scoped 召回与
                # bootstrap 段全部撤除（对偶下面群分支的撤法）。
                recalled_text = ""
                system_prompt = self._strip_section_text(
                    system_prompt,
                    getattr(context, "core_memory_text", "") or "",
                )
            return system_prompt, recalled_text
        core_text = getattr(context, "core_memory_text", "") or ""
        if not settings.get("group_memory_enabled", False):
            # 群记忆关闭：scoped 召回与 bootstrap 段全部撤除。
            recalled_text = ""
            system_prompt = self._strip_section_text(system_prompt, core_text)
        elif getattr(context, "used_member_subject", False) and not settings.get(
            "group_member_memory_enabled", False,
        ):
            # 仅 member 关闭：召回混合了群域与 participant 域、无法事后
            # 拆分，连同 participant 派生的 bootstrap 段一起撤除。
            recalled_text = ""
            system_prompt = self._strip_section_text(system_prompt, core_text)
        if not settings.get("allow_cross_group_context", False):
            system_prompt = self._strip_section_text(
                system_prompt, getattr(context, "cross_group_section", "") or "",
            )
        return system_prompt, recalled_text

    @staticmethod
    def _strip_section_text(system_prompt: str, section_text: str) -> str:
        """Remove one composed section (with its separator) from a prompt."""
        if not section_text or section_text not in system_prompt:
            return system_prompt
        separator = "\n\n"
        for candidate in (
            separator + section_text, section_text + separator, section_text,
        ):
            if candidate in system_prompt:
                return system_prompt.replace(candidate, "", 1)
        return system_prompt

    @staticmethod
    def _strip_scoped_sections(system_prompt: str, context: Any) -> str:
        """Remove scoped-memory sections from an already-composed prompt.

        Used when group memory is revoked between context construction and
        generation: the bootstrap section is the only scoped block left in
        the prompt (recall is passed separately and simply dropped)."""
        return QQReplyGenerationService._strip_section_text(
            system_prompt, getattr(context, "core_memory_text", "") or "",
        )

    def _apply_turn_memory_context(
        self, user_session: Any, system_prompt: str, recalled_memory_text: str,
        *, always_refresh: bool = False,
    ):
        # always_refresh：群轮即使无召回也要换 prompt——
        # _compose_turn_instructions 会自动省略空的召回段，swap 退化为
        # 纯 system_prompt 替换；restore 保证会话落盘的仍是创建时原文。
        if not recalled_memory_text and not always_refresh:
            return lambda: None
        conversation_history = getattr(user_session, "_conversation_history", None)
        if not conversation_history or not isinstance(conversation_history[0], SystemMessage):
            return lambda: None
        original_system_message = conversation_history[0]
        original_instructions = getattr(user_session, "_instructions", original_system_message.content)
        enhanced_instructions = self._compose_turn_instructions(system_prompt, recalled_memory_text)
        conversation_history[0] = SystemMessage(content=enhanced_instructions)
        user_session._instructions = enhanced_instructions

        def restore() -> None:
            current_history = getattr(user_session, "_conversation_history", None)
            if current_history and current_history[0] is not original_system_message:
                current_history[0] = original_system_message
            user_session._instructions = original_instructions

        return restore

    async def _sync_memory_after_success(
        self,
        *,
        session_key: str,
        user_data: dict[str, Any],
        context: QQReplyContext,
        reply_text: str = "",
        sync_tail_ai: bool = False,
    ) -> None:
        if user_data.get("memory_enabled"):
            try:
                # member turn 已在主生成完成点单点记录（含空回复轮）；此处
                # 只做 cache 同步，避免同一发言重复入 bucket。
                count = await self.plugin._cache_session_delta(
                    session_key, user_data,
                    # 生成成功不代表发送成功。尾部回复只在投递确认后同步。
                    sync_tail_ai=sync_tail_ai,
                )
                if count:
                    self.plugin.logger.info(f"[管理员] 成功同步 {count} 条消息到 Memory Server (会话: {session_key})")
            except Exception as e:
                self.plugin.logger.error(f"记忆同步失败: {e}")
            # mention 计数不在这里记：本钩子跑在生成成功时刻，buffer 可能
            # 把这条回复截停并用 summary 取代——没投递的草稿不得推进
            # suppression 计数。投递点统一调 record_scoped_mentions_on_delivery。
            return

        if user_data.get("memory_context_used"):
            self.plugin.logger.info(f"[临时发送] 已使用记忆上下文但跳过记忆同步 (会话: {session_key})")
            return
        if context.is_group:
            # 未授权边界已在 run_primary_session_call 每次尝试后统一记录。
            self.plugin.logger.info(f"[群聊] 跳过记忆同步 (群: {context.group_id}, 用户: {context.sender_id})")
            return
        self.plugin.logger.info(f"[非管理员] 跳过记忆同步 (用户: {context.sender_id}, 权限: {context.permission_level})")

    def append_fallback_ai_row(
        self, context: QQReplyContext, reply_text: str,
    ) -> None:
        """Put a delivered direct-fallback reply into the shared history.

        The primary session accepted the human row but produced nothing, so
        the fallback's text exists only in the outbound message: without
        this a scoped digest persists a one-sided conversation and loses
        whatever the bot disclosed. Idempotent — the row is tagged so a
        second delivery hook cannot double-append."""
        is_group = bool(getattr(context, "is_group", False))
        is_participant = bool(
            not is_group
            and getattr(context, "participant_memory_enabled", False)
        )
        if not (is_group or is_participant) or not reply_text:
            return
        if getattr(context, "ephemeral_session", False):
            return
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        user_data = (getattr(self.plugin, "_user_sessions", {}) or {}).get(
            session_key
        )
        if not user_data or not user_data.get("memory_enabled"):
            return
        if (
            is_participant
            and user_data.get("private_memory_mode") != "participant"
        ):
            return
        session = user_data.get("session")
        history = getattr(session, "_conversation_history", None)
        if history is None:
            return
        # 幂等键取本轮消息 ID：context 对象可能被重建（构造时的 turn_uid
        # 就变了），而重复的投递钩子未必紧挨着——扫最近几行也会漏，故全
        # 历史精确匹配。没有消息 ID 的轮次（主动发言/合成轮/平台缺字段）
        # 退到 context 构造时生成的 turn_uid：绝不能用 id(context)，地址
        # 复用会让后续每一条 fallback 行都被误判成重复而永久丢失。
        turn_id = (
            str(getattr(context, "current_message_id", "") or "")
            or str(getattr(context, "turn_uid", "") or "")
        )
        if not turn_id:
            history.append(self._build_fallback_row(reply_text, ""))
            return
        marker = f"fallback:{turn_id}"
        for msg in reversed(history):
            if getattr(msg, "type", "") == "ai" and (
                getattr(msg, "additional_kwargs", None) or {}
            ).get("neko_fallback_row") == marker:
                return
        history.append(self._build_fallback_row(reply_text, marker))

    @staticmethod
    def _build_fallback_row(reply_text: str, marker: str):
        try:
            from langchain_core.messages import AIMessage

            row = AIMessage(content=reply_text)
            row.additional_kwargs["neko_fallback_row"] = marker
            return row
        except Exception:
            return SimpleNamespace(
                type="ai", content=reply_text,
                additional_kwargs={"neko_fallback_row": marker},
            )

    async def record_scoped_mentions_on_delivery(
        self, context: QQReplyContext, reply_text: str,
    ) -> None:
        """Bump scoped mention counters when a reply is ACTUALLY delivered.

        群路径绕开 legacy post_turn，scoped 条目的 mention 计数（防重复注入
        的 suppression 输入）只能在插件侧补记——且必须绑定投递而非生成：
        buffer 合并场景的草稿没人看到，各记一次会把被引用条目推进 suppression
        阈值、错误地从后续上下文消失。best-effort：失败只影响该条目晚几轮
        进入"暂不主动提及"。"""
        if not reply_text or context.ephemeral_session:
            return
        if not context.is_group:
            await self._record_participant_mentions_on_delivery(
                context, reply_text,
            )
            return
        if not (getattr(self.plugin, "_qq_settings", {}) or {}).get(
            "group_memory_enabled", False,
        ):
            # mention 计数是对群域记忆元数据的写：开关关掉之后不得再改，
            # 哪怕会话侧的 flag 还没被后台结算清掉。
            return
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        user_data = self.plugin._user_sessions.get(session_key)
        if not user_data or not user_data.get("memory_enabled"):
            return
        await self._record_scoped_mentions_best_effort(context, reply_text)

    async def _record_participant_mentions_on_delivery(
        self, context: QQReplyContext, reply_text: str,
    ) -> None:
        """私聊 participant 轮的 mention 计数（对偶群路径）。

        没有它，scoped 条目的防重复 suppression 对私聊 participant 永不
        生效，模型会在每次回复里重复提起同一条事实。legacy admin 私聊走
        本体 post_turn，不在这里记。"""
        if not getattr(context, "participant_memory_enabled", False):
            return
        if not (getattr(self.plugin, "_qq_settings", {}) or {}).get(
            "private_participant_memory_enabled", False,
        ):
            # 与群分支同语义：开关关掉之后不得再改 participant 域元数据。
            return
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        user_data = self.plugin._user_sessions.get(session_key)
        if not user_data or not user_data.get("memory_enabled"):
            return
        sender_id = str(context.sender_id or "").strip()
        if not sender_id or is_synthetic_source(
            getattr(context, "source_kind", ""),
        ):
            # 合成轮的名义 sender 不是真实对话方；缺 sender 时 fail-closed
            # ——绝不退化成无 subject 的 legacy 写。
            return
        try:
            await self.plugin.memory_bridge.post_scoped_mentions(
                context.her_name, reply_text,
                subjects=[
                    self.plugin.memory_bridge.participant_subject(sender_id),
                ],
            )
        except Exception as e:
            self.plugin.logger.warning(f"participant mention 记录失败（忽略）: {e}")

    async def _record_scoped_mentions_best_effort(
        self, context: QQReplyContext, reply_text: str,
    ) -> None:
        """Bump scoped mention counters with the subjects this reply was
        authorized to see, so repeatedly-volunteered scoped entries reach the
        suppression threshold like legacy entries do."""
        group_id = str(context.group_id or "").strip()
        if not group_id:
            return
        bridge = self.plugin.memory_bridge
        subjects = [bridge.group_subject(group_id)]
        sender_id = str(context.sender_id or "").strip()
        synthetic = is_synthetic_source(getattr(context, "source_kind", ""))
        member_authorized = bool(
            getattr(context, "member_memory_enabled", False)
            and (getattr(self.plugin, "_qq_settings", {}) or {}).get(
                "group_member_memory_enabled", False,
            )
        )
        if sender_id and not synthetic and member_authorized:
            # 合成轮的名义 sender 不是真实发言人——mention 计数只按群域记，
            # 与召回/写入侧的合成轮过滤对齐。member 未授权时也不记：该域
            # 本轮没被召回，扫描/改写留存条目会把没展示过的事实压进
            # suppression、之后 opt-in 也不再出现。
            subjects.append(bridge.group_participant_subject(group_id, sender_id))
        try:
            await bridge.post_scoped_mentions(
                context.her_name, reply_text, subjects=subjects,
            )
        except Exception as e:
            self.plugin.logger.warning(f"scoped mention 记录失败（忽略）: {e}")

    async def run_fallback_memory_hooks(
        self, context: QQReplyContext, fallback_reply: str,
    ) -> None:
        """fallback 成功也要跑 scoped 记忆钩子：成员发言入 bucket、被展示
        的 scoped 条目计 mention——主会话空回复不代表这轮没发生。生产
        pipeline 走 QQReplyModelNode.generate()，legacy 入口走
        generate_from_context()——两条 fallback 成功路径都必须调这里。
        会话可能已被超时丢弃（user_data 不在了则跳过）。"""
        if not context.is_group or context.ephemeral_session:
            # ephemeral 键含 time_ns，重新生成必 miss；且 ephemeral 会话
            # persist=False、finally 即丢弃，记忆钩子本就无意义。
            return
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        user_data = self.plugin._user_sessions.get(session_key)
        if user_data is not None:
            await self._sync_memory_after_success(
                session_key=session_key, user_data=user_data,
                context=context, reply_text=fallback_reply,
            )

    async def generate_from_context(self, context: QQReplyContext) -> QQModelResult:
        if not context.is_group and context.permission_level not in ["admin", "trusted"]:
            return QQModelResult(reply_text=None, source="none")

        if context.is_group and context.group_id:
            from .group_send_safety import group_send_safety

            async with group_send_safety(self.plugin).generation(context.group_id) as reason:
                if reason:
                    emit_bridge_log(self.plugin, "INFO", f"[Pace] 群{context.group_id}跳过生成：{reason}，不补发")
                    return QQModelResult(reply_text=None, source="none", traces=[
                        QQPipelineStageTrace(stage="model_primary", status="skipped", detail=reason),
                    ])
                return await self._generate_admitted_context(context)
        return await self._generate_admitted_context(context)

    async def _generate_admitted_context(self, context: QQReplyContext) -> QQModelResult:
        primary_result = await self.run_primary_session_call(context)
        if not primary_result.allow_fallback:
            return primary_result

        fallback_reply = await self.generate_fallback_from_context(context)
        if fallback_reply:
            primary_result.traces.append(
                QQPipelineStageTrace(
                    stage="model_fallback",
                    status="success",
                    metadata={"reply_length": len(fallback_reply), "group_scene_mode": context.group_scene_mode},
                )
            )
            await self.run_fallback_memory_hooks(context, fallback_reply)
            # direct fallback 也是"成功回复"，同样要回写（primary 成功已在
            # run_primary_session_call 内写回并提前 return，不会重复）。
            await self.writeback_deepchat(context)
            return QQModelResult(reply_text=fallback_reply, source="direct_llm_fallback", used_fallback=True, traces=primary_result.traces)
        primary_result.traces.append(
            QQPipelineStageTrace(
                stage="model_fallback",
                status="empty",
                metadata={"reply_length": 0, "group_scene_mode": context.group_scene_mode},
            )
        )
        return QQModelResult(reply_text=None, source="none", used_fallback=True, traces=primary_result.traces)

    async def generate_reply(
        self,
        message: str,
        permission_level: str,
        sender_id: str,
        attachments: list[dict[str, Any]] | None = None,
        is_group: bool = False,
        group_id: str = None,
        user_nickname: Optional[str] = None,
        use_memory_context: Optional[bool] = None,
        persist_memory: Optional[bool] = None,
        ephemeral_session: bool = False,
        group_facing: bool = False,
        group_scene_mode: str = "",
    ) -> Optional[str]:
        context = await self.plugin.reply_context_node.build(
            message=message,
            permission_level=permission_level,
            sender_id=sender_id,
            attachments=attachments,
            is_group=is_group,
            group_id=group_id,
            user_nickname=user_nickname,
            use_memory_context=use_memory_context,
            persist_memory=persist_memory,
            ephemeral_session=ephemeral_session,
            group_facing=group_facing,
            group_scene_mode=group_scene_mode,
        )
        model_result = await self.generate_from_context(context)
        outcome = self.plugin.reply_postprocess_node.finalize(context, model_result)
        return outcome.reply_text
