"""P3 烂梗/低质内容守卫：行为契约测试。

只断言 ``is_bad_meme`` 的对外行为，不断言内部正则写法——
实现可以换正则、换词表结构，只要行为不变。
"""

from __future__ import annotations

import unittest

from deepchat.bad_meme_words import BAD_MEME_KEYWORDS, is_bad_meme

#: 契约要求词表至少覆盖的低质/烂梗/冒犯说法。
REQUIRED_KEYWORDS: tuple[str, ...] = (
    "家人们谁懂啊",
    "绝绝子",
    "yyds",
    "awsl",
    "栓Q",
    "尊嘟假嘟",
    "666",
    "绷不住了",
    "下头",
    "普信",
    "舔狗",
    "海王",
    "绿茶",
    "键盘侠",
    "杠精",
    "韭菜",
    "智商税",
)

#: 明显的低俗/辱骂用语，用于验证"正则/冒犯"这条分支而不是词表命中。
PROFANE_SAMPLES: tuple[str, ...] = (
    "你他妈在说什么",
    "傻逼吧你",
)


class BadMemeKeywordTableTests(unittest.TestCase):
    def test_keyword_table_is_a_non_empty_tuple(self) -> None:
        self.assertIsInstance(BAD_MEME_KEYWORDS, tuple)
        self.assertTrue(BAD_MEME_KEYWORDS)

    def test_keyword_table_covers_required_entries(self) -> None:
        lowered = {str(item).lower() for item in BAD_MEME_KEYWORDS}
        missing = [word for word in REQUIRED_KEYWORDS if word.lower() not in lowered]
        self.assertEqual(missing, [], f"词表缺少：{missing}")

    def test_every_keyword_is_detected_inside_a_sentence(self) -> None:
        for word in REQUIRED_KEYWORDS:
            with self.subTest(word=word):
                self.assertTrue(
                    is_bad_meme(f"这也太{word}了吧"),
                    f"应判定为烂梗：{word}",
                )


class IsBadMemeTests(unittest.TestCase):
    def test_keyword_hits_are_bad(self) -> None:
        self.assertTrue(is_bad_meme("家人们谁懂啊"))
        self.assertTrue(is_bad_meme("这也太绝绝子了吧"))

    def test_keyword_match_is_case_insensitive(self) -> None:
        self.assertTrue(is_bad_meme("YYDS"))
        self.assertTrue(is_bad_meme("Yyds"))
        self.assertTrue(is_bad_meme("AWSL"))
        self.assertTrue(is_bad_meme("栓q"))

    def test_profane_or_offensive_text_is_bad(self) -> None:
        for sample in PROFANE_SAMPLES:
            with self.subTest(sample=sample):
                self.assertTrue(is_bad_meme(sample), f"应判定为冒犯：{sample}")

    def test_offensive_slang_is_bad(self) -> None:
        for word in ("舔狗", "海王", "绿茶", "键盘侠", "杠精", "韭菜", "智商税"):
            with self.subTest(word=word):
                self.assertTrue(is_bad_meme(f"他就是个{word}"))

    def test_empty_and_blank_are_bad(self) -> None:
        self.assertTrue(is_bad_meme(""))
        self.assertTrue(is_bad_meme("   "))
        self.assertTrue(is_bad_meme("\n\t "))
        self.assertTrue(is_bad_meme(None))

    def test_punctuation_only_is_bad(self) -> None:
        self.assertTrue(is_bad_meme("！！！？？？"))
        self.assertTrue(is_bad_meme("。。。"))
        self.assertTrue(is_bad_meme("……——"))

    def test_emoji_only_is_bad(self) -> None:
        self.assertTrue(is_bad_meme("😂😂😂"))
        self.assertTrue(is_bad_meme("🙃"))

    def test_repeated_single_char_is_bad(self) -> None:
        self.assertTrue(is_bad_meme("哈哈哈哈哈哈哈哈"))
        self.assertTrue(is_bad_meme("啊啊啊啊啊啊啊"))

    def test_normal_text_is_not_bad(self) -> None:
        for text in (
            "今天加班好累",
            "晚上吃什么",
            "这个方案我明天再改一版",
            "你昨天说的那个电影叫什么来着",
            "路上有点堵，会晚到十分钟",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_bad_meme(text), f"误判为烂梗：{text}")

    def test_normal_text_with_trailing_punctuation_is_not_bad(self) -> None:
        self.assertFalse(is_bad_meme("今天加班好累……"))
        self.assertFalse(is_bad_meme("晚上吃什么？"))


class BadMemeFalsePositiveTests(unittest.TestCase):
    """S2：词表按子串命中会误伤正常聊天（家人称呼、金额、菜名）。"""

    #: 都是正常对话，命中说明判定在按子串硬匹配。
    BENIGN_SAMPLES: tuple[str, ...] = (
        "你妈妈最近身体好吗",
        "老妈的生日快到了",
        "花了1666块买这个",
        "订单号是666123",
        "绿茶蛋糕挺好吃的",
        "海底捞的韭菜盒子",
    )

    def test_benign_chat_is_not_flagged(self) -> None:
        for text in self.BENIGN_SAMPLES:
            with self.subTest(text=text):
                self.assertFalse(is_bad_meme(text), f"误伤正常文本：{text}")

    def test_obvious_cases_are_still_flagged(self) -> None:
        self.assertTrue(is_bad_meme("666"))
        self.assertTrue(is_bad_meme("你他妈在说什么"))
        self.assertTrue(is_bad_meme("傻逼吧你"))


if __name__ == "__main__":
    unittest.main()
