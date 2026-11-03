"""P2 轻量表达学习：行为契约测试。

覆盖两层：
- ``deepchat.expression.extract_expressions`` / ``ExpressionStore`` 的纯函数与存储行为；
- ``DeepChatService`` 的开关、每 N 次互动学习、prompt 注入、状态视图与重置。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepchat.bad_meme_words import is_bad_meme
from deepchat.expression import ExpressionStore, extract_expressions
from deepchat.service import DeepChatService
from deepchat.store import DeepChatStore

FRAGMENT_HEADER = "## 群友常用说法（参考，不要生硬模仿）"

#: 学习源的三种可能形态（会话历史 / 直接喂文本）都应能学到这句。
REPEATED_PHRASE = "加班好累"


class FakeConfigStore:
    def __init__(self) -> None:
        self.saved: dict | None = None

    async def save(self, config: dict) -> dict:
        self.saved = dict(config)
        return config


class FakePlugin:
    def __init__(self, data_dir: str) -> None:
        self._qq_settings: dict = {}
        self._data_dir = data_dir
        self._her_name = "兰兰"
        self.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
        self.config_store = FakeConfigStore()
        self.memory_bridge = None
        self._user_sessions: dict = {}
        self.proactive_message_service = None

    def data_path(self) -> Path:
        return Path(self._data_dir)

    def _build_session_key(
        self, *, sender_id: str, is_group: bool = False, group_id: str | None = None
    ) -> str:
        return f"group:{group_id or ''}" if is_group else f"private:{sender_id}"


# --------------------------------------------------------------------------- #
# extract_expressions
# --------------------------------------------------------------------------- #


class ExtractExpressionsTests(unittest.TestCase):
    def test_repeated_phrase_is_extracted(self) -> None:
        result = extract_expressions(
            ["今天加班好累", "加班好累啊", "今天加班好累", "晚上吃什么"]
        )
        self.assertIsInstance(result, list)
        self.assertIn(REPEATED_PHRASE, result, result)

    def test_single_mention_is_dropped_by_min_freq(self) -> None:
        result = extract_expressions(["今天加班好累", "晚上吃什么"])
        self.assertNotIn(REPEATED_PHRASE, result, result)

    def test_bad_memes_are_not_learned(self) -> None:
        result = extract_expressions(["666", "666", "家人们谁懂啊"])
        self.assertNotIn("666", result, result)
        self.assertFalse(
            [item for item in result if is_bad_meme(item)],
            f"烂梗不应进入表达库：{result}",
        )
        self.assertFalse(
            [
                item
                for item in result
                if any(word in item for word in ("666", "家人们谁懂啊", "绝绝子", "yyds"))
            ],
            f"烂梗原句不应整段进入表达库：{result}",
        )

    def test_single_char_fragment_is_filtered(self) -> None:
        result = extract_expressions(["啊", "啊", "啊", "哦", "哦"])
        self.assertNotIn("啊", result, result)
        self.assertNotIn("哦", result, result)

    def test_result_is_capped_at_twenty(self) -> None:
        messages: list[str] = []
        for index in range(25):
            word = f"话题{index:02d}号"
            messages.extend([f"我们聊{word}吧", f"又是{word}"])
        result = extract_expressions(messages)
        self.assertLessEqual(len(result), 20, f"结果超长：{len(result)} 条")
        self.assertTrue(result)

    def test_bad_meme_sentence_yields_no_partial_fragments(self) -> None:
        """S1：n-gram 会把烂梗句切成半截，半截不含完整关键词就会逃过词表。"""
        result = extract_expressions(
            ["家人们谁懂啊", "家人们谁懂啊", "今天加班好累", "今天加班好累"]
        )
        leaked = [
            item for item in result if any(key in item for key in ("家人", "谁懂"))
        ]
        self.assertEqual(leaked, [], f"烂梗句的切片不得入库：{leaked}")
        for fragment in ("家人们谁懂", "人们谁懂啊", "谁懂啊"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, result)
        self.assertIn("加班好累", result, "同批次里的正常短语仍应学到")

    def test_mechanical_two_char_slices_dropped_but_real_words_survive(self) -> None:
        """S1b 的真实意图是「切片噪声不入库」，不是「所有 2 字词都封杀」。

        机械切片（``班好``/``天加``/``今天``/``加班``）只是长句被切碎的产物，
        没有区分度；但独立出现的 2 字实词（``吃饭``）是群聊里真实的口头说法，
        应当能学到。两者要分开对待。
        """
        sliced = extract_expressions(
            ["今天加班好累", "加班好累啊", "今天加班好累", "晚上吃什么"]
        )
        mechanical = [item for item in ("班好", "天加", "今天", "加班") if item in sliced]
        self.assertEqual(mechanical, [], f"机械切片不得入库：{mechanical}")
        self.assertIn("加班好累", sliced, "正常的 4 字短语仍应学到")

        self.assertEqual(
            extract_expressions(["吃饭", "吃饭"]),
            ["吃饭"],
            "独立出现的 2 字实词应当可学，不能被一刀切掉",
        )

    def test_mixed_sentence_keeps_normal_clause(self) -> None:
        """S1 副作用：烂梗和正常内容同句时，只该丢掉烂梗那半句。

        整句级过滤会把「今天加班好累」一起丢掉，等于用户越爱玩梗、学到的
        正常说法越少——与 P2 的目的相反。
        """
        mixed = "今天加班好累，家人们谁懂啊"
        result = extract_expressions([mixed, mixed])

        self.assertIn("今天加班好累", result, f"同句里的正常短语被连带丢掉了：{result}")
        leaked = [
            item for item in result if any(key in item for key in ("家人", "谁懂"))
        ]
        self.assertEqual(leaked, [], f"烂梗分句的切片不得入库：{leaked}")


# --------------------------------------------------------------------------- #
# ExpressionStore
# --------------------------------------------------------------------------- #


class ExpressionStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = DeepChatStore(Path(self._tmp.name) / "deepchat.sqlite3")
        self.expressions = ExpressionStore(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class ExpressionStoreTests(ExpressionStoreTestCase):
    def test_count_is_zero_for_unknown_user(self) -> None:
        self.assertEqual(self.expressions.count("nobody"), 0)

    def test_upsert_increments_count(self) -> None:
        first = self.expressions.upsert("u1", [REPEATED_PHRASE], now=1000.0)
        self.assertIsInstance(first, int)
        self.assertEqual(self.expressions.count("u1"), 1)
        self.expressions.upsert("u1", [REPEATED_PHRASE], now=1001.0)
        self.expressions.upsert("u1", [REPEATED_PHRASE], now=1002.0)
        self.assertEqual(self.expressions.count("u1"), 1, "同一表达不应新增行")
        entry = self.expressions.entries("u1")[0]
        self.assertEqual(entry["count"], 3)

    def test_entries_expose_text_and_count(self) -> None:
        self.expressions.upsert("u1", [REPEATED_PHRASE, "晚上吃什么"], now=1000.0)
        entries = self.expressions.entries("u1")
        self.assertEqual(len(entries), 2, entries)
        for entry in entries:
            self.assertIn("text", entry)
            self.assertIn("count", entry)
        self.assertEqual({entry["text"] for entry in entries}, {REPEATED_PHRASE, "晚上吃什么"})

    def test_select_respects_limit(self) -> None:
        self.expressions.upsert("u1", [REPEATED_PHRASE, "晚上吃什么", "明天再改"], now=1000.0)
        picked = self.expressions.select("u1", limit=2)
        self.assertLessEqual(len(picked), 2, picked)
        self.assertEqual(len(set(picked)), len(picked), f"不应重复：{picked}")

    def test_select_returns_known_subset(self) -> None:
        known = {REPEATED_PHRASE, "晚上吃什么", "明天再改"}
        self.expressions.upsert("u1", sorted(known), now=1000.0)
        picked = self.expressions.select("u1", limit=5)
        self.assertTrue(set(picked) <= known, picked)
        self.assertTrue(picked)

    def test_select_on_unknown_user_is_empty(self) -> None:
        self.assertEqual(self.expressions.select("nobody"), [])

    def test_fragment_is_empty_without_records(self) -> None:
        self.assertEqual(self.expressions.fragment("nobody"), "")

    def test_fragment_starts_with_header(self) -> None:
        self.expressions.upsert("u1", [REPEATED_PHRASE], now=1000.0)
        fragment = self.expressions.fragment("u1")
        self.assertTrue(fragment.startswith(FRAGMENT_HEADER), fragment)
        self.assertIn(REPEATED_PHRASE, fragment)

    def test_clear_removes_records_and_returns_count(self) -> None:
        self.expressions.upsert("u1", [REPEATED_PHRASE, "晚上吃什么"], now=1000.0)
        removed = self.expressions.clear("u1")
        self.assertEqual(removed, 2)
        self.assertEqual(self.expressions.count("u1"), 0)
        self.assertEqual(self.expressions.fragment("u1"), "")

    def test_max_entries_evicts_oldest(self) -> None:
        capped = ExpressionStore(self.store, max_entries=3)
        for index in range(1, 6):
            capped.upsert("u1", [f"话题{index}"], now=1000.0 + index)
        self.assertEqual(capped.count("u1"), 3)
        texts = {entry["text"] for entry in capped.entries("u1")}
        self.assertIn("话题5", texts)
        self.assertNotIn("话题1", texts, f"最旧的一条应被淘汰：{texts}")
        self.assertNotIn("话题2", texts, f"第二旧的也应被淘汰：{texts}")

    def test_last_learn_at_defaults_to_zero(self) -> None:
        self.assertEqual(self.expressions.last_learn_at("nobody"), 0.0)

    def test_mark_learned_records_timestamp(self) -> None:
        self.expressions.mark_learned("u1", now=1234.5)
        self.assertAlmostEqual(self.expressions.last_learn_at("u1"), 1234.5, places=3)


# --------------------------------------------------------------------------- #
# DeepChatService 集成
# --------------------------------------------------------------------------- #


class ExpressionServiceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = FakePlugin(self._tmp.name)
        self.service = DeepChatService(self.plugin)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enable(self) -> None:
        self.plugin._qq_settings["deepchat_expression_learning_enabled"] = True

    def _seed_history(self, user_id: str = "u1") -> None:
        """最近三条都是同一句，任一种取材方式都能凑够 min_freq。"""
        history = [
            {"role": "user", "content": "晚上吃什么"},
            {"role": "assistant", "content": "随便点个外卖吧"},
            {"role": "user", "content": "今天加班好累"},
            {"role": "assistant", "content": "辛苦了"},
            {"role": "user", "content": "加班好累啊"},
            {"role": "assistant", "content": "早点休息"},
            {"role": "user", "content": "今天加班好累"},
        ]
        session = SimpleNamespace(_conversation_history=history)
        self.plugin._user_sessions[f"private:{user_id}"] = {"session": session}

    def _interact(self, times: int, user_id: str = "u1") -> None:
        for _ in range(times):
            self.service.writeback(sender_id=user_id, text="今天加班好累")

    def _expression_count(self, user_id: str = "u1") -> int:
        return int(self.service.view_state(user_id)["expressions"])

    def test_view_state_exposes_expression_count(self) -> None:
        state = self.service.view_state("u1")
        self.assertIn("expressions", state)
        self.assertIsInstance(state["expressions"], int)
        self.assertEqual(state["expressions"], 0)

    def test_learning_is_disabled_by_default(self) -> None:
        self._seed_history()
        self._interact(10)
        self.assertEqual(self._expression_count(), 0, "默认关闭时不应学习")

    def test_learn_expressions_if_due_returns_zero_when_disabled(self) -> None:
        self._seed_history()
        learned = self.service.learn_expressions_if_due("u1", now=2000.0)
        self.assertIsInstance(learned, int)
        self.assertEqual(learned, 0)

    def test_learning_runs_every_ten_interactions(self) -> None:
        self._enable()
        self._seed_history()
        self._interact(9)
        self.assertEqual(self._expression_count(), 0, "不足 10 次不应学习")
        self._interact(1)
        self.assertGreater(self._expression_count(), 0, "第 10 次互动后应学到表达")

    def test_custom_min_messages_lowers_cadence(self) -> None:
        self._enable()
        self.plugin._qq_settings["deepchat_expression_min_messages"] = 5
        self._seed_history()
        self._interact(4)
        self.assertEqual(self._expression_count(), 0)
        self._interact(1)
        self.assertGreater(self._expression_count(), 0, "配置为 5 时第 5 次应学习")

    def test_enrich_prompt_contains_expression_fragment(self) -> None:
        self._enable()
        self._seed_history()
        self._interact(10)
        prompt = self.service.enrich_system_prompt(
            system_prompt="BASE",
            sender_id="u1",
            sender_name="小明",
        )
        self.assertTrue(prompt.startswith("BASE"))
        self.assertIn("## 群友常用说法", prompt)
        self.assertIn(REPEATED_PHRASE, prompt)

    def test_enrich_prompt_has_no_expression_fragment_when_learning_off(self) -> None:
        self._seed_history()
        self._interact(10)
        prompt = self.service.enrich_system_prompt(system_prompt="BASE", sender_id="u1")
        self.assertNotIn("## 群友常用说法", prompt)

    def test_reset_user_clears_expressions(self) -> None:
        self._enable()
        self._seed_history()
        self._interact(10)
        self.assertGreater(self._expression_count(), 0)
        self.service.reset_user("u1")
        self.assertEqual(self._expression_count(), 0)


if __name__ == "__main__":
    unittest.main()
