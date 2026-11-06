"""DSML 工具调用标记的剥离：必须覆盖真实见过的每一种变体。

为什么需要这个测试（这不是假想的风险，是已经发生的两次）：

1. **2026-11-01 13:13**：对方收到一条内容只有闭合标签的消息——

       </｜｜DSML｜｜ invoke> </｜｜DSML｜｜ calls>

2. **2026-11-05 21:43**：对方让换个头像，她"答应了"，但什么都没发生。
   抓到的原始输出是——

       <｜｜DSML｜｜ calls>
       <｜｜DSML｜｜ invoke name="set_avatar">
       <｜｜DSML｜｜ parameter name="index" string="true">g9</｜｜DSML｜｜ parameter>
       </｜｜DSML｜｜ invoke>
       </｜｜DSML｜｜ calls>

   她把工具调用写成了正文（工具额度只有一次，先 browse_gallery 用光了），
   宿主不认那是工具调用，清洗层把它剥成空——调用没执行，一个字也没发出去。

两次都是同一层的问题：**模型不遵守 tool_calls 协议是常态，清洗必须按
"它可能乱来"写**。这个测试就是那份"乱来"的清单。

判据：只要标记里含 DSML 字样且被尖括号包住，就整体删掉；壳里的字留着。
"""
from __future__ import annotations

import pytest
from plugin.plugins.qq_auto_reply.dsml_markers import strip_dsml_markers

#: 真实抓到的 set_avatar 调用原文（含孤零零的参数块）。
SET_AVATAR_CALL = (
    "<｜｜DSML｜｜ calls>\n"
    '<｜｜DSML｜｜ invoke name="set_avatar">\n'
    '<｜｜DSML｜｜ parameter name="index" string="true">g9</｜｜DSML｜｜ parameter>\n'
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>"
)


def test_pure_call_becomes_empty() -> None:
    """整段都是调用 → 空串。调用方据此走"这一轮她没说话"的分支。"""
    assert strip_dsml_markers(SET_AVATAR_CALL) == ""


def test_param_value_does_not_leak() -> None:
    """参数值必须跟着参数块一起走。

    只删标记的话，剥完正文里会剩一个孤零零的 `g9`——看着像她说了个莫名其妙的词。
    """
    assert "g9" not in strip_dsml_markers(SET_AVATAR_CALL)


def test_body_before_call_survives() -> None:
    """调用前后她真说的话要留下。"""
    assert strip_dsml_markers("换好了喵。\n" + SET_AVATAR_CALL) == "换好了喵。"


def test_text_inside_invoke_shell_survives() -> None:
    """`invoke` 壳里的正文是正文，不是标记。"""
    raw = '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="msg">还是403'
    assert strip_dsml_markers(raw) == "还是403"


def test_the_2026_11_01_incident() -> None:
    """历史事故原文，必须剥干净。"""
    assert strip_dsml_markers("</｜｜DSML｜｜ invoke> </｜｜DSML｜｜ calls>") == ""


@pytest.mark.parametrize(
    "variant",
    [
        "<｜DSML｜ calls>",          # 单个全角管道
        "<|DSML| calls>",           # 单个半角管道
        "<‖DSML‖ invok>",           # 双竖线 U+2016
        "< ｜DSML｜ calls>",         # 管道前带空格
        "<｜DSML｜ calls >",         # 管道后带空格、右尖括号前带空格
        "<dsml calls>",             # 小写
        "<｜｜DSML｜｜ tool_calls>",  # 标签家族不止 calls
    ],
)
def test_variants(variant: str) -> None:
    """管道符号/空白/大小写/标签家族的各种组合都要认。"""
    assert strip_dsml_markers(variant) == ""


def test_ordinary_message_untouched() -> None:
    """正常消息一个字都不能动（这层只删，不改写）。"""
    raw = "<msg><text>行啊，你想给我换个鱼骨头？</text></msg>"
    assert strip_dsml_markers(raw) == raw


def test_mentioning_dsml_in_plain_speech_is_kept() -> None:
    """有人在话里提到 DSML 这几个字母，不能被当成标记删掉。

    判据要求"被尖括号包住"，就是为了这一条。
    """
    raw = "DSML 是什么东西啊"
    assert strip_dsml_markers(raw) == raw


def test_no_dsml_returns_input_unchanged() -> None:
    """没有 DSML 字样就走快路，原样返回（每句话都跑两遍正则没必要）。"""
    raw = "刚刷到个做筋角弓的，十四分钟光看手工就过去了喵。"
    assert strip_dsml_markers(raw) == raw


def test_empty_input() -> None:
    assert strip_dsml_markers("") == ""
