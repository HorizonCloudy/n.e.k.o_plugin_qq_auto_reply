"""P2 review：direct 模式绕过了 host 管线的 writeback 钩子，必须自己回写。

direct 模式走 raw-LLM 路径（`generate_fallback_from_context`），不经过
`context`/`writeback` 钩子；如果不在 `_run_direct_primary` 里补一次
`deepchat.writeback(...)`，情绪与关系状态就永远不会更新。
反向要求同样重要：`source_kind="synthetic"` 的轮次不得回写，否则同一条
用户消息会被计两次。
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sqlite3
import sys
import tempfile
import time
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
QQModelResult = _load_plugin_module("pipeline_models").QQModelResult

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
        # host 模式的 primary 调用：主开关关掉时会走到这里
        self.run_primary_session_call = AsyncMock(
            return_value=QQModelResult(reply_text="host-reply", source="primary")
        )

    async def writeback_deepchat(self, context: QQReplyContext) -> None:
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


    def test_node_awaits_writeback_without_blocking_loop(self) -> None:
        """本用例**替换**服务层实现，因此不覆盖 `writeback_deepchat` 的内部写法。

        它守的是节点侧纪律：必须 await 回写（不能 fire-and-forget），且 await 期间
        同一 loop 上的心跳/定时任务还能跑。服务层是否走 `to_thread` 由
        `test_writeback_is_offloaded_to_a_thread`（AST 契约）负责。
        """
        import time as _time

        BLOCK = 0.5
        max_gap = {"value": 0.0}
        finished = {"value": False}

        async def blocking_writeback(context) -> None:
            await asyncio.to_thread(_time.sleep, BLOCK)
            finished["value"] = True

        self.plugin.reply_generation_service.writeback_deepchat = blocking_writeback

        async def scenario() -> None:
            # 预热默认 executor：首次建线程本身可能上百 ms，不该算进"是否阻塞 loop"
            await asyncio.to_thread(lambda: None)
            last = {"t": _time.perf_counter()}
            stop = {"flag": False}

            async def ticker() -> None:
                while not stop["flag"]:
                    await asyncio.sleep(0.02)
                    now = _time.perf_counter()
                    max_gap["value"] = max(max_gap["value"], now - last["t"])
                    last["t"] = now

            tick = asyncio.create_task(ticker())
            started = _time.perf_counter()
            await QQReplyModelNode(self.plugin).generate(_context())
            elapsed = _time.perf_counter() - started
            stop["flag"] = True
            await tick
            return elapsed

        elapsed = asyncio.run(scenario())

        self.assertTrue(finished["value"], "回写必须被 await 完成，不能 fire-and-forget")
        self.assertGreaterEqual(elapsed, BLOCK, "await 应等回写真正跑完")
        self.assertLess(
            max_gap["value"],
            BLOCK / 2,
            f"回写阻塞了事件循环：ticker 最大间隔 {max_gap['value'] * 1000:.0f}ms"
            f"（阻塞 {BLOCK * 1000:.0f}ms 的一半以上）",
        )


    def test_writeback_fails_fast_under_lock_contention(self) -> None:
        """锁争用时必须快速失败（`timeout=0.5`），而不是等默认 5s。

        另一连接持 `BEGIN IMMEDIATE` 制造写锁争用。阈值 1.5s 落在"0.5s 量级"与
        "默认 5s"之间，余量很大，不会因 CI 抖动假红。
        """
        db_path = Path(self._tmp.name) / "deepchat.sqlite3"
        holder = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            started = time.perf_counter()
            with self.assertRaises(sqlite3.OperationalError):
                self.service.writeback(sender_id="u1", text="谢谢")
            elapsed = time.perf_counter() - started
        finally:
            holder.execute("ROLLBACK")
            holder.close()

        self.assertLess(
            elapsed,
            1.5,
            f"锁等待应在 0.5s 量级返回，实际 {elapsed:.2f}s（是不是退回默认 5s 了？）",
        )

class WritebackThreadContractTests(unittest.TestCase):
    """``reply_generation_service`` 载不进单测（宿主依赖），所以对它做源码级契约。"""

    def test_writeback_is_offloaded_to_a_thread(self) -> None:
        """AST 契约：真实实现必须把 `deepchat.writeback` 交给 to_thread，且不得有同步调用。

        刻意不用缩进敏感的字面量断言（`assertNotIn("            deepchat.writeback(", ...)`）：
        有人把调用挪进 if/try 多一层、或统一格式化之后，那条断言会在**保护已消失**的
        情况下继续绿。AST 判定对"退回同步调用"和"只改缩进"都有效。
        """
        source = (_REPO_ROOT / "reply_generation_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def _callee(node: ast.AST) -> str:
            if isinstance(node, ast.Attribute):
                return node.attr
            return getattr(node, "id", "")

        offloaded = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node.func) == "to_thread":
                args = list(node.args) + [kw.value for kw in node.keywords]
                if any(
                    isinstance(arg, ast.Attribute) and arg.attr == "writeback"
                    for arg in args
                ):
                    offloaded = True

        sync_calls: list[int] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _callee(node.func) == "writeback"):
                continue
            ancestor = parents.get(node)
            wrapped = False
            while ancestor is not None:
                if isinstance(ancestor, ast.Call) and _callee(ancestor.func) == "to_thread":
                    wrapped = True
                    break
                ancestor = parents.get(ancestor)
            if not wrapped:
                sync_calls.append(node.lineno)

        self.assertTrue(
            offloaded, "必须把 deepchat.writeback 作为参数交给 asyncio.to_thread"
        )
        self.assertEqual(
            sync_calls,
            [],
            f"不得在事件循环里同步调用 deepchat.writeback（违规行号 {sync_calls}）",
        )



class MasterSwitchRoutingTests(unittest.TestCase):
    """P2-②：`deepchat_enabled` 关掉后不得再走 DeepChat 自己的生成路径。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = FakePlugin(self._tmp.name)
        self.plugin._qq_settings["deepchat_generation_mode"] = "direct"
        self.service = DeepChatService(self.plugin)
        self.plugin.deepchat_service = self.service

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_disabled_forces_host_path(self) -> None:
        self.plugin._qq_settings["deepchat_enabled"] = False

        self.assertFalse(self.service.enabled)
        self.assertEqual(
            self.service.generation_mode,
            "host",
            "主开关关掉后必须回到 host 路径，否则会绕过宿主会话",
        )

        result = asyncio.run(QQReplyModelNode(self.plugin).generate(_context()))
        self.assertNotEqual(
            result.source,
            "deepchat_direct",
            "主开关关掉后节点仍走了 direct 路径",
        )


