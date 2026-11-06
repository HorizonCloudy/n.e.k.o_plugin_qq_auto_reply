"""P2-③：配置"重启后仍生效"的契约。

`config_store.load()` 的 JSON 往返由 qq 那套配置测试覆盖（pytest，需要宿主包路径）；
这里验的是 **DeepChatService 这一侧**：把持久化后的设置交给一个新实例，它必须
按新值构造 —— 等价于"重启后读到 0.8"。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepchat.service import DeepChatService


class FakeConfigStore:
    """只记录落盘内容；真实 JSON 往返不在本用例范围（见模块 docstring）。"""

    def __init__(self) -> None:
        self.saved: dict | None = None

    async def save(self, config: dict) -> dict:
        self.saved = dict(config)
        return config


class FakePlugin:
    def __init__(self, data_dir: str, settings: dict | None = None) -> None:
        self._qq_settings: dict = dict(settings or {})
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


class ConfigLoadRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tuned_value_survives_restart(self) -> None:
        first = FakePlugin(self._tmp.name)
        service_before = DeepChatService(first)
        asyncio.run(service_before.tune("deepchat_mood_decay_rate", "0.8"))
        self.assertAlmostEqual(service_before.mood.decay_rate, 0.8, places=6)

        persisted = dict(first._qq_settings)  # 等价于 config_store.save 落盘的内容
        self.assertIsNotNone(first.config_store.saved, "tune 应当落盘")

        # 模拟重启：新 plugin 用落盘内容填充设置，再新建 service
        second = FakePlugin(self._tmp.name, settings=persisted)
        service_after = DeepChatService(second)

        self.assertAlmostEqual(
            service_after.mood.decay_rate,
            0.8,
            places=6,
            msg="重启后没有读到持久化的 deepchat_mood_decay_rate",
        )

    def test_unsaved_setting_does_not_leak_across_restart(self) -> None:
        """反向：只改内存没落盘的设置，重启后必须回到默认值。"""
        first = FakePlugin(self._tmp.name)
        DeepChatService(first)  # 构造一次，确保默认值已就位
        first._qq_settings["deepchat_mood_decay_rate"] = 0.8  # 绕过 tune，不落盘
        self.assertNotIn(
            "deepchat_mood_decay_rate",
            first.config_store.saved or {},
            "前置条件：这条设置没有落盘",
        )

        second = FakePlugin(self._tmp.name, settings=dict(first.config_store.saved or {}))
        service_after = DeepChatService(second)
        self.assertAlmostEqual(
            service_after.mood.decay_rate,
            0.05,
            places=6,
            msg="未落盘的设置被当成持久化了",
        )


if __name__ == "__main__":
    unittest.main()
