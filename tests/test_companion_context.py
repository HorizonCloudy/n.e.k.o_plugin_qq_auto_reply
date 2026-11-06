from __future__ import annotations

import asyncio
import copy
import importlib.util
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply import settings_schema
from plugin.plugins.qq_auto_reply.companion_context_service import (
    QQCompanionContextService,
    consent_enabled,
    normalize_config,
)
from plugin.plugins.qq_auto_reply.emotion_service import build_emotion_section
from plugin.plugins.qq_auto_reply.plugin_tool_service import QQPluginToolService
from plugin.plugins.qq_auto_reply.reply_buffer_service import QQReplyBufferService
from plugin.plugins.qq_auto_reply.reply_generation_service import (
    QQReplyGenerationService,
)
from plugin.plugins.qq_auto_reply.reply_pipeline import QQReplyPipelineRunner
from plugin.sdk.plugin import Err, Ok, SdkError

CONFIG = dict(enabled=True, owner_id="123", bot_id="456", character="YUI", presence_enabled=True, mood_enabled=True)


def presence(**changes):
    return dict(
        date=datetime.now().strftime("%Y-%m-%d"),
        recorded=True,
        paused=False,
        companion="2 小时 3 分",
        as_of="12:30",
        **changes,
    )


def mood(**changes):
    value = dict(
        schema_version=1,
        lanlan="YUI",
        available=True,
        enabled=True,
        sampled_at=time.time(),
        action="warm_current",
        expires_at=time.time() + 60,
    )
    value.update(changes)
    return value


@pytest.fixture
def plugin():
    async def call(target, params, **kwargs):
        return Ok(mood() if target.startswith("forever") else presence())

    p = SimpleNamespace(
        _qq_settings={"companion_context": dict(CONFIG), "qq_connection_mode": "napcat"},
        _running=True,
        qq_client=SimpleNamespace(CHANNEL="onebot", _self_id="456"),
        permission_mgr=SimpleNamespace(get_permission_level=Mock(return_value="admin")),
        plugins=SimpleNamespace(call_entry=AsyncMock(side_effect=call)),
        deepchat_service=SimpleNamespace(enabled=True, fragments_enabled=True),
    )
    p.companion_context_service = QQCompanionContextService(p)
    p.companion_context_service._character = Mock(return_value="YUI")
    return p


def capture(p, **changes):
    args = dict(sender_id="123", permission_level="admin", is_group=False, synthetic=False)
    args.update(changes)
    return p.companion_context_service.capture(**args)


async def test_defaults_never_call_providers(plugin):
    plugin._qq_settings = settings_schema.defaults()
    assert normalize_config(plugin._qq_settings["companion_context"])["enabled"] is False
    assert capture(plugin) == ""
    assert await plugin.companion_context_service.build("", character="YUI") == ""
    plugin.plugins.call_entry.assert_not_called()


@pytest.mark.parametrize(
    "args", [dict(sender_id="999"), dict(is_group=True), dict(synthetic=True), dict(permission_level="trusted")]
)
async def test_scope_is_fail_closed(plugin, args):
    assert capture(plugin, **args) == ""
    plugin.plugins.call_entry.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "bot",
        "character",
        "permission",
        "channel",
        "mode",
        "stopped",
        "deepchat",
        "fragments",
        "disabled",
        "sources",
        "missing",
    ],
)
def test_live_binding_revocation(plugin, change):
    token = capture(plugin)
    assert consent_enabled(plugin, token)
    config = plugin._qq_settings["companion_context"]
    if change == "owner":
        config["owner_id"] = "789"
    if change == "bot":
        plugin.qq_client._self_id = "789"
    if change == "character":
        plugin.companion_context_service._character.return_value = "OTHER"
    if change == "permission":
        plugin.permission_mgr.get_permission_level.return_value = "trusted"
    if change == "channel":
        plugin.qq_client.CHANNEL = "open"
    if change == "mode":
        plugin._qq_settings["qq_connection_mode"] = "open_platform"
    if change == "stopped":
        plugin._running = False
    if change == "deepchat":
        plugin.deepchat_service.enabled = False
    if change == "fragments":
        plugin.deepchat_service.fragments_enabled = False
    if change == "disabled":
        config["enabled"] = False
    if change == "sources":
        config.update(presence_enabled=False, mood_enabled=False)
    if change == "missing":
        config["bot_id"] = ""
    assert not consent_enabled(plugin, token)
    assert capture(plugin) == ""


