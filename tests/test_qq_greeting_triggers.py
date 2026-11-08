"""Deterministic online/random window scheduling; never contacts a real QQ account."""

import asyncio
import json
from datetime import datetime

import pytest
from fake_clock import patch_module_clock
from plugin.plugins.qq_auto_reply import proactive_budget as budget_module
from plugin.plugins.qq_auto_reply import proactive_initiative_service as module
from plugin.plugins.qq_auto_reply.proactive_initiative_service import STATE_FILE, QQProactiveInitiativeService
from test_qq_initiative_schedule import configured, slot
from test_qq_proactive_initiative import ADMIN


@pytest.fixture
def greeting_clock(monkeypatch):
    clock = [datetime(2026, 11, 8, 9, 0, 30).timestamp()]
    def now():
        return clock[0]
    patch_module_clock(monkeypatch, module, time=now)
    patch_module_clock(monkeypatch, budget_module, time=now)
    return clock


def at(clock, text):
    clock[0] = datetime.fromisoformat("2026-11-08 " + text).timestamp()


def tick(service):
    asyncio.run(service.tick())


def plan(service):
    return service.snapshot()["greeting"]["slots"][0]


@pytest.fixture(autouse=True)
def midpoint(monkeypatch):
    monkeypatch.setattr(module.random, "random", lambda: 0.5)


def test_already_online_waits_random_target_not_window_start(tmp_path, greeting_clock):
    at(greeting_clock, "08:00")
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    tick(s)
    expected = datetime(2026, 11, 8, 9, 30).timestamp()
    assert plan(s)["planned_at"] == expected
    assert plan(s)["trigger"] == "random"
    saved = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert saved["greeting_plans"][ADMIN]["morning"]["due_at"] == expected
    at(greeting_clock, "09:00")
    tick(s)
    assert not p.sent and "随机" in plan(s)["blocked_reason"]
    greeting_clock[0] = expected - 0.1
    tick(s)
    assert not p.sent
    greeting_clock[0] = expected
    tick(s)
    tick(s)
    assert len(p.sent) == 1


def test_offline_to_online_overrides_random_then_no_repeat(tmp_path, greeting_clock):
    at(greeting_clock, "08:00")
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    connected = [True]
    p.qq_client.is_connected = lambda: connected[0]
    tick(s)
    connected[0] = False
    at(greeting_clock, "09:05")
    tick(s)
    assert not p.sent and not s._greeting_slot_attempts
    assert s.snapshot()["sent_today"] == 0
    connected[0] = True
    tick(s)
    assert len(p.sent) == 1 and plan(s)["trigger"] == "online"
    connected[0] = False
    tick(s)
    connected[0] = True
    tick(s)
    tick(QQProactiveInitiativeService(p))
    at(greeting_clock, "09:30")
    tick(s)
    assert len(p.sent) == 1


@pytest.mark.parametrize("switch", ["proactive_initiative_enabled", "proactive_initiative_greeting_enabled"])
def test_enable_mid_window_while_online_draws_remaining_interval(tmp_path, greeting_clock, switch):
    p = configured(tmp_path, [slot()], settings={switch: False})
    s = p.proactive_initiative_service
    tick(s)  # observe connection even when disabled
    at(greeting_clock, "09:20")
    p._qq_settings[switch] = True
    tick(s)
    assert not p.sent
    assert plan(s)["planned_at"] == datetime(2026, 11, 8, 9, 40).timestamp()
    assert plan(s)["trigger"] == "random"


def test_new_slot_mid_window_random_and_snapshot_is_read_only(tmp_path, greeting_clock):
    p = configured(tmp_path, [])
    s = p.proactive_initiative_service
    tick(s)
    p._qq_settings["proactive_initiative_greeting_slots"] = [slot()]
    assert plan(s)["planned_at"] is None
    assert not (tmp_path / STATE_FILE).exists()
    at(greeting_clock, "09:20")
    tick(s)
    assert not p.sent and plan(s)["trigger"] == "random"


def test_startup_mid_window_and_snapshot_does_not_consume_online_event(tmp_path, greeting_clock):
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    assert plan(s)["planned_at"] is None
    assert not s._greeting_was_online
    tick(s)
    assert len(p.sent) == 1 and plan(s)["trigger"] == "online"


def test_saved_random_target_restores_before_window_without_redraw(tmp_path, greeting_clock, monkeypatch):
    at(greeting_clock, "08:00")
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    tick(s)
    target = plan(s)["planned_at"]
    monkeypatch.setattr(module.random, "random", lambda: pytest.fail("must not redraw"))
    restored = QQProactiveInitiativeService(p)
    tick(restored)
    assert plan(restored)["planned_at"] == target
    at(greeting_clock, "09:00")
    tick(restored)
    assert not p.sent


