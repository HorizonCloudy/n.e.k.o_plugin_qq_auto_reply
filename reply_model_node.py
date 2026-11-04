from __future__ import annotations

from typing import Any

from .pipeline_models import QQModelResult, QQPipelineStageTrace, QQReplyContext


class QQReplyModelNode:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    async def generate(self, context: QQReplyContext) -> QQModelResult:
        if not self._should_generate(context):
            return QQModelResult(
                reply_text=None,
                source="none",
                traces=[
                    QQPipelineStageTrace(
                        stage="model_primary",
                        status="skipped",
                        metadata={"reason": "permission_blocked"},
                    )
                ],
            )

        deepchat = getattr(self.plugin, "deepchat_service", None)
        if deepchat is not None and deepchat.generation_mode == "direct":
            return await self._run_direct_primary(context)

        primary_result = await self._run_primary(context)
        if not primary_result.allow_fallback:
            return primary_result

        fallback_reply = await self._run_fallback(context)
        primary_result.traces.append(
            QQPipelineStageTrace(
                stage="model_fallback",
                status="success" if fallback_reply else "empty",
                metadata={
                    "reply_length": len(fallback_reply or ""),
                },
            )
        )
        if fallback_reply:
            # 生产管线的 fallback 成功路径：与 legacy generate_from_context
            # 对偶，跑同一份 scoped 记忆钩子（成员 bucket / mention 计数）。
            await self.plugin.reply_generation_service.run_fallback_memory_hooks(
                context, fallback_reply,
            )
            await self._writeback_deepchat(context)
            return QQModelResult(
                reply_text=fallback_reply,
                source="direct_llm_fallback",
                used_fallback=True,
                traces=primary_result.traces,
            )
        return QQModelResult(reply_text=None, source="none", used_fallback=True, traces=primary_result.traces)

    async def _writeback_deepchat(self, context: QQReplyContext) -> None:
        """回写 DeepChat（情绪 / 关系 / 表达学习），synthetic 轮不计入互动。

        规则只有一份：``QQReplyGenerationService.writeback_deepchat``。这里不再
        保留"服务层缺失就自己写一遍"的兜底——两套规则迟早分叉，而那个分支也没有
        任何回归护栏（测试替身都提供了服务层方法）。
        """
        await self.plugin.reply_generation_service.writeback_deepchat(context)

    async def _run_direct_primary(self, context: QQReplyContext) -> QQModelResult:
        """Deep-chat direct mode: per-turn prompt built from scratch.

        direct 模式不经过 host session：它每轮读 ``context.system_prompt``
        （由 ``reply_context_node`` 现算，含 mood/relationship 片段），代价是不用
        host session 的工具/记忆编排。

        注意 **不要**在这里写"片段只在新会话生效"——宿主侧
        ``reply_generation_service._apply_turn_memory_context`` 每轮替换
        ``conversation_history[0]``（默认 ``always_refresh``），所以 host 模式同样
        是逐轮生效；两种模式的区别只在"用不用 host session 的工具/记忆编排"。
        """
        fallback_reply = await self._run_fallback(context)
        trace = QQPipelineStageTrace(
            stage="model_direct",
            status="success" if fallback_reply else "empty",
            metadata={"reply_length": len(fallback_reply or "")},
        )
        if fallback_reply:
            await self.plugin.reply_generation_service.run_fallback_memory_hooks(
                context, fallback_reply,
            )
            # direct 模式没有 host session 主路径，这一轮的唯一成功路径就是
            # fallback，所以回写放在这里（否则 direct 模式永不回写情绪/关系）。
            await self._writeback_deepchat(context)
            return QQModelResult(
                reply_text=fallback_reply,
                source="deepchat_direct",
                used_fallback=True,
                traces=[trace],
            )
        return QQModelResult(reply_text=None, source="none", used_fallback=True, traces=[trace])

    def _should_generate(self, context: QQReplyContext) -> bool:
        # 开放平台：全部生成（不限制权限）
        if self.plugin.qq_client and not self.plugin.qq_client.needs_attention:
            return True
        return context.is_group or context.permission_level in ["admin", "trusted"]

    async def _run_primary(self, context: QQReplyContext) -> QQModelResult:
        return await self.plugin.reply_generation_service.run_primary_session_call(context)

    async def _run_fallback(self, context: QQReplyContext) -> str | None:
        return await self.plugin.reply_generation_service.generate_fallback_from_context(context)
