from __future__ import annotations

import json
import time

from .models import RelationshipEdge
from .store import DeepChatStore


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def affinity_tier(affinity: float) -> str:
    if affinity <= -0.3:
        return "明显疏远"
    if affinity < 0.1:
        return "普通群友"
    if affinity < 0.4:
        return "聊得来"
    if affinity < 0.75:
        return "关系不错"
    return "很亲近"


class RelationshipGraph:
    """Per-user affinity/trust with lazy day-based decay.

    ``decay_rate`` is per day: a value of 0.01 means roughly 1% of the
    deviation from neutral is lost per day. Milestones are capped so a long
    relationship cannot grow the prompt without bound.
    """

    MAX_MILESTONES = 20

    def __init__(self, store: DeepChatStore, decay_rate: float = 0.01):
        self.store = store
        self.decay_rate = max(0.0, float(decay_rate))

    def get(self, user_id: str, *, now: float | None = None) -> RelationshipEdge:
        now = time.time() if now is None else float(now)
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT user_id, affinity, trust, interactions, milestones, updated_at "
                "FROM deepchat_relationship WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return RelationshipEdge(user_id=user_id, updated_at=now)
        edge = RelationshipEdge(
            user_id=str(row["user_id"]),
            affinity=float(row["affinity"]),
            trust=float(row["trust"]),
            interactions=int(row["interactions"]),
            milestones=_load_milestones(row["milestones"]),
            updated_at=float(row["updated_at"]),
        )
        decayed = self._decay(edge, now)
        if decayed.affinity != edge.affinity or decayed.trust != edge.trust:
            self._save(decayed)
        return decayed

    def reinforce(self, user_id: str, amount: float = 0.02, *, now: float | None = None) -> RelationshipEdge:
        edge = self.get(user_id, now=now)
        now = time.time() if now is None else float(now)
        edge.affinity = _clamp(edge.affinity + amount, -1.0, 1.0)
        edge.trust = _clamp(edge.trust + amount / 2.0, 0.0, 1.0)
        edge.interactions += 1
        edge.updated_at = now
        self._save(edge)
        return edge

    def weaken(self, user_id: str, amount: float = 0.03, *, now: float | None = None) -> RelationshipEdge:
        edge = self.get(user_id, now=now)
        now = time.time() if now is None else float(now)
        edge.affinity = _clamp(edge.affinity - amount, -1.0, 1.0)
        edge.trust = _clamp(edge.trust - amount / 2.0, 0.0, 1.0)
        edge.interactions += 1
        edge.updated_at = now
        self._save(edge)
        return edge

    def add_milestone(self, user_id: str, label: str, *, now: float | None = None) -> RelationshipEdge:
        edge = self.get(user_id, now=now)
        text = str(label or "").strip()
        if text and text not in edge.milestones:
            edge.milestones.append(text)
            if len(edge.milestones) > self.MAX_MILESTONES:
                edge.milestones = edge.milestones[-self.MAX_MILESTONES:]
            edge.updated_at = time.time() if now is None else float(now)
            self._save(edge)
        return edge

    def decay_all(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT user_id, affinity, trust, interactions, milestones, updated_at "
                "FROM deepchat_relationship"
            ).fetchall()
        changed = 0
        for row in rows:
            edge = RelationshipEdge(
                user_id=str(row["user_id"]),
                affinity=float(row["affinity"]),
                trust=float(row["trust"]),
                interactions=int(row["interactions"]),
                milestones=_load_milestones(row["milestones"]),
                updated_at=float(row["updated_at"]),
            )
            decayed = self._decay(edge, now)
            if decayed.affinity != edge.affinity or decayed.trust != edge.trust:
                self._save(decayed)
                changed += 1
        return changed

    def list_user_ids(self) -> list[str]:
        """All users with a relationship row, newest activity first."""
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT user_id FROM deepchat_relationship ORDER BY updated_at DESC"
            ).fetchall()
        return [str(row["user_id"]) for row in rows]

    def fragment(self, edge: RelationshipEdge, display_name: str = "") -> str:
        name = display_name or edge.user_id
        parts = [
            "## 与当前对话者的关系（Relationship）",
            f"你和「{name}」的关系：{affinity_tier(edge.affinity)}"
            f"（好感 {edge.affinity:+.2f}，信任 {edge.trust:.2f}，已互动 {edge.interactions} 次）。",
        ]
        if edge.milestones:
            parts.append("记得的共同经历：" + "；".join(edge.milestones[-3:]) + "。")
        parts.append("按这个关系亲疏自然地说话，不要直接复述数值。")
        return "\n".join(parts)

    def _decay(self, edge: RelationshipEdge, now: float) -> RelationshipEdge:
        days = max(0.0, (now - edge.updated_at) / 86400.0)
        if days <= 0.0:
            return edge
        factor = max(0.0, 1.0 - self.decay_rate) ** days
        return RelationshipEdge(
            user_id=edge.user_id,
            affinity=_clamp(edge.affinity * factor, -1.0, 1.0),
            trust=_clamp(0.5 + (edge.trust - 0.5) * factor, 0.0, 1.0),
            interactions=edge.interactions,
            milestones=list(edge.milestones),
            updated_at=now,
        )

    def _save(self, edge: RelationshipEdge) -> None:
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO deepchat_relationship(user_id, affinity, trust, interactions, milestones, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    affinity = excluded.affinity,
                    trust = excluded.trust,
                    interactions = excluded.interactions,
                    milestones = excluded.milestones,
                    updated_at = excluded.updated_at
                """,
                (
                    edge.user_id,
                    edge.affinity,
                    edge.trust,
                    edge.interactions,
                    json.dumps(edge.milestones, ensure_ascii=False),
                    edge.updated_at,
                ),
            )


def _load_milestones(raw: object) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]
