from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from plugin.plugins.qq_auto_reply.deepchat.service import DeepChatService
from plugin.plugins.qq_auto_reply.proactive_budget import ProactiveBudget
from plugin.plugins.qq_auto_reply.proactive_initiative_service import STATE_FILE, QQProactiveInitiativeService
from plugin.sdk.plugin import Err, Ok, SdkError


@pytest.fixture
def plugin(tmp_path):
    settings = {
        "deepchat_autonomy_enabled": True, "proactive_initiative_enabled": True,
        "deepchat_autonomy_max_per_tick": 2, "proactive_initiative_max_per_day": 1,
        "proactive_initiative_quiet_start_hour": 0, "proactive_initiative_quiet_end_hour": 0,
        "proactive_initiative_min_gap_seconds": 0,
    }
    plugin = SimpleNamespace(
        _qq_settings=settings, _admin_qq="u1", _running=True,
        data_path=lambda *names: tmp_path.joinpath(*names),
        proactive_message_service=SimpleNamespace(send_private_message=AsyncMock(return_value=Ok({}))),
    )
    plugin.proactive_initiative_service = QQProactiveInitiativeService(plugin)
    plugin.deepchat_service = DeepChatService(plugin)
    return plugin


@pytest.mark.parametrize("cap,gap", [(1, 0), (2, 3600)])
async def test_real_autonomy_loop_rechecks_each_candidate(plugin, cap, gap):
    plugin._qq_settings.update(proactive_initiative_max_per_day=cap, proactive_initiative_min_gap_seconds=gap)
    service = plugin.deepchat_service
    for user in ("u1", "u2"):
        service.relationship.reinforce(user, 0.8)
        service.autonomy.record_private_interaction(user, now=time.time() - 7 * 3600)
    ticks = 0

    async def sleep(_):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    with patch("plugin.plugins.qq_auto_reply.deepchat.service.asyncio.sleep", sleep):
        with pytest.raises(asyncio.CancelledError):
            await service.autonomy_loop()
    assert plugin.proactive_message_service.send_private_message.await_count == 1
    assert plugin.proactive_initiative_service.budget.sent_today() == 1
    assert sum(service.autonomy.snapshot(user)["count_today"] for user in ("u1", "u2")) == 1


async def test_inflight_blocks_other_source_and_survives_restart_and_cancel(plugin):
    plugin._qq_settings["proactive_initiative_max_per_day"] = 2
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await hold.wait()

    plugin.proactive_message_service.send_private_message.side_effect = send
    service = plugin.proactive_initiative_service
    task = asyncio.create_task(service.send_with_budget(source="initiative", target="u1", message="hello"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        result = await service.send_with_budget(source="deepchat", target="u2", message="hello")
        assert isinstance(result, Err) and "BUDGET_BLOCKED" in str(result.error)
        assert plugin.proactive_message_service.send_private_message.await_count == 1
        restored = QQProactiveInitiativeService(plugin)
        assert restored.budget.sent_today() == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service.budget.snapshot()["in_flight"] == 0
    assert service.budget.sent_today() == 1
    assert json.loads(plugin.data_path(STATE_FILE).read_text(encoding="utf-8"))["today_count"] == 1


@pytest.mark.parametrize("error,expected", [("GENERATE_FAILED: empty", 0), ("SEND_FAILED: timeout", 1)])
async def test_only_proven_unsent_result_refunds_budget(plugin, error, expected):
    plugin.proactive_message_service.send_private_message.return_value = Err(SdkError(error))
    service = plugin.proactive_initiative_service
    await service.send_with_budget(source="deepchat", target="u1", message="hello")
    assert service.budget.sent_today() == expected
    assert service.budget.snapshot()["in_flight"] == 0
    assert QQProactiveInitiativeService(plugin).budget.sent_today() == expected


async def test_cannot_send_when_ledger_write_fails(plugin, monkeypatch):
    service = plugin.proactive_initiative_service
    monkeypatch.setattr(service, "_save", lambda: False)
    with pytest.raises(RuntimeError, match="额度未能保存"):
        await service.send_with_budget(source="deepchat", target="u1", message="hello")
    plugin.proactive_message_service.send_private_message.assert_not_awaited()
    assert service.budget.sent_today() == 0


def test_settlement_is_idempotent_and_midnight_keeps_live_reservations():
    budget = ProactiveBudget(max_per_day=lambda: 2, min_gap_seconds=lambda: 0)
    reservation = budget.try_reserve("initiative", now=1000)
    budget.mark_sending(reservation)
    assert budget.sent_today(1000 + 86400) == 1
    assert isinstance(budget.try_reserve("deepchat", now=1000 + 86400), str)
    budget.commit(reservation, now=1000 + 86401)
    budget.commit(reservation, now=1000 + 86402)
    assert budget.sent_today(1000 + 86402) == 1
    assert not budget.release(reservation)


def test_shared_budget_read_failure_blocks_candidates(plugin):
    plugin.proactive_initiative_service.remaining_today = lambda **kw: 1 / 0
    assert not plugin.deepchat_service.autonomy._shared_budget_allows(time.time())


def test_uncertain_send_restored_next_day_still_consumes_budget(monkeypatch):
    from plugin.plugins.qq_auto_reply import proactive_budget as module

    budget = ProactiveBudget(max_per_day=lambda: 1, min_gap_seconds=lambda: 3600)
    token = budget.try_reserve("deepchat", now=1000)
    budget.mark_sending(token)
    payload = budget.to_payload()
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 1000 + 86400))
    restored = ProactiveBudget(max_per_day=lambda: 1, min_gap_seconds=lambda: 3600)
    restored.restore(payload)
    assert restored.remaining_today() == 0
    assert restored.gap_remaining() == 3600
