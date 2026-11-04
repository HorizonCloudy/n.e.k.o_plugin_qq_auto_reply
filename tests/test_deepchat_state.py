from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deepchat.models import Mood
from deepchat.mood import MoodStateMachine
from deepchat.relationship import RelationshipGraph
from deepchat.store import DeepChatStore


class DeepChatStateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = DeepChatStore(Path(self._tmp.name) / "state.sqlite3")
        self.mood = MoodStateMachine(self.store, decay_rate=0.05)
        self.relationship = RelationshipGraph(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class MoodStateMachineTests(DeepChatStateTestCase):
    def test_praise_raises_valence_and_classifies_happy(self) -> None:
        state = self.mood.update("user:1", "praise", now=1000.0)
        self.assertGreater(state.valence, 0.0)
        self.assertEqual(state.current, Mood.HAPPY)

    def test_insult_lowers_valence_and_classifies_sulking(self) -> None:
        state = self.mood.update("user:2", "insult", now=1000.0)
        self.assertLess(state.valence, 0.0)
        self.assertEqual(state.current, Mood.SULKING)

    def test_bored_signal_classifies_bored(self) -> None:
        state = self.mood.update("user:3", "bored", now=1000.0)
        self.assertEqual(state.current, Mood.BORED)

    def test_decay_returns_to_neutral(self) -> None:
        self.mood.update("user:1", "praise", now=1000.0)
        decayed = self.mood.get("user:1", now=1000.0 + 30 * 60)
        self.assertAlmostEqual(decayed.valence, 0.0, places=6)
        self.assertEqual(decayed.current, Mood.NEUTRAL)

    def test_unknown_user_is_neutral(self) -> None:
        state = self.mood.get("user:unknown", now=1000.0)
        self.assertEqual(state.current, Mood.NEUTRAL)
        self.assertAlmostEqual(state.valence, 0.0)

    def test_set_mood_uses_anchor(self) -> None:
        state = self.mood.set_mood("user:4", "excited", now=1000.0)
        self.assertEqual(state.current, Mood.EXCITED)
        self.assertGreater(state.arousal, 0.5)

    def test_legacy_mood_alias_mechanism_and_registry(self) -> None:
        """3D 把 `neutral` 并入 `calm`，存量库里的旧值必须仍能读出来。

        注意**不能**只断言 `coerce_mood("neutral") == Mood.NEUTRAL`：`coerce_mood` 对未知值
        本来就会 fail-soft 落回 NEUTRAL，所以那条断言在"别名表被清空"时**仍然通过**（我第一版
        就是这么写的，变异验证抓出来的）。这里拆成两层：
        1. **机制**：别名表能把旧值映射到**非 NEUTRAL** 的目标（清空/删掉查表逻辑都会红）；
        2. **登记**：`neutral` 必须显式在表里，且目标必须是合法 Mood 值。
        """
        from deepchat import models as models_module

        # 1) 机制：用一个指向非 NEUTRAL 的别名，才能真正区分"查表生效"与"fallback 兜底"
        original = models_module.LEGACY_MOOD_ALIASES
        try:
            models_module.LEGACY_MOOD_ALIASES = {**original, "grumpy": "annoyed"}
            self.assertEqual(models_module.coerce_mood("grumpy"), Mood.ANNOYED)
        finally:
            models_module.LEGACY_MOOD_ALIASES = original

        # 2) 登记：旧值 neutral 必须在表里，且映射目标合法
        self.assertIn("neutral", original, "旧值 neutral 的别名登记被删了，存量库读取会退化")
        valid_values = {mood.value for mood in Mood}
        for legacy, target in original.items():
            self.assertIn(target, valid_values, f"别名 {legacy} → {target} 不是合法 Mood 值")

        # 3) 存量行能读、decay_all 能跑
        self.mood.set_mood("user:legacy", "happy", now=1000.0)
        with self.store.connection() as conn:
            conn.execute(
                "UPDATE deepchat_mood SET current = 'neutral' WHERE user_key = ?",
                ("user:legacy",),
            )
        state = self.mood.get("user:legacy", now=1001.0)
        self.assertIn(state.current, {Mood.HAPPY, Mood.NEUTRAL})
        self.assertEqual(self.mood.decay_all(now=2000.0), 1, "decay_all 也必须容忍旧值")


class RelationshipGraphTests(DeepChatStateTestCase):
    def test_reinforce_and_milestone(self) -> None:
        edge = self.relationship.reinforce("u1", 0.1, now=1000.0)
        self.assertAlmostEqual(edge.affinity, 0.1)
        self.assertEqual(edge.interactions, 1)
        edge = self.relationship.add_milestone("u1", "一起聊过加班", now=1001.0)
        self.assertIn("一起聊过加班", edge.milestones)

    def test_weaken_lowers_affinity(self) -> None:
        self.relationship.reinforce("u1", 0.2, now=1000.0)
        edge = self.relationship.weaken("u1", 0.05, now=1001.0)
        self.assertAlmostEqual(edge.affinity, 0.15)
        self.assertEqual(edge.interactions, 2)

    def test_decay_after_30_days(self) -> None:
        self.relationship.reinforce("u1", 0.5, now=1000.0)
        decayed = self.relationship.get("u1", now=1000.0 + 30 * 86400)
        self.assertLess(decayed.affinity, 0.5)

    def test_unknown_user_is_neutral(self) -> None:
        edge = self.relationship.get("nobody", now=1000.0)
        self.assertEqual(edge.affinity, 0.0)
        self.assertEqual(edge.trust, 0.5)
        self.assertEqual(edge.interactions, 0)


if __name__ == "__main__":
    unittest.main()