def test_restart_in_window_is_online_trigger_for_unattempted_slot(tmp_path, greeting_clock):
    at(greeting_clock, "08:00")
    p = configured(tmp_path, [slot()])
    tick(p.proactive_initiative_service)
    at(greeting_clock, "09:01")
    restored = QQProactiveInitiativeService(p)
    tick(restored)
    assert len(p.sent) == 1 and plan(restored)["trigger"] == "online"


@pytest.mark.parametrize("failure", ["offline", "missing", "exception", "stopped"])
def test_not_ready_never_claims_or_spends_budget(tmp_path, greeting_clock, failure):
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    if failure == "offline":
        p.qq_client.is_connected = lambda: False
    if failure == "missing":
        del p.qq_client
    if failure == "exception":

        def broken():
            raise RuntimeError("connection state unavailable")

        p.qq_client.is_connected = broken
    if failure == "stopped":
        p._running = False
    tick(s)
    assert not p.sent and not s._greeting_slot_attempts and not s._greeting_plans
    assert s.snapshot()["sent_today"] == 0


@pytest.mark.parametrize("phase", ["plan", "claim"])
def test_persistence_failure_does_not_send_and_online_can_retry(tmp_path, greeting_clock, monkeypatch, phase):
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    save = s._save
    calls = []

    def failing():
        calls.append(1)
        return False if phase == "plan" or len(calls) == 2 else save()

    monkeypatch.setattr(s, "_save", failing)
    tick(s)
    assert not p.sent and not s._greeting_slot_attempts
    assert s.budget.remaining_today(greeting_clock[0]) == 12
    monkeypatch.setattr(s, "_save", save)
    tick(s)
    assert len(p.sent) == 1 and plan(s)["trigger"] == "online"


def test_edit_sample_preserves_target_edit_window_replans(tmp_path, greeting_clock):
    at(greeting_clock, "08:00")
    row = slot()
    p = configured(tmp_path, [row])
    s = p.proactive_initiative_service
    tick(s)
    target = plan(s)["planned_at"]
    row.update(sample="新的样例", topic="cats", mode="search")
    tick(s)
    assert plan(s)["planned_at"] == target
    row.update(start="10:00", end="11:00")
    tick(s)
    assert plan(s)["planned_at"] == datetime(2026, 11, 8, 10, 30).timestamp()
    p._qq_settings["proactive_initiative_greeting_slots"] = []
    tick(s)
    assert s._greeting_plans[ADMIN] == {}


@pytest.mark.parametrize("blocker", ["quiet", "quota", "gap", "activity"])
def test_limits_defer_online_trigger_only_inside_window(tmp_path, greeting_clock, blocker):
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    if blocker == "quiet":
        p._qq_settings.update(proactive_initiative_quiet_start_hour=9, proactive_initiative_quiet_end_hour=10)
    if blocker == "quota":
        p._qq_settings["proactive_initiative_max_per_day"] = 0
    if blocker == "gap":
        p._qq_settings["proactive_initiative_min_gap_seconds"] = 3600
        reservation = s.budget.try_reserve("test", now=greeting_clock[0])
        s.budget.commit(reservation, now=greeting_clock[0])
    if blocker == "activity":
        s._last_admin_activity_at = greeting_clock[0]
    tick(s)
    assert not p.sent and plan(s)["trigger"] == "online"
    at(greeting_clock, "09:10")
    p._qq_settings.update(
        proactive_initiative_quiet_start_hour=0,
        proactive_initiative_quiet_end_hour=0,
        proactive_initiative_max_per_day=12,
        proactive_initiative_min_gap_seconds=0,
    )
    s._last_admin_activity_at = 0
    tick(s)
    assert len(p.sent) == 1


def test_expired_random_plan_is_not_backfilled_and_next_day_redraws(tmp_path, greeting_clock):
    at(greeting_clock, "08:00")
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    tick(s)
    at(greeting_clock, "10:00")
    tick(s)
    assert not p.sent and plan(s)["planned_at"] is None
    greeting_clock[0] = datetime(2026, 11, 9, 8).timestamp()
    tick(s)
    assert plan(s)["planned_at"] == datetime(2026, 11, 9, 9, 30).timestamp()
    greeting_clock[0] = plan(s)["planned_at"]
    tick(s)
    assert len(p.sent) == 1 and plan(s)["last_attempt_day"] == "2026-11-09"


def test_online_blocked_until_end_never_backfills(tmp_path, greeting_clock):
    p = configured(tmp_path, [slot()], settings={"proactive_initiative_max_per_day": 0})
    s = p.proactive_initiative_service
    tick(s)
    at(greeting_clock, "10:00")
    p._qq_settings["proactive_initiative_max_per_day"] = 12
    tick(s)
    assert not p.sent and not s._greeting_slot_attempts


