from types import SimpleNamespace

from plugin.plugins.qq_auto_reply import addressing
from plugin.plugins.qq_auto_reply.group_dialogue_guidance import (
    group_message_with_context,
)
from plugin.plugins.qq_auto_reply.prompt_builder import QQPromptBuilder
from plugin.plugins.qq_auto_reply.prompting import QQAutoReplyPromptingMixin


def build(message, *, sender="242817150", name="Hy", target="1919071715", context=""):
    verdict = addressing.resolve_addressee(
        self_id="3281414178", mentioned_user_ids=[target] if target else [],
    )
    plugin = SimpleNamespace(
        attention_service=SimpleNamespace(is_first_reply_after_own_speech=lambda gid: True),
        _build_group_turn_message=QQAutoReplyPromptingMixin._build_group_turn_message,
    )
    return QQPromptBuilder(plugin).build_prompt_message(
        is_group=True, group_facing=False, group_scene_mode="group_collective",
        user_title=name, sender_id=sender, group_id="1048307485",
        message=group_message_with_context(message, context), addressee=verdict,
        member_labels={"242817150": "Hy", "1919071715": "诺里", "3631688034": "汐祈"},
    )


def test_song_request_to_nori_is_not_reassigned_by_recent_bot_speech():
    prompt = build("我下班啦，唱首歌吧，来首下等马")
    assert "发言人: Hy（QQ: 242817150）" in prompt
    assert "对方在跟诺里说话" in prompt
    assert "本条消息指向的人: 诺里（QQ: 1919071715）" in prompt
    assert "可能在接你的话" not in prompt
    assert prompt.endswith("我下班啦，唱首歌吧，来首下等马")


def test_visual_greeting_remains_attributed_to_nori_and_is_not_current_claim():
    context = "Hy: 我下班啦\n诺里: [Image 卡通角色挥手，配‘早’字]"
    prompt = build("月亮出来了", sender="2276707102", name="狼外婆", target="", context=context)
    boundary = "【以上资料结束；以下才是当前发言人的本条消息】"
    assert prompt.index(context) < prompt.index(boundary)
    assert prompt.split(boundary)[1] == "\n月亮出来了"
    assert "图里写‘早’不代表现在是早上" in prompt
    assert "哭脸不证明本人难过" in prompt


def test_nested_first_person_stays_in_quote_not_current_speaker():
    context = "诺里: 本喵负责抱狐狐\n拾雨: 回头我记账也照这么来"
    prompt = build("抱抱抵账也行？", context=context)
    assert context in prompt
    assert "各句属于其原发言人，不是当前发言人的自述" in prompt
    assert prompt.endswith("抱抱抵账也行？")


def test_clear_explanation_request_keeps_actual_target():
    prompt = build("seki解释捏", target="3631688034", context="群友: chat酱是什么？")
    assert "对方在跟汐祈说话" in prompt
    assert "请求已明确就直接处理" in prompt
    assert "没有新内容时，可以不发消息" in prompt


def test_no_context_keeps_original_input():
    assert group_message_with_context("[Image 早]", "") == "[Image 早]"