class EnrichLoopBlockingTests(unittest.TestCase):
    """P2-④：`enrich_system_prompt` 目前在 async 的 context 节点里同步执行。

    它内部会 `mood.get()`（valenсe≠0 时**每次读都写库**）与 `relationship.get()`
    （跨结算边界时会写），所以锁争用会把整个事件循环拖住。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = FakePlugin(self._tmp.name)
        self.service = DeepChatService(self.plugin)
        self.plugin.deepchat_service = self.service
        self.service.set_mood("u1", "happy")  # valenсe≠0 → mood.get 每读必写
        self.db_path = Path(self._tmp.name) / "deepchat.sqlite3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _hold_write_lock(self) -> sqlite3.Connection:
        holder = sqlite3.connect(str(self.db_path), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        return holder

    def test_enrich_does_not_block_event_loop(self) -> None:
        holder = self._hold_write_lock()
        gaps: list[float] = []
        stop = {"flag": False}
        try:

            async def scenario() -> None:
                last = {"t": time.perf_counter()}

                async def ticker() -> None:
                    while not stop["flag"]:
                        await asyncio.sleep(0.02)
                        now = time.perf_counter()
                        gaps.append((now - last["t"]) * 1000)
                        last["t"] = now

                task = asyncio.create_task(ticker())
                await asyncio.sleep(0.05)
                try:
                    self.service.enrich_system_prompt(
                        system_prompt="BASE", sender_id="u1", sender_name="小明"
                    )
                except Exception:  # noqa: BLE001 —— 锁争用下抛错可以，阻塞不行
                    pass
                await asyncio.sleep(0.05)
                stop["flag"] = True
                await task

            asyncio.run(scenario())
        finally:
            holder.execute("ROLLBACK")
            holder.close()

        worst = max(gaps) if gaps else 0.0
        self.assertLess(
            worst,
            250.0,
            f"enrich 把事件循环阻塞了 {worst:.0f}ms（应在线程里跑）",
        )

    def test_enrich_fails_fast_under_lock_contention(self) -> None:
        """`timeout=0.5` 同样保护 enrich 的写路径：不得等默认 5s。"""
        holder = self._hold_write_lock()
        try:
            started = time.perf_counter()
            try:
                self.service.enrich_system_prompt(
                    system_prompt="BASE", sender_id="u1", sender_name="小明"
                )
            except sqlite3.OperationalError:
                pass
            elapsed = time.perf_counter() - started
        finally:
            holder.execute("ROLLBACK")
            holder.close()

        self.assertLess(
            elapsed,
            1.5,
            f"锁等待应在 0.5s 量级返回，实际 {elapsed:.2f}s（是不是退回默认 5s 了？）",
        )


if __name__ == "__main__":
    unittest.main()
