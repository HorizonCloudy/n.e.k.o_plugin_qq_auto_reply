# -*- coding: utf-8 -*-
"""官方机器人能力面提示词（`capabilities_open_platform` 层）。

写法取自 MaiBot 的 `planner_reply_capabilities`：**只声明这一轮真的在范围内的东西**，
并且明说"能用不等于一定发得出去，以实际结果为准"。

这个文件钉三件事，都是这类提示词最容易烂掉的地方：

1. **清单是现场生成的** —— 来源是当前连接的能力旗标、当前配置、已授权的插件分档。
   写死一份静态清单必然与平台权限漂移，而漂移的方向是"她向用户承诺自己做不到的事"
   （本仓库为 `<file>`/`<music>` 这类标签踩过一次，见 prompt_fragment_templates 里的 ⚠️ 注释）。
2. **做不到的事要明说**，否则她会答应"下次戳你一下""我给你贴个表情"。
3. **这一层必须真的通电，且只在官方机器人通道**：OneBot 那侧能力不一样，写进来就是假话。
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply import prompt_fragment_templates as frag
from plugin.plugins.qq_auto_reply.session_instruction_service import (
    QQSessionInstructionService,
)

PLUGIN = pathlib.Path(__file__).resolve().parents[1]


class _Bridge:
    def __init__(self, tiers):
        self._tiers = tiers

    def tiers(self):
        return dict(self._tiers)


class _Conn:
    """官方机器人连接对象在提示词眼里的那一小截表面。"""

    is_open_platform = True
    supports_voice = True
    supports_files = True
    supports_video = True
    supports_cards = True
    supports_keyboard = True
    supports_reference = True
    supports_recall = True
    bot_member_role = ""


def _service(conn=None, **settings) -> QQSessionInstructionService:
    logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    tiers = settings.pop("_tiers", {})
    plugin = SimpleNamespace(
        qq_client=conn, _qq_settings=settings, logger=logger,
        plugin_tool_service=_Bridge(tiers),
        data_path=lambda name: str(pathlib.Path(__file__).parent / "_scratch" / name),
    )
    return QQSessionInstructionService(plugin)


# ── 1. 清单跟着能力旗标与配置走 ──────────────────────────

def test_capability_lines_follow_the_live_flags():
    text = _service(_Conn())._build_open_platform_capability_lines()
    for needle in ("语音", "文件", "视频", "图文卡片", "按钮", "引用", "撤回"):
        assert needle in text, f"能力旗标开着却没写进清单: {needle}"


def test_capabilities_that_are_off_are_not_advertised():
    class _NoRich(_Conn):
        supports_voice = False
        supports_video = False
        supports_files = False
        supports_cards = False

    text = _service(_NoRich())._build_open_platform_capability_lines()
    assert "<record>" not in text
    assert "视频" not in text
    assert "图文卡片" not in text


def test_the_unusable_three_are_named_so_she_does_not_promise_them():
    text = _service(_Conn())._build_open_platform_capability_lines()
    for needle in ("戳一戳", "贴表情", "翻历史"):
        assert needle in text, f"做不到的事没写清楚: {needle}"


def test_platform_reply_limit_is_stated_because_splitting_hits_it():
    """回复条数上限必须和分段一起说：拆太碎会被平台拒掉后半段。"""
    text = _service(_Conn())._build_open_platform_capability_lines()
    assert "最多回复 5" in text and "60 分钟" in text
    assert "强制分段" not in text                      # 没开就别写

    text = _service(_Conn(), force_sentence_split=True)._build_open_platform_capability_lines()
    assert "强制分段已开" in text


def test_admin_only_powers_appear_only_for_admins():
    assert "@全体成员" not in _service(_Conn())._build_open_platform_capability_lines()
    conn = _Conn()
    conn.bot_member_role = "admin"
    assert "@全体成员" in _service(conn)._build_open_platform_capability_lines()


def test_proactive_line_names_the_channel_actually_used():
    text = _service(_Conn(), proactive_initiative_enabled=True)._build_open_platform_capability_lines()
    assert "主动消息" in text and "主动找管理员私聊" in text

    text = _service(
        _Conn(), proactive_initiative_enabled=True, qq_open_proactive_wakeup=True,
    )._build_open_platform_capability_lines()
    assert "互动召回" in text

    # 没开主动搭话就不该出现这条，否则她以为可以随时开口
    assert "主动找管理员私聊" not in _service(_Conn())._build_open_platform_capability_lines()


# ── 2. 外部能力：只列授权过的 ────────────────────────────

def test_no_external_capability_line_without_grants():
    text = _service(_Conn())._build_open_platform_capability_lines()
    assert "已授权的外部插件能力" not in text


def test_granted_plugins_are_listed_with_their_tier():
    text = _service(
        _Conn(), _tiers={"plugin_database": "admin", "neko_image_embed": "all"},
    )._build_open_platform_capability_lines()
    assert "plugin_database" in text and "只有管理员" in text
    assert "neko_image_embed" in text and "所有人都可以" in text
    # 真机核对过的边界：图像记忆库的 img_send 发不进 QQ
    assert "发不进 QQ" in text


def test_bridge_failure_does_not_break_the_prompt():
    class _Broken:
        def tiers(self):
            raise RuntimeError("注册表没起来")

    plugin = SimpleNamespace(
        qq_client=_Conn(), _qq_settings={},
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        plugin_tool_service=_Broken(),
        data_path=lambda name: str(pathlib.Path(__file__).parent / "_scratch" / name),
    )
    text = QQSessionInstructionService(plugin)._build_open_platform_capability_lines()
    assert "戳一戳" in text          # 外部能力缺了不影响主清单


# ── 3. 层必须通电，且只在官方机器人通道 ─────────────────

def test_layer_is_registered_with_a_default_template_and_placeholder():
    layer = next(
        item for item in QQSessionInstructionService._PROMPT_LAYERS
        if item["id"] == "capabilities_open_platform"
    )
    assert layer["i18n_key"] == "capability_scope_section_open_platform"
    assert "{capabilities}" in layer["required_placeholders"]
    defaults = frag.layer_default_templates()
    assert defaults[layer["i18n_key"]] is frag.CAPABILITY_SCOPE_SECTION_OPEN_PLATFORM
    assert "{capabilities}" in frag.CAPABILITY_SCOPE_SECTION_OPEN_PLATFORM


def test_section_is_injected_only_on_the_official_channel():
    src = (PLUGIN / "session_instruction_service.py").read_text(encoding="utf-8")
    body = src.split("def build_session_instructions", 1)[1].split("\n    def ", 1)[0]

    open_branch = body.split("if is_open_plat:", 1)[1].split("else:", 1)[0]
    assert "capability_scope_section_open_platform" in open_branch, "官方通道没注入能力面层"

    else_branch = body.split("else:", 1)[1].split("format_section = format_section.format", 1)[0]
    assert 'capability_section = ""' in else_branch, "OneBot 通道必须把这一段置空"

    assert body.index("format_section,") < body.index("capability_section,"), "没拼进 sections"


def test_new_templates_do_not_repeat_a_theme_a_third_time():
    """卫生闸门同样管新段落：同一主题最多两处（见 test_qq_prompt_hygiene）。"""
    for name in ("CAPABILITY_SCOPE_SECTION_OPEN_PLATFORM",):
        text = getattr(frag, name)
        assert not re.search(r"emoji|颜文字|动作描写|系统提示词|插件实现|记忆检索过程", text), name
