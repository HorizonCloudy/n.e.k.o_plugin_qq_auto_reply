from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import Mood, MoodState, RelationshipEdge


@dataclass(slots=True)
class AutonomyCandidate:
    user_id: str
    reason: str
    affinity: float
    idle_hours: float


class AutonomyScheduler:
    """Per-user proactive-contact gate.

    Reuses the existing proactive-initiative settings for quiet hours, daily
    cap and minimum gap. The DeepChat-specific part is the consent signal:
    only users who have actually talked to the bot in a private session are
    candidates. A group-only relationship never becomes a DM target.
    """

    def __init__(self, service: Any):
        self.service = service
        self.plugin = service.plugin
        self.store = service.store
        self._init_schema()

    def _init_schema(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deepchat_autonomy (
                    user_id TEXT PRIMARY KEY,
                    private_interactions INTEGER NOT NULL DEFAULT 0,
                    last_private_at REAL NOT NULL DEFAULT 0,
                    last_reach_out_at REAL NOT NULL DEFAULT 0,
                    count_today INTEGER NOT NULL DEFAULT 0,
                    day_key TEXT NOT NULL DEFAULT ''
                )
                """
            )

    # ── settings ────────────────────────────────────────────────────

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

    @property
    def enabled(self) -> bool:
        """双开关 AND：深聊自主联系 **且** 既有「有由头主动搭话」总开关。

        两者都会让"她"主动开口，只开一个就发消息等于绕过了使用者的另一个刹车。
        """
        return bool(self._setting("deepchat_autonomy_enabled", False)) and bool(
            self._setting("proactive_initiative_enabled", False)
        )

    @staticmethod
    def _day_key(now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    def _daily_quota_exhausted(self, now: float) -> bool:
        """「有由头主动搭话」的日限还剩 0 次时，深聊自主联系也不再开口。

        两个功能都是"她主动私聊"，共用 ``proactive_initiative_max_per_day`` 这一个
        日限——否则两套账本各记各的，用户设的 2 次会变成实际 4 次。
        账本仍在既有服务里（这里只读），完整共享账本留 follow-up。
        """
        initiative = getattr(self.service.plugin, "proactive_initiative_service", None)
        if initiative is None:
            return False
        try:
            return int(initiative.remaining_today(now=now)) <= 0
        except Exception:  # noqa: BLE001 —— 读不到日限时 fail-open，不因此彻底静默
            return False

    def _within_quiet_hours(self, now: float) -> bool:
        start = self._int_setting("proactive_initiative_quiet_start_hour", 23) % 24
        end = self._int_setting("proactive_initiative_quiet_end_hour", 8) % 24
        if start == end:
            return False
        hour = datetime.fromtimestamp(now).hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    # ── state ───────────────────────────────────────────────────────

    def _row(self, user_id: str) -> dict[str, Any] | None:
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT user_id, private_interactions, last_private_at, "
                "last_reach_out_at, count_today, day_key "
                "FROM deepchat_autonomy WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_private_interaction(self, user_id: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO deepchat_autonomy(
                    user_id, private_interactions, last_private_at,
                    last_reach_out_at, count_today, day_key
                ) VALUES (?, 1, ?, 0, 0, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    private_interactions = deepchat_autonomy.private_interactions + 1,
                    last_private_at = excluded.last_private_at
                """,
                (user_id, now, self._day_key(now)),
            )

    def mark_reached_out(self, user_id: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        today = self._day_key(now)
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO deepchat_autonomy(
                    user_id, private_interactions, last_private_at,
                    last_reach_out_at, count_today, day_key
                ) VALUES (?, 0, 0, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_reach_out_at = excluded.last_reach_out_at,
                    count_today = CASE
                        WHEN deepchat_autonomy.day_key = excluded.day_key
                        THEN deepchat_autonomy.count_today + 1
                        ELSE 1
                    END,
                    day_key = excluded.day_key
                """,
                (user_id, now, today),
            )

    # ── decision ────────────────────────────────────────────────────

    def should_reach_out(self, user_id: str, *, now: float | None = None) -> AutonomyCandidate | None:
        if not self.enabled:
            return None
        now = time.time() if now is None else float(now)
        if self._daily_quota_exhausted(now):
            return None
        if self._within_quiet_hours(now):
            return None
        row = self._row(user_id)
        if row is None or int(row["private_interactions"]) <= 0:
            return None
        edge = self.service.relationship.get(user_id, now=now)
        min_affinity = self._float_setting("deepchat_autonomy_min_affinity", 0.35)
        if edge.affinity < min_affinity:
            return None
        idle_hours = max(0.0, (now - float(row["last_private_at"])) / 3600.0)
        if idle_hours < self._float_setting("deepchat_autonomy_min_idle_hours", 6.0):
            return None
        if idle_hours > self._float_setting("deepchat_autonomy_max_idle_hours", 72.0):
            return None
        last_out = float(row["last_reach_out_at"] or 0.0)
        if last_out and now - last_out < self._float_setting(
            "proactive_initiative_min_gap_seconds", 10800.0,
        ):
            return None
        count_today = (
            int(row["count_today"])
            if str(row["day_key"]) == self._day_key(now)
            else 0
        )
        if count_today >= self._int_setting("proactive_initiative_max_per_day", 2):
            return None
        mood = self.service.mood.get(self.service._mood_key(user_id), now=now)
        return AutonomyCandidate(
            user_id=user_id,
            reason=self._reason(edge, mood),
            affinity=edge.affinity,
            idle_hours=idle_hours,
        )

    def tick(self, *, now: float | None = None, limit: int = 1) -> list[AutonomyCandidate]:
        if not self.enabled:
            return []
        now = time.time() if now is None else float(now)
        candidates: list[AutonomyCandidate] = []
        for user_id in self.service.relationship.list_user_ids():
            candidate = self.should_reach_out(user_id, now=now)
            if candidate is not None:
                candidates.append(candidate)
                if len(candidates) >= max(1, int(limit)):
                    break
        return candidates

    def snapshot(self, user_id: str) -> dict[str, Any]:
        row = self._row(user_id) or {}
        return {
            "enabled": self.enabled,
            "private_interactions": int(row.get("private_interactions", 0) or 0),
            "last_private_at": float(row.get("last_private_at", 0.0) or 0.0),
            "last_reach_out_at": float(row.get("last_reach_out_at", 0.0) or 0.0),
            "count_today": int(row.get("count_today", 0) or 0),
            "day_key": str(row.get("day_key", "") or ""),
        }

    def _reason(self, edge: RelationshipEdge, mood: MoodState) -> str:
        if mood.current in (Mood.HAPPY, Mood.EXCITED):
            return "今天心情不错，突然想起你了。"
        if mood.current in (Mood.SULKING, Mood.ANNOYED):
            return "有点小情绪，想找你聊两句。"
        if mood.current == Mood.BORED:
            return "有点无聊，你在干嘛？"
        if edge.affinity >= 0.75:
            return "有点想你了，最近怎么样？"
        return "突然想起上次聊的事，想接着说说。"
