from __future__ import annotations

import sqlite3
import time

from .models import Mood, MoodState
from .store import DeepChatStore

#: Keyword signals mapped to a (valence, arousal) delta. This is a cheap
#: heuristic on purpose: the deep-chat layer must not add an LLM call per turn.
SIGNAL_DELTAS: dict[str, tuple[float, float]] = {
    "praise": (0.18, 0.06),
    "thank": (0.12, 0.04),
    "funny": (0.10, 0.10),
    "insult": (-0.22, 0.12),
    "bored": (-0.04, -0.20),
    "serious": (0.0, -0.06),
}

MOOD_ANCHORS: dict[Mood, tuple[float, float]] = {
    Mood.NEUTRAL: (0.0, 0.5),
    Mood.HAPPY: (0.35, 0.55),
    Mood.EXCITED: (0.6, 0.8),
    Mood.SULKING: (-0.35, 0.4),
    Mood.EMBARRASSED: (0.1, 0.6),
    Mood.BORED: (-0.05, 0.15),
    Mood.ANNOYED: (-0.55, 0.65),
}

MOOD_LABELS: dict[Mood, str] = {
    Mood.NEUTRAL: "平静",
    Mood.HAPPY: "开心",
    Mood.EXCITED: "兴奋",
    Mood.SULKING: "有点闹别扭",
    Mood.EMBARRASSED: "有点不好意思",
    Mood.BORED: "有点无聊",
    Mood.ANNOYED: "有点烦",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def classify(valence: float, arousal: float) -> Mood:
    if valence >= 0.5 and arousal >= 0.6:
        return Mood.EXCITED
    if valence >= 0.15:
        return Mood.HAPPY
    if valence <= -0.5:
        return Mood.ANNOYED
    if valence <= -0.15:
        return Mood.SULKING
    if arousal <= 0.3:
        return Mood.BORED
    return Mood.NEUTRAL


class MoodStateMachine:
    """Persistent affect state with lazy time decay.

    ``decay_rate`` is per minute: after 20 minutes at the default 0.05 the
    deviation from neutral is gone. Decay is applied **lazily on read**, so no timer
    is required for correctness. ``decay_all`` 目前**未接线**（没有后台清扫）：
    需要批量结算时必须由调用方显式调用，不要假设有定时任务在跑。
    """

    def __init__(self, store: DeepChatStore, decay_rate: float = 0.05):
        self.store = store
        self.decay_rate = max(0.0, float(decay_rate))

    def get(
        self,
        user_key: str,
        *,
        now: float | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> MoodState:
        """读情绪状态；``conn`` 传入时复用调用方的连接（见 ``DeepChatStore.session``）。

        复用连接的意义：读之后若触发衰减还会**写一次**，同一连接可少拿一次锁。
        """
        now = time.time() if now is None else float(now)
        sql = (
            "SELECT valence, arousal, current, updated_at "
            "FROM deepchat_mood WHERE user_key = ?"
        )
        if conn is None:
            with self.store.connection() as own_conn:
                row = own_conn.execute(sql, (user_key,)).fetchone()
        else:
            row = conn.execute(sql, (user_key,)).fetchone()
        if row is None:
            return MoodState(updated_at=now)
        state = MoodState(
            valence=float(row["valence"]),
            arousal=float(row["arousal"]),
            current=Mood(str(row["current"])),
            updated_at=float(row["updated_at"]),
        )
        decayed = self._decay(state, now)
        if decayed.valence != state.valence or decayed.arousal != state.arousal:
            self._save(user_key, decayed, conn=conn)
        return decayed

    def update(self, user_key: str, signal: str, *, now: float | None = None) -> MoodState:
        now = time.time() if now is None else float(now)
        state = self.get(user_key, now=now)
        dv, da = SIGNAL_DELTAS.get(signal, (0.0, 0.0))
        state.valence = _clamp(state.valence + dv, -1.0, 1.0)
        state.arousal = _clamp(state.arousal + da, 0.0, 1.0)
        state.current = classify(state.valence, state.arousal)
        state.updated_at = now
        self._save(user_key, state)
        return state

    def set_mood(self, user_key: str, mood: Mood | str, *, now: float | None = None) -> MoodState:
        now = time.time() if now is None else float(now)
        target = mood if isinstance(mood, Mood) else Mood(str(mood))
        valence, arousal = MOOD_ANCHORS[target]
        state = MoodState(valence=valence, arousal=arousal, current=target, updated_at=now)
        self._save(user_key, state)
        return state

    def decay_all(self, *, now: float | None = None) -> int:
        """批量结算所有情绪行。**当前未接线**：没有任何定时器/循环调用它，
        正确性靠读路径的惰性结算；不要假设存在后台清扫。
        """
        now = time.time() if now is None else float(now)
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT user_key, valence, arousal, current, updated_at FROM deepchat_mood"
            ).fetchall()
        changed = 0
        for row in rows:
            state = MoodState(
                valence=float(row["valence"]),
                arousal=float(row["arousal"]),
                current=Mood(str(row["current"])),
                updated_at=float(row["updated_at"]),
            )
            decayed = self._decay(state, now)
            if decayed.valence != state.valence or decayed.arousal != state.arousal:
                self._save(str(row["user_key"]), decayed)
                changed += 1
        return changed

    def fragment(self, state: MoodState) -> str:
        label = MOOD_LABELS.get(state.current, "平静")
        return (
            "## 当前情绪（Mood）\n"
            f"你现在的心情是「{label}」（valence {state.valence:+.2f}, arousal {state.arousal:.2f}）。"
            "回复时自然体现这种心情，不要直接说出数值或情绪标签。"
        )

    def _decay(self, state: MoodState, now: float) -> MoodState:
        minutes = max(0.0, (now - state.updated_at) / 60.0)
        if minutes <= 0.0:
            return state
        factor = max(0.0, 1.0 - self.decay_rate * minutes)
        valence = _clamp(state.valence * factor, -1.0, 1.0)
        arousal = _clamp(0.5 + (state.arousal - 0.5) * factor, 0.0, 1.0)
        return MoodState(
            valence=valence,
            arousal=arousal,
            current=classify(valence, arousal),
            updated_at=now,
        )

    def _save(
        self,
        user_key: str,
        state: MoodState,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """写一行情绪状态；``conn`` 传入时复用调用方连接（不再自开）。"""
        if conn is None:
            with self.store.connection() as own_conn:
                self._execute_save(own_conn, user_key, state)
        else:
            self._execute_save(conn, user_key, state)

    @staticmethod
    def _execute_save(
        conn: sqlite3.Connection,
        user_key: str,
        state: MoodState,
    ) -> None:
        conn.execute(
            """
            INSERT INTO deepchat_mood(user_key, valence, arousal, current, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_key) DO UPDATE SET
                valence = excluded.valence,
                arousal = excluded.arousal,
                current = excluded.current,
                updated_at = excluded.updated_at
            """,
            (user_key, state.valence, state.arousal, state.current.value, state.updated_at),
        )
