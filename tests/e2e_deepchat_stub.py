"""End-to-end smoke test for DeepChat without a running N.E.K.O host.

Loads the real plugin package as ``plugin.plugins.qq_auto_reply`` with a
minimal stub SDK, instantiates ``QQAutoReplyPlugin``, then exercises:

  1. prompt enrichment (mood + relationship fragments)
  2. post-turn writeback (mood/relationship SQLite state)
  3. host-mode generation (stubbed session result)
  4. direct-mode generation (stubbed raw-LLM fallback)

Run from the repository root:

    PYTHONPATH=. python tests/e2e_deepchat_stub.py

This script intentionally does not touch the installed N.E.K.O plugin.
"""

from __future__ import annotations

import asyncio
import importlib.abc
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace


def _enrich(service, **kwargs):
    """同步测试里调用 async 的 enrich（公开入口）：等价于 await，但省掉每个用例改 async。"""
    return asyncio.run(service.enrich_system_prompt(**kwargs))

REPO_ROOT = Path(
    os.environ.get("DEEPCHAT_PLUGIN_ROOT", Path(__file__).resolve().parents[1])
).resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ── permissive stub machinery ────────────────────────────────────────────


class _Dummy:
    def __init__(self, name: str = "dummy"):
        self._name = name

    def __call__(self, *args, **kwargs):
        return _Dummy(f"{self._name}()")

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Dummy(f"{self._name}.{name}")

    def __await__(self):
        async def _noop():
            return self

        return _noop().__await__()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True


class _PermissiveModule(types.ModuleType):
    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        value = _Dummy(f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


class _HybridModule(_PermissiveModule):
    """Concrete module that still tolerates unknown names."""


def _module(name: str) -> _HybridModule:
    return _HybridModule(name)


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return _PermissiveModule(spec.name)

    def exec_module(self, module):
        return None


class _StubFinder(importlib.abc.MetaPathFinder):
    PREFIXES = (
        "utils", "main_logic", "config",
        "httpx", "websockets", "aiohttp", "yaml", "tiktoken", "psutil",
        "PIL", "openai", "anthropic", "anyio",
    )

    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + ".") for p in self.PREFIXES):
            return importlib.util.spec_from_loader(fullname, _StubLoader())
        return None


def _install_critical_stubs() -> None:
    """Concrete stubs for modules whose behaviour the plugin needs at import."""

    utils = _module("utils")
    utils.__path__ = []
    sys.modules["utils"] = utils

    file_utils = _module("utils.file_utils")

    async def atomic_write_json_async(path, data):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    async def read_json_async(path, default=None):
        target = Path(path)
        if not target.is_file():
            return default
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return default

    file_utils.atomic_write_json_async = atomic_write_json_async
    file_utils.read_json_async = read_json_async
    sys.modules["utils.file_utils"] = file_utils

    config_manager = _module("utils.config_manager")

    class _ConfigManager:
        def get_model_api_config(self, _name):
            return {}

        def get_character_data(self):
            return ("", "兰兰", "", "", "", "", "", "", "")

    config_manager.get_config_manager = lambda: _ConfigManager()
    sys.modules["utils.config_manager"] = config_manager

    language_utils = _module("utils.language_utils")
    language_utils.is_supported_language_code = lambda _code: False
    language_utils.get_global_language_full = lambda: "zh-CN"
    sys.modules["utils.language_utils"] = language_utils

    internal_http = _module("utils.internal_http_client")

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {}

        @property
        def text(self):
            return ""

    class _Client:
        async def post(self, *args, **kwargs):
            return _Response()

        async def get(self, *args, **kwargs):
            return _Response()

    internal_http.get_internal_http_client = lambda: _Client()
    sys.modules["utils.internal_http_client"] = internal_http

    llm_client = _module("utils.llm_client")

    class _Message:
        def __init__(self, content=""):
            self.content = content

    llm_client.AIMessage = _Message
    llm_client.SystemMessage = _Message
    llm_client.create_chat_llm_async = lambda **kwargs: _Dummy("llm")
    llm_client.strip_thinking_segments = lambda text: text
    sys.modules["utils.llm_client"] = llm_client

    token_tracker = _module("utils.token_tracker")
    token_tracker.set_call_type = lambda *args, **kwargs: None
    sys.modules["utils.token_tracker"] = token_tracker

    config = _module("config")
    config.__path__ = []
    config.MEMORY_SERVER_PORT = 0
    config.USER_PLUGIN_BASE = "http://127.0.0.1:0"
    sys.modules["config"] = config

    config_prompts = _module("config.prompts")
    config_prompts.__path__ = []
    sys.modules["config.prompts"] = config_prompts

    prompts_memory = _module("config.prompts.prompts_memory")
    prompts_memory.RECALL_MEMORY_TOOL_DESCRIPTION = "recall memory"
    prompts_memory.RECALL_MEMORY_TOOL_FOUND_HEADER = "found"
    prompts_memory.RECALL_MEMORY_TOOL_NO_RESULT = "no result"
    prompts_memory.RECALL_MEMORY_TOOL_QUERY_DESCRIPTION = "query"
    prompts_memory.RECALL_MEMORY_TOOL_TIME_DESCRIPTION = "time"
    prompts_memory._normalize_memory_prompt_lang = lambda value=None: "zh-CN"
    sys.modules["config.prompts.prompts_memory"] = prompts_memory

    prompts_sys = _module("config.prompts.prompts_sys")
    prompts_sys._loc = lambda key, default=None: default if default is not None else key
    sys.modules["config.prompts.prompts_sys"] = prompts_sys

    main_logic = _module("main_logic")
    main_logic.__path__ = []
    sys.modules["main_logic"] = main_logic

    tool_calling = _module("main_logic.tool_calling")

    class ToolResult:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class ToolDefinition:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    tool_calling.ToolResult = ToolResult
    tool_calling.ToolDefinition = ToolDefinition
    sys.modules["main_logic.tool_calling"] = tool_calling

    omni = _module("main_logic.omni_offline_client")

    class OmniOfflineClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    omni.OmniOfflineClient = OmniOfflineClient
    sys.modules["main_logic.omni_offline_client"] = omni


