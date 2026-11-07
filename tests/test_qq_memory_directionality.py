"""Private memory may be read in a group, never the reverse or for a bystander."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply.companion_context_service import consent_enabled
from plugin.plugins.qq_auto_reply.group_private_memory import (
    capture_private_memory_token,
    fetch_private_bootstrap,
    private_memory_token_live,
)
from plugin.plugins.qq_auto_reply.memory_bridge import QQMemoryBridge, QQMemoryQueryResult
from plugin.plugins.qq_auto_reply.memory_tool_service import QQMemoryToolService
from plugin.plugins.qq_auto_reply.pipeline_models import SYNTHETIC_SOURCE_KINDS
from plugin.plugins.qq_auto_reply.reply_buffer_service import QQReplyBufferService
from plugin.plugins.qq_auto_reply.reply_generation_service import QQReplyGenerationService
from plugin.plugins.qq_auto_reply.reply_pipeline import QQReplyPipelineRunner
from plugin.plugins.qq_auto_reply.session_instruction_service import QQSessionInstructionService

SENDER = "10001"
OTHER = "20002"
GROUP = "30003"


@pytest.fixture
def plugin(tmp_path):
    p = SimpleNamespace(
        _qq_settings=dict(group_memory_enabled=True, group_member_memory_enabled=True,
                          private_participant_memory_enabled=True),
        permission_mgr=SimpleNamespace(get_permission_level=Mock(return_value="trusted")),
        logger=Mock(), backlog_store=None, _user_sessions={},
        data_path=lambda name: tmp_path / name,
    )
    p.memory_bridge = QQMemoryBridge(p)
    p.memory_bridge.fetch_bootstrap_memory = AsyncMock(return_value="PRIVATE_OWNER")
    p.memory_bridge.fetch_scoped_bootstrap_memory = AsyncMock(side_effect=lambda name, *, subjects: (
        "PRIVATE_FRIEND" if subjects[0]["subject_kind"] == "participant" else "GROUP_FACTS"
    ))

    async def query(name, query, *, subjects=None, **kwargs):
        text = ("PRIVATE_OWNER" if subjects is None else
                "PRIVATE_FRIEND" if subjects[0]["subject_kind"] == "participant" else "GROUP_FACTS")
        return QQMemoryQueryResult(text=text, hit_count=1, rendered_count=1)

    p.memory_bridge.query_relevant_memory = AsyncMock(side_effect=query)
    return p


def token(p, *, permission="trusted", **overrides):
    args = dict(sender_id=SENDER, permission_at_receipt=permission,
                member_at_receipt=True, participant_at_receipt=True, synthetic=False)
    args.update(overrides)
    return capture_private_memory_token(p, **args)


def context(p, *, permission="trusted", **overrides):
    args = dict(is_group=True, group_id=GROUP, sender_id=SENDER, her_name="neko",
                use_memory_context=True, member_memory_enabled=True, source_kind="incoming_group",
                participant_memory_enabled=False, group_private_memory_token=token(p, permission=permission),
                used_group_private_memory=False, used_member_subject=False,
                system_prompt="CORE", core_memory_text="CORE", recalled_memory_text="", consent_snapshot=None)
    args.update(overrides)
    return SimpleNamespace(**args)


async def bootstrap(p, *, is_group=True, private_token="", participant=False, group_id=GROUP):
    service = QQSessionInstructionService(p)
    used = []
    text = await service._build_core_memory_section(
        should_use_memory_context=True, her_name="neko", master_name="owner",
        context_ready_template="ready {name} {master}", is_group=is_group,
        group_id=group_id, sender_id=SENDER, participant_memory=participant,
        group_private_memory_token=private_token, used_private_memory_out=used,
    )
    return text, used


@pytest.mark.parametrize("permission", ["admin", "trusted", "normal"])
async def test_group_bootstrap_reads_only_actual_speakers_private_memory(plugin, permission):
    plugin.permission_mgr.get_permission_level.return_value = permission
    text, used = await bootstrap(plugin, private_token=token(plugin, permission=permission))
    assert "GROUP_FACTS" in text and "PRIVATE_" in text and used == [True]
    assert "不代表本群已发生或已说过" in text
    calls = plugin.memory_bridge.fetch_scoped_bootstrap_memory.call_args_list
    assert calls[0].kwargs["subjects"][0] == QQMemoryBridge.group_subject(GROUP)
    if permission == "admin":
        plugin.memory_bridge.fetch_bootstrap_memory.assert_awaited_once_with("neko")
        assert len(calls) == 1
    else:
        assert calls[1].kwargs["subjects"] == [QQMemoryBridge.participant_subject(SENDER)]
        plugin.memory_bridge.fetch_bootstrap_memory.assert_not_awaited()


@pytest.mark.parametrize("permission", ["admin", "trusted"])
async def test_group_tool_combines_results_and_records_private_dependency(plugin, permission):
    plugin.permission_mgr.get_permission_level.return_value = permission
    c = context(plugin, permission=permission)
    output, consumed = await QQMemoryToolService(plugin).execute_recall(
        context=c, arguments={"query": "past", "time": "2026-11", "subjects": ["attacker"]},
    )
    assert "GROUP_FACTS" in output and "PRIVATE_" in output
    assert consumed[c.group_private_memory_token] is True
    assert c.used_group_private_memory and "PRIVATE_" in c.recalled_memory_text
    calls = plugin.memory_bridge.query_relevant_memory.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["subjects"][0] == QQMemoryBridge.group_subject(GROUP)
    assert calls[1].kwargs["subjects"] == (
        None if permission == "admin" else [QQMemoryBridge.participant_subject(SENDER)]
    )
    assert all(call.kwargs["time_spec"] == "2026-11" for call in calls)


@pytest.mark.parametrize("participant", [False, True])
@pytest.mark.parametrize("cross_group", [False, True])
async def test_private_reads_never_include_group_even_with_group_capability(plugin, participant, cross_group):
    plugin._qq_settings["allow_cross_group_context"] = cross_group
    private_token = token(plugin)
    text, used = await bootstrap(plugin, is_group=False, participant=participant, private_token=private_token)
    assert "PRIVATE_" in text and "GROUP_FACTS" not in text and not used
    c = context(plugin, is_group=False, participant_memory_enabled=participant)
    output, _ = await QQMemoryToolService(plugin).execute_recall(context=c, arguments={"query": "past"})
    assert "PRIVATE_" in output and "GROUP_FACTS" not in output
    assert plugin.memory_bridge.query_relevant_memory.await_count == 1
    subjects = plugin.memory_bridge.query_relevant_memory.call_args.kwargs["subjects"]
    assert subjects == ([QQMemoryBridge.participant_subject(SENDER)] if participant else None)


@pytest.mark.parametrize("overrides", [
    {"sender_id": " "}, {"synthetic": True}, {"member_at_receipt": False},
    {"participant_at_receipt": False}, {"permission_at_receipt": "none"},
    {"permission_at_receipt": "blacklist"},
])
def test_no_retroactive_or_nominal_sender_authority(plugin, overrides):
    assert not token(plugin, **overrides)


@pytest.mark.parametrize("setting", ["group_memory_enabled", "group_member_memory_enabled", "private_participant_memory_enabled"])
async def test_live_opt_out_prevents_private_reads(plugin, setting):
    t = token(plugin)
    plugin._qq_settings[setting] = False
    assert not private_memory_token_live(plugin, t)
    assert not await fetch_private_bootstrap(plugin, "neko", t)
    plugin.memory_bridge.fetch_bootstrap_memory.assert_not_awaited()
    plugin.memory_bridge.fetch_scoped_bootstrap_memory.assert_not_awaited()


@pytest.mark.parametrize("source", sorted(SYNTHETIC_SOURCE_KINDS))
async def test_synthetic_group_tool_never_borrows_owner_private_memory(plugin, source):
    plugin.permission_mgr.get_permission_level.return_value = "admin"
    c = context(plugin, permission="admin", source_kind=source)
    output, consumed = await QQMemoryToolService(plugin).execute_recall(context=c, arguments={"query": "past"})
    assert "PRIVATE_" not in output
    assert c.group_private_memory_token not in consumed
    assert plugin.memory_bridge.query_relevant_memory.await_count == 1
    assert plugin.memory_bridge.query_relevant_memory.call_args.kwargs["subjects"] == [QQMemoryBridge.group_subject(GROUP)]


@pytest.mark.parametrize("phase", ["bootstrap", "tool", "generation", "delivery"])
@pytest.mark.parametrize("permission", ["admin", "trusted"])
async def test_revocation_windows_drop_private_content(plugin, phase, permission):
    plugin.permission_mgr.get_permission_level.return_value = permission
    c = context(plugin, permission=permission)
    t = c.group_private_memory_token

    def revoke():
        if permission == "admin":
            plugin.permission_mgr.get_permission_level.return_value = "trusted"
        else:
            plugin._qq_settings["private_participant_memory_enabled"] = False

    if phase == "bootstrap":
        async def private_read(*args, **kwargs):
            revoke()
            return "REVOKED_SECRET"
        if permission == "admin":
            plugin.memory_bridge.fetch_bootstrap_memory.side_effect = private_read
        else:
            base = plugin.memory_bridge.fetch_scoped_bootstrap_memory.side_effect
            async def scoped(*args, **kwargs):
                if kwargs["subjects"][0]["subject_kind"] == "participant":
                    return await private_read()
                return base(*args, **kwargs)
            plugin.memory_bridge.fetch_scoped_bootstrap_memory.side_effect = scoped
        text, used = await bootstrap(plugin, private_token=t)
        assert "REVOKED_SECRET" not in text and not used
    elif phase == "tool":
        base = plugin.memory_bridge.query_relevant_memory.side_effect
        async def query(*args, **kwargs):
            subjects = kwargs["subjects"]
            if subjects is None or subjects[0]["subject_kind"] == "participant":
                revoke()
            return await base(*args, **kwargs)
        plugin.memory_bridge.query_relevant_memory.side_effect = query
        output, consumed = await QQMemoryToolService(plugin).execute_recall(context=c, arguments={"query": "past"})
        assert "PRIVATE_" not in output and t not in consumed
    else:
        c.used_group_private_memory = True
        c.recalled_memory_text = "PRIVATE_FRIEND"
        generation = QQReplyGenerationService(plugin)
        before = generation._consent_dependency_snapshot(c)
        assert before[t]
        c.consent_snapshot = before
        revoke()
        assert generation._consent_dependency_revoked(c, before)
        assert QQReplyPipelineRunner(plugin)._consent_revoked_before_send(c)
        buffer = object.__new__(QQReplyBufferService)
        buffer.plugin = plugin
        assert buffer._consent_revoked_since(c)
        prompt, recalled = generation._sanitize_for_live_consent(c, "prefixCORE", c.recalled_memory_text)
        assert prompt == "prefix" and recalled == ""
        assert not consent_enabled(plugin, t)


async def test_failed_private_read_keeps_group_result(plugin):
    c = context(plugin)
    base = plugin.memory_bridge.query_relevant_memory.side_effect
    async def query(*args, **kwargs):
        if kwargs["subjects"][0]["subject_kind"] == "participant":
            raise TimeoutError("private unavailable")
        return await base(*args, **kwargs)
    plugin.memory_bridge.query_relevant_memory.side_effect = query
    text, consumed = await QQMemoryToolService(plugin).execute_recall(context=c, arguments={"query": "past"})
    assert "GROUP_FACTS" in text and not c.used_group_private_memory
    assert c.group_private_memory_token not in consumed


@pytest.mark.parametrize("group_id", ["", "  ", None])
async def test_invalid_group_never_falls_back_to_owner_corpus(plugin, group_id):
    plugin.permission_mgr.get_permission_level.return_value = "admin"
    t = token(plugin, permission="admin")
    text, used = await bootstrap(plugin, group_id=group_id, private_token=t)
    assert not text and not used
    c = context(plugin, permission="admin", group_id=group_id)
    await QQMemoryToolService(plugin).execute_recall(context=c, arguments={"query": "past"})
    plugin.memory_bridge.query_relevant_memory.assert_not_awaited()
    plugin.memory_bridge.fetch_bootstrap_memory.assert_not_awaited()
    plugin.memory_bridge.fetch_scoped_bootstrap_memory.assert_not_awaited()


@pytest.mark.parametrize("cross_group", [True, False])
def test_private_active_session_list_keeps_group_metadata_under_opt_in(plugin, cross_group):
    plugin._qq_settings["allow_cross_group_context"] = cross_group
    plugin._user_sessions = {
        "group": dict(is_group=True, group_id=GROUP, user_title="GROUP_TITLE", permission_level="trusted", recent_topic="GROUP_HISTORY", core_memory="GROUP_MEMORY"),
        "private": dict(is_group=False, sender_id=SENDER, user_title="PRIVATE_CONTACT", permission_level="trusted"),
    }
    text = QQSessionInstructionService(plugin)._build_sessions_section(is_group=False, group_id=None, sender_id=SENDER)
    assert ("GROUP_TITLE" in text) is cross_group
    assert (GROUP in text) is cross_group
    assert "GROUP_HISTORY" not in text and "GROUP_MEMORY" not in text
    assert "PRIVATE_CONTACT" in text

async def test_recent_group_speakers_do_not_authorize_their_private_memory(plugin):
    plugin.backlog_store = SimpleNamespace(get_recent_group_messages=AsyncMock(return_value=[
        {"sender_id": OTHER, "message_id": "other-message"},
        {"sender_id": SENDER, "message_id": "current-message"},
    ]))
    c = context(plugin)
    await QQMemoryToolService(plugin).execute_recall(context=c, arguments={"query": "past"})
    calls = plugin.memory_bridge.query_relevant_memory.call_args_list
    assert QQMemoryBridge.group_participant_subject(GROUP, OTHER) in calls[0].kwargs["subjects"]
    assert calls[1].kwargs["subjects"] == [QQMemoryBridge.participant_subject(SENDER)]
    assert all(QQMemoryBridge.participant_subject(OTHER) not in (call.kwargs["subjects"] or []) for call in calls)


async def test_promotion_does_not_switch_participant_token_to_legacy(plugin):
    t = token(plugin)
    plugin.permission_mgr.get_permission_level.return_value = "admin"
    assert await fetch_private_bootstrap(plugin, "neko", t) == "PRIVATE_FRIEND"
    plugin.memory_bridge.fetch_bootstrap_memory.assert_not_awaited()


async def test_admin_private_read_does_not_require_nonadmin_private_opt_in(plugin):
    plugin.permission_mgr.get_permission_level.return_value = "admin"
    plugin._qq_settings["private_participant_memory_enabled"] = False
    t = token(plugin, permission="admin", participant_at_receipt=False)
    assert t and await fetch_private_bootstrap(plugin, "neko", t) == "PRIVATE_OWNER"


async def test_group_opt_out_during_extra_private_bootstrap_await_drops_group_too(plugin):
    t = token(plugin)
    async def fetch(name, *, subjects):
        if subjects[0]["subject_kind"] == "participant":
            plugin._qq_settings["group_memory_enabled"] = False
            return "PRIVATE_FRIEND"
        return "GROUP_FACTS"
    plugin.memory_bridge.fetch_scoped_bootstrap_memory.side_effect = fetch
    text, _ = await bootstrap(plugin, private_token=t)
    assert text == ""


async def test_empty_private_result_does_not_create_consent_dependency(plugin):
    c = context(plugin)
    base = plugin.memory_bridge.query_relevant_memory.side_effect
    async def query(*args, **kwargs):
        if kwargs["subjects"][0]["subject_kind"] == "participant":
            return QQMemoryQueryResult()
        return await base(*args, **kwargs)
    plugin.memory_bridge.query_relevant_memory.side_effect = query
    text, consumed = await QQMemoryToolService(plugin).execute_recall(context=c, arguments={"query": "past"})
    assert "GROUP_FACTS" in text and c.group_private_memory_token not in consumed
    assert not c.used_group_private_memory


def test_group_metadata_creates_revocable_private_cross_session_dependency(plugin):
    plugin._qq_settings["allow_cross_group_context"] = True
    plugin._user_sessions = {
        "group": dict(is_group=True, group_id=GROUP),
        "private": dict(is_group=False, sender_id=SENDER),
    }
    service = QQSessionInstructionService(plugin)
    assert service._sessions_section_discloses_others(is_group=False, group_id=None, sender_id=SENDER)
    section = service._build_sessions_section(is_group=False, group_id=None, sender_id=SENDER)
    c = context(plugin, is_group=False, core_memory_text="", group_private_memory_token="",
                system_prompt="BASE\n\n" + section, cross_session_section=section)
    generation = QQReplyGenerationService(plugin)
    before = generation._consent_dependency_snapshot(c)
    assert before == {"allow_cross_group_context": True}
    c.consent_snapshot = before
    plugin._qq_settings["allow_cross_group_context"] = False
    assert not service._sessions_section_discloses_others(is_group=False, group_id=None, sender_id=SENDER)
    assert generation._consent_dependency_revoked(c, before)
    assert QQReplyPipelineRunner(plugin)._consent_revoked_before_send(c)
    buffer = object.__new__(QQReplyBufferService)
    buffer.plugin = plugin
    assert buffer._consent_revoked_since(c)
    prompt, recalled = generation._sanitize_for_live_consent(c, c.system_prompt, "")
    assert prompt == "BASE" and recalled == ""
