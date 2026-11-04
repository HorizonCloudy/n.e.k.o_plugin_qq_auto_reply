from __future__ import annotations

import asyncio
import math
import threading
import time
from pathlib import Path
from typing import Any

from .autonomy import AutonomyScheduler
from .expression import ExpressionStore, extract_expressions
from .fragments import append_fragments
from .guard import is_safe_text, sanitize_text
from .models import LEGACY_MOOD_ALIASES, Mood, coerce_mood
from .mood import MoodStateMachine
from .relationship import RelationshipGraph
from .store import DeepChatStore
from .subject_index import SubjectIndex

POSITIVE_SIGNALS = frozenset({"praise", "thank", "funny"})
NEGATIVE_SIGNALS = frozenset({"insult"})

#: 印象文本上限（字符）。里程碑从"原话副本"改成"提炼后的印象"，长度必须有界。
REMEMBER_IMPRESSION_MAX_CHARS = 120
#: 原话子串命中判定时取样多少条最近用户原话。
REMEMBER_GUARD_SAMPLE = 3
#: 原话子串判定的最小命中长度：太短的片段（"好的"）当子串命中会误杀正常提炼。
REMEMBER_GUARD_MIN_CHARS = 6
#: 「写入成功过、随后连续零新事实」的告警阈值 M。
REMEMBER_EMPTY_STREAK_WARN = 3

#: 学习路径的硬预算：``writeback`` 跑在回复主链路上，历史再长也不能阻塞。
LEARNING_PER_MESSAGE = 300
LEARNING_BUDGET = 8000

#: Cheap keyword routing. Order matters: insults win over everything else,
#: then boredom, then positive signals.
_SIGNAL_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("insult", ("讨厌", "滚", "闭嘴", "笨蛋", "烦死", "去死")),
    ("bored", ("无聊", "没意思", "好闲")),
    ("praise", ("厉害", "好棒", "喜欢", "可爱", "优秀", "牛", "赞")),
    ("thank", ("谢谢", "谢啦", "感谢", "辛苦了")),
    ("funny", ("哈哈", "笑死", "好笑", "草")),
)


def classify_signal(text: str) -> str:
    content = str(text or "")
    for signal, words in _SIGNAL_PATTERNS:
        if any(word in content for word in words):
            return signal
    return "serious"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("数值必须是有限值")
    if number < 0:
        raise ValueError("数值不能为负")
    return number


def _as_int(value: Any) -> int:
    number = int(value)
    if number < 0:
        raise ValueError("数值不能为负")
    return number


def _as_positive_int(value: Any) -> int:
    """用于"取 0 会造成数据丢失"的键：0 不是降级，而是清库。"""
    number = int(value)
    if number <= 0:
        raise ValueError("数值必须大于 0")
    return number


def _as_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"host", "direct"}:
        raise ValueError("generation_mode 只能取 host 或 direct")
    return mode


def _mask_id(value: Any) -> str:
    """日志里只留前 3 位：账号 ID 不进日志。"""
    text = str(value or "").strip()
    return f"{text[:3]}***" if len(text) > 3 else "***"


def _error_kind(exc: BaseException) -> str:
    """只记异常类型：异常原文可能夹带用户内容。"""
    return type(exc).__name__


TUNING_KEYS: dict[str, Any] = {
    "deepchat_enabled": _as_bool,
    "deepchat_fragments_enabled": _as_bool,
    "deepchat_guard_enabled": _as_bool,
    "deepchat_reflection_enabled": _as_bool,
    "deepchat_mood_decay_rate": _as_float,
    "deepchat_relationship_half_life_days": _as_float,
    "deepchat_relationship_freeze_threshold": _as_float,
    "deepchat_relationship_revive_threshold": _as_float,
    "deepchat_generation_mode": _as_mode,
    "deepchat_autonomy_enabled": _as_bool,
    "deepchat_autonomy_min_affinity": _as_float,
    "deepchat_autonomy_min_idle_hours": _as_float,
    "deepchat_autonomy_max_idle_hours": _as_float,
    "deepchat_autonomy_interval_seconds": _as_float,
    "deepchat_autonomy_max_per_tick": _as_int,
    "deepchat_expression_learning_enabled": _as_bool,
    "deepchat_expression_min_messages": _as_int,
    "deepchat_expression_min_interval_seconds": _as_float,
    "deepchat_expression_max_entries": _as_positive_int,
    "deepchat_expression_max_selected": _as_int,
}