def _install_plugin_sdk() -> None:
    plugin_pkg = types.ModuleType("plugin")
    plugin_pkg.__path__ = []
    sys.modules["plugin"] = plugin_pkg

    plugins_pkg = types.ModuleType("plugin.plugins")
    plugins_pkg.__path__ = []
    sys.modules["plugin.plugins"] = plugins_pkg

    sdk_pkg = types.ModuleType("plugin.sdk")
    sdk_pkg.__path__ = []
    sys.modules["plugin.sdk"] = sdk_pkg

    sdk = types.ModuleType("plugin.sdk.plugin")

    class SdkError(Exception):
        def __init__(self, message: str = "", code: str = ""):
            super().__init__(message)
            self.message = message
            self.code = code

    class Ok:
        def __init__(self, data=None):
            self.data = data

    class Err:
        def __init__(self, error):
            self.error = error

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def exception(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class _I18n:
        def t(self, key, default=None, **kwargs):
            text = default if default is not None else key
            try:
                return str(text).format(**kwargs)
            except Exception:
                return str(text)

    class PluginConfig:
        def __init__(self, data):
            self._data = dict(data or {})

        async def dump(self):
            return dict(self._data)

        async def get(self, key, default=None):
            return self._data.get(key, default)

    class NekoPluginBase:
        plugin_id = "qq_auto_reply"

        def __init__(self, ctx):
            self.ctx = ctx
            self.config = ctx.config
            self.logger = ctx.logger
            self.bus = ctx.bus
            self.i18n = ctx.i18n
            self._data_dir = ctx.data_dir
            self._config_dir = ctx.config_dir

        def enable_file_logging(self, **_kwargs):
            return self.logger

        def data_path(self, *parts):
            path = self._data_dir.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            return path

        @property
        def config_dir(self):
            return self._config_dir

        def push_message(self, **kwargs):
            self.ctx.pushed.append(kwargs)

    def neko_plugin(cls):
        return cls

    def lifecycle(**_kwargs):
        def decorate(fn):
            return fn

        return decorate

    def plugin_entry(**_kwargs):
        def decorate(fn):
            return fn

        return decorate

    def tr(key, default=None, **kwargs):
        text = default if default is not None else key
        try:
            return str(text).format(**kwargs)
        except Exception:
            return str(text)

    class _Ui:
        @staticmethod
        def action(**_kwargs):
            return lambda fn: fn

        @staticmethod
        def context(**_kwargs):
            return lambda fn: fn

    sdk.SdkError = SdkError
    sdk.Ok = Ok
    sdk.Err = Err
    sdk.NekoPluginBase = NekoPluginBase
    sdk.neko_plugin = neko_plugin
    sdk.lifecycle = lifecycle
    sdk.plugin_entry = plugin_entry
    sdk.tr = tr
    sdk.ui = _Ui
    sdk.PluginConfig = PluginConfig
    sdk._Logger = _Logger
    sdk._I18n = _I18n
    sys.modules["plugin.sdk.plugin"] = sdk


def _load_plugin_package():
    package_name = "plugin.plugins.qq_auto_reply"
    spec = importlib.util.spec_from_file_location(
        package_name,
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot build plugin package spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


# ── end-to-end checks ───────────────────────────────────────────────────


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    _install_plugin_sdk()
    _install_critical_stubs()
    sys.meta_path.insert(0, _StubFinder())

    module = _load_plugin_package()
    sdk = sys.modules["plugin.sdk.plugin"]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ctx = SimpleNamespace(
            config=sdk.PluginConfig({}),
            logger=sdk._Logger(),
            bus=SimpleNamespace(conversations=_Dummy("bus.conversations"), frames=_Dummy("bus.frames")),
            i18n=sdk._I18n(),
            data_dir=root / "data",
            config_dir=root / "config",
            pushed=[],
        )
        plugin = module.QQAutoReplyPlugin(ctx)

        # 1. prompt enrichment
        prompt = _enrich(plugin.deepchat_service, 
            system_prompt="BASE PROMPT",
            sender_id="10001",
            sender_name="小明",
        )
        _assert(prompt.startswith("BASE PROMPT"), "base prompt must be preserved")
        _assert("当前情绪" in prompt, "mood fragment missing")
        _assert("关系" in prompt, "relationship fragment missing")
        _assert("小明" in prompt, "display name missing from relationship fragment")

        # 2. writeback
        result = plugin.deepchat_service.writeback(
            sender_id="10001", text="谢谢你，好棒", is_group=False,
        )
        _assert(result["signal"] == "praise", f"unexpected signal: {result}")
        state = plugin.deepchat_service.view_state("10001")
        _assert(state["relationship"]["affinity"] > 0, "affinity did not increase")
        _assert(state["relationship"]["interactions"] == 1, "interaction not counted")

        # 3. host-mode generation
        pipeline_models = sys.modules["plugin.plugins.qq_auto_reply.pipeline_models"]
        QQModelResult = pipeline_models.QQModelResult
        plugin._qq_settings["deepchat_generation_mode"] = "host"
        plugin.qq_client = None
        plugin.reply_generation_service.run_primary_session_call = _async_return(
            QQModelResult(reply_text="会话回复", source="session", allow_fallback=False)
        )
        context = SimpleNamespace(
            is_group=False, permission_level="admin",
            sender_id="10001", message="谢谢你", source_kind="incoming",
        )
        host_result = asyncio.run(plugin.reply_model_node.generate(context))
        _assert(host_result.reply_text == "会话回复", "host mode did not use session result")
        _assert(host_result.source == "session", f"host source: {host_result.source}")

        # 4. direct-mode generation
        plugin._qq_settings["deepchat_generation_mode"] = "direct"
        plugin.reply_generation_service.generate_fallback_from_context = _async_return("直连回复")
        plugin.reply_generation_service.run_fallback_memory_hooks = _async_noop()
        direct_result = asyncio.run(plugin.reply_model_node.generate(context))
        _assert(direct_result.reply_text == "直连回复", "direct mode did not use raw LLM result")
        _assert(direct_result.source == "deepchat_direct", f"direct source: {direct_result.source}")

        # 5. integration seams are actually present in the composed plugin
        context_source = inspect.getsource(module.QQReplyContextNode.build)
        _assert("deepchat.enrich_system_prompt" in context_source, "context hook missing")
        generation_source = inspect.getsource(module.QQReplyGenerationService.run_primary_session_call)
        writeback_source = inspect.getsource(module.QQReplyGenerationService.writeback_deepchat)
        # 双向扫描：主路径必须调用写回入口，写回入口里必须真的落到 deepchat。
        # 只扫其中一边都会漏——只扫 helper 就证明不了"主路径确实调了它"。
        _assert(
            "writeback_deepchat" in generation_source and "deepchat.writeback" in writeback_source,
            "writeback hook missing",
        )

        print("PASS: plugin package loaded and instantiated")
        print("PASS: prompt enrichment (mood + relationship)")
        print("PASS: writeback (mood/relationship SQLite)")
        print("PASS: host-mode generation")
        print("PASS: direct-mode generation")
        print("PASS: context/writeback hooks present")
        print(f"DATA: {state}")
    return 0


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def _async_noop():
    async def _inner(*_args, **_kwargs):
        return None

    return _inner


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"FAIL: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
