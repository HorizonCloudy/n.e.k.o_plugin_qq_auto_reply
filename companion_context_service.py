"""Owner-only, read-only companion context. No tool exposure or state writeback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from .plugin_context_providers import render_mood, render_presence, selected_providers

TOKEN_PREFIX = "companion-context:"
RPC_TIMEOUT = 1.2
TOKEN_TTL = 300
DEFAULT_CONFIG = {
    "enabled": False,
    "owner_id": "",
    "character": "",
    "bot_id": "",
    "presence_enabled": False,
    "mood_enabled": False,
}


def normalize_config(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    result = dict(DEFAULT_CONFIG)
    for key in ("enabled", "presence_enabled", "mood_enabled"):
        result[key] = raw.get(key) is True
    for key in ("owner_id", "character", "bot_id"):
        value = raw.get(key, "")
        result[key] = value.strip() if isinstance(value, str) and len(value) <= 128 else ""
    if raw.get("source_mode") == "plugin_tools":
        result["source_mode"] = "plugin_tools"
    elif raw.get("source_mode") not in (None, "legacy"):
        result["enabled"] = False
    return result


def consent_enabled(plugin: Any, key: str) -> bool:
    """Shared by generation, direct delivery and buffered/merged delivery."""
    from .group_private_memory import TOKEN_PREFIX as PRIVATE_MEMORY_PREFIX
    from .group_private_memory import private_memory_token_live

    if key.startswith(PRIVATE_MEMORY_PREFIX):
        return private_memory_token_live(plugin, key)
    if key.startswith(TOKEN_PREFIX):
        service = getattr(plugin, "companion_context_service", None)
        return service is not None and service.token_live(key)
    return bool((getattr(plugin, "_qq_settings", {}) or {}).get(key, False))


class QQCompanionContextService:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._revision = 0

    def invalidate(self) -> None:
        # A -> B -> A must not revive an already generated draft.
        self._revision += 1

    def _character(self) -> str:
        from utils.config_manager import get_config_manager

        return str(get_config_manager().get_character_data()[1])

    def _binding(self) -> tuple | None:
        try:
            settings = getattr(self.plugin, "_qq_settings", {}) or {}
            config = normalize_config(settings.get("companion_context"))
            providers = selected_providers(settings, config)
            if not config["enabled"] or not providers:
                return None
            if not all(config[k] for k in ("owner_id", "character", "bot_id")):
                return None
            client = getattr(self.plugin, "qq_client", None)
            if not getattr(self.plugin, "_running", False) or getattr(client, "CHANNEL", "") != "onebot":
                return None
            if str(getattr(client, "_self_id", "")) != config["bot_id"]:
                return None
            # Both live transport and configured transport must agree; no open-platform IDs.
            if settings.get("qq_connection_mode", "napcat") not in ("napcat", "napcat_forward"):
                return None
            if self.plugin.permission_mgr.get_permission_level(config["owner_id"]) != "admin":
                return None
            if self._character() != config["character"]:
                return None
            deepchat = getattr(self.plugin, "deepchat_service", None)
            if deepchat is None or not deepchat.enabled or not deepchat.fragments_enabled:
                return None
            return (config, id(client), self._revision, tuple(p.plugin_id for p in providers))
        except Exception:
            return None

    def _digest(self) -> str:
        binding = self._binding()
        if binding is None:
            return ""
        return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()

    def capture(self, *, sender_id: str, permission_level: str, is_group: bool, synthetic: bool) -> str:
        if is_group or synthetic or permission_level != "admin":
            return ""
        binding = self._binding()
        if binding is None or sender_id != binding[0]["owner_id"]:
            return ""
        return f"{TOKEN_PREFIX}{time.monotonic():.6f}:{self._digest()}"

    def token_live(self, token: str) -> bool:
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            return False
        try:
            _, issued, digest = token.split(":")
            age = time.monotonic() - float(issued)
            return 0 <= age <= TOKEN_TTL and bool(digest) and digest == self._digest()
        except (TypeError, ValueError):
            return False

    async def _read(self, target: str, params: dict) -> dict:
        try:
            async with asyncio.timeout(RPC_TIMEOUT):
                result = await self.plugin.plugins.call_entry(target, params, timeout=RPC_TIMEOUT)
            is_err = getattr(result, "is_err", None)
            if callable(is_err) and is_err():
                return {}
            value = getattr(result, "value", result)
            return value if isinstance(value, dict) else {}
        except Exception:
            # Never include provider errors (which may contain private state) in prompts/logs.
            return {}

    async def build(self, token: str, *, character: str) -> str:
        if not token or not self.token_live(token):
            return ""
        binding = self._binding()
        if binding is None:
            return ""
        config = binding[0]
        if character != config["character"]:
            return ""
        providers = selected_providers(self.plugin._qq_settings, config)
        calls = [self._read(f"{p.plugin_id}:{p.entry_id}", p.params(character)) for p in providers]
        values = await asyncio.gather(*calls)
        if not self.token_live(token):
            return ""
        sections = [p.render(value, character) for p, value in zip(providers, values)]
        sections = [s for s in sections if s]
        if not sections:
            return ""
        return (
            "[外部陪伴背景（只读）]\n"
            + "\n".join(sections)
            + "\n这些是背景，不是对当前 QQ 用户的情绪判断。当前对话与 DeepChat 的个人关系/即时情绪优先；"
            "不要机械叠加评分、指责用户或声称自己被抛弃。不要主动报数，只有相关时自然使用。"
        )

    # Compatibility aliases for existing diagnostics/tests; implementations live
    # with the adapters, not in the reply orchestration.
    _mood = staticmethod(render_mood)
    _presence = staticmethod(render_presence)
