"""烂梗 / 低质内容识别。

纯函数、无 IO、无模型调用：既能当表达学习（P2）的过滤器，也能被其它
模块当作"这句话值不值得学"的廉价前置判断。
"""

from __future__ import annotations

import re
from collections import Counter

#: 网络烂梗与贴标签式说法。命中即视为不该学、不该模仿的文本。
BAD_MEME_KEYWORDS: tuple[str, ...] = (
    "家人们谁懂啊",
    "绝绝子",
    "yyds",
    "awsl",
    "栓Q",
    "尊嘟假嘟",
    "666",
    "绷不住了",
    "退退退",
    "无语子",
    "我真的会谢",
    "泰裤辣",
    "挖呀挖",
    "小趴菜",
    "显眼包",
    "下头",
    "普信",
    "舔狗",
    "海王",
    "绿茶",
    "键盘侠",
    "杠精",
    "韭菜",
    "智商税",
    "白莲花",
    "圣母婊",
)

#: 明显低俗/辱骂用语。词表之外单独一条正则，避免把整段脏话写进词表。
#: ``你妈`` 要排除"你妈妈"这种家人称呼；``妈的`` 不收（会误伤"老妈的生日"）。
_PROFANE_PATTERN = re.compile(
    r"傻逼|傻b|煞笔|他妈|你妈(?!妈)|滚蛋|去死|贱人|脑残|智障|白痴|神经病",
    re.IGNORECASE,
)

#: 这几个词单独看是日常词（绿茶蛋糕、海底捞的韭菜盒子、1666 块），
#: 只按上下文判定，不参与裸子串匹配。
_CONTEXTUAL_KEYWORDS: frozenset[str] = frozenset({"666", "绿茶", "韭菜"})

#: 上下文/边界匹配：
#: - ``666`` 要数字边界（``1666``、``666123`` 不是烂梗）；
#: - ``绿茶``/``韭菜`` 只有在被当作人的评价时才成立（``他就是个绿茶``、
#:   ``这也太韭菜了``），跟着菜名走（``绿茶蛋糕``）不算。
_CONTEXTUAL_PATTERN = re.compile(
    r"(?<!\d)666(?!\d)"
    r"|(?:是|当|做|被|成|太|很|真|个|这种|就是|妥妥)(?:个)?(?:绿茶|韭菜)"
    r"|(?:绿茶|韭菜)(?:男|女|婊|精|逼|思维|行为|命)",
    re.IGNORECASE,
)

#: 裸子串匹配的关键词（排除需要上下文判定的那几个）。
_PLAIN_KEYWORDS: tuple[str, ...] = tuple(
    word.lower() for word in BAD_MEME_KEYWORDS if word not in _CONTEXTUAL_KEYWORDS
)

#: 长度达到这个值才检查"单字复读"，避免误杀"哈哈""呜呜"这类短回声。
_REPEAT_MIN_LEN = 6

#: 单一字符占比超过这个比例即视为无信息复读。
_REPEAT_RATIO = 0.7


def _is_symbol(ch: str) -> bool:
    """True for punctuation / emoji / whitespace — anything not a word char."""
    return not ch.isalnum()


def is_bad_meme(text: object) -> bool:
    """True when ``text`` is noise, a meme, or an outright insult.

    The order matters: cheap structural checks first, then the keyword table,
    then the single-character-repetition heuristic. Everything here is
    substring/heuristic work — no tokenizer, no network, no model.

    词表分两档：大部分按裸子串判定；``666``/``绿茶``/``韭菜`` 这类日常词只按
    **上下文**判定，否则「花了 1666 块」「绿茶蛋糕」「海底捞的韭菜盒子」都会被误伤。
    """
    raw = str(text or "").strip()
    if not raw:
        return True
    lowered = raw.lower()
    if any(word in lowered for word in _PLAIN_KEYWORDS):
        return True
    if _CONTEXTUAL_PATTERN.search(raw):
        return True
    if _PROFANE_PATTERN.search(raw):
        return True
    meaningful = [ch for ch in raw if not ch.isspace()]
    # 走到这里 meaningful 一定非空：纯空白在前面就返回了。
    # 纯标点 / 纯 emoji：没有可学的字面内容。
    if all(_is_symbol(ch) for ch in meaningful):
        return True
    if len(meaningful) >= _REPEAT_MIN_LEN:
        top = Counter(meaningful).most_common(1)[0][1]
        if top / len(meaningful) > _REPEAT_RATIO:
            return True
    return False
