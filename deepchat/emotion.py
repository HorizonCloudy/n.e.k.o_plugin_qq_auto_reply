"""Shared emotion policy; storage adapters own persistence, never a second policy.

Group state models short-lived conversational attention (30-second steps).
User state models private affect (numeric decay). They share vocabulary and
transition/rendering code, but never overwrite or blend one another.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Mood(str, Enum):
    NEUTRAL = "calm"
    ARGUING = "arguing"
    PROUD = "proud"
    PLAYFUL = "playful"
    CURIOUS = "curious"
    SAD = "sad"
    HAPPY = "happy"
    EXCITED = "excited"
    SULKING = "sulking"
    EMBARRASSED = "embarrassed"
    BORED = "bored"
    ANNOYED = "annoyed"



#: 历史取值 → 现取值。3D 统一词表时 ``neutral`` 并入 ``calm``（attention 侧唯一的
#: 零点），但旧 SQLite 行与旧配置里仍可能存着 ``neutral``。
LEGACY_MOOD_ALIASES: dict[str, str] = {"neutral": Mood.NEUTRAL.value}


def coerce_mood(value: object) -> Mood:
    """把（可能是历史取值的）值转成 :class:`Mood`；未知值退回 ``NEUTRAL``。

    为什么不用 ``Mood(value)``：存量库里写着 ``neutral`` 的行会让它抛 ValueError，
    而情绪是「读不到就按默认」的软状态——宁可归一化，也不要让一次读取炸掉回复链路。
    """
    if isinstance(value, Mood):
        return value
    text = str(value or "").strip()
    text = LEGACY_MOOD_ALIASES.get(text, text)
    try:
        return Mood(text)
    except ValueError:
        return Mood.NEUTRAL


@dataclass(slots=True)
class MoodState:
    valence: float = 0.0
    arousal: float = 0.5
    current: Mood = Mood.NEUTRAL
    updated_at: float = field(default_factory=time.time)



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
    Mood.ARGUING: (-0.6, 0.95),
    Mood.PROUD: (0.7, 0.55),
    Mood.PLAYFUL: (0.3, 0.7),
    Mood.CURIOUS: (0.05, 0.7),
    Mood.SAD: (-0.4, 0.2),
    Mood.HAPPY: (0.35, 0.55),
    Mood.EXCITED: (0.6, 0.8),
    Mood.SULKING: (-0.35, 0.4),
    Mood.EMBARRASSED: (0.1, 0.6),
    Mood.BORED: (-0.05, 0.15),
    Mood.ANNOYED: (-0.55, 0.65),
}

MOOD_LABELS: dict[Mood, str] = {
    Mood.NEUTRAL: "平静",
    Mood.ARGUING: "上头",
    Mood.PROUD: "得意",
    Mood.PLAYFUL: "想玩闹",
    Mood.CURIOUS: "好奇",
    Mood.SAD: "难过",
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



EMOTION_MULTIPLIER: dict[str, float] = {
    "arguing": 1.2,      # 上头死磕，涨得快跌得慢
    "proud": 0.8,        # 赢了要炫耀，猛拉注意力
    "excited": 0.6,      # 兴奋：比"不爽"更往外冲，但没到炫耀
    "annoyed": 0.5,      # 不爽，比正常更专注
    "happy": 0.4,        # 开心：比玩闹更稳，比不爽更轻
    "playful": 0.3,      # 玩闹中，微微挂住
    "curious": 0.2,      # 被勾起兴趣
    "calm": 0.0,         # 正常
    "sad": -0.4,         # 难过，不太想说话
    "embarrassed": -0.6,  # 尴尬想溜
    "bored": -0.7,       # 没兴趣的话题，主动掉注意力去找别的群
    "sulking": -0.9,     # 赌气——基本清零，主动让出焦点
}
# 强情绪直接触发焦点切换。
#
# ⚠️ 这两个集合与 `EMOTION_MULTIPLIER` 是**分开维护**的：往倍率表加新情绪不会
# 自动让它获得抢/让焦点行为。新增情绪时必须同时决定它属于哪一档（或都不属于），
# 否则该情绪会「能配置但无行为」。`tests/test_qq_emotion_vocabulary.py` 盯着这条
# 一致性 —— 新情绪若既不在抢焦点也不在让焦点集合里，必须显式登记到该测试的
# `_NEUTRAL_EMOTIONS` 白名单，逼作者做一次有意识的选择。
EMOTION_FORCE_FOCUS = {"arguing", "proud"}     # 立刻抢焦点
EMOTION_DROP_FOCUS = {"sulking", "embarrassed", "bored"}  # 立刻让出焦点
# 情绪降温阶梯：30 秒无新情绪就朝 calm 方向走一级。顺序 = 倍率从高到低，
# 于是「上升侧 = calm 之前」「回落侧 = calm 之后」与倍率符号自动一致。
EMOTION_DECAY_ORDER = sorted(EMOTION_MULTIPLIER, key=EMOTION_MULTIPLIER.__getitem__, reverse=True)
EMOTION_DECAY_SECONDS = 30



def update_affect(state: MoodState, signal: str, now: float) -> MoodState:
    dv, da = SIGNAL_DELTAS.get(signal, (0.0, 0.0))
    valence = _clamp(state.valence + dv, -1.0, 1.0)
    arousal = _clamp(state.arousal + da, 0.0, 1.0)
    return MoodState(valence, arousal, classify(valence, arousal), now)


def decay_affect(state: MoodState, now: float, decay_rate: float) -> MoodState:
    minutes = max(0.0, (now - state.updated_at) / 60.0)
    if minutes <= 0.0 or decay_rate <= 0.0:
        return state
    factor = max(0.0, 1.0 - decay_rate * minutes)
    valence = _clamp(state.valence * factor, -1.0, 1.0)
    arousal = _clamp(0.5 + (state.arousal - 0.5) * factor, 0.0, 1.0)
    return MoodState(valence, arousal, classify(valence, arousal), now)


def update_group(state, emotion: str, now: int) -> None:
    state.emotion = emotion
    state.emotion_updated_at = now
    state.emotion_display = emotion
    state.emotion_display_until = now + 120


def decay_group(state, now: int) -> None:
    if state.emotion == "calm" or now - state.emotion_updated_at < EMOTION_DECAY_SECONDS:
        return
    order = EMOTION_DECAY_ORDER
    calm = order.index("calm")
    idx = order.index(state.emotion) if state.emotion in order else -1
    if idx < 0:
        state.emotion = "calm"
    else:
        state.emotion = order[idx + 1 if idx < calm else idx - 1]
    state.emotion_updated_at = now
    state.emotion_display = state.emotion


def emotion_fragment(emotion: Mood | str, *, is_group: bool) -> str:
    mood = coerce_mood(emotion)
    if is_group and mood == Mood.NEUTRAL:
        return ""
    label = MOOD_LABELS[mood]
    instruction = "用 <feeling>情绪</feeling> 更新群状态（不发给对方）。" if is_group else ""
    return (
        "## 当前情绪（Mood）\n"
        f"[内部状态] 你现在的情绪: {mood.value}（{label}）。"
        f"{instruction}回复时自然体现这种心情，不要直接说出数值或情绪标签。"
    )
