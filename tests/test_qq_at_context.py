"""@ 场景下"谁是谁"必须进提示词。

真机症状：别人 @ 她，她回得很突兀 —— 因为提示词里除当前发言人外**谁都没有名字**：
被 @ 的人一律被 `addressing.aim_label` 渲染成"别人"，她想 @ 别人时也没有号可填
（她的 `<at>` 段要写裸 QQ 号，见 `reply_postprocess_node`）。

两条不变量：
1. 有名字时必须说人名；
2. **没名字时必须逐字退回改动前的样子** —— 那是所有既有测试与既有行为的回归网，
   绝不为了让新功能好看而改写老输出。
"""

from __future__ import annotations

from plugin.plugins.qq_auto_reply import addressing
from plugin.plugins.qq_auto_reply.prompting import (
    QQAutoReplyPromptingMixin,
    _render_people_block,
)

GROUP = "888"


def _turn(**overrides) -> str:
    kwargs = dict(
        group_scene_mode="shared_context",
        user_title="小明",
        sender_id="111",
        group_id=GROUP,
        message="在吗",
    )
    kwargs.update(overrides)
    return QQAutoReplyPromptingMixin._build_group_turn_message(**kwargs)


def _at_other() -> addressing.AddresseeVerdict:
    return addressing.AddresseeVerdict(
        kind=addressing.KIND_AT_OTHER, target_id="222",
    )


# ── 回归网：没有名字时一个字节都不许变 ──────────────────────────

def test_no_labels_keeps_speaker_and_message_with_context_guidance():
    out = _turn(
        current_message_id="m1",
        group_scene_mode="directed_user",
    )
    assert out == (
        "当前消息ID: m1\n"
        "发言人: 小明（QQ: 111）\n"
        "群号: 888\n"
        "【这条消息是冲你来的】对方直接@了你，在对你说话。你应该回复。\n"
        "接话时先分清谁对谁说：称呼、身份和经历归原发言人，别把别人的‘你’或称号认成自己。"
        "承接已经说出的具体内容；对方答过的问题不要换个问法继续追问，也别把新话题硬拉回口头禅。\n"
        "消息:\n在吗"
    )


def test_empty_labels_dict_is_treated_as_no_labels():
    assert _turn(member_labels={}) == _turn()
    assert _turn(member_labels=None) == _turn()


def test_people_block_is_empty_without_labels():
    assert _render_people_block(labels={}, sender_id="111", aim_target="") == ""


def test_hint_still_says_someone_when_name_is_unknown():
    """名字拿不到时退回"别人"，且**不许**把裸 QQ 号摆进提示词。"""
    out = _turn(addressee=_at_other())
    assert "对方在跟别人说话" in out
    assert "QQ: 222" not in out


# ── ① 有名字 → 说人名 ────────────────────────────────────────

def test_at_other_hint_names_the_person():
    out = _turn(addressee=_at_other(), member_labels={"111": "小明", "222": "小红"})
    assert "对方在跟小红说话" in out
    assert "对方在跟别人说话" not in out


def test_people_block_lists_everyone_with_their_qq():
    out = _turn(member_labels={"111": "小明", "222": "小红"})
    assert "本条消息涉及的人:" in out
    assert "发言人: 小明（QQ: 111）" in out
    assert "本条消息里被 @ / 被引用的人: 小红（QQ: 222）" in out


def test_the_aim_target_is_labelled_as_such():
    out = _turn(addressee=_at_other(), member_labels={"111": "小明", "222": "小红"})
    assert "本条消息指向的人: 小红（QQ: 222）" in out


def test_speaker_wins_over_aim_target_for_the_role_label():
    """她自己被 @（target 指向她）时，别把发言人标成"指向的人"。"""
    block = _render_people_block(
        labels={"111": "小明"}, sender_id="111", aim_target="111",
    )
    assert "发言人: 小明（QQ: 111）" in block
    assert "本条消息指向的人" not in block


# ── ② 她 @ 别人：给出能填的号 + 保守指令 ──────────────────────

def test_people_block_teaches_the_at_syntax_with_a_real_number():
    block = _render_people_block(
        labels={"111": "小明", "222": "小红"},
        sender_id="111",
        aim_target="222",
    )
    assert "<at>对方的QQ号</at>" in block
    # 名单本身就是"能填的号"，否则那句指令是空头支票
    assert "小红（QQ: 222）" in block


def test_people_block_tells_her_not_to_guess():
    block = _render_people_block(labels={"111": "小明"}, sender_id="111", aim_target="")
    assert "不确定号码就不要 @" in block


def test_blank_names_are_dropped_not_rendered_as_empty_rows():
    out = _turn(member_labels={"111": "小明", "222": "   ", "": "无名"})
    assert "QQ: 222" not in out
    assert "无名" not in out
