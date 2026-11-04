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


class _AutonomyFixture(unittest.TestCase):
    """共用 fixture（不含用例）：子类各自持有自己的用例。"""

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

class AutonomyTests(_AutonomyFixture):
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


class FakeSharedBudget:
    """P2 契约替身：**两个来源共用一个账本**。

    现状（实现前）只有单向只读闸门：`autonomy` 读 `remaining_today()` 但不记账，
    proactive 也只记自己的内存计数。所以这些用例现在应当是红的——它们描述的是
    "共享账本"这一目标行为，不是当前行为。
    """

    def __init__(self, max_per_day: int = 2) -> None:
        self.max_per_day = max_per_day
        self.sent: list[tuple[str, float]] = []

    def record(self, source: str, now: float) -> None:
        self.sent.append((source, now))

    def remaining_today(self, **_: object) -> int:
        return max(0, self.max_per_day - len(self.sent))

    def last_sent_at(self) -> float:
        return self.sent[-1][1] if self.sent else 0.0


class SharedBudgetTests(_AutonomyFixture):
    """P2-①：proactive_initiative 与 deepchat autonomy 必须共用一本账。"""

    def setUp(self) -> None:
        super().setUp()
        self.plugin._qq_settings["proactive_initiative_max_per_day"] = 2
        self.ledger = FakeSharedBudget(2)
        self.plugin.proactive_initiative_service = SimpleNamespace(
            remaining_today=self.ledger.remaining_today,
            mark_reached_out=lambda *a, **k: self.ledger.record("proactive", self.now),
            last_sent_at=self.ledger.last_sent_at,
        )

    def test_deepchat_send_decrements_shared_budget(self) -> None:
        self._seed()
        self.ledger.record("proactive", self.now - 2 * 3600)  # 今天已由原服务发出 1
        self.assertEqual(self.ledger.remaining_today(), 1)

        self.assertIsNotNone(self.service.autonomy.should_reach_out("u1", now=self.now))
        self.service.autonomy.mark_reached_out("u1", now=self.now)

        self.assertEqual(
            self.ledger.remaining_today(),
            0,
            f"DeepChat 的发送没有记进共享账本（账本：{self.ledger.sent}）",
        )

    def test_interleaved_sends_never_exceed_max_per_day(self) -> None:
        self._seed()
        self.ledger.record("proactive", self.now - 2 * 3600)
        self.service.autonomy.mark_reached_out("u1", now=self.now)  # DeepChat 发第 2 次

        self.assertEqual(
            self.ledger.remaining_today(),
            0,
            f"两来源交错后总额度未被共享限制（账本：{self.ledger.sent}）",
        )
        self.assertIsNone(
            self.service.autonomy.should_reach_out("u1", now=self.now + 10),
            "额度已满时 DeepChat 不得再开口",
        )

    def test_min_gap_is_shared_across_both_sources(self) -> None:
        self._seed()
        self.ledger.record("proactive", self.now - 60)  # 60s 前原服务刚发过（gap=3600）
        self.assertIsNone(
            self.service.autonomy.should_reach_out("u1", now=self.now),
            "间隔闸门未共享：原服务刚发完，DeepChat 仍认为可以开口",
        )

    def test_mark_reached_out_records_into_shared_ledger(self) -> None:
        self._seed()
        self.service.autonomy.mark_reached_out("u1", now=self.now)
        self.assertTrue(
            any(source == "deepchat" for source, _ in self.ledger.sent),
            f"共享账本里没有 deepchat 的记录：{self.ledger.sent}",
        )


class MasterSwitchTests(_AutonomyFixture):
    """P2-②：`deepchat_enabled` 是总开关，关掉后不得再有任何 DeepChat 行为。"""

    def test_master_switch_off_disables_autonomy(self) -> None:
        self.plugin._qq_settings["deepchat_enabled"] = False
        self.assertFalse(
            self.service.autonomy.enabled,
            "主开关关掉后 autonomy 必须视为关闭",
        )
        self._seed()
        self.assertIsNone(self.service.autonomy.should_reach_out("u1", now=self.now))

    def test_autonomy_is_hard_gated_until_shared_budget_lands(self) -> None:
        """3A 期硬闸：即便两个子开关都为 true，自主联系也不得开口。

        `deepchat_autonomy_enabled` 已被放进 `RETIRED_TUNING_KEYS`（tune 拒调），
        但**旧配置/存量存档里仍可能是 true**。3B 的共享账本落地前，开了它就等于
        绕过「有由头搭话」的日限（两套账本各记各的），所以这里必须硬闸关闭 ——
        只靠 RETIRED 拦 tune 是不够的。
        """
        self.plugin._qq_settings["deepchat_autonomy_enabled"] = True
        self.plugin._qq_settings["proactive_initiative_enabled"] = True
        self.assertTrue(self.service.enabled, "前置：主开关是开的")

        self.assertFalse(
            self.service.autonomy.enabled,
            "3A 期自主联系必须硬闸关闭（共享预算未落地），否则可绕过日限",
        )
        self._seed()
        self.assertIsNone(
            self.service.autonomy.should_reach_out("u1", now=self.now),
            "硬闸关闭时不得产出候选",
        )


if __name__ == "__main__":
    unittest.main()