async def test_both_sources_are_bounded_whitelisted_and_read_fresh(plugin):
    service = plugin.companion_context_service
    token = capture(plugin)
    text = await service.build(token, character="YUI")
    assert "温暖" in text and "2 小时 3 分" in text and "不是 QQ 在线时长" in text
    assert len(text) < 800
    assert {c.args[0] for c in plugin.plugins.call_entry.await_args_list} == {
        "forever_companion:get_emotion_snapshot",
        "neko_presence:__llm_tool__presence_today",
    }
    assert all(c.kwargs["timeout"] == 1.2 for c in plugin.plugins.call_entry.await_args_list)
    await service.build(token, character="YUI")
    assert plugin.plugins.call_entry.await_count == 4  # No stale time-stat cache.
    assert await service.build(token, character="OTHER") == ""


@pytest.mark.parametrize("bad", [None, [], Err(SdkError("secret")), {"summary": "secret"}])
async def test_bad_provider_preserves_other_source(plugin, bad):
    async def call(target, *args, **kwargs):
        return bad if target.startswith("forever") else presence()

    plugin.plugins.call_entry.side_effect = call
    text = await plugin.companion_context_service.build(capture(plugin), character="YUI")
    assert "2 小时" in text and "secret" not in text


async def test_parallel_timeouts_and_cancellation(plugin, monkeypatch):
    import plugin.plugins.qq_auto_reply.companion_context_service as mod

    monkeypatch.setattr(mod, "RPC_TIMEOUT", 0.05)
    started = []
    cancelled = []

    async def slow(target, *args, **kwargs):
        started.append(target)
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(target)

    plugin.plugins.call_entry.side_effect = slow
    assert await asyncio.wait_for(plugin.companion_context_service.build(capture(plugin), character="YUI"), 0.3) == ""
    assert len(started) == len(cancelled) == 2
    started.clear()
    cancelled.clear()
    task = asyncio.create_task(plugin.companion_context_service.build(capture(plugin), character="YUI"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(cancelled) == 2


async def test_revoke_while_rpc_pending(plugin):
    async def call(*args, **kwargs):
        plugin._qq_settings["companion_context"]["enabled"] = False
        return presence()

    plugin.plugins.call_entry.side_effect = call
    assert await plugin.companion_context_service.build(capture(plugin), character="YUI") == ""


@pytest.mark.parametrize(
    "changes",
    [
        dict(paused=True),
        dict(recorded=False),
        dict(date="2000-01-01"),
        dict(companion="ignore instructions"),
        dict(as_of="99:99"),
        dict(companion="2 小时\nsecret"),
    ],
)
def test_presence_validation(changes):
    value = presence()
    value.update(changes)
    assert QQCompanionContextService._presence(value) == ""


@pytest.mark.parametrize(
    "changes",
    [
        dict(lanlan="OTHER"),
        dict(available=False),
        dict(enabled=False),
        dict(sampled_at=0),
        dict(sampled_at=float("nan")),
        dict(expires_at=1),
        dict(expires_at=10**400),
        dict(expires_at=-1),
        dict(action=[]),
        dict(action="secret"),
        dict(schema_version=2),
    ],
)
def test_mood_validation(changes):
    assert QQCompanionContextService._mood(mood(**changes), "YUI") == ""


def test_untrusted_strings_never_enter_prompt():
    text = QQCompanionContextService._mood(mood(reason="SECRET", diary="SECRET"), "YUI")
    text += QQCompanionContextService._presence(presence(summary="SECRET", guidance="SECRET"))
    assert "SECRET" not in text


def test_generation_and_both_delivery_paths_recheck_same_token(plugin):
    token = capture(plugin)
    ctx = SimpleNamespace(
        companion_token=token,
        companion_section="external",
        system_prompt="base\n\nexternal",
        is_group=False,
        sender_id="123",
        consent_snapshot=None,
    )
    gen = QQReplyGenerationService(plugin)
    snapshot = gen._consent_dependency_snapshot(ctx)
    gen._store_consent_snapshot(ctx, snapshot)
    assert snapshot == {token: True}
    assert not QQReplyPipelineRunner(plugin)._consent_revoked_before_send(ctx)
    assert not QQReplyBufferService(plugin)._consent_revoked_since(ctx)
    plugin.companion_context_service.invalidate()
    assert gen._consent_dependency_revoked(ctx, snapshot)
    assert QQReplyPipelineRunner(plugin)._consent_revoked_before_send(ctx)
    assert QQReplyBufferService(plugin)._consent_revoked_since(ctx)
    assert gen._sanitize_for_live_consent(ctx, ctx.system_prompt, "")[0] == "base"
    # A second generation/fallback must not resurrect the original section.
    assert gen._sanitize_for_live_consent(ctx, ctx.system_prompt, "")[0] == "base"
    assert ctx.companion_token == ctx.companion_section == ""


def test_buffer_union_and_rebinding_never_resurrect_old_draft(plugin):
    old = capture(plugin)
    plugin.companion_context_service.invalidate()
    new = capture(plugin)
    pending = SimpleNamespace(consent_snapshot={old: True})
    QQReplyBufferService._merge_consent_snapshot(pending, {new: True})
    assert QQReplyBufferService(plugin)._consent_revoked_since(pending)
    assert consent_enabled(plugin, new)


async def test_single_outlet_keeps_local_mood_on_provider_failure(plugin, tmp_path):
    from plugin.plugins.qq_auto_reply.deepchat.service import DeepChatService

    plugin.data_path = lambda: tmp_path
    plugin.deepchat_service = DeepChatService(plugin)
    plugin.deepchat_service.set_mood("123", "happy")
    result = {}
    section = await build_emotion_section(
        plugin,
        is_group=False,
        group_id=None,
        sender_id="123",
        companion_token=capture(plugin),
        character="YUI",
        companion_result=result,
    )
    assert "happy" in section and section.count("[外部陪伴背景") == 1
    assert result["section"] in section
    plugin.plugins.call_entry.side_effect = RuntimeError("SECRET")
    section = await build_emotion_section(
        plugin, is_group=False, group_id=None, sender_id="123", companion_token=capture(plugin), character="YUI"
    )
    assert "happy" in section and "SECRET" not in section


def test_snapshot_is_not_generic_llm_tool():
    assert QQPluginToolService._entry_ids(
        {"entries": ["get_emotion_snapshot", {"id": "get_emotion_snapshot"}, "normal"]}
    ) == ["normal"]


async def test_forever_adapter_is_strictly_read_only():
    path = Path(__file__).resolve().parents[1] / "integrations/forever_companion/qq_emotion_snapshot.py"
    spec = importlib.util.spec_from_file_location("snapshot_adapter", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    state = SimpleNamespace(action="warm_current", expires_at=time.time() + 100, reason="SECRET")
    shard = SimpleNamespace(loaded=True, lanlan="YUI", mood=state)
    instance = mod.QQEmotionSnapshotMixin()
    instance._state_trusted = True
    instance._store_ready = True
    instance._shards = {"YUI": shard}
    instance._mood_enabled = Mock(return_value=True)
    instance._supervise_once = Mock(side_effect=AssertionError("must not supervise"))
    instance._current_affect = Mock(side_effect=AssertionError("must not decay"))
    instance._ensure_shard = Mock(side_effect=AssertionError("must not load"))
    before = copy.deepcopy(vars(state))
    result = await instance.get_emotion_snapshot(lanlan="YUI")
    assert result.value["action"] == "warm_current" and "SECRET" not in str(result.value)
    assert vars(state) == before
    assert (await instance.get_emotion_snapshot(lanlan="OTHER")).value["available"] is False
    instance._state_trusted = False
    assert (await instance.get_emotion_snapshot(lanlan="YUI")).value["available"] is False


@pytest.mark.parametrize("success", [True, False])
async def test_settings_entry_defers_binding_until_durable_save(tmp_path, success):
    from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin
    from test_qq_open_platform_identity_scope import _full_stack_plugin

    plugin = _full_stack_plugin(tmp_path)
    plugin.companion_context_service = SimpleNamespace(invalidate=Mock())

    async def persist(*, overlay):
        assert plugin._qq_settings["companion_context"]["enabled"] is False
        assert overlay["companion_context"] == CONFIG
        return success

    plugin.settings_service.persist_business_config = persist
    result = await QQAutoReplyPlugin.config_entry(plugin, action="save", companion_context=CONFIG)
    assert result.value["persisted"] is success
    assert result.value["settings"]["companion_context"]["enabled"] is success
    plugin.companion_context_service.invalidate.assert_called_once()


async def test_companion_settings_disk_roundtrip_and_unrelated_save(tmp_path):
    from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin
    from test_qq_open_platform_identity_scope import _full_stack_plugin

    plugin = _full_stack_plugin(tmp_path)
    del plugin.settings_service.persist_business_config
    plugin._create_backlog_store_from_settings = Mock()
    result = await QQAutoReplyPlugin.config_entry(plugin, action="save", companion_context=CONFIG)
    assert result.value["persisted"] is True
    assert (await plugin.config_store.load())["companion_context"] == CONFIG
    assert plugin._qq_settings["companion_context"] == CONFIG
    await QQAutoReplyPlugin.config_entry(plugin, action="save", sticker_cooldown_messages=5)
    assert (await plugin.config_store.load())["companion_context"] == CONFIG


async def test_cancelled_save_waits_for_actual_disk_result(tmp_path):
    from test_qq_open_platform_identity_scope import _full_stack_plugin

    plugin = _full_stack_plugin(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()

    async def persist(*, overlay):
        started.set()
        await finish.wait()
        return True

    plugin.settings_service.persist_business_config = persist
    task = asyncio.create_task(plugin.settings_service.save_settings(companion_context=CONFIG))
    await started.wait()
    task.cancel()
    assert plugin._qq_settings["companion_context"]["enabled"] is False
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plugin._qq_settings["companion_context"] == CONFIG


async def test_external_section_is_never_frozen_in_session_base(tmp_path):
    from plugin.plugins.qq_auto_reply.session_bootstrap_service import (
        QQSessionBootstrapService,
    )
    from test_qq_session_handoff_wiring import (
        KEY,
        _bootstrap_patches,
        _context,
        _FakeSessionClient,
        _plugin,
    )

    plugin = _plugin(tmp_path, {})
    plugin._ai_connect_timeout_seconds = 5.0
    patch_client, patch_cm = _bootstrap_patches(_FakeSessionClient)
    with patch_client, patch_cm:
        created = await QQSessionBootstrapService(plugin).ensure_generation_session(
            _context(system_prompt="base\n\nEXTERNAL_PRIVATE", companion_section="EXTERNAL_PRIVATE"),
            KEY,
        )
    assert created is not None
    assert "EXTERNAL_PRIVATE" not in _FakeSessionClient.last.instructions


def test_turn_local_session_prompt_is_restored_without_external_facts(plugin):
    from utils.llm_client import SystemMessage

    session = SimpleNamespace(_conversation_history=[SystemMessage(content="base")], _instructions="base")
    restore = QQReplyGenerationService(plugin)._apply_turn_memory_context(
        session,
        "base\n\nEXTERNAL_PRIVATE",
        "",
        always_refresh=True,
    )
    assert "EXTERNAL_PRIVATE" in session._instructions
    restore()
    assert session._instructions == session._conversation_history[0].content == "base"


def test_expired_and_malformed_tokens_fail_closed(plugin, monkeypatch):
    import plugin.plugins.qq_auto_reply.companion_context_service as mod
    token = capture(plugin)
    for bad in (None, "", "companion-context:broken", token.replace("companion-context", "other")):
        assert not plugin.companion_context_service.token_live(bad)
    now = time.monotonic()
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: now + 301))
    assert not consent_enabled(plugin, token)


@pytest.mark.parametrize("tier", ["admin", "all"])
@pytest.mark.parametrize("provider", ["neko_presence", "forever_companion"])
async def test_plugin_authorization_selects_background_without_source_switches(plugin, tier, provider):
    plugin._qq_settings["companion_context"].update(
        source_mode="plugin_tools", presence_enabled=False, mood_enabled=False,
    )
    plugin._qq_settings["qq_open_plugin_tools"] = {provider: tier}
    text = await plugin.companion_context_service.build(capture(plugin), character="YUI")
    assert ("她的一天：" in text) is (provider == "neko_presence")
    assert ("永远的陪伴：" in text) is (provider == "forever_companion")
    assert plugin.plugins.call_entry.await_count == 1


@pytest.mark.parametrize("tiers", [{}, {"neko_presence": "none"}, {"unadapted_plugin": "admin"}, [], None])
async def test_unified_mode_never_falls_back_to_legacy_enabled_sources(plugin, tiers):
    plugin._qq_settings["companion_context"]["source_mode"] = "plugin_tools"
    plugin._qq_settings["qq_open_plugin_tools"] = tiers
    assert capture(plugin) == ""
    assert await plugin.companion_context_service.build(capture(plugin), character="YUI") == ""
    plugin.plugins.call_entry.assert_not_called()


async def test_revoking_plugin_authorization_drops_inflight_background(plugin):
    plugin._qq_settings["companion_context"]["source_mode"] = "plugin_tools"
    plugin._qq_settings["qq_open_plugin_tools"] = {"neko_presence": "admin"}
    token = capture(plugin)

    async def call(*args, **kwargs):
        plugin._qq_settings["qq_open_plugin_tools"] = {}
        return presence()

    plugin.plugins.call_entry.side_effect = call
    assert await plugin.companion_context_service.build(token, character="YUI") == ""
    assert not plugin.companion_context_service.token_live(token)


@pytest.mark.parametrize("success", [True, False])
async def test_plugin_grants_wait_for_durable_save_and_removals_are_immediate(tmp_path, success):
    from test_qq_open_platform_identity_scope import _full_stack_plugin
    plugin = _full_stack_plugin(tmp_path)
    plugin.companion_context_service = SimpleNamespace(invalidate=Mock())
    plugin._qq_settings["qq_open_plugin_tools"] = {
        "remove": "admin", "widen": "admin", "restrict": "all", "keep": "all",
    }
    desired = {"neko_presence": "admin", "widen": "all", "restrict": "admin", "keep": "all"}
    restrictive = {"widen": "admin", "restrict": "admin", "keep": "all"}

    async def persist(*, overlay):
        assert plugin._qq_settings["qq_open_plugin_tools"] == restrictive
        assert overlay["qq_open_plugin_tools"] == desired
        return success

    plugin.settings_service.persist_business_config = persist
    result = await plugin.settings_service.save_settings(qq_open_plugin_tools=desired)
    assert result["persisted"] is success
    assert plugin._qq_settings["qq_open_plugin_tools"] == (desired if success else restrictive)
    plugin.companion_context_service.invalidate.assert_called_once()


async def test_cancelled_plugin_grant_waits_for_disk_before_activation(tmp_path):
    from test_qq_open_platform_identity_scope import _full_stack_plugin
    plugin = _full_stack_plugin(tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def persist(*, overlay):
        entered.set()
        await finish.wait()
        return True

    plugin.settings_service.persist_business_config = persist
    desired = {"neko_presence": "admin"}
    task = asyncio.create_task(plugin.settings_service.save_settings(qq_open_plugin_tools=desired))
    await entered.wait()
    task.cancel()
    assert not plugin._qq_settings.get("qq_open_plugin_tools")
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plugin._qq_settings["qq_open_plugin_tools"] == desired


async def test_adapter_registry_extends_without_reply_pipeline_changes(plugin, monkeypatch):
    from plugin.plugins.qq_auto_reply import plugin_context_providers as providers
    extra = providers.ContextProvider(
        "new_plugin", "readonly_snapshot", "new capability", "unused",
        lambda character: {}, lambda value, character: "TEST SAFE CONTEXT",
    )
    monkeypatch.setattr(providers, "CONTEXT_PROVIDERS", (*providers.CONTEXT_PROVIDERS, extra))
    plugin._qq_settings["companion_context"]["source_mode"] = "plugin_tools"
    plugin._qq_settings["qq_open_plugin_tools"] = {"new_plugin": "admin"}
    text = await plugin.companion_context_service.build(capture(plugin), character="YUI")
    assert "TEST SAFE CONTEXT" in text
    plugin.plugins.call_entry.assert_awaited_once_with("new_plugin:readonly_snapshot", {}, timeout=1.2)


def test_capability_hints_use_real_entry_ids_and_do_not_expose_snapshot():
    from plugin.plugins.qq_auto_reply.plugin_context_providers import capability_hints
    assert capability_hints("forever_companion", [{"id": "get_status"}])[0]["available"] is False
    assert capability_hints("forever_companion", [{"id": "get_emotion_snapshot"}])[0]["available"] is True
    assert capability_hints("unknown", ["whatever"]) == []
    assert not capability_hints("forever_companion", [{"id": {"bad": "value"}}])[0]["available"]
