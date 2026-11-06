from __future__ import annotations

import time
from dataclasses import dataclass, field

from .emotion import LEGACY_MOOD_ALIASES, Mood
from .emotion import MoodState as MoodState
from .emotion import coerce_mood as _coerce_mood


def coerce_mood(value: object) -> Mood:
    # Compatibility exports for existing DeepChat callers and stored aliases.
    text = LEGACY_MOOD_ALIASES.get(str(value or ""), value)
    return _coerce_mood(text)


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
