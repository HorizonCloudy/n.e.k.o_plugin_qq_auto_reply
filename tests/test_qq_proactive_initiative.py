# -*- coding: utf-8 -*-
"""管理员私聊的「有由头」主动搭话（A 方案，2026-10-02）。

使用者口径演变（很重要，别把两条混起来）：

* 2026-09-29 删掉**主动破冰**：「干脆不要这个先」——它拿「群静默 1800 秒」当"该说话了"
  的理由，使用者明确否决这种判据；
* 2026-10-02 要做主动搭话，指定「主要针对**管理员的私聊**」，并在三个候选里选了
  **A = 有由头**：只在她自己说过要交代某件事时才开口。

所以这里钉的是：

1. **判据是内容**：她说过"晚点告诉你 / 明天给你看 / 等我查一下"→ 记一条待办；
   光有时间词（"等一下，我不是那个意思"）不算承诺；
2. **时间只做到期与刹车**：静音时段 / 每日上限 / 最小间隔 —— 都不是触发；
3. **范围只到管理员私聊**：群聊、非管理员、合成轮（含她自己的主动发言）一律不登记；
4. **她可以拒绝**：那一轮空回复在 `reply_postprocess_node` 就变成"不发"，本服务据此
   作废这条由头，不留着反复试。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fake_clock import patch_module_clock
from plugin.plugins.qq_auto_reply import proactive_budget as budget_module
from plugin.plugins.qq_auto_reply import proactive_initiative_service as initiative_module
from plugin.plugins.qq_auto_reply.attention_gate_service import QQAttentionGateService
from plugin.plugins.qq_auto_reply.proactive_initiative_service import (
    ACTIVITY_GRACE_SECONDS,
    MAX_ATTEMPTS,
    MAX_ITEM_AGE_SECONDS,
    STATE_FILE,
    QQProactiveInitiativeService,
    detect_promise,
)
from plugin.sdk.plugin import Err, Ok, SdkError

ADMIN = "10001"

#: 大部分用例不希望撞上静音时段：起止相同 = 没有静音。
NO_QUIET = {
    "proactive_initiative_quiet_start_hour": 0,
    "proactive_initiative_quiet_end_hour": 0,
}


# ── 夹具 ────────────────────────────────────────────────────────────


def _plugin(tmp_path, *, enabled=True, admin=ADMIN, send_result=None, settings=None, running=True):
    sent: list[dict] = []

    class _Proactive:
        async def send_private_message(self, *, target, message, verbatim=False):
            sent.append({"target": target, "message": message, "verbatim": verbatim})
            return send_result if send_result is not None else Ok({"status": "sent"})

    values = {"proactive_initiative_enabled": enabled, **NO_QUIET}
    values.update(settings or {})
    emitted: list[tuple[str, str]] = []
    logged: list[str] = []
    plugin = SimpleNamespace(
        _qq_settings=values,
        _admin_qq=admin,
        _running=running,
        qq_client=SimpleNamespace(is_connected=lambda: True),
        proactive_message_service=_Proactive(),
        data_path=lambda name: tmp_path / name,
        logger=SimpleNamespace(
            info=lambda msg, *a, **k: logged.append(str(msg)),
            warning=lambda msg, *a, **k: logged.append(str(msg)),
        ),
        _emit_log=lambda level, msg: emitted.append((level, msg)),
        sent=sent,
        emitted=emitted,
        logged=logged,
    )
    plugin.proactive_initiative_service = QQProactiveInitiativeService(plugin)
    return plugin


def _service(plugin) -> QQProactiveInitiativeService:
    return plugin.proactive_initiative_service


def _turn(service, *, text="我晚点告诉你结果", source="incoming_private", is_group=False, sender=None):
    request = SimpleNamespace(is_group=is_group, sender_id=sender or service._admin())
    outcome = SimpleNamespace(reply_text=text)
    service.note_turn(source=source, request=request, outcome=outcome)


def _force_due(service, *, ago=60):
    """把待办挪到"已经到期"（不碰判据，只挪时钟）。"""
    item = service._items[service._admin()]
    item["due_at"] = datetime.now().timestamp() - ago
    service._save()
    return item


# ── 1. 承诺识别：判据是内容，不是时间 ────────────────────────────────


@pytest.mark.parametrize(
    "text, expect_due",
    [
        ("我晚点告诉你结果", 3 * 3600),
        ("明天给你看", 20 * 3600),
        ("明天晚点给你看", 20 * 3600),  # 命中多个取最晚
        ("等我查一下哈", 30 * 60),
        ("稍后把整理好的发你", 3600),
        ("回头跟你说", 2 * 3600),
    ],
)
def test_promise_phrases_are_recognised(text, expect_due):
    found = detect_promise(text)
    assert found, f"这句该被认成承诺: {text!r}"
    assert found["due_seconds"] == expect_due
    assert text[:4] in found["quote"] or found["quote"] in text


@pytest.mark.parametrize(
    "text",
    [
        "等一下，我不是那个意思",  # 有时间词、没有交代类动词
        "我先去告诉妈妈了",  # 有动词、没有时间词
        "嗯嗯~",
        "明天见",  # 有交互没有承诺
        "",
    ],
)
def test_non_promises_are_not_recorded(text):
    assert detect_promise(text) is None


# ── 2. 登记范围：只认管理员私聊的非合成轮 ────────────────────────────


def test_admin_private_turn_with_a_promise_is_recorded(tmp_path):
    service = _service(_plugin(tmp_path))
    _turn(service)

    pending = service.pending()
    assert pending and "晚点告诉你" in pending["quote"]
    assert pending["due_at"] > pending["created_at"], "到期时刻没有排到将来"
    assert (tmp_path / STATE_FILE).exists(), "待办没落盘（宿主重启就丢）"


def test_group_turns_are_never_recorded(tmp_path):
    """范围就是管理员私聊 —— 群聊不走这条路。"""
    service = _service(_plugin(tmp_path))
    _turn(service, is_group=True)
    assert service.pending() is None


def test_turns_from_other_people_are_ignored(tmp_path):
    service = _service(_plugin(tmp_path))
    _turn(service, sender="20002")
    assert service.pending() is None


def test_synthetic_turns_are_ignored(tmp_path):
    """她的主动发言 / 回投 / 快速补发这些合成轮不能续期自己的由头。"""
    from plugin.plugins.qq_auto_reply.pipeline_models import (
        KIND_PLUGIN_TOOL_RESULT,
        KIND_PROACTIVE_GROUP,
        KIND_PROACTIVE_PRIVATE,
        KIND_RAPID_FIRE,
        KIND_RETROACTIVE_REVIEW,
    )

    service = _service(_plugin(tmp_path))
    # 常量从源码取，别手写字面量：`KIND_RAPID_FIRE` 的值是 `rapid_fire_flush`，
    # 抄错了这条测试就会假绿（第一版就是这么错的）。
    for source in (
        KIND_PROACTIVE_PRIVATE,
        KIND_PROACTIVE_GROUP,
        KIND_PLUGIN_TOOL_RESULT,
        KIND_RAPID_FIRE,
        KIND_RETROACTIVE_REVIEW,
    ):
        _turn(service, source=source)
    assert service.pending() is None


def test_a_reply_without_a_promise_keeps_the_existing_item(tmp_path):
    """这轮没说承诺 ≠ 取消上一个承诺。"""
    service = _service(_plugin(tmp_path))
    _turn(service, text="我晚点告诉你结果")
    first = service.pending()
    _turn(service, text="嗯嗯~")
    assert service.pending() == first


# ── 3. 判据：时间只做到期与刹车 ──────────────────────────────────────


def test_blocked_reasons_are_ordered_and_shared_with_the_send_path(tmp_path):
    plugin = _plugin(tmp_path, enabled=False)  # 默认是关的
    service = _service(plugin)
    assert service.blocked_reason() == "开关关着"

    plugin._qq_settings["proactive_initiative_enabled"] = True
    plugin._running = False
    assert service.blocked_reason() == "值班已停"

    plugin._running = True
    assert service.blocked_reason() == "还没有由头"

    _turn(service)
    assert service.blocked_reason() == "还没到期"

    _force_due(service)
    # `note_turn` 刚把"他最近说过话"刷新到现在 → 这一刻别抢在自己正常回合前面插话
    assert service.blocked_reason() == "刚跟她说过话"

    service._last_admin_activity_at = 0.0
    assert service.blocked_reason() == ""


def test_quiet_hours_block_and_cross_midnight(tmp_path, monkeypatch):
    from plugin.plugins.qq_auto_reply import proactive_initiative_service as pis

    plugin = _plugin(
        tmp_path,
        settings={
            "proactive_initiative_quiet_start_hour": 23,
            "proactive_initiative_quiet_end_hour": 8,
        },
    )
    service = _service(plugin)
    _turn(service)
    service._last_admin_activity_at = 0.0

    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    at_3am = base.replace(hour=3).timestamp()
    at_noon = base.replace(hour=12).timestamp()
    # 到期时刻钉在这两个坐标之前，避免用例依赖"现在几点"
    service._items[service._admin()]["due_at"] = at_3am - 3600
    service._save()

    assert service._within_active_hours(at_3am) is False
    assert service._within_active_hours(at_noon) is True
    assert service.blocked_reason(at_3am) == "静音时段"

    # 时钟只在本模块内伪造（全局换 stdlib time 会被 plugin/conftest 的守卫拦下）
    monkeypatch.setattr(pis, "time", SimpleNamespace(time=lambda: at_3am))
    asyncio.run(service.tick())
    assert plugin.sent == [], "静音时段还发消息"

    # 白天同样的状态就会发
    monkeypatch.setattr(pis, "time", SimpleNamespace(time=lambda: at_noon))
    service._last_admin_activity_at = 0.0
    asyncio.run(service.tick())
    assert len(plugin.sent) == 1


def test_daily_cap_and_gap_and_attempts_brake_the_send(tmp_path):
    plugin = _plugin(
        tmp_path,
        settings={"proactive_initiative_max_per_day": 1, "proactive_initiative_min_gap_seconds": 3600},
    )
    service = _service(plugin)
    _turn(service)
    item = _force_due(service)
    service._last_admin_activity_at = 0.0
    assert service.blocked_reason() == ""

    service.budget.restore({"today": datetime.now().strftime("%Y-%m-%d"), "today_count": 1})
    assert service.blocked_reason() == "今天已经说够了"

    service.budget.restore(
        {"today": datetime.now().strftime("%Y-%m-%d"), "today_count": 0, "last_sent_at": datetime.now().timestamp()}
    )
    assert service.blocked_reason() == "距上次太近"

    service.budget.restore({})
    item["attempts"] = MAX_ATTEMPTS
    assert service.blocked_reason() == "试过太多次"


# ── 4. 开口：由头必须进提示词，结清后不再提 ──────────────────────────


def test_tick_sends_the_quote_and_settles_the_item(tmp_path):
    plugin = _plugin(tmp_path)
    service = _service(plugin)
    _turn(service, text="我明天给你看那个配置")
    _force_due(service)
    service._last_admin_activity_at = 0.0

    asyncio.run(service.tick())

    assert len(plugin.sent) == 1, "该开口却没开口"
    sent = plugin.sent[0]
    assert sent["target"] == ADMIN
    assert "我明天给你看那个配置" in sent["message"], "由头没进提示词 —— 她会说不出话"
    assert "空回复" in sent["message"], "没有给她'不想说就可以不说'的出口"
    assert service.pending() is None, "说完了这条由头该结清"
    assert service.snapshot()["sent_today"] == 1


def test_she_can_decline_and_the_item_is_dropped(tmp_path):
    """空回复 → `send_private_message` 回 GENERATE_FAILED；这条由头作废，别反复试。"""
    plugin = _plugin(tmp_path, send_result=Err(SdkError("GENERATE_FAILED: AI 未生成可发送的私聊内容")))
    service = _service(plugin)
    _turn(service)
    _force_due(service)
    service._last_admin_activity_at = 0.0

    asyncio.run(service.tick())

    assert service.pending() is None, "她不想说的时候不该留着这条一直问"
    assert service.snapshot()["sent_today"] == 0


def test_a_send_failure_keeps_the_item_for_a_later_retry(tmp_path):
    plugin = _plugin(tmp_path, send_result=Err(SdkError("SEND_FAILED: 网络炸了")))
    service = _service(plugin)
    _turn(service)
    item = _force_due(service)
    service._last_admin_activity_at = 0.0

    asyncio.run(service.tick())

    assert service.pending() is not None, "发失败不等于她不想说"
    assert item["attempts"] == 1
    assert service.blocked_reason() == "距上次太近", "不确定是否送达时保留额度与全局间隔"


def test_disabled_switch_never_sends(tmp_path):
    plugin = _plugin(tmp_path, enabled=False)
    service = _service(plugin)
    _turn(service)
    _force_due(service)
    service._last_admin_activity_at = 0.0

    asyncio.run(service.tick())

    assert plugin.sent == []


# ── 5. 落盘往返 ─────────────────────────────────────────────────────


def test_pending_item_and_counter_survive_a_restart(tmp_path):
    plugin = _plugin(tmp_path)
    service = _service(plugin)
    _turn(service, text="我晚点告诉你结果")
    _force_due(service)
    service._last_admin_activity_at = 0.0
    asyncio.run(service.tick())

    _turn(service, text="明天给你看")  # 再记一条，看计数与待办是否都留住
    stored = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert stored["today_count"] == 1

    again = _service(plugin)  # 同一份 data_path → 模拟宿主重启
    assert again.pending() is not None
    assert again.snapshot()["sent_today"] == 1


# ── 6. 接线：维护循环当心跳，查询给界面 ───────────────────────────────


def test_the_maintenance_tick_drives_the_initiative(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.attention_service = SimpleNamespace(
        _current_time=lambda: 0,
        _normalized_groups=lambda: [],
    )
    gate = QQAttentionGateService(plugin)
    service = _service(plugin)
    _turn(service)
    _force_due(service)
    service._last_admin_activity_at = 0.0

    asyncio.run(gate.run_maintenance_tick())

    assert len(plugin.sent) == 1, "维护循环没把主动搭话带起来"


def test_nothing_to_say_means_no_send_from_the_tick(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.attention_service = SimpleNamespace(
        _current_time=lambda: 0,
        _normalized_groups=lambda: [],
    )
    asyncio.run(QQAttentionGateService(plugin).run_maintenance_tick())
    assert plugin.sent == []


def test_snapshot_is_json_friendly(tmp_path):
    service = _service(_plugin(tmp_path))
    snap = service.snapshot()
    for key in (
        "enabled",
        "admin",
        "has_admin",
        "pending",
        "blocked_reason",
        "sent_today",
        "max_per_day",
        "quiet_start_hour",
        "quiet_end_hour",
    ):
        assert key in snap, f"界面要的字段缺了 {key}"
    json.dumps(snap)  # 必须能直接进 Ok({...}) 的返回


# ── 6.5 停机 / 重启：计时按绝对时刻接着走 ────────────────────────────
#
# 使用者问过「如果计时中间关闭了插件会如何」，这组就是答案：
#   · 待办的到期时刻是**绝对时间戳**，落盘 → 重启后接着算，不用重新"从头等"；
#   · 停机期间不发（`_running` 假 → 值班已停），重启后在**下一个 tick** 就发；
#   · 每人只留最新一条 → 不会出现"停机三天回来补发一串"；
#   · 超过 7 天的由头按"过去太久"丢掉（"晚点告诉你"隔一周再说已经没意义）；
#   · 「他刚说过话」的宽限也落盘，否则重启会把它清掉、立刻翻旧账。


def test_the_countdown_uses_an_absolute_deadline_and_survives_a_restart(tmp_path):
    plugin = _plugin(tmp_path)
    service = _service(plugin)
    _turn(service)
    stored = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert stored["items"][ADMIN]["due_at"] > 0, "到期时刻没落盘"
    assert stored["last_admin_activity_at"] > 0, "「他刚说过话」的时刻没落盘"

    # 「插件关着的这段时间」里到期了
    _force_due(service)

    restarted = _service(plugin)  # 同一份 data_path = 宿主重启
    assert restarted.pending() is not None, "重启把待办弄丢了"
    assert restarted.blocked_reason() == "刚跟她说过话", "重启后宽限失效 —— 刚聊完重启就会立刻抖出旧由头"

    restarted._last_admin_activity_at = 0.0
    asyncio.run(restarted.tick())
    assert len(plugin.sent) == 1, "重启后应该在下一次心跳就发"


def test_nothing_is_sent_while_stopped_and_the_item_is_kept(tmp_path):
    """关掉自动回复（值班停了）期间不发，但也不丢 —— 这正是"停机"的语义。"""
    plugin = _plugin(tmp_path)
    service = _service(plugin)
    _turn(service)
    _force_due(service)
    service._last_admin_activity_at = 0.0
    plugin._running = False

    asyncio.run(service.tick())

    assert plugin.sent == []
    assert service.blocked_reason() == "值班已停"
    assert service.pending() is not None, "值班停了不等于把承诺丢了"

    plugin._running = True
    asyncio.run(service.tick())
    assert len(plugin.sent) == 1


def test_an_item_older_than_a_week_is_dropped_instead_of_fired(tmp_path):
    """停机很久回来：一句隔了一周的"晚点告诉你"再说出来只会奇怪，按过期丢掉。"""
    plugin = _plugin(tmp_path)
    service = _service(plugin)
    _turn(service, text="我明天给你看那个配置")
    item = service._items[ADMIN]
    item["created_at"] = datetime.now().timestamp() - MAX_ITEM_AGE_SECONDS - 60
    item["due_at"] = datetime.now().timestamp() - 60
    service._save()
    service._last_admin_activity_at = 0.0

    assert service.blocked_reason() == "这件事过去太久了"
    asyncio.run(service.tick())
    assert plugin.sent == [], "过期由头不该再发"
    assert service.pending() is None, "过期即清掉，别一直挡着下一条"


def test_stop_start_does_not_stack_up_messages(tmp_path):
    """每人只留最新一条 + 每天上限 → 停机三天回来最多补一次，不会刷一串。"""
    plugin = _plugin(tmp_path, settings={"proactive_initiative_max_per_day": 1})
    service = _service(plugin)
    for _ in range(3):  # 同一件事被反复承诺
        _turn(service, text="我晚点告诉你结果")
    assert len(service._items) == 1, "同一人堆了多条待办"

    _force_due(service)
    service._last_admin_activity_at = 0.0
    asyncio.run(service.tick())
    _turn(service, text="我晚点告诉你结果")  # 又许一次
    _force_due(service)
    service._last_admin_activity_at = 0.0
    asyncio.run(service.tick())

    assert len(plugin.sent) == 1, "超过每日上限还在发"


def test_the_daily_counter_starts_over_after_midnight(tmp_path):
    plugin = _plugin(tmp_path)
    service = _service(plugin)
    service.budget.restore({"today": "2000-01-01", "today_count": 2})
    assert service.snapshot()["sent_today"] == 0, "跨天后没归零"


def test_the_grace_window_is_the_documented_length(tmp_path):
    """宽限是个有声明的常量（默认 10 分钟），不是随手一个数。"""
    assert ACTIVITY_GRACE_SECONDS == 600

    # ── 7. 结构契约：这是新功能，不是破冰复活 ──────────────────────────def test_the_new_settings_exist_and_the_deleted_icebreaker_keys_stay_dead():
    from plugin.plugins.qq_auto_reply import settings_schema

    for key in (
        "proactive_initiative_enabled",
        "proactive_initiative_quiet_start_hour",
        "proactive_initiative_quiet_end_hour",
        "proactive_initiative_max_per_day",
        "proactive_initiative_min_gap_seconds",
        "proactive_initiative_greeting_enabled",
        "proactive_initiative_greeting_slots",
    ):
        assert key in settings_schema.SAVEABLE_KEYS, f"{key} 不是可保存键（界面存不下去）"

    gone = set(settings_schema.SAVEABLE_KEYS)
    for zombie in (
        "icebreaker_idle_seconds",
        "icebreaker_hold_seconds",
        "icebreaker_dormant_enabled",
        "icebreaker_dormant_seconds",
        "proactive_topics",
    ):
        assert zombie not in gone, f"{zombie} 又回来了 —— 那是 2026-09-29 删除的破冰键"


def test_default_is_off(tmp_path):
    """新功能不许偷偷开着：默认配置下一条都不会发。"""
    service = _service(_plugin(tmp_path, enabled=False))
    from plugin.plugins.qq_auto_reply import settings_schema

    spec = settings_schema.BY_KEY["proactive_initiative_enabled"]
    assert spec.default is False, "默认值不是关"
    assert service.enabled() is False


# ── 8. 界面契约：napcat 面板上的那张卡 ──────────────────────────────


def _page() -> str:
    from pathlib import Path

    import plugin.plugins.qq_auto_reply as pkg

    return (Path(pkg.__file__).resolve().parent / "static" / "napcat.html").read_text(encoding="utf-8")


def test_the_panel_has_the_initiative_card():
    text = _page()
    for element in (
        "cfg-initiative-on",
        "cfg-initiative-quiet-start",
        "cfg-initiative-quiet-end",
        "cfg-initiative-max-per-day",
        "cfg-initiative-min-gap-hours",
        "initiative-status",
        "cfg-initiative-greeting-enabled",
        "initiative-slots",
    ):
        assert f'id="{element}"' in text, f"面板缺控件 {element}"
    assert "saveInitiativeSettings()" in text, "卡片上没有保存入口"


def test_initiative_card_is_immediately_before_normal_relay_in_params():
    from html.parser import HTMLParser

    class CardsParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.divs = []
            self.cards = []

        def handle_starttag(self, tag, attrs):
            if tag != "div":
                return
            attrs = dict(attrs)
            if "card" in attrs.get("class", "").split():
                self.cards.append((attrs.get("id"), tuple(self.divs)))
            self.divs.append(attrs.get("id"))

        def handle_endtag(self, tag):
            if tag == "div":
                self.divs.pop()

    text = _page()
    parser = CardsParser()
    parser.feed(text)
    initiative = [i for i, (card, _) in enumerate(parser.cards) if card == "initiative-card"]
    assert len(initiative) == 1
    index = initiative[0]
    _, parents = parser.cards[index]
    assert parents[-1] == "page-config-params"
    assert "page-config-accounts" not in parents
    assert parser.cards[index + 1] == ("normal-relay-card", parents)
    assert text.count('data-i18n="ui.initiative.title"') == 1
    for control in (
        "cfg-initiative-on",
        "cfg-initiative-quiet-start",
        "cfg-initiative-quiet-end",
        "cfg-initiative-max-per-day",
        "cfg-initiative-min-gap-hours",
        "initiative-status",
    ):
        assert text.count(f'id="{control}"') == 1


def test_the_panel_saves_all_initiative_keys():
    """界面提交的键必须包含原有搭话和定时问候设置 —— 少一个就是"能改但存不下去"。"""
    text = _page()
    assert text.count("...initiativeSettingsPayload()") == 2, "两个保存入口必须共用载荷"
    block = text[text.index("function initiativeSettingsPayload") :]
    block = block[: block.index("\n}")]
    for key in (
        "proactive_initiative_enabled",
        "proactive_initiative_quiet_start_hour",
        "proactive_initiative_quiet_end_hour",
        "proactive_initiative_max_per_day",
        "proactive_initiative_min_gap_seconds",
        "proactive_initiative_greeting_enabled",
        "proactive_initiative_greeting_slots",
    ):
        assert key in block, f"保存时没带 {key}"


def test_the_panel_shows_the_backend_verdict_instead_of_recomputing_it():
    """界面只显示后端算好的 `blocked_reason` —— 别在前端再写一份判据。"""
    text = _page()
    assert "action:'proactive_initiative'" in text.replace(" ", ""), "没有去查状态"
    assert "blocked_reason" in text, "没有显示'为什么还没开口'"
    # 前端不许自己判"静音时段"（那是后端的事）
    assert "quiet_hours" not in text, "前端又算了一遍静音时段"


def test_every_initiative_label_exists_in_both_bundles():
    import json
    from pathlib import Path

    import plugin.plugins.qq_auto_reply as pkg

    base = Path(pkg.__file__).resolve().parent / "i18n"
    zh = json.loads((base / "zh-CN.json").read_text(encoding="utf-8"))
    en = json.loads((base / "en.json").read_text(encoding="utf-8"))
    keys = {key for key in zh if key.startswith("ui.initiative.")}
    assert keys, "一个主动搭话的文案键都没有"
    assert keys == {key for key in en if key.startswith("ui.initiative.")}, "两个语种键集合不一致"
    for required in ("title", "hint", "enabled", "save", "blocked", "pending"):
        assert f"ui.initiative.{required}" in zh, f"缺键 {required}"


# ── 每日定时问候：显式开启、共享额度、先落盘再发送 ────────────────


@pytest.fixture
def greeting_clock(monkeypatch):
    clock = [datetime(2026, 11, 8, 9, 0, 30).timestamp()]
    def now():
        return clock[0]
    patch_module_clock(monkeypatch, initiative_module, time=now)
    patch_module_clock(monkeypatch, budget_module, time=now)
    return clock


def _greeting_plugin(tmp_path, **kwargs):
    settings = {
        "proactive_initiative_greeting_enabled": True,
        "proactive_initiative_greeting_hour": 9,
        "proactive_initiative_greeting_minute": 0,
    }
    settings.update(kwargs.pop("settings", {}))
    return _plugin(tmp_path, settings=settings, **kwargs)


def test_greeting_needs_no_promise_and_survives_restart(tmp_path, greeting_clock):
    plugin = _greeting_plugin(tmp_path)
    service = plugin.proactive_initiative_service
    assert service.pending() is None
    asyncio.run(service.tick())
    assert len(plugin.sent) == 1
    assert plugin.sent[0]["target"] == ADMIN
    assert "09:00" in plugin.sent[0]["message"]
    assert "问候" in plugin.sent[0]["message"]
    assert plugin.sent[0]["verbatim"] is False  # use the character reply pipeline
    again = QQProactiveInitiativeService(plugin)
    asyncio.run(again.tick())
    assert len(plugin.sent) == 1
    assert again.snapshot()["sent_today"] == 1
    assert again.snapshot()["greeting"]["last_attempt_day"] == "2026-11-08"
    # Changing the time or toggling the feature does not cause another greeting today.
    plugin._qq_settings["proactive_initiative_greeting_minute"] = 1
    greeting_clock[0] += 60
    asyncio.run(again.tick())
    assert len(plugin.sent) == 1
    greeting_clock[0] = datetime(2026, 11, 9, 9, 1, 30).timestamp()
    asyncio.run(again.tick())
    assert len(plugin.sent) == 1  # continuously online: wait for the new day's random target
    greeting_clock[0] = again.snapshot()["greeting"]["slots"][0]["planned_at"]
    asyncio.run(again.tick())
    assert len(plugin.sent) == 2


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"enabled": False}, "总开关"),
        ({"running": False}, "值班已停"),
        ({"admin": ""}, "还没有管理员"),
        ({"settings": {"proactive_initiative_greeting_enabled": False}}, "未开启"),
        ({"settings": {"proactive_initiative_quiet_start_hour": 8, "proactive_initiative_quiet_end_hour": 10}}, "静音"),
        ({"settings": {"proactive_initiative_max_per_day": 0}}, "已经说够"),
    ],
)
def test_greeting_respects_switches_admin_and_limits(tmp_path, greeting_clock, kwargs, reason):
    plugin = _greeting_plugin(tmp_path, **kwargs)
    assert reason in plugin.proactive_initiative_service.greeting_blocked_reason()
    asyncio.run(plugin.proactive_initiative_service.tick())
    assert plugin.sent == []


@pytest.mark.parametrize("offset, expected", [(-1, 0), (0, 1), (299, 1), (300, 0), (3600, 0)])
def test_greeting_window_does_not_backfill(tmp_path, greeting_clock, offset, expected):
    greeting_clock[0] = datetime(2026, 11, 8, 9).timestamp() + offset
    plugin = _greeting_plugin(tmp_path)
    asyncio.run(plugin.proactive_initiative_service.tick())
    assert len(plugin.sent) == expected


def test_greeting_midnight_and_minute(tmp_path, greeting_clock):
    greeting_clock[0] = datetime(2026, 11, 9, 0, 17, 20).timestamp()
    plugin = _greeting_plugin(
        tmp_path, settings={"proactive_initiative_greeting_hour": 0, "proactive_initiative_greeting_minute": 17}
    )
    asyncio.run(plugin.proactive_initiative_service.tick())
    assert len(plugin.sent) == 1
    assert plugin.proactive_initiative_service.snapshot()["greeting"]["time"] == "00:17"


@pytest.mark.parametrize("blocker", ["budget", "gap", "activity"])
def test_greeting_shares_budget_gap_and_recent_activity(tmp_path, greeting_clock, blocker):
    plugin = _greeting_plugin(tmp_path)
    service = plugin.proactive_initiative_service
    if blocker == "activity":
        service._last_admin_activity_at = greeting_clock[0] - 1
    elif blocker == "gap":
        service.record_send("deepchat", greeting_clock[0] - 1)
    else:
        plugin._qq_settings["proactive_initiative_max_per_day"] = 1
        service.record_send("initiative", greeting_clock[0] - 20000)
    asyncio.run(service.tick())
    assert plugin.sent == []


def test_greeting_concurrent_ticks_only_send_once(tmp_path, greeting_clock):
    plugin = _greeting_plugin(tmp_path)
    service = plugin.proactive_initiative_service

    async def send(**kwargs):
        await asyncio.sleep(0)
        plugin.sent.append(kwargs)
        return Ok({"status": "sent"})

    plugin.proactive_message_service.send_private_message = send

    async def run():
        await asyncio.gather(service.tick(), service.tick(), service.tick())

    asyncio.run(run())
    assert len(plugin.sent) == 1
    assert service.snapshot()["sent_today"] == 1


@pytest.mark.parametrize("error, charged", [("GENERATE_FAILED: empty", 0), ("SEND_FAILED: timeout", 1)])
def test_greeting_failures_are_not_retried(tmp_path, greeting_clock, error, charged):
    plugin = _greeting_plugin(tmp_path, send_result=Err(SdkError(error)))
    asyncio.run(plugin.proactive_initiative_service.tick())
    again = QQProactiveInitiativeService(plugin)
    asyncio.run(again.tick())
    assert len(plugin.sent) == 1
    assert again.snapshot()["sent_today"] == charged


def test_greeting_disk_failure_prevents_send(tmp_path, greeting_clock, monkeypatch):
    plugin = _greeting_plugin(tmp_path)
    service = plugin.proactive_initiative_service
    monkeypatch.setattr(service, "_save", lambda: False)
    asyncio.run(service.tick())
    assert plugin.sent == []
    assert service.snapshot()["sent_today"] == 0
    assert not service._greeting_attempt_days


def test_greeting_does_not_clear_existing_promise(tmp_path, greeting_clock):
    plugin = _greeting_plugin(tmp_path)
    service = plugin.proactive_initiative_service
    _turn(service)
    service._items[ADMIN]["due_at"] = greeting_clock[0] - 60
    service._last_admin_activity_at = 0
    asyncio.run(service.tick())
    assert len(plugin.sent) == 1
    assert service.pending() is not None
    assert service.blocked_reason() == "距上次太近"


def test_greeting_settings_reach_dashboard_and_plain_settings(tmp_path):
    from unittest.mock import AsyncMock

    from plugin.plugins.qq_auto_reply import settings_schema
    from plugin.plugins.qq_auto_reply.dashboard_service import QQDashboardService
    from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService

    plugin = _plugin(tmp_path)
    values = {
        "proactive_initiative_greeting_enabled": True,
        "proactive_initiative_greeting_hour": 6,
        "proactive_initiative_greeting_minute": 35,
        "proactive_initiative_enabled": True,
    }
    service = QQSettingsService(plugin)
    captured = {}

    async def save(**kwargs):
        captured.update(kwargs)
        service._apply_plain_settings(kwargs)
        return {"persisted": True}

    plugin.settings_service = SimpleNamespace(save_settings=save)
    dashboard = QQDashboardService(plugin)
    dashboard.build_dashboard_state = AsyncMock(return_value={"business_config": {}})
    result = asyncio.run(dashboard.save_settings(**values))
    assert isinstance(result, Ok)
    for key, value in values.items():
        assert key in settings_schema.SAVEABLE_KEYS
        assert key in settings_schema.input_schema_properties()
        assert captured[key] == value
        assert plugin._qq_settings[key] == value
    service._apply_plain_settings(
        {"proactive_initiative_greeting_hour": 99, "proactive_initiative_greeting_minute": -1}
    )
    assert plugin._qq_settings["proactive_initiative_greeting_hour"] == 23
    assert plugin._qq_settings["proactive_initiative_greeting_minute"] == 0


def test_greeting_cancelled_delivery_is_not_repeated(tmp_path, greeting_clock):
    plugin = _greeting_plugin(tmp_path)
    service = plugin.proactive_initiative_service

    async def send(**kwargs):
        plugin.sent.append(kwargs)
        raise asyncio.CancelledError()

    plugin.proactive_message_service.send_private_message = send
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.tick())
    again = QQProactiveInitiativeService(plugin)
    asyncio.run(again.tick())
    assert len(plugin.sent) == 1
    assert again.snapshot()["sent_today"] == 1


def test_old_state_without_greeting_data_loads(tmp_path, greeting_clock):
    (tmp_path / STATE_FILE).write_text(json.dumps({"items": {}}), encoding="utf-8")
    plugin = _greeting_plugin(tmp_path)
    asyncio.run(plugin.proactive_initiative_service.tick())
    assert len(plugin.sent) == 1


def test_greeting_uses_maintenance_heartbeat(tmp_path, greeting_clock):
    plugin = _greeting_plugin(tmp_path)
    plugin.attention_service = SimpleNamespace(
        _current_time=lambda: greeting_clock[0],
        _normalized_groups=lambda: [],
    )
    asyncio.run(QQAttentionGateService(plugin).run_maintenance_tick())
    assert len(plugin.sent) == 1


