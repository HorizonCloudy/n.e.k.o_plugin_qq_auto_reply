# -*- coding: utf-8 -*-
r"""离线冒烟：官方机器人能力层到底挂上了没有。

跑法（不需要宿主仓库、不需要 QQ 凭据、不发任何网络请求）：

    python tests/e2e_open_platform_stub.py

为什么单独一个脚本而不是 pytest 用例：这里要验的是**接线**——真的导入插件包、
真的按设置造出官方机器人连接对象、真的走入口拿到 Ok/Err。单元测试用假基类
测不到"元类冲突""ABC 布局不让换 __class__""入口漏注册"这一类只在真装配时
才暴露的问题（这三条在本次开发里都真的撞上过）。

复用 ``e2e_deepchat_stub`` 的 stub SDK 与导入钩子；那两个东西一-直是"插件能脱离
宿主被加载"的前提。websockets / httpx 用**真的**（连接器要 import 它们），
所以别在缺这两个包的裸环境里跑。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_deepchat_stub as E  # noqa: E402

_FAILS: list[str] = []


def payload(result):
    """取 Result 的载荷。stub SDK 用 ``.data``，真 SDK 属性名可能不同 —— 两个都认。"""
    for name in ("data", "value", "result"):
        data = getattr(result, name, None)
        if isinstance(data, dict):
            return data
    return {}


def check(condition: bool, message: str) -> None:
    if not condition:
        _FAILS.append(message)


def main() -> int:
    E._install_plugin_sdk()
    E._install_critical_stubs()
    # utils.http 得是"包"，否则 from utils.http.internal_client import ... 会炸
    http_pkg = E._module("utils.http")
    http_pkg.__path__ = []
    sys.modules["utils.http"] = http_pkg
    # 连接器要真的 websockets/httpx（stub 会把它们变成假模块）
    E._StubFinder.PREFIXES = tuple(
        name for name in E._StubFinder.PREFIXES if name not in ("httpx", "websockets")
    )
    sys.meta_path.insert(0, E._StubFinder())

    module = E._load_plugin_package()
    sdk = sys.modules["plugin.sdk.plugin"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ctx = SimpleNamespace(
            config=sdk.PluginConfig({}),
            logger=sdk._Logger(),
            bus=SimpleNamespace(
                conversations=E._Dummy("bus.conversations"), frames=E._Dummy("bus.frames"),
            ),
            i18n=sdk._I18n(),
            data_dir=root / "data",
            config_dir=root / "config",
            pushed=[],
        )
        plugin = module.QQAutoReplyPlugin(ctx)
        plugin._emit_log = lambda *_a, **_k: None

        # ── 官方机器人通道 ──────────────────────────────────
        plugin._qq_settings = {
            "qq_connection_mode": "open_platform",
            "qq_open_app_id": "1",
            "qq_open_client_secret": "2",
        }
        conn = plugin._make_qq_connection()
        check(
            type(conn).__name__ == "QQOpenPlatformConnectionV2",
            f"能力层没挂上，实际类型 {type(conn).__name__}",
        )
        check(getattr(conn, "is_open_platform", False) is True, "身份旗标没上")
        caps = conn.capability_snapshot()
        for key, want in (("voice", True), ("file", True), ("recall", True),
                          ("reference", True), ("rich_card", True),
                          ("ark_card", False), ("poke", False), ("reactions", False)):
            check(caps[key] is want, f"能力旗标 {key} 应为 {want}，实际 {caps[key]}")
        check(caps["receives_all_messages"] is False, "@ 模式默认不该自称收到全量")
        check(conn.needs_attention is False, "@ 模式不该启用注意力竞争")
        check(getattr(conn, "_v2_listeners", []), "官方事件监听器没注册")

        plugin.qq_client = conn
        ok = asyncio.run(plugin.open_platform(action="capabilities"))
        check(type(ok).__name__ == "Ok", f"capabilities 应返回 Ok，实际 {type(ok).__name__}")
        ok_data = payload(ok)
        check(ok_data.get("capabilities", {}).get("voice") is True, "capabilities 载荷缺内容")
        check(len(ok_data.get("endpoints", [])) >= 47, "接口索引表缩水了")

        conn._http = None      # 不发真实请求：让"没连接"这条路径自己说话
        err = asyncio.run(plugin.open_platform(action="mute", group_id="G1", member_openid="M1"))
        check(type(err).__name__ == "Err", "接口调用失败必须报 Err，不能假装成功")
        unknown = asyncio.run(plugin.open_platform(action="kick_everybody", group_id="G1"))
        check(type(unknown).__name__ == "Err" and "未知动作" in str(unknown.error),
              "表外的动作要挡住")

        handled = asyncio.run(
            plugin._on_qq_open_platform_event("OPEN_PLATFORM_FULL_MESSAGES_ON", {}),
        )
        check(handled is True, "全量模式事件没人接手")
        ignored = asyncio.run(plugin._on_qq_open_platform_event("FRIEND_ADD", {"group_id": "G"}))
        check(ignored is False, "非消息事件不该被当成已处理")

        # ── OneBot 通道：入口必须拒绝，也不该被升级 ─────────
        plugin._qq_settings = {"qq_connection_mode": "napcat", "onebot_url": "ws://127.0.0.1:1"}
        onebot = plugin._make_qq_connection()
        check(getattr(onebot, "is_open_platform", False) is False, "OneBot 连接被误升级")
        plugin.qq_client = onebot
        refused = asyncio.run(plugin.open_platform(action="mute", group_id="G1", member_openid="M1"))
        check(type(refused).__name__ == "Err", "OneBot 通道必须拒绝官方机器人动作")
        report = asyncio.run(plugin.open_platform(action="capabilities"))
        check(payload(report).get("connected") is False, "OneBot 通道的 capabilities 该说没连")

    seam = importlib.import_module("plugin.plugins.qq_auto_reply.connector_seam")
    check(seam.CONNECTOR_SOURCE in ("host", "vendored"), "连接器解析没结果")

    if _FAILS:
        for line in _FAILS:
            print(f"FAIL: {line}")
        return 1
    print(f"E2E OK（连接器来源 {seam.CONNECTOR_SOURCE}，官方机器人能力层已挂载）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"FAIL: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
