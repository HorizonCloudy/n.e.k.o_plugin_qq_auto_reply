"""P2 review：direct 模式绕过了 host 管线的 writeback 钩子，必须自己回写。

direct 模式走 raw-LLM 路径（`generate_fallback_from_context`），不经过
`context`/`writeback` 钩子；如果不在 `_run_direct_primary` 里补一次
`deepchat.writeback(...)`，情绪与关系状态就永远不会更新。
反向要求同样重要：`source_kind="synthetic"` 的轮次不得回写，否则同一条
用户消息会被计两次。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from deepchat.service import DeepChatService

#: 仓库根同时是插件包根：`reply_model_node` 用 `from .pipeline_models import ...`，
#: 因此必须把根目录当成一个包来加载，直接 `import reply_model_node` 会因
#: 相对导入无父包而失败。这里只加载需要的两个模块，不执行 `__init__.py`
#: （那是 160KB 的宿主入口，会拖进 utils/httpx 等一堆宿主依赖）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG = "_qq_plugin_under_test"


def _load_plugin_module(module_name: str) -> types.ModuleType:
    if _PKG not in sys.modules:
        package = types.ModuleType(_PKG)
        package.__path__ = [str(_REPO_ROOT)]  # type: ignore[attr-defined]
        sys.modules[_PKG] = package
    full_name = f"{_PKG}.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, _REPO_ROOT / f"{module_name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


QQReplyContext = _load_plugin_module("pipeline_models").QQReplyContext
QQReplyModelNode = _load_plugin_module("reply_model_node").QQReplyModelNode
is_synthetic_source = _load_plugin_module("pipeline_models").is_synthetic_source
SYNTHETIC_SOURCE_KINDS = _load_plugin_module("pipeline_models").SYNTHETIC_SOURCE_KINDS

#: review 描述里写的是 `source_kind="synthetic"`，但那不是生产取值——
#: 生产只认 `SYNTHETIC_SOURCE_KINDS` 里这几个常量，`"synthetic"` 会被判成
#: 真实用户轮（写回照常发生）。所以这里取一个真实的合成轮类型。
SYNTHETIC_KIND = "proactive_private"


class FakeReplyGenerationService:
    """只复刻 `writeback_deepchat` 的对外语义，不引宿主依赖。

    `reply_generation_service.py` 会 `import main_logic.tool_calling` /
    `utils.llm_client`，无法在单测里直接载入；这里按它的实现照抄判定顺序
    （先看 deepchat 是否存在、再看是否为 synthetic 轮），并记录调用次数，
    这样"节点有没有回写"和"合成轮有没有被跳过"都能被观察。
    """

    def __init__(self, plugin: "FakePlugin") -> None:
        self.plugin = plugin
        self.writeback_calls: list[QQReplyContext] = []
        self.generate_fallback_from_context = AsyncMock(return_value="ok")
        self.run_fallback_memory_hooks = AsyncMock(return_value=None)

    def writeback_deepchat(self, context: QQReplyContext) -> None:
        self.writeback_calls.append(context)
        deepchat = self.plugin.deepchat_service
        if deepchat is None or is_synthetic_source(getattr(context, "source_kind", "") or ""):
            return
        deepchat.writeback(
            sender_id=context.sender_id,
            text=context.message,
            is_group=context.is_group,
            group_id=context.group_id,
        )


class FakePlugin:
    def __init__(self, data_dir: str) -> None:
        self._qq_settings: dict = {}
        self._data_dir = data_dir
        self._her_name = "兰兰"
        self.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
        self._user_sessions: dict = {}
        self.qq_client = SimpleNamespace(needs_attention=False)
        self.deepchat_service = None
        self.reply_generation_service = FakeReplyGenerationService(self)

    def data_path(self) -> Path:
        return Path(self._data_dir)

    def _build_session_key(
        self, *, sender_id: str, is_group: bool = False, group_id: str | None = None
    ) -> str:
        return f"group:{group_id or ''}" if is_group else f"private:{sender_id}"


def _context(**overrides) -> QQReplyContext:
    base: dict = {
        "message": "你好",
        "attachments": None,
        "permission_level": "trusted",
        "sender_id": "u1",
        "is_group": False,
        "group_id": None,
        "user_nickname": "小明",
        "use_memory_context": False,
        "persist_memory": True,
        "ephemeral_session": False,
        "group_facing": False,
        "group_scene_mode": "",
        "scene_mode": "",
        "master_name": "",
        "her_name": "兰兰",
        "user_title": "",
        "character_prompt": "",
        "character_card_fields": {},
        "prompt_message": "你好",
        "system_prompt": "BASE",
        "memory_context_used": False,
        "core_memory_text": "",
        "recalled_memory_text": "",
        "recalled_memory_used": False,
        "login_status": "online",
        "login_self_id": "10000",
        "login_nickname": "bot",
    }
    base.update(overrides)
    return QQReplyContext(**base)


class DirectModeWritebackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = FakePlugin(self._tmp.name)
        self.plugin._qq_settings["deepchat_generation_mode"] = "direct"
        self.service = DeepChatService(self.plugin)
        self.plugin.deepchat_service = self.service

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _interactions(self, user_id: str = "u1") -> int:
        return self.service.relationship.get(user_id).interactions

    def test_direct_generation_calls_deepchat_writeback_once(self) -> None:
        """review 的核心：direct 模式不经过 host 的 context/writeback 钩子，
        节点必须自己补一次回写，且只补一次。"""
        context = _context()
        asyncio.run(QQReplyModelNode(self.plugin).generate(context))

        calls = self.plugin.reply_generation_service.writeback_calls
        self.assertEqual(len(calls), 1, f"direct 成功路径应回写恰好一次，实际 {len(calls)} 次")
        self.assertEqual(calls[0].sender_id, "u1")
        self.assertEqual(calls[0].message, "你好")

    def test_direct_generation_writes_back_relationship(self) -> None:
        result = asyncio.run(QQReplyModelNode(self.plugin).generate(_context()))

        self.assertEqual(result.source, "deepchat_direct")
        self.assertGreater(
            self._interactions(), 0, "direct 模式必须自己回写，否则关系状态永不更新"
        )

    def test_synthetic_turn_does_not_write_back(self) -> None:
        """合成控制轮不写回：它不计入真实互动。

        跳过逻辑在 `reply_generation_service.writeback_deepchat` 里（宿主模块
        无法在单测里载入，见 FakeReplyGenerationService 的说明），所以这里断言
        的是端到端结果：节点照常调用、但状态不变。
        """
        self.assertIn(
            SYNTHETIC_KIND,
            SYNTHETIC_SOURCE_KINDS,
            "用的是生产登记的合成轮类型，而不是自造字符串",
        )
        asyncio.run(
            QQReplyModelNode(self.plugin).generate(_context(source_kind=SYNTHETIC_KIND))
        )

        self.assertEqual(
            self._interactions(), 0, "合成轮不得回写，否则同一条消息被计两次"
        )


if __name__ == "__main__":
    unittest.main()