def test_overnight_random_and_online_share_start_date(tmp_path, greeting_clock):
    at(greeting_clock, "22:00")
    p = configured(tmp_path, [slot(start="23:30", end="00:30")])
    s = p.proactive_initiative_service
    tick(s)
    assert plan(s)["planned_at"] == datetime(2026, 11, 9, 0).timestamp()
    greeting_clock[0] = plan(s)["planned_at"]
    tick(s)
    assert len(p.sent) == 1 and plan(s)["last_attempt_day"] == "2026-11-08"
    greeting_clock[0] += 60
    tick(QQProactiveInitiativeService(p))
    assert len(p.sent) == 1


def test_random_wakeup_before_window_end(tmp_path, greeting_clock, monkeypatch):
    at(greeting_clock, "08:00")
    monkeypatch.setattr(module.random, "random", lambda: 0.9999)
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    tick(s)
    at(greeting_clock, "09:59:59")
    delay = s._greeting_poll_delay()
    assert 0 < delay < 1
    greeting_clock[0] += delay
    tick(s)
    assert len(p.sent) == 1


@pytest.mark.parametrize("bad", [None, "nan", float("nan"), float("inf"), -1, True])
def test_corrupt_persisted_target_is_replaced(tmp_path, greeting_clock, bad):
    at(greeting_clock, "08:00")
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    tick(s)
    s._greeting_plans[ADMIN]["morning"]["due_at"] = bad
    s._save()
    restored = QQProactiveInitiativeService(p)
    tick(restored)
    assert plan(restored)["planned_at"] == datetime(2026, 11, 8, 9, 30).timestamp()


async def test_scheduler_lifecycle_and_concurrent_maintenance(tmp_path, greeting_clock):
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    await s.start_scheduler()
    first = s._scheduler_task
    await s.start_scheduler()
    assert first is s._scheduler_task
    await asyncio.gather(s.tick(), s.tick(), asyncio.sleep(0))
    assert len(p.sent) == 1
    await s.stop_scheduler()
    assert first.done() and s._scheduler_task is None
    await s.stop_scheduler()
    await s.start_scheduler()
    await asyncio.sleep(0)
    await s.stop_scheduler()
    assert len(p.sent) == 1


async def test_gate_owns_scheduler_and_duplicate_gate_cannot_stop_it(tmp_path, greeting_clock):
    from test_qq_per_group_maintenance import GROUP_A, _gate, _state

    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    first = _gate({GROUP_A: _state()})
    second = _gate({GROUP_A: _state()})
    first.plugin.proactive_initiative_service = s
    second.plugin.proactive_initiative_service = QQProactiveInitiativeService(p)
    try:
        await first.start_proactive_loop()
        task = s._scheduler_task
        await second.start_proactive_loop()
        await second.stop_proactive_loop()
        assert task is not None and not task.done()
        assert second.plugin.proactive_initiative_service._scheduler_task is None
    finally:
        await first.stop_proactive_loop()
        await second.stop_proactive_loop()
    assert task.done()


async def test_background_loop_sends_at_target_without_maintenance(tmp_path, greeting_clock, monkeypatch):
    at(greeting_clock, "08:59:59")
    p = configured(tmp_path, [slot(end="09:01")])
    s = p.proactive_initiative_service
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if p.sent:
            raise asyncio.CancelledError
        greeting_clock[0] += delay

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await s._greeting_loop()
    assert len(p.sent) == 1
    assert greeting_clock[0] == datetime(2026, 11, 8, 9, 0, 30).timestamp()
    assert min(delays) < 5


async def test_slow_generation_serializes_background_and_maintenance(tmp_path, greeting_clock):
    from plugin.sdk.plugin import Ok

    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        p.sent.append(kwargs)
        entered.set()
        await release.wait()
        return Ok({"status": "sent"})

    p.proactive_message_service.send_private_message = send
    await s.start_scheduler()
    await entered.wait()
    maintenance = asyncio.create_task(s.tick())
    await asyncio.sleep(0)
    assert not maintenance.done() and len(p.sent) == 1
    release.set()
    await maintenance
    await s.stop_scheduler()
    assert len(p.sent) == 1


async def test_scheduler_recovers_after_one_round_raises(tmp_path, greeting_clock, monkeypatch):
    p = configured(tmp_path, [slot()])
    s = p.proactive_initiative_service
    original = s._tick_greeting
    calls = []

    async def faulty():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        await original()

    async def sleep(delay):
        if p.sent:
            raise asyncio.CancelledError

    monkeypatch.setattr(s, "_tick_greeting", faulty)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await s._greeting_loop()
    assert len(calls) == 2 and len(p.sent) == 1


