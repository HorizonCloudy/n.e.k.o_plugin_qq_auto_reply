"""DeepSeek 原生工具调用标记的剥离。

V4.1 不总是走 OpenAI 的 ``tool_calls``。在工具额度已经用光、或宿主的工具
通道被摘掉的那一轮，它会把调用**直接当正文**吐出来：

.. code-block:: text

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="set_avatar">
    <｜｜DSML｜｜ parameter name="id" string="true">g9</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ invoke>
    </｜｜DSML｜｜ calls>

这些不是给人看的字。留着就是一条"内容是标记"的消息发到对方手机上
（2026-11-01 真实发生过：对方收到一条只有闭合标签的消息），而那一轮她
真正想做的事——换个头像——也一起没了。

同一套剥离要用在两个地方，所以提成独立模块：

* **发送侧**（``prompting._sanitize_generated_reply``）：保住发出去的是话。
* **记忆侧**（``session_memory_service._strip_internal_markup``）：否则她
  "记得"的自己发言是一堆标记，下一轮接不上自己说过什么。

## 为什么正则写得这么松

写死一种形状必然漏，实测的变体：

* 管道符号不止 ``|`` (U+007C)，还有 ``｜`` (U+FF5C 全角) 和 ``‖`` (U+2016)
* 会带前导空格：``< ｜DSML｜ calls>``，要求 ``<`` 后紧跟管道就匹配不到
* 闭合标签是 ``</｜DSML｜ invoke>``，斜杠卡在 DSML 前面
* 标签家族不止 ``calls``，还有 ``invoke`` / ``parameter`` / ``tool_calls``

所以写成"变体无关"：可选斜杠 + 可选空白 + 可选管道(1-2 个) + 可选空白 +
字面 ``DSML``（大小写不敏感）+ 到 ``>`` 为止的任意内容。只要标记里含 DSML
字样且被尖括号包住，就整体删掉。
"""

from __future__ import annotations

import re

#: ``<…DSML…parameter…>…</…DSML…parameter>``：参数块连里面的值一起删。
#:
#: 顺序不能反。只删标记的话参数值会孤零零留下来——``set_avatar`` 那次剥完
#: 正文里剩一个 ``g9``，看着像她说了个莫名其妙的词。
_DSML_PARAM_BLOCK_RE = re.compile(
    r"<[^<>]*DSML[^<>]*parameter[^<>]*>.*?</[^<>]*DSML[^<>]*parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)

_DSML_MARKER_RE = re.compile(
    r"</?\s*"
    r"(?:[|\uff5c\u2016]{1,2}\s*)?"
    r"DSML"
    r"(?:\s*[|\uff5c\u2016]{1,2})?"
    r"(?:\s+[^>]*)?"
    r"\s*>",
    re.IGNORECASE,
)

#: 剥完可能剩下被标记撑开的空行和行尾空白。
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def strip_dsml_markers(text: str) -> str:
    """删掉漏进正文的 DSML 工具调用标记，返回剩下的部分。

    没有 ``DSML`` 字样就直接原样返回：绝大多数回复走这条快路，不该为一次
    两次事故给每一句都跑两遍正则。

    只做删除，不改写别的东西——这层不负责润色，只负责去掉不属于她的话的
    部分。整段都是标记时返回空串，调用方据此走"这条没有正文"的分支。
    """
    if not text or "DSML" not in text.upper():
        return text
    cleaned = _DSML_PARAM_BLOCK_RE.sub("", text)
    cleaned = _DSML_MARKER_RE.sub("", cleaned)
    cleaned = _TRAILING_SPACE_RE.sub("\n", cleaned)
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned)
    return cleaned.strip()
