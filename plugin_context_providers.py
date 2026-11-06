"""Trusted read-only context adapters. Generic tool grants remain the single source of authorization.

Unknown plugins still use the ordinary tool bridge. Automatic background requires
an explicit typed/whitelisted adapter here; plugin descriptions are never prompts.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .plugin_authorization import normalize_plugin_tiers

ACTIONS = {
    "ebb_tide": "暂时希望保持一些距离",
    "sea_fog": "暂时不想继续某个话题",
    "shallow_reef": "暂时不太想展开交流",
    "storm_surge": "整体情绪较强烈，仍应尊重对方",
    "seek_harbor": "希望获得安慰与安全感",
    "ripple": "心情有轻微波动",
    "warm_current": "整体温暖、亲近",
    "spring_tide": "整体开心、活跃",
    "rising_tide": "之前的情绪动作记录已结束，不代表必须立刻亲近",
}
def render_mood(value: dict, character: str) -> str:
    if (
        value.get("schema_version") != 1
        or value.get("lanlan") != character
        or value.get("available") is not True
        or value.get("enabled") is not True
    ):
        return ""
    now = time.time()
    stamp, expires = value.get("sampled_at"), value.get("expires_at")
    if not all(type(v) in (int, float) and 0 <= v <= 1e12 for v in (stamp, expires)):
        return ""
    if not -5 <= now - stamp <= 30 or (expires > 0 and expires <= now):
        return ""
    action = value.get("action")
    description = ACTIONS.get(action) if isinstance(action, str) else None
    return "永远的陪伴：角色整体状态为「" + description + "」。" if description else ""

def render_presence(value: dict) -> str:
    if (
        value.get("date") != datetime.now().strftime("%Y-%m-%d")
        or value.get("recorded") is not True
        or value.get("paused") is not False
    ):
        return ""
    duration, clock = value.get("companion"), value.get("as_of")
    # Only accept the provider's duration grammar, never summary/guidance/diary text.
    if not isinstance(duration, str) or not re.fullmatch(
        r"(?:[0-9]{1,2} 秒|[0-9]{1,2} 分钟|[0-9]{1,2} 小时(?: [0-9]{1,2} 分)?)",
        duration,
    ):
        return ""
    if not isinstance(clock, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
        return ""
    return (
        f"她的一天：今天截至 {clock}，桌面陪伴记录为 {duration}。"
        "这是绑定主人的桌面统计，不是 QQ 在线时长；记录空白/输入空闲不能证明离开，"
        "不代表全天完整陪伴，也不据此增减亲密度。"
    )

@dataclass(frozen=True)
class ContextProvider:
    plugin_id: str
    entry_id: str
    label: str
    legacy_flag: str
    params: Callable[[str], dict]
    render: Callable[[dict, str], str]


CONTEXT_PROVIDERS = (
    ContextProvider(
        "forever_companion", "get_emotion_snapshot", "角色整体情绪", "mood_enabled",
        lambda character: {"lanlan": character}, render_mood,
    ),
    ContextProvider(
        "neko_presence", "__llm_tool__presence_today", "桌面陪伴事实", "presence_enabled",
        lambda character: {"date": ""}, lambda value, character: render_presence(value),
    ),
)


def selected_providers(settings: dict, binding: dict) -> tuple[ContextProvider, ...]:
    if binding.get("source_mode") == "plugin_tools":
        tiers = normalize_plugin_tiers(settings.get("qq_open_plugin_tools"))
        # 'all' also authorizes administrators. Owner/character/transport scoping
        # is enforced separately and is NEVER widened to all users here.
        return tuple(p for p in CONTEXT_PROVIDERS if p.plugin_id in tiers)
    # Existing users must explicitly save the new binding to migrate. A prior
    # opt-out is not silently changed just because generic tools were allowed.
    return tuple(p for p in CONTEXT_PROVIDERS if binding.get(p.legacy_flag) is True)


def capability_hints(plugin_id: str, entries: list) -> list[dict]:
    ids = set()
    for entry in entries if isinstance(entries, list) else []:
        entry_id = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(entry_id, str):
            ids.add(entry_id)
    return [{"label": p.label, "entry_id": p.entry_id, "available": p.entry_id in ids}
            for p in CONTEXT_PROVIDERS if p.plugin_id == plugin_id]
