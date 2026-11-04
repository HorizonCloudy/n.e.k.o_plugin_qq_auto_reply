from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Mood(str, Enum):
    NEUTRAL = "calm"
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


@dataclass(slots=True)
class RelationshipEdge:
    user_id: str
    affinity: float = 0.0
    trust: float = 0.5
    interactions: int = 0
    milestones: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    #: 休眠标记：关系淡到阈值以内后停在这里，不再继续衰减，等互动解冻。
    frozen: bool = False


@dataclass(slots=True)
class ChatEvent:
    """Normalized inbound turn.

    The QQ adapter normalizes platform messages before this layer; this
    envelope is deliberately dependency-free so the deep-chat state modules
    can be tested without the host SDK.
    """

    sender_id: str
    sender_name: str = ""
    group_id: str | None = None
    is_group: bool = False
    text: str = ""
    addressed: bool = False
    timestamp: float = field(default_factory=time.time)
