from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Mood(str, Enum):
    NEUTRAL = "neutral"
    HAPPY = "happy"
    EXCITED = "excited"
    SULKING = "sulking"
    EMBARRASSED = "embarrassed"
    BORED = "bored"
    ANNOYED = "annoyed"


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
