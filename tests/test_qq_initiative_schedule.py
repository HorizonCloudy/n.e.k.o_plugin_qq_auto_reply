"""Multi-window scheduling: local-time boundaries, persistence, UI save and tools."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.qq_auto_reply.initiative_schedule import normalize_greeting_slots, slot_window
from plugin.plugins.qq_auto_reply.proactive_initiative_service import STATE_FILE, QQProactiveInitiativeService
from plugin.sdk.plugin import Err, Ok, SdkError
from test_qq_proactive_initiative import ADMIN, _greeting_plugin


@pytest.fixture
def greeting_clock(monkeypatch):
    import time

    clock = [datetime(2026, 11, 8, 9, 0, 30).timestamp()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    return clock


def slot(sid="morning", **kw):
    return dict(
        dict(id=sid, enabled=True, start="09:00", end="10:00", mode="greeting", sample="早安呀", topic=""), **kw
    )


def configured(tmp_path, slots, **kw):
    settings = {
        "proactive_initiative_greeting_slots": slots,
        "proactive_initiative_min_gap_seconds": 0,
        "proactive_initiative_max_per_day": 12,
    }
    settings.update(kw.pop("settings", {}))
    return _greeting_plugin(tmp_path, settings=settings, **kw)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        "09:00",
        [None],
        [slot(start="9:00")],
        [slot(end="24:00")],
        [slot(end="09:00")],
        [slot(enabled=1)],
        [slot(mode="bad")],
        [slot(id="<bad>")],
        [slot(), slot()],
        [slot(sample="x" * 501)],
        [slot(topic="x" * 301)],
        [slot(sample=None)],
        [slot(str(i)) for i in range(13)],
    ],
)
def test_invalid_schedule_rejects_entire_edit(value):
    with pytest.raises(ValueError):
        normalize_greeting_slots(value)


def test_normalization_and_explicit_empty():
    assert normalize_greeting_slots([]) == []
    assert normalize_greeting_slots([slot(sample="  hi  ", topic=" cats ")])[0] == slot(sample="hi", topic="cats")


@pytest.mark.parametrize(
    "clock, start, end",
    [
        ("2026-11-08 23:30", "2026-11-08 23:30", "2026-11-09 00:30"),
        ("2026-11-09 00:00", "2026-11-08 23:30", "2026-11-09 00:30"),
        ("2026-11-09 00:29", "2026-11-08 23:30", "2026-11-09 00:30"),
        ("2026-11-09 00:30", "2026-11-09 23:30", "2026-11-10 00:30"),
    ],
)
def test_overnight_start_date(clock, start, end):
    actual = slot_window(slot(start="23:30", end="00:30"), datetime.fromisoformat(clock).timestamp())
    assert actual == (datetime.fromisoformat(start), datetime.fromisoformat(end))


def test_independent_windows_restart_and_stable_id(tmp_path, greeting_clock):
    plugin = configured(tmp_path, [slot(), slot("search", mode="search", topic="猫猫")])
    service = plugin.proactive_initiative_service
    asyncio.run(service.tick())
    assert len(plugin.sent) == 1
    service = QQProactiveInitiativeService(plugin)
    asyncio.run(service.tick())
    assert len(plugin.sent) == 2
    plugin._qq_settings["proactive_initiative_greeting_slots"][0].update(start="08:00", sample="changed")
    asyncio.run(QQProactiveInitiativeService(plugin).tick())
    assert len(plugin.sent) == 2
    assert service._greeting_slot_attempts[ADMIN] == {"morning": "2026-11-08", "search": "2026-11-08"}
    greeting_clock[0] += 86400
    asyncio.run(service.tick())
    assert len(plugin.sent) == 2
    greeting_clock[0] = min(row["planned_at"] for row in service.snapshot()["greeting"]["slots"])
    asyncio.run(service.tick())
    assert len(plugin.sent) == 3


def test_disabled_slot_does_not_block_later_slot(tmp_path, greeting_clock):
    plugin = configured(tmp_path, [slot(enabled=False), slot("second")])
    asyncio.run(plugin.proactive_initiative_service.tick())
    assert len(plugin.sent) == 1
    assert set(plugin.proactive_initiative_service._greeting_slot_attempts[ADMIN]) == {"second"}


def test_overnight_dedup_and_next_occurrence(tmp_path, greeting_clock):
    plugin = configured(tmp_path, [slot(start="23:30", end="00:30")])
    for text, count in [
        ("2026-11-08 23:30", 1),
        ("2026-11-09 00:10", 1),
        ("2026-11-09 00:30", 1),
        ("2026-11-09 23:30", 2),
    ]:
        greeting_clock[0] = datetime.fromisoformat(text).timestamp()
        service = QQProactiveInitiativeService(plugin)
        asyncio.run(service.tick())
        assert len(plugin.sent) == count


@pytest.mark.parametrize("blocker", ["quiet", "gap", "activity", "budget"])
def test_blockers_can_defer_within_window(tmp_path, greeting_clock, blocker):
    plugin = configured(tmp_path, [slot()])
    service = plugin.proactive_initiative_service
    if blocker == "quiet":
        plugin._qq_settings.update(proactive_initiative_quiet_start_hour=9, proactive_initiative_quiet_end_hour=10)
    elif blocker == "gap":
        plugin._qq_settings["proactive_initiative_min_gap_seconds"] = 600
        service.record_send("deepchat", greeting_clock[0])
    elif blocker == "activity":
        service._last_admin_activity_at = greeting_clock[0]
    else:
        plugin._qq_settings["proactive_initiative_max_per_day"] = 0
    asyncio.run(service.tick())
    assert not plugin.sent
    assert not service._greeting_slot_attempts
    greeting_clock[0] += 601
    plugin._qq_settings.update(
        proactive_initiative_quiet_start_hour=0,
        proactive_initiative_quiet_end_hour=0,
        proactive_initiative_max_per_day=12,
    )
    asyncio.run(service.tick())
    assert len(plugin.sent) == 1


@pytest.mark.parametrize("time, expected", [("08:59", 0), ("09:00", 1), ("09:59", 1), ("10:00", 0)])
def test_window_end_is_exclusive(tmp_path, greeting_clock, time, expected):
    greeting_clock[0] = datetime.fromisoformat("2026-11-08 " + time).timestamp()
    plugin = configured(tmp_path, [slot()])
    asyncio.run(plugin.proactive_initiative_service.tick())
    assert len(plugin.sent) == expected


@pytest.mark.parametrize("value", [[], [slot(start="bad")], {}])
def test_empty_or_corrupt_schedule_never_resurrects_legacy(tmp_path, greeting_clock, value):
    plugin = configured(tmp_path, value)
    asyncio.run(plugin.proactive_initiative_service.tick())
    assert not plugin.sent
    assert plugin.proactive_initiative_service.greeting_slots() == []


def test_migration_preserves_legacy_attempt(tmp_path, greeting_clock):
    (tmp_path / STATE_FILE).write_text(json.dumps({"greeting_attempt_days": {ADMIN: "2026-11-08"}}))
    plugin = configured(tmp_path, None)
    service = plugin.proactive_initiative_service
    assert service.greeting_slots()[0]["id"] == "legacy"
    plugin._qq_settings["proactive_initiative_greeting_slots"] = service.greeting_slots()
    asyncio.run(service.tick())
    assert not plugin.sent


def test_persistence_failure_rolls_back_new_slot(tmp_path, greeting_clock, monkeypatch):
    plugin = configured(tmp_path, [slot()])
    service = plugin.proactive_initiative_service
    monkeypatch.setattr(service, "_save", lambda: False)
    asyncio.run(service.tick())
    assert not plugin.sent
    assert not service._greeting_slot_attempts
    assert service.snapshot()["sent_today"] == 0
    assert service.remaining_today() == 12


def test_failed_generation_does_not_retry_but_other_slot_can_run(tmp_path, greeting_clock):
    plugin = configured(tmp_path, [slot(), slot("second")], send_result=Err(SdkError("GENERATE_FAILED: empty")))
    for _ in range(3):
        asyncio.run(QQProactiveInitiativeService(plugin).tick())
    assert len(plugin.sent) == 2


@pytest.mark.parametrize("mode", ["greeting", "search", "video"])
def test_prompt_modes_and_snapshot(tmp_path, greeting_clock, mode):
    row = slot(mode=mode, topic="萌宠")
    plugin = configured(tmp_path, [row])
    service = plugin.proactive_initiative_service
    prompt = service.build_greeting_prompt(row, greeting_clock[0])
    assert "早安呀" in prompt and "萌宠" in prompt
    assert "不必照抄" in prompt
    if mode != "greeting":
        assert "实际调用" in prompt
        assert "退回样例问候或普通问候" in prompt
        assert "不编造" in prompt
        assert "不要点赞" in prompt
    snapshot = service.snapshot()["greeting"]["slots"][0]
    assert snapshot["mode"] == mode and "等待调度" in snapshot["blocked_reason"]
    asyncio.run(service.tick())
    assert service.snapshot()["greeting"]["slots"][0]["last_attempt_day"] == "2026-11-08"


def test_dashboard_saves_normalized_windows(tmp_path):
    from plugin.plugins.qq_auto_reply.dashboard_service import QQDashboardService
    from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService

    plugin = configured(tmp_path, [])
    settings = QQSettingsService(plugin)

    async def save(**kw):
        settings._apply_plain_settings(kw)
        return {"persisted": True}

    plugin.settings_service = SimpleNamespace(save_settings=save)
    dashboard = QQDashboardService(plugin)
    dashboard.build_dashboard_state = AsyncMock(return_value={"business_config": {}})
    result = asyncio.run(dashboard.save_settings(proactive_initiative_greeting_slots=[slot(sample=" hi ")]))
    assert isinstance(result, Ok)
    assert plugin._qq_settings["proactive_initiative_greeting_slots"] == [slot(sample="hi")]


def test_invalid_save_fails_before_mutation(tmp_path):
    from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService

    plugin = configured(tmp_path, [slot()])
    before = dict(plugin._qq_settings)
    with pytest.raises(ValueError):
        asyncio.run(
            QQSettingsService(plugin)._save_settings_locked(
                proactive_initiative_enabled=False, proactive_initiative_greeting_slots=[slot(end="09:00")]
            )
        )
    assert plugin._qq_settings == before


@pytest.mark.parametrize("authorized", [True, False])
def test_proactive_private_keeps_existing_tool_permissions(authorized):
    from test_qq_plugin_tool_bridge import _configured_plugin, _context, _generation_service, _Session

    plugin = _configured_plugin(tiers={"web_search": "all"} if authorized else {})
    service, session = _generation_service(plugin), _Session()
    context = _context()
    context.source_kind = "proactive_private"
    asyncio.run(service._arm_turn_tools(context=context, user_session=session, consent_before={}))
    assert ("plugin_web_search" in [tool.name for tool in session.tools or []]) is authorized
