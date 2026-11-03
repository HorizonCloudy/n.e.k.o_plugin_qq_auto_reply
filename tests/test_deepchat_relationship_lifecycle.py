"""P1 关系记忆生命周期：half-life 衰减 + freeze/revive 双阈值 + 迁移。

只断言对外可观测行为（``service.relationship`` 的 get/reinforce 与 sqlite 列），
不断言内部实现细节（不断言 ``_decay`` 私有方法、不断言阈值存储字段名）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepchat.models import RelationshipEdge
from deepchat.relationship import RelationshipGraph
from deepchat.service import DeepChatService

DAY = 86400.0
T0 = 1_000_000.0

DEFAULT_HALF_LIFE_DAYS = 14.0
DEFAULT_FREEZE_THRESHOLD = 0.10
DEFAULT_REVIVE_THRESHOLD = 0.15


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


class RelationshipLifecycleTestCase(unittest.TestCase):
    """默认配置（半衰期 14 天 / 冻结 0.10 / 解冻 0.15）下的服务实例。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = FakePlugin(self._tmp.name)
        self.service = DeepChatService(self.plugin)
        self.relationship = self.service.relationship

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _new_service(self, settings: dict) -> DeepChatService:
        """另开一个数据目录的服务，用来验证配置键读取与回落。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plugin = FakePlugin(tmp.name)
        plugin._qq_settings.update(settings)
        return DeepChatService(plugin)


class RelationshipEdgeModelTests(unittest.TestCase):
    def test_edge_defaults_to_not_frozen(self) -> None:
        edge = RelationshipEdge(user_id="u1")
        self.assertFalse(edge.frozen)


class RelationshipHalfLifeDecayTests(RelationshipLifecycleTestCase):
    def test_half_life_decay(self) -> None:
        """0.8 经过一个半衰期（14 天）应衰减到 0.4，信任同步向 0.5 收敛。"""
        self.relationship.reinforce("u1", 0.8, now=T0)
        edge = self.relationship.get("u1", now=T0 + 14 * DAY)
        self.assertAlmostEqual(edge.affinity, 0.4, delta=0.02)
        self.assertAlmostEqual(edge.trust, 0.7, delta=0.02)

        # N1：``decay_rate`` 已废弃但形参仍留着接旧调用方——
        # 第二位位置传参必须不炸，且不得影响半衰期、也不得落成属性。
        legacy = RelationshipGraph(self.service.store, 0.5)
        self.assertEqual(
            legacy.half_life_days,
            DEFAULT_HALF_LIFE_DAYS,
            "位置传入的 0.5 是已废弃的 decay_rate，必须被忽略",
        )
        self.assertFalse(
            hasattr(legacy, "decay_rate"), "废弃形参不应再落成实例属性"
        )

    def test_custom_half_life_is_honoured(self) -> None:
        """半衰期 7 天时，7 天后就应减半（用于区分配置是否真的被读取）。"""
        service = self._new_service({"deepchat_relationship_half_life_days": 7})
        service.relationship.reinforce("u1", 0.8, now=T0)
        edge = service.relationship.get("u1", now=T0 + 7 * DAY)
        self.assertAlmostEqual(edge.affinity, 0.4, delta=0.02)

    def test_negative_half_life_falls_back_to_default(self) -> None:
        service = self._new_service({"deepchat_relationship_half_life_days": -5})
        service.relationship.reinforce("u1", 0.8, now=T0)
        edge = service.relationship.get("u1", now=T0 + DEFAULT_HALF_LIFE_DAYS * DAY)
        self.assertAlmostEqual(
            edge.affinity,
            0.4,
            delta=0.02,
            msg="负半衰期必须回落到默认 14 天，而不是产生放大或异常值",
        )

    def test_whole_day_settlement_skips_sub_day_decay(self) -> None:
        """衰减按整天结算：未满一天不结算，满一天才一次性下沉一整天。

        这条同时锁住两件事：23 小时仍是原值（不是按 1 分钟下限连续衰减），
        以及满 24 小时后按 ``0.5 ** (1/14)`` 结算而不是直接按半天或整天算错。
        """
        self.relationship.reinforce("u1", 0.8, now=T0)

        same_day = self.relationship.get("u1", now=T0 + 23 * 3600)
        self.assertAlmostEqual(
            same_day.affinity, 0.8, delta=1e-9, msg="未满一天不应结算"
        )

        one_day = self.relationship.get("u1", now=T0 + 24 * 3600)
        self.assertAlmostEqual(
            one_day.affinity,
            0.8 * 0.5 ** (1 / DEFAULT_HALF_LIFE_DAYS),
            delta=0.002,
            msg="满一天应按一天结算（≈0.7614，不是 0.4）",
        )

        half_life = self.relationship.get("u1", now=T0 + 14 * DAY)
        self.assertAlmostEqual(half_life.affinity, 0.4, delta=0.02)

    def test_multi_settlement_carries_fractional_remainder(self) -> None:
        """连续两次结算必须累计零头，而不是每次各自截断把零头吃掉。

        +1.9 天结算 1 天、零头 0.9 天留到下次；再读到 +3.8 天时累计应是
        3 天（1 + 2），而不是「上次 1.9 被推进到 1.9，再取 1.9 天又只算 1 天」。
        """
        self.relationship.reinforce("u1", 0.8, now=T0)

        first = self.relationship.get("u1", now=T0 + 1.9 * DAY)
        self.assertAlmostEqual(
            first.affinity, 0.8 * 0.5 ** (1 / DEFAULT_HALF_LIFE_DAYS), delta=0.002
        )
        self.assertAlmostEqual(
            first.updated_at,
            T0 + 1 * DAY,
            delta=1.0,
            msg="updated_at 只应推进已结算的整天数，零头要留给下次",
        )

        second = self.relationship.get("u1", now=T0 + 3.8 * DAY)
        self.assertAlmostEqual(
            second.affinity, 0.8 * 0.5 ** (3 / DEFAULT_HALF_LIFE_DAYS), delta=0.002
        )
        self.assertNotAlmostEqual(
            second.affinity,
            0.8 * 0.5 ** (2 / DEFAULT_HALF_LIFE_DAYS),
            delta=0.002,
            msg="只结算 2 天说明零头被吃了",
        )


class HotTuneTests(RelationshipLifecycleTestCase):
    """S3：``dc_tune`` 改完参数必须立刻生效，不能等重启。

    只断言「属性变了」不够——实现完全可以只更新属性却让图/库继续用旧值，
    所以每条都再验一次可观测行为。
    """

    def test_relationship_half_life_applies_without_restart(self) -> None:
        asyncio.run(self.service.tune("deepchat_relationship_half_life_days", "7"))
        self.assertAlmostEqual(self.service.relationship.half_life_days, 7.0, places=6)

        self.service.relationship.reinforce("u1", 0.8, now=T0)
        edge = self.service.relationship.get("u1", now=T0 + 7 * DAY)
        self.assertAlmostEqual(
            edge.affinity, 0.4, delta=0.02, msg="半衰期改成 7 天后 7 天就应减半"
        )

    def test_expression_max_entries_applies_without_restart(self) -> None:
        asyncio.run(self.service.tune("deepchat_expression_max_entries", "3"))
        self.assertEqual(self.service.expressions.max_entries, 3)

        self.service.expressions.upsert(
            "u1", [f"码{index}" for index in range(5)], now=1000.0
        )
        self.assertEqual(
            self.service.expressions.count("u1"), 3, "改完上限后应立即按 3 条淘汰"
        )


class RelationshipFreezeTests(RelationshipLifecycleTestCase):
    def _freeze(self, user_id: str = "u1") -> None:
        """把关系衰减到冻结阈值以下并落库。"""
        self.relationship.reinforce(user_id, 0.12, now=T0)
        frozen = self.relationship.get(user_id, now=T0 + 14 * DAY)
        self.assertTrue(frozen.frozen, f"0.12 衰减 14 天后应冻结：{frozen.affinity}")

    def test_freeze_below_threshold(self) -> None:
        self.relationship.reinforce("u1", 0.12, now=T0)
        edge = self.relationship.get("u1", now=T0 + 14 * DAY)
        self.assertTrue(edge.frozen)
        self.assertLessEqual(edge.affinity, DEFAULT_FREEZE_THRESHOLD + 1e-6)

    def test_custom_freeze_threshold_is_honoured(self) -> None:
        """阈值抬到 0.5 时，0.4 也应判定为冻结。"""
        service = self._new_service({"deepchat_relationship_freeze_threshold": 0.5})
        service.relationship.reinforce("u1", 0.8, now=T0)
        edge = service.relationship.get("u1", now=T0 + 14 * DAY)
        self.assertAlmostEqual(edge.affinity, 0.4, delta=0.02)
        self.assertTrue(edge.frozen)

    def test_negative_freeze_threshold_falls_back_to_default(self) -> None:
        """阈值配成 -1 必须回落 0.10，否则永远不会冻结。"""
        service = self._new_service({"deepchat_relationship_freeze_threshold": -1})
        service.relationship.reinforce("u1", 0.12, now=T0)
        edge = service.relationship.get("u1", now=T0 + 14 * DAY)
        self.assertTrue(edge.frozen, "负阈值应回落到默认 0.10")

    def test_frozen_does_not_decay_further(self) -> None:
        self._freeze()
        frozen = self.relationship.get("u1", now=T0 + 14 * DAY)
        later = self.relationship.get("u1", now=T0 + 14 * DAY + 365 * DAY)
        self.assertTrue(later.frozen)
        self.assertAlmostEqual(later.affinity, frozen.affinity, places=6)
        self.assertAlmostEqual(later.trust, frozen.trust, places=6)

    def test_revive_on_reinforce(self) -> None:
        self._freeze()
        revived = self.relationship.reinforce("u1", 0.1, now=T0 + 14 * DAY)
        self.assertFalse(revived.frozen, f"加回 0.1 后应解冻：{revived.affinity}")
        self.assertGreaterEqual(revived.affinity + 1e-9, DEFAULT_REVIVE_THRESHOLD)

    def test_revive_threshold_not_crossed_stays_frozen(self) -> None:
        self._freeze()
        still = self.relationship.reinforce("u1", 0.01, now=T0 + 14 * DAY)
        self.assertTrue(still.frozen, "没越过解冻阈值就必须保持冻结")
        self.assertLess(still.affinity, DEFAULT_REVIVE_THRESHOLD)

    def test_frozen_state_persists_across_reloads(self) -> None:
        self._freeze()
        reloaded = DeepChatService(FakePlugin(self._tmp.name))
        edge = reloaded.relationship.get("u1", now=T0 + 15 * DAY)
        self.assertTrue(edge.frozen, "冻结状态必须落库，重启后不能自动解冻")

    def test_negative_affinity_decays_toward_neutral(self) -> None:
        """负向好感的冻结判定按 |affinity| 算：先衰减回中性带，而不是立刻冻结。

        字面口径 ``affinity <= threshold`` 会让 -0.4 当场命中阈值、
        把「明显疏远」永久钉死，负向关系再也回不到中性——这与「关系会淡忘」相反。
        """
        self.relationship.weaken("u1", 0.8, now=T0)
        seeded = self.relationship.get("u1", now=T0)
        self.assertAlmostEqual(seeded.affinity, -0.8, delta=0.02)

        edge = self.relationship.get("u1", now=T0 + 14 * DAY)
        self.assertAlmostEqual(edge.affinity, -0.4, delta=0.02)
        self.assertFalse(edge.frozen, "负向关系衰减到 -0.4 时不应被冻结")

    def test_negative_affinity_freezes_after_returning_to_band(self) -> None:
        """负向好感受到中性带以内（|affinity| <= 阈值）时才冻结。"""
        self.relationship.weaken("u1", 0.30, now=T0)
        edge = self.relationship.get("u1", now=T0 + 30 * DAY)
        self.assertLessEqual(abs(edge.affinity), DEFAULT_FREEZE_THRESHOLD + 1e-6)
        self.assertTrue(edge.frozen, f"回到中性带应冻结：{edge.affinity}")

    def test_negative_frozen_revives_on_reinforce(self) -> None:
        """负向冻结的关系也能被一次够分量的好感加回来，不会被永久钉死。"""
        self.relationship.weaken("u1", 0.8, now=T0)
        seeded = self.relationship.get("u1", now=T0)
        self.assertAlmostEqual(seeded.affinity, -0.8, delta=0.02)

        frozen = self.relationship.get("u1", now=T0 + 42 * DAY)
        self.assertAlmostEqual(frozen.affinity, -0.10, delta=0.02)
        self.assertTrue(frozen.frozen, "负向关系回到中性带后应冻结")

        revived = self.relationship.reinforce("u1", 0.3, now=T0 + 42 * DAY)
        self.assertAlmostEqual(revived.affinity, 0.20, delta=0.02)
        self.assertFalse(revived.frozen, f"越过 revive 阈值后应解冻：{revived.affinity}")


class RelationshipMigrationTests(unittest.TestCase):
    """旧库（deepchat_relationship 没有 frozen 列）必须能原地升级。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "deepchat.sqlite3"
        self._create_legacy_database()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create_legacy_database(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE deepchat_relationship (
                    user_id TEXT PRIMARY KEY,
                    affinity REAL NOT NULL DEFAULT 0.0,
                    trust REAL NOT NULL DEFAULT 0.5,
                    interactions INTEGER NOT NULL DEFAULT 0,
                    milestones TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO deepchat_relationship"
                "(user_id, affinity, trust, interactions, milestones, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("u1", 0.3, 0.6, 4, "[]", time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def _columns(self) -> set[str]:
        conn = sqlite3.connect(self.db_path)
        try:
            return {str(row[1]) for row in conn.execute("PRAGMA table_info(deepchat_relationship)")}
        finally:
            conn.close()

    def test_migration_adds_frozen_column(self) -> None:
        service = DeepChatService(FakePlugin(self._tmp.name))
        self.assertIn("frozen", self._columns(), "旧库初始化时应自动 ALTER TABLE 补 frozen 列")
        edge = service.relationship.get("u1")
        self.assertFalse(edge.frozen, "旧行迁移后必须读作未冻结")
        self.assertEqual(edge.interactions, 4)
        self.assertAlmostEqual(edge.affinity, 0.3, delta=0.01)


if __name__ == "__main__":
    unittest.main()
