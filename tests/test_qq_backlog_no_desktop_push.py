"""Backlog remains queryable, but must never wake or inject into the desktop."""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin
from plugin.plugins.qq_auto_reply import backlog_service as backlog_service_module
from plugin.plugins.qq_auto_reply.backlog_service import QQBacklogService
from plugin.plugins.qq_auto_reply.backlog_store import QQBacklogStore
from plugin.plugins.qq_auto_reply.message_dispatcher import QQMessageDispatcher
from plugin.plugins.qq_auto_reply.settings_schema import BY_KEY


def make_plugin(tmp_path):
    plugin = SimpleNamespace(
        _admin_qq="900000001",
        # Persisted old configurations must not reactivate desktop notifications.
        _qq_settings={"backlog_labels": [], "backlog_notify_cooldown_seconds": 60,
                      "backlog_issue_notify_threshold": 1},
        _backlog_notify_cooldown_seconds=60,
        _backlog_issue_notify_threshold=1,
        _user_sessions={},
        enricher=None,
        qq_client=SimpleNamespace(needs_attention=True, self_id="900000002",
                                  is_group_muted=lambda gid: False),
        permission_mgr=SimpleNamespace(get_permission_level=lambda uid: "trusted",
                                       get_nickname=lambda uid: "tester"),
        group_permission_mgr=SimpleNamespace(get_group_level=lambda gid: "trusted",
                                             list_groups=lambda: []),
        _sanitize_message_text=lambda text, **kw: text,
        _build_session_key=lambda **kw: "group:900000003",
        _build_backlog_conversation_key=lambda **kw: "group:900000003:900000001",
        _emit_log=Mock(),
        logger=SimpleNamespace(info=Mock(), warning=Mock()),
        push_message=Mock(side_effect=AssertionError("No desktop push, including read/blind")),
        # Catch a leftover dispatcher invocation even if the service was removed.
        _maybe_notify_backlog_summary=AsyncMock(side_effect=AssertionError("Obsolete notify hook")),
        backlog_store=QQBacklogStore(tmp_path),
    )
    plugin.backlog_service = QQBacklogService(plugin)
    return plugin


@pytest.mark.asyncio
async def test_group_traffic_after_old_cooldown_never_repushes_backlog(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path)
    dispatcher = QQMessageDispatcher(plugin)
    dispatcher.handle_group_message = AsyncMock()
    # Same old mention stays unreviewed as later unrelated messages arrive.
    for index, offset in enumerate((0, 903, 1806)):
        now = 1_794_000_000 + offset
        # Scope the old notifier clock to its module, never the whole process.
        # raising=False keeps this regression guard after its time import is removed.
        monkeypatch.setattr(
            backlog_service_module, "time", SimpleNamespace(time=lambda: now), raising=False,
        )
        await dispatcher.handle_message({
            "message_type": "group", "group_id": "900000003", "user_id": "900000001",
            "content": "我是谁" if index == 0 else "普通群消息",
            "message_id": f"test-{index}", "timestamp": now,
            "is_at_bot": index == 0,
        })
    assert dispatcher.handle_group_message.await_count == 3
    plugin.push_message.assert_not_called()
    plugin._maybe_notify_backlog_summary.assert_not_awaited()
    summary = await plugin.backlog_service.get_summary_payload()
    assert summary["unread_count"] == 3
    assert summary["label_counts"]["mention"] == 1
    detail = await plugin.backlog_service.get_group_detail_payload("900000003")
    assert "我是谁" in str(detail)
    assert "普通群消息" in str(detail)
    # Viewing does not silently consume messages; explicit review still works.
    assert (await plugin.backlog_service.get_summary_payload())["unread_count"] == 3
    reviewed = await plugin.backlog_service.mark_group_reviewed_payload(
        "900000003", message_ids={"test-0"},
    )
    assert reviewed["status"] == "reviewed"
    assert (await plugin.backlog_service.get_summary_payload())["unread_count"] == 2
    await plugin.backlog_service.mark_group_reviewed_payload("900000003")
    assert (await plugin.backlog_service.get_summary_payload())["unread_count"] == 0
    plugin.push_message.assert_not_called()


def test_desktop_notification_path_is_removed_not_merely_muted():
    assert not hasattr(QQAutoReplyPlugin, "_maybe_notify_backlog_summary")
    assert not hasattr(QQBacklogService, "maybe_notify_summary")
    assert not hasattr(QQBacklogStore, "update_group_last_notified_at")
    assert "push_message" not in inspect.getsource(QQBacklogService)


@pytest.mark.parametrize("key", ["backlog_notify_cooldown_seconds", "backlog_issue_notify_threshold"])
def test_old_notification_settings_are_explicitly_deprecated(key):
    assert "已废弃" in BY_KEY[key].description
