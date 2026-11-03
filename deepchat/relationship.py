from __future__ import annotations

import json
import time
from typing import Any

from .models import RelationshipEdge
from .store import DeepChatStore


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _positive_or(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return number if number > 0.0 else float(fallback)


def _non_negative_or(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return number if number >= 0.0 else float(fallback)


#: 好感每经过一个半衰期减半（信任同步向 0.5 收敛）。
DEFAULT_HALF_LIFE_DAYS = 14.0
#: 好感落进 ±阈值 的带内即视为休眠，不再继续衰减。
DEFAULT_FREEZE_THRESHOLD = 0.10
#: 休眠关系要重新有分量（|affinity| 达到该值）才解冻。
DEFAULT_REVIVE_THRESHOLD = 0.15
#: 关系衰减的结算粒度：整天。关系是"天"尺度的事，秒级零头只会让同一场
#: 对话里的数值抖动，也会把相隔 1 秒的连续操作算成一次衰减。
_DAY_SECONDS = 86400.0


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

    Decay follows a half-life model: every ``half_life_days`` the deviation
    from neutral is halved (affinity halves, trust converges to 0.5). 旧的逐日
    衰减率（``decay_rate``）已被这个模型取代并移除。

    关系按整天结算：未满一天不结算、``updated_at`` 不更新、零头累计到下次
    （所以连续操作不会被算成衰减，也不会丢时长）。满一天后按
    ``0.5 ** (天数 / half_life_days)`` 结算一次。

    好感落进 ±``freeze_threshold`` 的带内即 *frozen*：停止衰减并落库，
    免得一段"淡了但还算暖"的关系悄悄归零；之后一次把 ``|affinity|`` 推回
    ``revive_threshold`` 的互动会解冻它。

    **freeze / revive 阈值按 ``abs(affinity)`` 判定**（不是字面的
    ``affinity <= threshold``）：负向关系会继续向中性收敛，进入
    ±``freeze_threshold`` 带内才冻结。否则 ``-0.8`` 首次衰减到 ``-0.4``
    就会命中 ``-0.4 <= 0.10`` 被永久钉死在"明显疏远"，既不再淡忘、又要
    ``+0.55`` 好感才解冻，与"关系会随时间淡掉"的目标相反。正负两侧对称，
    ``revive`` 同理用 ``abs``。

    Milestones 有上限，长关系不会无限撑大提示词。
    """

    MAX_MILESTONES = 20

    def __init__(
        self,
        store: DeepChatStore,
        # 放在 * 之前：既接住旧的关键字传参，也接住旧的位置传参
        # （改造前的签名是 ``(store, decay_rate=0.01)``，第 2 个位置就是它）。
        decay_rate: float | None = None,
        *,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        freeze_threshold: float = DEFAULT_FREEZE_THRESHOLD,
        revive_threshold: float = DEFAULT_REVIVE_THRESHOLD,
    ):
        """``decay_rate`` 是 deprecated 形参：接受但忽略，请不要再用（改用
        ``half_life_days``）。保留它只为公开 API 兼容——``RelationshipGraph`` 在
        ``deepchat/__all__`` 里对外暴露，外部可能仍按关键字或位置传入。
        """
        self.store = store
        self.half_life_days = _positive_or(half_life_days, DEFAULT_HALF_LIFE_DAYS)
        self.freeze_threshold = _non_negative_or(freeze_threshold, DEFAULT_FREEZE_THRESHOLD)
        # 解冻阈值必须 ≥ 冻结阈值，否则同一次互动会让关系反复冻结/解冻。
        self.revive_threshold = max(
            _non_negative_or(revive_threshold, DEFAULT_REVIVE_THRESHOLD),
            self.freeze_threshold,
        )

    def get(self, user_id: str, *, now: float | None = None) -> RelationshipEdge:
        now = time.time() if now is None else float(now)
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT user_id, affinity, trust, interactions, milestones, updated_at, frozen "
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
            frozen=bool(row["frozen"]),
        )
        decayed = self._decay(edge, now)
        if (
            decayed.affinity != edge.affinity
            or decayed.trust != edge.trust
            or decayed.frozen != edge.frozen
        ):
            self._save(decayed)
        return decayed

    def reinforce(self, user_id: str, amount: float = 0.02, *, now: float | None = None) -> RelationshipEdge:
        edge = self.get(user_id, now=now)
        now = time.time() if now is None else float(now)
        edge.affinity = _clamp(edge.affinity + amount, -1.0, 1.0)
        edge.trust = _clamp(edge.trust + amount / 2.0, 0.0, 1.0)
        edge.interactions += 1
        edge.updated_at = now
        self._revive_if_due(edge)
        self._save(edge)
        return edge

    def weaken(self, user_id: str, amount: float = 0.03, *, now: float | None = None) -> RelationshipEdge:
        edge = self.get(user_id, now=now)
        now = time.time() if now is None else float(now)
        edge.affinity = _clamp(edge.affinity - amount, -1.0, 1.0)
        edge.trust = _clamp(edge.trust - amount / 2.0, 0.0, 1.0)
        edge.interactions += 1
        edge.updated_at = now
        self._revive_if_due(edge)
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
                "SELECT user_id, affinity, trust, interactions, milestones, updated_at, frozen "
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
                frozen=bool(row["frozen"]),
            )
            decayed = self._decay(edge, now)
            if (
                decayed.affinity != edge.affinity
                or decayed.trust != edge.trust
                or decayed.frozen != edge.frozen
            ):
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
        if edge.frozen:
            # 休眠关系停在原值：既不继续变淡，也不用反复落库。
            return edge
        # 关系按整天结算：未满一天不结算、updated_at 不更新、零头累计到下次。
        elapsed = max(0.0, (now - edge.updated_at) / _DAY_SECONDS)
        days = float(int(elapsed))
        if days <= 0.0:
            return edge
        factor = 0.5 ** (days / self.half_life_days)
        decayed = RelationshipEdge(
            user_id=edge.user_id,
            affinity=_clamp(edge.affinity * factor, -1.0, 1.0),
            trust=_clamp(0.5 + (edge.trust - 0.5) * factor, 0.0, 1.0),
            interactions=edge.interactions,
            milestones=list(edge.milestones),
            # 只推进"已结算的整天"，不足一天的零头留在 elapsed 里累计到下次；
            # 直接写 now 会把零头吃掉，与 docstring 的承诺相反。
            updated_at=edge.updated_at + days * _DAY_SECONDS,
            frozen=edge.frozen,
        )
        # 按 |affinity| 判定：负向关系先继续向中性收敛，进带内才冻结。
        if abs(decayed.affinity) <= self.freeze_threshold:
            decayed.frozen = True
        return decayed

    def _revive_if_due(self, edge: RelationshipEdge) -> None:
        """A dormant relationship wakes up once it carries weight again."""
        # 同样按 |affinity|：强烈的好感或反感都算"重新有分量"，对称解冻。
        if edge.frozen and abs(edge.affinity) >= self.revive_threshold:
            edge.frozen = False

    def _save(self, edge: RelationshipEdge) -> None:
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO deepchat_relationship(
                    user_id, affinity, trust, interactions, milestones, updated_at, frozen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    affinity = excluded.affinity,
                    trust = excluded.trust,
                    interactions = excluded.interactions,
                    milestones = excluded.milestones,
                    updated_at = excluded.updated_at,
                    frozen = excluded.frozen
                """,
                (
                    edge.user_id,
                    edge.affinity,
                    edge.trust,
                    edge.interactions,
                    json.dumps(edge.milestones, ensure_ascii=False),
                    edge.updated_at,
                    1 if edge.frozen else 0,
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
