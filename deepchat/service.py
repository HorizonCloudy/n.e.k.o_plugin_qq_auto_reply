from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .autonomy import AutonomyScheduler
from .fragments import append_fragments
from .guard import sanitize_fragment
from .models import Mood
from .mood import MoodStateMachine
from .relationship import RelationshipGraph
from .store import DeepChatStore

POSITIVE_SIGNALS = frozenset({"praise", "thank", "funny"})
NEGATIVE_SIGNALS = frozenset({"insult"})

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
    if number < 0:
        raise ValueError("数值不能为负")
    return number


def _as_int(value: Any) -> int:
    number = int(value)
    if number < 0:
        raise ValueError("数值不能为负")
    return number


def _as_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"host", "direct"}:
        raise ValueError("generation_mode 只能取 host 或 direct")
    return mode


TUNING_KEYS: dict[str, Any] = {
    "deepchat_enabled": _as_bool,
    "deepchat_fragments_enabled": _as_bool,
    "deepchat_guard_enabled": _as_bool,
    "deepchat_reflection_enabled": _as_bool,
    "deepchat_mood_decay_rate": _as_float,
    "deepchat_relationship_decay_rate": _as_float,
    "deepchat_generation_mode": _as_mode,
    "deepchat_autonomy_enabled": _as_bool,
    "deepchat_autonomy_min_affinity": _as_float,
    "deepchat_autonomy_min_idle_hours": _as_float,
    "deepchat_autonomy_max_idle_hours": _as_float,
    "deepchat_autonomy_interval_seconds": _as_float,
    "deepchat_autonomy_max_per_tick": _as_int,
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
        self.mood = MoodStateMachine(
            self.store,
            decay_rate=self._float_setting("deepchat_mood_decay_rate", 0.05),
        )
        self.relationship = RelationshipGraph(
            self.store,
            decay_rate=self._float_setting("deepchat_relationship_decay_rate", 0.01),
        )
        self.autonomy = AutonomyScheduler(self)

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
        mode = str(self._setting("deepchat_generation_mode", "host") or "").strip().lower()
        return mode if mode in {"host", "direct"} else "host"

    @staticmethod
    def _mood_key(sender_id: str) -> str:
        return f"user:{str(sender_id or '').strip()}"

    def enrich_system_prompt(
        self,
        *,
        system_prompt: str,
        sender_id: str,
        group_id: str | None = None,
        is_group: bool = False,
        sender_name: str = "",
    ) -> str:
        if not self.enabled or not self.fragments_enabled:
            return system_prompt
        sender_id = str(sender_id or "").strip()
        if not sender_id:
            return system_prompt
        state = self.mood.get(self._mood_key(sender_id))
        edge = self.relationship.get(sender_id)
        fragments: list[str] = []
        for fragment in (
            self.mood.fragment(state),
            self.relationship.fragment(edge, sender_name or sender_id),
        ):
            if self.guard_enabled:
                safe = sanitize_fragment(fragment)
                if safe is None:
                    self._warn("[DeepChat] 片段未通过人格守卫，已跳过")
                    continue
                fragments.append(safe)
            else:
                fragments.append(fragment)
        return append_fragments(system_prompt, *fragments)

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
        result = {
            "signal": signal,
            "mood": state.current.value,
            "affinity": round(edge.affinity, 4),
            "trust": round(edge.trust, 4),
        }
        if reflected:
            result["reflection"] = reflected
        return result

    def reflect_if_due(
        self,
        user_id: str,
        *,
        edge: Any | None = None,
        now: float | None = None,
    ) -> str | None:
        """Every N interactions, turn the last few user lines into a milestone.

        This is a lightweight reflection, not a replacement for the host's
        session-memory digest: it only adds a short, human-readable "what we
        talked about" line to the relationship edge. No LLM call, no new memory
        store.
        """
        if not bool(self._setting("deepchat_reflection_enabled", True)):
            return None
        edge = edge or self.relationship.get(user_id, now=now)
        if edge.interactions <= 0 or edge.interactions % 10 != 0:
            return None
        summary = self._recent_summary(user_id)
        if not summary:
            return None
        milestone = f"最近聊到：{summary}"
        if milestone in edge.milestones:
            return None
        self.relationship.add_milestone(user_id, milestone, now=now)
        return milestone

    def _recent_summary(self, user_id: str) -> str:
        sessions = getattr(self.plugin, "_user_sessions", {}) or {}
        build_key = getattr(self.plugin, "_build_session_key", None)
        if not callable(build_key):
            return ""
        try:
            session_key = build_key(sender_id=user_id, is_group=False)
        except Exception:  # noqa: BLE001 —— 测试/轻量宿主可能没有完整会话键
            return ""
        user_data = sessions.get(session_key) or {}
        session = user_data.get("session")
        history = getattr(session, "_conversation_history", []) or []
        texts: list[str] = []
        for item in reversed(history):
            role = item.get("role") if isinstance(item, dict) else getattr(item, "role", "")
            content = item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
            if str(role) != "user":
                continue
            text = str(content or "").strip()
            if text:
                texts.append(text)
            if len(texts) >= 3:
                break
        texts.reverse()
        if not texts:
            return ""
        return "；".join(texts)[:160]

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
                        self._warn(f"[DeepChat] 主动联系失败 {candidate.user_id}: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不得终止循环
                self._warn(f"[DeepChat] 自主调度轮次异常: {exc}")

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
                "updated_at": edge.updated_at,
            },
            "autonomy": self.autonomy.snapshot(sender_id),
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
        return {
            "user_id": sender_id,
            "mood_deleted": mood_deleted,
            "relationship_deleted": relationship_deleted,
            "autonomy_deleted": autonomy_deleted,
        }

    def set_mood(self, user_id: str, mood: str) -> dict[str, Any]:
        sender_id = str(user_id or "").strip()
        try:
            target = Mood(str(mood or "").strip())
        except ValueError:
            valid = ", ".join(item.value for item in Mood)
            raise ValueError(f"未知情绪 {mood!r}；可选：{valid}") from None
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
            raise ValueError(f"未知参数 {key!r}；可选：{', '.join(TUNING_KEYS)}")
        parsed = TUNING_KEYS[key](value)
        settings = getattr(self.plugin, "_qq_settings", None)
        if settings is None:
            raise ValueError("插件配置不可用")
        settings[key] = parsed
        save = getattr(getattr(self.plugin, "config_store", None), "save", None)
        if callable(save):
            await save(settings)
        return {"param": key, "value": parsed}

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
