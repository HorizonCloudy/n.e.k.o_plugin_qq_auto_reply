# -*- coding: utf-8 -*-
"""转移话题能力与烂梗筛选（v0.15.14）。

之前只有提示词里一句静态的"话题自检"规则，但**没人替她数轮数**；烂梗则是
完全没判定。这两个文件钉的是判定本身：

* 干涸（绕圈 / 敷衍）→ 本轮贴「转移话题指令」，15 分钟冷却；
* 烂梗表命中 → 本轮贴「别接别复读」，并且**出站拦下**（outbound_guard 第 3 条）；
* 开关关掉 = 一个字都不贴、一条都不拦。
"""

from __future__ import annotations

from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.outbound_guard_service import QQOutboundGuardService
from plugin.plugins.qq_auto_reply.topic_shift_service import (
    HINT_COOLDOWN_SECONDS,
    QQTopicShiftService,
)

GROUP = "1048307485"
T0 = 1_700_000_000.0

MEMES = ["city不city", "你干嘛~哎哟"]


def _plugin(settings=None):
    return SimpleNamespace(
        _qq_settings={"stale_meme_list": list(MEMES), **(settings or {})},
        logger=SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
    )


def _svc(settings=None):
    return QQTopicShiftService(_plugin(settings))


# ── 干涸判定 ───────────────────────────────────────────────────

def test_repeating_the_same_keyword_over_four_messages_is_dry():
    svc = _svc()
    for i, text in enumerate(["王者荣耀好好玩", "王者荣耀上分了", "今天打王者荣耀", "别的不说王者荣耀真难"]):
        svc.observe(key=GROUP, text=text, now=T0 + i)
    dry, why = svc.is_dry(GROUP, now=T0 + 10)
    assert dry
    assert "王者荣耀"[:2] in why or "王者" in why   # 原因里带命中的关键词


def test_fresh_topic_is_not_dry():
    svc = _svc()
    texts = ["猫又掉毛了", "晚饭吃了拉面", "周末去看展", "新电影海报出了"]
    for i, text in enumerate(texts):
        svc.observe(key=GROUP, text=text, now=T0 + i)
    assert svc.is_dry(GROUP, now=T0 + 10) == (False, "")


def test_curt_one_char_replies_are_dry():
    svc = _svc()
    for i, text in enumerate(["聊点别的吧", "嗯", "哦", "哈"]):
        svc.observe(key=GROUP, text=text, now=T0 + i)
    dry, why = svc.is_dry(GROUP, now=T0 + 10)
    assert dry and "敷衍" in why


# ── 本轮指令与冷却 ────────────────────────────────────────────

def test_directive_fires_once_then_cools_down():
    svc = _svc()
    for i, text in enumerate(["好喜欢猫", "猫真可爱", "想买只猫", "猫粮推荐下"]):
        svc.observe(key=GROUP, text=text, now=T0 + i)
    first = svc.turn_directives(key=GROUP, message="还有猫吗", now=T0 + 10)
    assert "【转移话题指令】" in first
    second = svc.turn_directives(key=GROUP, message="猫呢猫呢", now=T0 + 11)
    assert "转移话题指令" not in second            # 冷却内不连环催
    third = svc.turn_directives(
        key=GROUP, message="猫呢猫呢", now=T0 + 10 + HINT_COOLDOWN_SECONDS + 1,
    )
    assert "【转移话题指令】" in third              # 冷却过后还能催（话题还没换）


def test_switch_off_means_no_topic_directive_but_meme_lines_stay():
    # 两个开关各管各的：关掉转移话题，烂梗行不受牵连。
    svc = _svc({"topic_shift_enabled": False})
    for text in ["好喜欢猫", "猫真可爱", "想买只猫", "猫粮推荐下"]:
        svc.observe(key=GROUP, text=text, now=T0)
    assert svc.is_dry(GROUP, now=T0 + 10)[0] is False
    out = svc.turn_directives(key=GROUP, message="猫呢", now=T0 + 10)
    assert "转移话题指令" not in out
    assert "【烂梗表】" in out


# ── 烂梗表 ─────────────────────────────────────────────────────

def test_meme_hit_normalizes_case_and_punctuation():
    svc = _svc()
    assert svc.meme_hit("City不City啊兄弟") == "city不city"
    assert svc.meme_hit("你干嘛 ～ 哎哟！") == "你干嘛~哎哟"
    assert svc.meme_hit("今天天气不错") == ""


def test_single_char_entries_never_match():
    svc = _svc({"stale_meme_list": ["Q", "猫"]})
    assert svc.meme_hit("QQ群里有猫") == ""


def test_meme_switch_off_disables_matching():
    svc = _svc({"stale_meme_enabled": False})
    assert svc.meme_hit("City不City") == ""


def test_directives_carry_list_and_hit_lines():
    svc = _svc()
    out = svc.turn_directives(key=GROUP, message="你干嘛哎哟", now=T0)
    assert "【烂梗表】" in out and "city不city" in out
    assert "【烂梗筛选】" in out and "你干嘛~哎哟" in out


def test_empty_list_adds_nothing():
    svc = _svc({"stale_meme_list": []})
    assert svc.turn_directives(key=GROUP, message="随便聊", now=T0) == ""


# ── 出站拦截（outbound_guard 第 3 条）─────────────────────────

def _guard(settings=None):
    plugin = _plugin(settings)
    plugin.topic_shift_service = QQTopicShiftService(plugin)
    return QQOutboundGuardService(plugin), plugin


def test_outbound_blocks_reply_and_echo_holding_a_stale_meme():
    guard, _ = _guard()
    v = guard.check(group_id=GROUP, text="City不City啊老铁", kind="reply", now=T0)
    assert v.blocked and v.reason == "stale_meme" and v.detail == "city不city"
    v2 = guard.check(group_id=GROUP, text="你干嘛哎哟", kind="echo", now=T0)
    assert v2.blocked and v2.reason == "stale_meme"


def test_outbound_passes_clean_text():
    guard, plugin = _guard()
    plugin.topic_shift_service = QQTopicShiftService(plugin)
    v = guard.check(group_id=GROUP, text="今天去的展挺有意思", kind="reply", now=T0)
    assert v.allowed


def test_guard_survives_missing_service():
    plugin = _plugin()
    guard = QQOutboundGuardService(plugin)      # 没有 topic_shift_service 属性
    assert guard.check(group_id=GROUP, text="City不City", kind="reply", now=T0).allowed


def test_meme_switch_off_passes_outbound():
    guard, _ = _guard({"stale_meme_enabled": False})
    assert guard.check(group_id=GROUP, text="City不City", kind="reply", now=T0).allowed