#: 键仍在 ``TUNING_KEYS`` 里（Phase 1 可能恢复），但当前**调了也不会生效**，
#: 所以 ``tune`` 会显式抛错、``dc_tune`` 的说明也不再广告它们。
#: 留着能调但静默无效的键，比拒绝更糟：调用方会以为设置成功了。
#: 3B 已落地（共享预算账本 ``ProactiveBudget`` 是两来源唯一计数源），
#: 所以 ``deepchat_autonomy_enabled`` 已从停用集合移出、可以正常 tune；
#: 留下的这个键是 Phase 0 停写的里程碑开关（见下）。
RETIRED_TUNING_KEYS: frozenset[str] = frozenset({
    "deepchat_reflection_enabled",
})

#: 停用键的拒绝文案。要写成"可行动"的：为什么停 + 什么时候回来 + 现在别调。
RETIRED_TUNING_REASON: dict[str, str] = {
    "deepchat_reflection_enabled": (
        "该参数已停用：里程碑已停止写入用户原话（Phase 0 隐私修复），"
        "后续改由宿主 participant 记忆承载后恢复；当前调用不会生效。"
    ),
}


class DeepChatService:
    """Mood + relationship layer for the QQ pipeline.

    The existing plugin already owns debounce, address detection, attention,
    memory recall and delivery. This service deliberately adds only the two
    pieces that were missing: a persistent affect state and a weighted
    relationship graph. It is injected into the prompt at the context-building
    boundary and written back after a successful generation.
    """

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.store = DeepChatStore(Path(plugin.data_path()) / "deepchat.sqlite3")
        #: 宿主记忆的社交坐标索引（只存坐标，不存内容）——遗忘时按双轴枚举用。
        self.mood = MoodStateMachine(
            self.store,
            decay_rate=self._float_setting("deepchat_mood_decay_rate", 0.05),
        )
        self.relationship = RelationshipGraph(
            self.store,
            half_life_days=self._float_setting("deepchat_relationship_half_life_days", 14.0),
            freeze_threshold=self._float_setting("deepchat_relationship_freeze_threshold", 0.10),
            revive_threshold=self._float_setting("deepchat_relationship_revive_threshold", 0.15),
        )
        self.autonomy = AutonomyScheduler(self)
        self.subject_index = SubjectIndex(self.store)
        #: `remember_milestone` 的可观测状态（见该方法 docstring 的三档告警）。
        self._remember_stats: dict[str, int] = {
            "attempts": 0,
            "succeeded": 0,
            "empty_streak": 0,
        }
        # 写回是跨多张表/多个连接的读-改-写；并发调用会交错覆盖（实测 16 并发送回丢 5 次）
        # 且静默无异常。这里用**整条链**的互斥串行化——⚠️ 不要改成"给 connection() 加锁":
        # 那是单连接粒度，RMW 照样交错，实测比不修更差；
        # 对照时须先关掉 _writeback_lock，否则测到的是整链锁的效果（connection 锁 16 并发只落 1~2 次）。
        # 这是止血；长期治因是把 reinforce/weaken 改成单语句原子 UPDATE。
        # 注意**只覆盖单实例**：插件 reload 窗口旧实例的在飞写回、或外部工具直连同一 db 时，
        # 仍可能丢更新（进程级锁/原子 UPDATE 才是跨实例解，见 PR 已知限制）。
        self._writeback_lock = threading.Lock()
        self.expressions = ExpressionStore(
            self.store,
            max_entries=self._int_setting("deepchat_expression_max_entries", 200),
        )

    def _setting(self, key: str, default: Any) -> Any:
        return (getattr(self.plugin, "_qq_settings", {}) or {}).get(key, default)

    def _float_setting(self, key: str, default: float) -> float:
        try:
            return float(self._setting(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _int_setting(self, key: str, default: int) -> int:
        try:
            return int(self._setting(key, default))
        except (TypeError, ValueError):
            return int(default)

    def _warn(self, message: str) -> None:
        logger = getattr(self.plugin, "logger", None)
        if logger is None:
            return
        try:
            logger.warning(message)
        except Exception:  # noqa: BLE001 —— 诊断日志失败不影响主流程
            pass

    def _info(self, message: str) -> None:
        logger = getattr(self.plugin, "logger", None)
        if logger is None:
            return
        try:
            logger.info(message)
        except Exception:  # noqa: BLE001
            pass

    @property
    def enabled(self) -> bool:
        return bool(self._setting("deepchat_enabled", True))

    @property
    def fragments_enabled(self) -> bool:
        return bool(self._setting("deepchat_fragments_enabled", True))

    @property
    def guard_enabled(self) -> bool:
        return bool(self._setting("deepchat_guard_enabled", True))

    @property
    def generation_mode(self) -> str:
        """**配置值**：``deepchat_generation_mode`` 归一化后的取值（host/direct）。

        这个属性只回答「配置里写的是什么」，**不反映总开关**——主开关关闭时它仍可能是
        ``direct``（``dc_view_state`` 需要如实展示配置）。判断「是否真的在走 direct」
        请用 :attr:`direct_mode_active`，路由处也只认它。
        """
        mode = str(self._setting("deepchat_generation_mode", "host") or "").strip().lower()
        return mode if mode in {"host", "direct"} else "host"

    @property
    def direct_mode_active(self) -> bool:
        """是否真的在走 direct 路径：总开关 ON 且配置为 direct。

        路由处只认这个属性，不要再各自比较 ``generation_mode == 'direct'`` ——
        「关掉总开关」与「重新打开」两处都要同时正确，散在多个调用点迟早漏一个。
        """
        return self.enabled and self.generation_mode == "direct"

    @property
    def expression_learning_enabled(self) -> bool:
        """Off by default: learning someone's phrasing is opt-in."""
        return _as_bool(self._setting("deepchat_expression_learning_enabled", False))

    @staticmethod
    def _mood_key(sender_id: str) -> str:
        return f"user:{str(sender_id or '').strip()}"

    async def enrich_system_prompt(
        self,
        *,
        system_prompt: str,
        sender_id: str,
        group_id: str | None = None,
        is_group: bool = False,
        sender_name: str = "",
    ) -> str:
        """异步外壳：同步 SQLite 读写丢到线程里跑（与 ``writeback_deepchat`` 同一理由）。

        调用方在 async 的 context 节点里，直接跑同步读会拖住事件循环；而 enrich 恰好会
        触发两处**写**（mood 衰减、relationship 跨结算边界），锁争用下最明显。
        同步内核保留为 ``_enrich_sync``，便于测试直接调用。
        """
        return await asyncio.to_thread(
            self._enrich_sync,
            system_prompt=system_prompt,
            sender_id=sender_id,
            group_id=group_id,
            is_group=is_group,
            sender_name=sender_name,
        )

    def _enrich_sync(
        self,
        *,
        system_prompt: str,
        sender_id: str,
        group_id: str | None = None,
        is_group: bool = False,
        sender_name: str = "",
    ) -> str:
        """同步内核：两次读 + 可能的两次衰减写，全程共用**一个**连接。"""
        if not self.enabled or not self.fragments_enabled:
            return system_prompt
        sender_id = str(sender_id or "").strip()
        if not sender_id:
            return system_prompt
        # 一个 session 一个连接：读两张表 + 可能的两次衰减写都在同一连接上完成；
        # 少开连接 = 少一次拿锁的机会（enrich 在回复主链路上）。
        with self.store.session() as conn:
            state = self.mood.get(self._mood_key(sender_id), conn=conn)
            edge = self.relationship.get(sender_id, conn=conn)
        candidates: list[str] = []
        if not is_group:
            # 群聊不注入"她自己的情绪"：群是多人语境，情绪句会让她的心情变成
            # 公开发言；群情绪由 session_instruction_service._append_emotion_section
            # 唯一提供，这里再加一份就是两个来源互相打脸。
            candidates.append(self.mood.fragment(state))
        candidates.append(
            # 群聊也不注入私聊里程碑（跨语境泄露），只保留数值派生的好感/信任/互动次数。
            self.relationship.fragment(
                edge,
                sender_name or sender_id,
                include_milestones=not is_group,
            )
        )
        # 私聊学到的短语只回私聊：群聊里注入等于把 A 的私聊原话放到 B 群面前。
        if self.expression_learning_enabled and not is_group:
            candidates.append(self._expression_fragment(sender_id))
        fragments: list[str] = []
        for fragment in candidates:
            # 去控制字符 + 长度上限无条件执行：关守卫也不能把用户原话原样送 prompt。
            cleaned = sanitize_text(fragment)
            if not cleaned:
                continue
            if self.guard_enabled and not is_safe_text(cleaned):
                self._warn("[DeepChat] 片段未通过人格守卫，已跳过")
                continue
            fragments.append(cleaned)
        return append_fragments(system_prompt, *fragments)

    def _expression_fragment(self, sender_id: str) -> str:
        """Learned phrasing as a prompt fragment; errors degrade to nothing."""
        try:
            return self.expressions.fragment(
                sender_id,
                limit=self._int_setting("deepchat_expression_max_selected", 5),
            )
        except Exception as exc:  # noqa: BLE001 —— 装饰性片段失败不影响回复
            self._warn(f"[DeepChat] 表达片段读取失败: {_error_kind(exc)}")
            return ""

    def writeback(
        self,
        *,
        sender_id: str,
        text: str,
        is_group: bool = False,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        sender_id = str(sender_id or "").strip()
        if not self.enabled or not sender_id:
            return {}
        with self._writeback_lock:
            return self._writeback_locked(
                sender_id=sender_id, text=text, is_group=is_group, group_id=group_id,
            )

    def _writeback_locked(
        self,
        *,
        sender_id: str,
        text: str,
        is_group: bool,
        group_id: str | None,
    ) -> dict[str, Any]:
        """``writeback`` 的本体：调用方必须已持有 ``_writeback_lock``。"""
        now = time.time()
        signal = classify_signal(text)
        state = self.mood.update(self._mood_key(sender_id), signal, now=now)
        if signal in NEGATIVE_SIGNALS:
            edge = self.relationship.weaken(sender_id, 0.03, now=now)
        else:
            bonus = 0.01 if signal in POSITIVE_SIGNALS else 0.0
            edge = self.relationship.reinforce(sender_id, 0.01 + bonus, now=now)
        if not is_group:
            # Only private contact is a consent signal for proactive DMs.
            self.autonomy.record_private_interaction(sender_id, now=now)
        reflected = self.reflect_if_due(sender_id, edge=edge, now=now)
        learned = self.learn_expressions_if_due(sender_id, edge=edge, now=now)
        result = {
            "signal": signal,
            "mood": state.current.value,
            "affinity": round(edge.affinity, 4),
            "trust": round(edge.trust, 4),
        }
        if reflected:
            result["reflection"] = reflected
        if learned:
            result["expressions_learned"] = learned
        return result

    def reflect_if_due(
        self,
        user_id: str,
        *,
        edge: Any | None = None,
        now: float | None = None,
    ) -> str | None:
        """**Phase 0 起停写**：里程碑不再保存用户原话，本方法恒返回 None。

        原实现每 10 次互动取最近 3 条用户消息逐字拼成「最近聊到：…」落库。这等于
        在 sqlite 里留一份**用户原话副本**：默认开启、无 UI 开关、撤销群记忆也不会
        清它，而它又会每轮注入 prompt（宿主 turn-memory hook 每轮刷新
        ``_apply_turn_memory_context``），拆分后的指令句还能绕过人格守卫持久生效。

        因此 Phase 0 直接停写（不写脱敏占位——占位会被 ``relationship.fragment``
        当成"共同经历"注入，纯噪声）。后续里程碑改由宿主的 participant 记忆承载。
        签名与 ``deepchat_reflection_enabled`` 开关保留，方便 Phase 1 恢复。

        ⚠️ 已删除：``_recent_summary()``（"读用户历史 + 拼成一句"的私有 helper）。
        Phase 1 要恢复里程碑内容时**不要**把它拿回来 —— 那会把这次的原话副本缺陷
        原样带回。``_recent_user_messages()`` 仍然保留：表达学习在用。
        """
        return None

    def learn_expressions_if_due(
        self,
        user_id: str,
        *,
        edge: Any | None = None,
        now: float | None = None,
    ) -> int:
        """Every N interactions, learn the short phrases this user reuses.

        Three gates: the feature switch (off by default), the interaction
        cadence, and a minimum interval between rounds. Never raises — a failed
        learn round must not touch the reply path.
        """
        if not self.expression_learning_enabled:
            return 0
        uid = str(user_id or "").strip()
        if not uid:
            return 0
        min_messages = max(1, self._int_setting("deepchat_expression_min_messages", 10))
        edge = edge or self.relationship.get(uid, now=now)
        if edge.interactions <= 0 or edge.interactions % min_messages != 0:
            return 0
        stamp = time.time() if now is None else float(now)
        interval = self._float_setting("deepchat_expression_min_interval_seconds", 30.0)
        last = self.expressions.last_learn_at(uid)
        if last > 0.0 and stamp - last < interval:
            return 0
        try:
            messages = self._recent_user_messages(uid, limit=30)
            if not messages:
                return 0
            learned = extract_expressions(messages)
            written = self.expressions.upsert(uid, learned, now=stamp) if learned else 0
            # Mark even on a dry round, otherwise every further interaction at
            # the same cadence would re-scan history.
            self.expressions.mark_learned(uid, now=stamp)
            return written
        except Exception as exc:  # noqa: BLE001 —— 学习失败不得影响回复主链路
            self._warn(f"[DeepChat] 表达学习失败 {_mask_id(uid)}: {_error_kind(exc)}")
            return 0

    def _recent_user_messages(
        self,
        user_id: str,
        limit: int = 30,
        *,
        per_message: int = LEARNING_PER_MESSAGE,
        budget: int = LEARNING_BUDGET,
    ) -> list[str]:
        """Last ``limit`` non-empty user lines, clipped per line and in total.

        **仅取私聊会话**（``_build_session_key(..., is_group=False)``）：群聊用户
        不学习、不反思，因此这里永远不读群会话历史。

        Clipping lives in the reader, not at the call sites: every consumer
        (milestone summary, expression learning) feeds a model, and
        ``extract_expressions`` is roughly linear in input size — an oversized
        history would let one chatty user stall ``writeback`` on the reply path.
        """
        sessions = getattr(self.plugin, "_user_sessions", {}) or {}
        build_key = getattr(self.plugin, "_build_session_key", None)
        if not callable(build_key):
            return []
        try:
            session_key = build_key(sender_id=user_id, is_group=False)
        except Exception:  # noqa: BLE001 —— 测试/轻量宿主可能没有完整会话键
            return []
        user_data = sessions.get(session_key) or {}
        session = user_data.get("session")
        history = getattr(session, "_conversation_history", []) or []
        texts: list[str] = []
        used = 0
        for item in reversed(history):
            role = item.get("role") if isinstance(item, dict) else getattr(item, "role", "")
            content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
            if str(role) != "user":
                continue
            text = str(content or "").strip()
            if not text:
                continue
            remaining = budget - used
            if remaining <= 0:
                break
            piece = text[: min(per_message, remaining)]
            texts.append(piece)
            used += len(piece)
            if len(texts) >= max(1, int(limit)):
                break
        texts.reverse()
        return texts

    async def autonomy_loop(self, interval_seconds: float | None = None) -> None:
        """Opt-in proactive loop. Disabled by default; see ``deepchat_autonomy_*``."""
        interval = float(
            interval_seconds
            if interval_seconds is not None
            else self._float_setting("deepchat_autonomy_interval_seconds", 600.0)
        )
        interval = max(30.0, interval)
        while True:
            try:
                await asyncio.sleep(interval)
                if not self.autonomy.enabled:
                    continue
                candidates = self.autonomy.tick(
                    limit=self._int_setting("deepchat_autonomy_max_per_tick", 1),
                )
                for candidate in candidates:
                    # Mark before sending: a failed send should still respect the
                    # cooldown, otherwise the next tick can spam the same user.
                    self.autonomy.mark_reached_out(candidate.user_id)
                    sender = getattr(self.plugin, "proactive_message_service", None)
                    if sender is None:
                        continue
                    try:
                        await sender.send_private_message(
                            target=candidate.user_id,
                            message=candidate.reason,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._warn(
                            f"[DeepChat] 主动联系失败 {_mask_id(candidate.user_id)}: "
                            f"{_error_kind(exc)}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不得终止循环
                self._warn(f"[DeepChat] 自主调度轮次异常: {_error_kind(exc)}")

    def view_state(self, user_id: str) -> dict[str, Any]:
        sender_id = str(user_id or "").strip()
        state = self.mood.get(self._mood_key(sender_id))
        edge = self.relationship.get(sender_id)
        return {
            "user_id": sender_id,
            "enabled": self.enabled,
            "mood": {
                "current": state.current.value,
                "valence": round(state.valence, 4),
                "arousal": round(state.arousal, 4),
                "updated_at": state.updated_at,
            },
            "relationship": {
                "affinity": round(edge.affinity, 4),
                "trust": round(edge.trust, 4),
                "interactions": edge.interactions,
                "milestones": list(edge.milestones),
                "frozen": bool(edge.frozen),
                "updated_at": edge.updated_at,
            },
            "autonomy": self.autonomy.snapshot(sender_id),
            "expressions": self.expressions.count(sender_id),
        }

    def reset_user(self, user_id: str) -> dict[str, Any]:
        sender_id = str(user_id or "").strip()
        with self.store.connection() as conn:
            mood_deleted = conn.execute(
                "DELETE FROM deepchat_mood WHERE user_key = ?",
                (self._mood_key(sender_id),),
            ).rowcount
            relationship_deleted = conn.execute(
                "DELETE FROM deepchat_relationship WHERE user_id = ?",
                (sender_id,),
            ).rowcount
            autonomy_deleted = conn.execute(
                "DELETE FROM deepchat_autonomy WHERE user_id = ?",
                (sender_id,),
            ).rowcount
        expressions_deleted = self.expressions.clear(sender_id)
        return {
            "user_id": sender_id,
            "mood_deleted": mood_deleted,
            "relationship_deleted": relationship_deleted,
            "autonomy_deleted": autonomy_deleted,
            "expressions_deleted": expressions_deleted,
        }

    def set_mood(self, user_id: str, mood: str) -> dict[str, Any]:
        sender_id = str(user_id or "").strip()
        raw = str(mood or "").strip()
        if raw not in {item.value for item in Mood} and raw not in LEGACY_MOOD_ALIASES:
            valid = ", ".join(item.value for item in Mood)
            raise ValueError(f"未知情绪 {mood!r}；可选：{valid}")
        # 历史值（neutral）在这里被归一化到 calm，而不是报错。
        target = coerce_mood(raw)
        state = self.mood.set_mood(self._mood_key(sender_id), target)
        return {
            "user_id": sender_id,
            "mood": state.current.value,
            "valence": round(state.valence, 4),
            "arousal": round(state.arousal, 4),
        }

    async def tune(self, param: str, value: str) -> dict[str, Any]:
        key = str(param or "").strip()
        if key not in TUNING_KEYS:
            # 可选列表与 dc_tune 描述同口径：停用键单独说，不混进"可选"。
            available = ", ".join(k for k in TUNING_KEYS if k not in RETIRED_TUNING_KEYS)
            raise ValueError(f"未知参数 {key!r}；可选：{available}")
        if key in RETIRED_TUNING_KEYS:
            # 显式拒绝，而不是"接受并静默无效"——那是本仓库反复在修的同一形状。
            raise ValueError(RETIRED_TUNING_REASON.get(key, f"{key} 已停用"))
        parsed = TUNING_KEYS[key](value)
        settings = getattr(self.plugin, "_qq_settings", None)
        if settings is None:
            raise ValueError("插件配置不可用")
        # 先落盘、再改内存：save 抛错时不会留下"内存已改但对象没重建"的中间态。
        candidate = {**settings, key: parsed}
        save = getattr(getattr(self.plugin, "config_store", None), "save", None)
        if callable(save):
            await save(candidate)
        settings[key] = parsed
        # 配置在构造时读取，重建这三个无状态对象（mood/relationship/expressions）才能立即生效。
        self.apply_runtime_config()
        return {"param": key, "value": parsed}

    def apply_runtime_config(self) -> None:
        """按当前配置重建「构造期读配置」的对象（mood / relationship / expressions）。

        两个调用方：``tune``（改完立即生效）与 ``SettingsService.apply_runtime_settings``
        （配置从文件/UI 载入后统一应用）。重建的都是廉价无状态对象，无副作用；
        刻意做成**公开**方法，避免「配置入口各自重建一次」的重复实现漂移。
        """
        self.mood = MoodStateMachine(
            self.store,
            decay_rate=self._float_setting("deepchat_mood_decay_rate", 0.05),
        )
        self.relationship = RelationshipGraph(
            self.store,
            half_life_days=self._float_setting("deepchat_relationship_half_life_days", 14.0),
            freeze_threshold=self._float_setting("deepchat_relationship_freeze_threshold", 0.10),
            revive_threshold=self._float_setting("deepchat_relationship_revive_threshold", 0.15),
        )
        self.expressions = ExpressionStore(
            self.store,
            max_entries=self._int_setting("deepchat_expression_max_entries", 200),
        )

    # ── 里程碑写入（Phase 1 形态：写"印象"，不写原话）────────────────────

    def remember_milestone(
        self,
        *,
        sender_id: str,
        impression: str,
        group_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """把一轮提炼出的**印象**写进宿主的 participant 记忆。

        与 Phase 0 删掉的「原话副本」的区别，是三道闸 + 一条写入形态：

        1. **只写私聊 participant 域**：`group_id` 非空直接 `private_only`，**零请求**
           （群成员域由 scoped history 那条链路负责，不走这里）。
        2. **文本闸**：`sanitize_text` 去控制字符后截断 120 字；再取最近
           `REMEMBER_GUARD_SAMPLE` 条用户原话做**子串命中**判定——命中即拒。
           理由：所谓"提炼"很容易退化成"抄写"，抄写就把 Phase 0 修掉的原话副本
           又请回来了，而且这次还带着"已脱敏"的错觉。
        3. **授权闸（发前复检）**：`private_participant_memory_enabled` +
           `private_memory_mode == "participant"` + 记忆服务可用，三者同时满足才写；
           legacy 模式一律 **fail-closed**（不清、不猜、写明原因）。
           闸在入口查一次、**发 HTTP 前再查一次**：这中间可能被用户改设置。

        ⚠️ **当前不发真实 HTTP**：服务端的 assistant 抽取行为与原话守卫尚未确认，
        `_post_impression` 是占位实现。三道闸与可观测性现在就能生效并可被 mock 验证，
        将来只需替换 `_post_impression`。

        可观测（`created_fact_identities` 为空时）：
        - 还没成功过 → **WARNING** `never_succeeded`（一次就够，别刷屏）；
        - 成功过、且连续空 ≥ `REMEMBER_EMPTY_STREAK_WARN` → **WARNING** `empty_streak`；
        - 其余情况 → **INFO** + `no_new_fact`（单次空是正常现象）。
        """
        stamp = time.time() if now is None else float(now)
        uid = str(sender_id or "").strip()
        if not uid:
            return {"ok": False, "written": False, "reason": "invalid_sender"}

        # 闸 1：只写私聊个人域。
        if str(group_id or "").strip():
            self._info("[DeepChat] 里程碑印象跳过：private_only")
            return {"ok": False, "written": False, "reason": "private_only"}

        # 闸 2：文本清洗 + 截断 + 原话子串守卫。
        cleaned = sanitize_text(str(impression or "")).strip()
        if not cleaned:
            return {"ok": False, "written": False, "reason": "empty_impression"}
        if len(cleaned) > REMEMBER_IMPRESSION_MAX_CHARS:
            cleaned = cleaned[:REMEMBER_IMPRESSION_MAX_CHARS].rstrip()
        leaked = self._verbatim_overlap(uid, cleaned)
        if leaked:
            self._warn(f"[DeepChat] 里程碑印象被拒：命中用户原话子串（{len(leaked)} 字）")
            return {"ok": False, "written": False, "reason": "verbatim_user_words"}

        # 闸 3（入口先查一次）。
        gate = self._remember_gate(uid)
        if not gate["allowed"]:
            return {"ok": False, "written": False, "reason": gate["reason"]}

        self._remember_stats["attempts"] += 1

        # 闸 3（发前复检）：入口到这里之间用户可能改了设置。
        gate = self._remember_gate(uid)
        if not gate["allowed"]:
            return {"ok": False, "written": False, "reason": f"recheck_{gate['reason']}"}

        result = self._post_impression(sender_id=uid, impression=cleaned, now=stamp)
        identities = [str(item) for item in (result.get("identities") or []) if str(item).strip()]

        if identities:
            self._remember_stats["succeeded"] += 1
            self._remember_stats["empty_streak"] = 0
            self._info(f"[DeepChat] 里程碑印象写入：新增 {len(identities)} 条事实")
            return {
                "ok": True,
                "written": True,
                "reason": "written",
                "identities": identities,
                "transport": result.get("transport", "unknown"),
            }

        streak = self._remember_stats["empty_streak"] + 1
        self._remember_stats["empty_streak"] = streak
        threshold = max(1, self._int_setting("deepchat_remember_empty_streak_warn", REMEMBER_EMPTY_STREAK_WARN))
        if self._remember_stats["succeeded"] == 0:
            # 一次就够：从没成功过说明契约/闸/线路有一处不对，别每轮刷同一条。
            if streak == 1:
                self._warn(
                    "[DeepChat] 里程碑写入从未产出新事实（never_succeeded）："
                    "检查服务端契约、开关与线路"
                )
            reason = "never_succeeded"
        elif streak >= threshold:
            self._warn(
                f"[DeepChat] 里程碑写入连续 {streak} 次零新事实（empty_streak）："
                "可能的判断是提炼退化成空转或服务端丢弃"
            )
            reason = "empty_streak"
        else:
            self._info("[DeepChat] 里程碑写入本次无新事实（no_new_fact）")
            reason = "no_new_fact"
        return {
            "ok": False,
            "written": False,
            "reason": reason,
            "identities": [],
            "transport": result.get("transport", "unknown"),
        }

    def _verbatim_overlap(self, sender_id: str, text: str) -> str:
        """返回命中的用户原话片段（空串 = 未命中）。

        只看**较长的**原话片段（≥ `REMEMBER_GUARD_MIN_CHARS`）：否则"好的"/"嗯"这类
        高频短句会把正常提炼全部误杀。
        """
        try:
            recent = self._recent_user_messages(
                sender_id, limit=REMEMBER_GUARD_SAMPLE, per_message=REMEMBER_IMPRESSION_MAX_CHARS,
                budget=REMEMBER_IMPRESSION_MAX_CHARS * REMEMBER_GUARD_SAMPLE,
            )
        except Exception:  # noqa: BLE001 —— 取不到原话就不做这道守卫，不阻断写入
            return ""
        for line in recent:
            snippet = str(line or "").strip()
            if len(snippet) >= REMEMBER_GUARD_MIN_CHARS and snippet in text:
                return snippet
        return ""

    def _remember_gate(self, sender_id: str) -> dict[str, Any]:
        """授权闸：开关 + 模式 + 记忆服务可用。三者缺一即 fail-closed。"""
        if not bool(self._setting("private_participant_memory_enabled", False)):
            return {"allowed": False, "reason": "participant_memory_disabled"}
        bridge = getattr(self.plugin, "memory_bridge", None)
        if bridge is None:
            return {"allowed": False, "reason": "memory_unavailable"}
        user_data = (getattr(self.plugin, "_user_sessions", {}) or {}).get(f"private:{sender_id}")
        mode = str((user_data or {}).get("private_memory_mode") or "")
        if mode != "participant":
            # legacy（管理员私聊走宿主的"私聊主人语料"）与"没有会话记录"都 fail-closed：
            # 没有 participant 域可写，猜一个等于把印象丢进别的域。
            return {"allowed": False, "reason": f"mode_{mode or 'unknown'}"}
        return {"allowed": True, "reason": ""}

    def _post_impression(self, *, sender_id: str, impression: str, now: float) -> dict[str, Any]:
        """把印象交给宿主的 participant 记忆。**当前是占位**，不发真实 HTTP。

        服务端契约（assistant 抽取行为、原话守卫是否在服务端再做一次）尚未确认；
        在此之前发出去等于用未验证的语义污染真实记忆库。替换本方法即可上线。
        """
        return {"transport": "pending_server_contract", "identities": []}

    async def recall(
        self,
        *,
        query: str,
        scope: str = "",
        group_id: str = "",
        user_id: str = "",
        her_name: str = "",
        limit: int = 5,
    ) -> dict[str, Any]:
        bridge = getattr(self.plugin, "memory_bridge", None)
        if bridge is None:
            return {"ok": False, "reason": "memory_bridge_unavailable"}
        subjects = None
        if group_id and user_id:
            subjects = [bridge.group_participant_subject(group_id, user_id)]
        elif group_id:
            subjects = [bridge.group_subject(group_id)]
        elif user_id:
            subjects = [bridge.participant_subject(user_id)]
        elif scope == "global":
            # Explicit opt-in to the master's legacy corpus.
            subjects = None
        else:
            # No authorized subject: never fall back to the legacy corpus.
            subjects = []
        name = her_name or str(getattr(self.plugin, "_her_name", "") or "")
        if not name:
            try:
                from utils.config_manager import get_config_manager

                _, name, *_ = get_config_manager().get_character_data()
            except Exception:  # noqa: BLE001 —— 独立运行/测试环境没有宿主配置
                name = ""
        if not name:
            return {"ok": False, "reason": "her_name_unavailable"}
        result = await bridge.query_relevant_memory(
            name,
            query,
            subjects=subjects,
            limit=limit,
        )
        return {
            "ok": True,
            "text": result.text,
            "hit_count": result.hit_count,
            "rendered_count": result.rendered_count,
            "elapsed_ms": result.elapsed_ms,
            "subjects": subjects,
        }
