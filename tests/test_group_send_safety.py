from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply import group_send_safety as module
from plugin.plugins.qq_auto_reply.pipeline_models import QQDeliveryPlan, QQMessageBlock
from plugin.plugins.qq_auto_reply.reply_delivery_node import QQReplyDeliveryNode


def setup(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    client = SimpleNamespace(
        is_group_muted=lambda gid: False,
        get_login_info=AsyncMock(return_value={"user_id": 123}),
        get_group_member_info=AsyncMock(return_value={"shut_up_timestamp": 200}),
    )
    plugin = SimpleNamespace(qq_client=client, logger=Mock(), _get_reply_mode=lambda: "text")
    return clock, plugin, module.group_send_safety(plugin)


@pytest.mark.asyncio
async def test_missing_notice_is_recovered_and_old_drafts_never_resume(monkeypatch):
    clock, plugin, safety = setup(monkeypatch)
    assert await safety.blocked("1", created_at=90, force=True)
    clock[0] = 210
    plugin.qq_client.get_group_member_info.return_value = {"shut_up_timestamp": 0}
    assert await safety.blocked("1", created_at=150, force=True)
    clock[0] = 211
    assert not await safety.blocked("1", created_at=211, force=True)
    assert await safety.blocked("1", created_at=90, force=True)


@pytest.mark.asyncio
async def test_refresh_failure_does_not_clear_known_mute(monkeypatch):
    clock, plugin, safety = setup(monkeypatch)
    assert await safety.blocked("1", force=True)
    clock[0] = 110
    plugin.qq_client.get_group_member_info.side_effect = RuntimeError("offline")
    assert await safety.blocked("1", force=True)


def test_burst_is_dropped_not_queued_and_groups_are_independent(monkeypatch):
    clock, _, safety = setup(monkeypatch)
    assert all(safety.admit("1") for _ in range(3))
    assert not safety.admit("1")
    assert safety.admit("2")
    clock[0] += 61
    assert safety.admit("1")


@pytest.mark.asyncio
async def test_muted_delivery_does_not_send(monkeypatch):
    _, plugin, _ = setup(monkeypatch)
    node = QQReplyDeliveryNode(plugin)
    node._send_text = AsyncMock(return_value=True)
    result = await node.deliver(QQDeliveryPlan("group", "1", [QQMessageBlock(text="old")]))
    assert not result.delivered
    node._send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_mute_during_voice_synthesis_discards_audio(monkeypatch):
    _, plugin, _ = setup(monkeypatch)
    plugin._get_reply_mode = lambda: "voice"
    plugin.voice_reply_service = SimpleNamespace(synthesize_reply_voice_file=AsyncMock(return_value=("file", "mime")))
    plugin.qq_client.send_group_record = AsyncMock()
    node = QQReplyDeliveryNode(plugin)
    node._outbound_allowed = lambda *a: True
    plan = QQDeliveryPlan("group", "1", [QQMessageBlock(record="old")])
    assert not await node._send_record(plan, plan.blocks[0])
    plugin.qq_client.send_group_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_mute_between_blocks_stops_remaining_text(monkeypatch):
    from plugin.plugins.qq_auto_reply import reply_delivery_node

    _, plugin, _ = setup(monkeypatch)
    plugin.qq_client.get_group_member_info.return_value = {"shut_up_timestamp": 0}

    async def pause(_):
        plugin.qq_client.get_group_member_info.return_value = {"shut_up_timestamp": 200}

    monkeypatch.setattr(reply_delivery_node.asyncio, "sleep", pause)
    node = QQReplyDeliveryNode(plugin)
    node._send_text = AsyncMock(return_value=True)
    result = await node.deliver(QQDeliveryPlan(
        "group", "1", [QQMessageBlock(text="first"), QQMessageBlock(text="second")],
    ))
    assert not result.delivered
    assert node._send_text.await_count == 1


@pytest.mark.asyncio
async def test_muted_buffer_is_detached_and_settled_without_delivery(monkeypatch):
    from plugin.plugins.qq_auto_reply.reply_buffer_service import PendingReply, QQReplyBufferService

    _, plugin, _ = setup(monkeypatch)
    plugin._user_sessions = {"group:1": {}}
    plugin._emit_log = Mock()
    plugin.reply_delivery_node = SimpleNamespace(deliver=AsyncMock())
    service = QQReplyBufferService(plugin)
    pending = PendingReply("old", 0, "123", True, "1")
    service._pending["group:1"] = pending
    service._settle_provisional = Mock()
    await service._deliver_after_wait("group:1", pending)
    assert "group:1" not in service._pending
    service._settle_provisional.assert_called_once()
    plugin.reply_delivery_node.deliver.assert_not_awaited()
