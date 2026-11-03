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
        self.relationship = RelationshipGraph(self.store, decay_rate=0.01)

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
