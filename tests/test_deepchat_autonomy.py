from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from deepchat.service import DeepChatService


class FakeConfigStore:
    async def save(self, config: dict) -> dict:
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

    def _build_session_key(self, *, sender_id: str, is_group: bool = False, group_id: str | None = None) -> str:
        return f"group:{group_id or ''}" if is_group else f"private:{sender_id}"


class AutonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = FakePlugin(self._tmp.name)
        self.service = DeepChatService(self.plugin)
        self.now = datetime(2026, 11, 3, 12, 0, 0).timestamp()
        self.plugin._qq_settings.update({
            "deepchat_autonomy_enabled": True,
            # 双开关 AND 之后，自主联系的用例必须两个总开关都打开；
            # 「只开一个不许开口」由 test_both_switches_are_required 专测。
            "proactive_initiative_enabled": True,
            "deepchat_autonomy_min_affinity": 0.35,
            "deepchat_autonomy_min_idle_hours": 6,
            "deepchat_autonomy_max_idle_hours": 72,
            "proactive_initiative_min_gap_seconds": 3600,
            "proactive_initiative_max_per_day": 1,
            "proactive_initiative_quiet_start_hour": 23,
            "proactive_initiative_quiet_end_hour": 8,
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(
        self,
        user_id: str = "u1",
        *,
        affinity: float = 0.5,
        private: bool = True,
        at: float | None = None,
    ) -> None:
        at = self.now - 7 * 3600 if at is None else at
        if private:
            self.service.autonomy.record_private_interaction(user_id, now=at)
        self.service.relationship.reinforce(user_id, affinity, now=at)

    def test_due_private_user_is_candidate(self) -> None:
        self._seed()
        candidate = self.service.autonomy.should_reach_out("u1", now=self.now)
        self.assertIsNotNone(candidate)
        self.assertGreater(candidate.affinity, 0.35)
        self.assertAlmostEqual(candidate.idle_hours, 7.0, places=1)

    def test_quiet_hours_block(self) -> None:
        quiet_now = datetime(2026, 11, 3, 23, 30, 0).timestamp()
        self._seed(at=quiet_now - 7 * 3600)
        self.assertIsNone(self.service.autonomy.should_reach_out("u1", now=quiet_now))

    def test_low_affinity_blocks(self) -> None:
        self._seed(affinity=0.1)
        self.assertIsNone(self.service.autonomy.should_reach_out("u1", now=self.now))

    def test_group_only_user_blocks(self) -> None:
        self._seed(private=False)
        self.assertIsNone(self.service.autonomy.should_reach_out("u1", now=self.now))

    def test_cooldown_blocks_after_send(self) -> None:
        self._seed()
        self.service.autonomy.mark_reached_out("u1", now=self.now)
        self.assertIsNone(self.service.autonomy.should_reach_out("u1", now=self.now + 60))

    def test_daily_cap_blocks(self) -> None:
        self._seed()
        self.service.autonomy.mark_reached_out("u1", now=self.now)
        self.assertIsNone(self.service.autonomy.should_reach_out("u1", now=self.now + 2 * 3600))

    def test_tick_returns_one(self) -> None:
        self._seed("u1")
        self._seed("u2")
        candidates = self.service.autonomy.tick(now=self.now, limit=1)
        self.assertEqual(len(candidates), 1)

    def test_disabled_returns_none(self) -> None:
        self.plugin._qq_settings["deepchat_autonomy_enabled"] = False
        self._seed()
        self.assertIsNone(self.service.autonomy.should_reach_out("u1", now=self.now))

    def test_both_switches_are_required(self) -> None:
        """Phase 0：deepchat 与 proactive 两个总开关必须 AND，任一关掉都不许开口。"""
        self.plugin._qq_settings["proactive_initiative_enabled"] = False
        self.assertFalse(
            self.service.autonomy.enabled, "proactive 总开关关掉时 autonomy 必须视为关闭"
        )
        self._seed()
        self.assertIsNone(
            self.service.autonomy.should_reach_out("u1", now=self.now),
            "两个开关任一关闭都不得主动私聊",
        )

    def test_daily_budget_from_proactive_service_gates_outreach(self) -> None:
        """Phase 0：当日额度由 proactive 服务统一记账，用尽即不得再开口。

        契约：``proactive_initiative_service.remaining_today(*, now=None) -> int``（同步）。
        """
        self.plugin._qq_settings["proactive_initiative_enabled"] = True
        self._seed()

        self.plugin.proactive_initiative_service = SimpleNamespace(
            remaining_today=lambda *a, **k: 0
        )
        self.assertIsNone(
            self.service.autonomy.should_reach_out("u1", now=self.now),
            "今日额度已用尽，不得再主动私聊",
        )

        self.plugin.proactive_initiative_service = SimpleNamespace(
            remaining_today=lambda *a, **k: 1
        )
        self.assertIsNotNone(
            self.service.autonomy.should_reach_out("u1", now=self.now),
            "额度仍有剩余时不应被这道 gate 拦住",
        )


if __name__ == "__main__":
    unittest.main()
