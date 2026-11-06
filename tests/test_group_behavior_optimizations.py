from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply import session_memory_service as memory_module
from plugin.plugins.qq_auto_reply.reply_necessity import NecessitySignals, score_necessity


def test_busy_group_backlog_alone_does_not_invite_interruption():
    for kind in ("group_chatter", "reply_to_other", "at_other"):
        signals = NecessitySignals(
            message_text="今晚的烟花好漂亮", focus_active=True, pending_count=100,
            human_pair_streak=5, addressee_kind=kind,
        )
        assert score_necessity(signals).decision == "wait"
        signals = NecessitySignals(
            message_text="帮我查一下这个问题", focus_active=True, pending_count=100,
            human_pair_streak=5, addressee_kind=kind, is_at_bot=True,
        )
        assert score_necessity(signals).decision == "trigger"


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds,fail_at,expected", [(1, None, 5), (20, None, 2), (1, 2, 2)])
async def test_member_settlement_uses_time_budget_and_preserves_failed_order(monkeypatch, seconds, fail_at, expected):
    clock = [100.0]
    monkeypatch.setattr(memory_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []

    async def send(name, segments, **kwargs):
        calls.append(segments[0]["speaker_id"])
        clock[0] += seconds
        if len(calls) == fail_at:
            raise RuntimeError("temporary failure")
        return {"segments": [{"status": "ok"} for _ in segments]}

    plugin = SimpleNamespace(logger=Mock(), memory_bridge=SimpleNamespace(
        post_scoped_memory_history_batch=AsyncMock(side_effect=send),
        group_participant_subject=lambda gid, sid: {"group": gid, "sender": sid},
        speaker_account_id=lambda sid: sid,
    ))
    service = memory_module.QQSessionMemoryService(plugin)
    # Force separate chronological requests, matching alternating speakers whose
    # combined histories exceed a single request. Exercise real settlement/cleanup.
    service._pack_member_segment_groups = lambda groups, **kw: groups
    service._trust_reporting_ready = lambda: False
    buckets = {str(i): [{"role": "user", "content": f"fact {i}", "_speaker_sequence": i}] for i in range(5)}
    user_data = {"group_member_memory_messages": buckets}
    failed = await service._flush_member_buckets(user_data, group_id="1", her_name="test", reason="idle_timeout")
    assert calls == [str(i) for i in range(expected)]
    successful = expected if fail_at is None else fail_at - 1
    assert list(buckets) == [str(i) for i in range(successful, 5)]
    assert failed == list(buckets)
