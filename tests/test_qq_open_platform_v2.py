# -*- coding: utf-8 -*-
"""官方机器人 API v2 能力层（``qq_open_platform_v2``）。

**这里钉住的是"文档说过的事实"**，不是实现的顺手写法：

- 语音能发（``file_type=3``）、图文卡片是 ``msg_type=8``、表情表态只有频道有、
  戳一戳没有接口 —— 这几条一旦被人"顺手改回去"，功能就整条消失，所以逐条断言。
- ``needs_attention`` 必须由"实际收到过全量群消息"驱动，不能写死；写死等于
  接话判定在官方机器人上永远不工作。
- 引用回复要的是 ``REFIDX_`` 令牌（``ext.msg_idx``），**不是消息 id**：填错的话
  平台照样返回成功，只是引用没生效，肉眼看不出来。
- 被动回复过期（``40034005``）只能重发一次，且必须先把 ``msg_id`` 摘掉。
"""

from __future__ import annotations

import asyncio
import re

import pytest
from plugin.plugins.qq_auto_reply.qq_open_platform_v2 import (
    FILE_TYPE_IMAGE,
    FILE_TYPE_VOICE,
    V2_ENDPOINTS,
    QQOpenPlatformUnsupported,
    QQOpenPlatformV2Mixin,
    is_official_bot,
    v2_connection_class,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._payload


class FakeHttp:
    """按脚本回放响应；``requests`` 记下每一次调用，断言"发了什么"就靠它。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests = []

    async def request(self, method, url, headers=None, json=None):
        self.requests.append({"method": method, "url": url, "body": json, "headers": headers or {}})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return FakeResponse({"id": f"msg-{len(self.requests)}", "ext_info": {"ref_idx": f"REFIDX_{len(self.requests)}"}})


class FakeBase:
    """宿主连接对象在插件眼里的那一小截表面。"""

    CHANNEL = "open"
    mode = "open_platform"

    def __init__(self, http=None):
        self._http = http or FakeHttp()
        self.uploads = []
        self._API_BASE = "https://api.sgroup.qq.com"
        self.logger = None
        self._closing = False
        self._ws = None
        self.ws = None
        self._last_seq = 0
        self._self_id = "bot-app-id"
        self._self_nickname = "猫娘"
        self._access_token = "t"
        self._token_expires_at = 0
        self._sent_message_ids = {}
        self._message_queue = asyncio.Queue(maxsize=2)
        self.token_refreshes = 0
        self._v2_recv_setting = ""

    def _auth_headers(self):
        return {"Authorization": f"QQBot {self._access_token}"}

    async def _ensure_token(self):
        return None

    async def _refresh_token(self):
        self.token_refreshes += 1

    def record_sent_message_id(self, message_id):
        self._sent_message_ids[str(message_id)] = 0.0

    def _next_msg_seq(self, reply_id):
        return 1

    @staticmethod
    def _expand_cq_segments(segments):
        return list(segments)

    # 富媒体上传交给 media_seam 分派；连接对象自带这两个方法时它就走宿主适配器。
    # 测试在这里桩掉，正好把"连接器传对了 file_type/scope"和"上传内部怎么谈"分开验。
    async def upload_image(self, *, scope, owner_id, source, token_checked=False):
        self.uploads.append((scope, owner_id, FILE_TYPE_IMAGE, source))
        return "FI-IMAGE"

    async def upload_media(self, *, scope, owner_id, source, file_type):
        self.uploads.append((scope, owner_id, file_type, source))
        return {FILE_TYPE_VOICE: "FI-VOICE"}.get(file_type, "FI-OTHER")

    async def _try_reconnect(self):
        self.reconnected = getattr(self, "reconnected", 0) + 1

    async def _log_identity_probe(self, event_type, data):
        self.probed = getattr(self, "probed", [])
        self.probed.append(event_type)


def make_connection(script=None):
    """按线上路径造对象：能力层与连接器实现合成子类，再构造实例。"""
    return v2_connection_class(FakeBase)(FakeHttp(script))


class Bot(QQOpenPlatformV2Mixin, FakeBase):
    """直接实例化版，省掉换类那一步。"""


# ── 身份判据 ────────────────────────────────────────────

@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (None, False),
        (type("C", (), {"CHANNEL": "open"}), True),
        (type("C", (), {"mode": "open_platform"}), True),
        (type("C", (), {"CHANNEL": "onebot", "mode": "napcat"}), False),
        # 老测试桩只有 needs_attention：维持旧判据，不然一片测试会假装官方通道
        (type("C", (), {"needs_attention": False}), True),
        (type("C", (), {"needs_attention": True}), False),
    ],
)
def test_is_official_bot_prefers_identity_over_side_effect(client, expected):
    assert is_official_bot(client) is expected


# ── 能力旗标：文档口径 ──────────────────────────────────

def test_capability_flags_match_the_v2_docs():
    conn = Bot()
    assert conn.supports_voice is True           # file_type=3 存在，语音能发
    assert conn.can_send_record.__self__ is conn  # 仍是 async 方法，不是属性
    assert conn.supports_files is True
    assert conn.supports_video is True
    assert conn.supports_recall is True
    assert conn.supports_reference is True
    assert conn.supports_ark_cards is False      # v2 发送侧没有 ark
    assert conn.supports_cards is True           # 但图文卡片 msg_type=8 能发
    assert conn.supports_poke is False           # 没有戳一戳接口
    assert conn.supports_reactions is False      # 表情表态仅频道
    assert conn.is_open_platform is True


def test_needs_attention_follows_actual_reception():
    conn = Bot()
    assert conn.needs_attention is False          # @ 模式：进来的每条都是对她说
    assert conn.receives_all_messages is False
    conn._v2_recv_setting = "all"                  # bot_state 说这个群开了全量
    assert conn.receives_all_messages is True
    assert conn.needs_attention is True


def test_group_message_create_event_is_hard_evidence():
    conn = Bot()
    assert conn._v2_note_full_mode() is True
    assert conn._v2_note_full_mode() is False      # 只报一次，不刷屏
    assert conn.receives_all_messages is True


# ── 发送 ────────────────────────────────────────────────

def _sent_body(conn):
    return conn._http.requests[-1]["body"]


def test_plain_text_sends_msg_type_zero():
    conn = Bot()
    asyncio.run(conn.send_group_message("G1", "在吗"))
    assert conn._http.requests[-1]["url"].endswith("/v2/groups/G1/messages")
    assert _sent_body(conn) == {"msg_type": 0, "content": "在吗"}


def test_markdown_is_detected_and_sent_as_msg_type_two():
    conn = Bot()
    asyncio.run(conn.send_group_message("G1", "**重点**：明天交"))
    body = _sent_body(conn)
    assert body["msg_type"] == 2
    assert body["markdown"]["content"].startswith("**重点**")


def test_reply_segment_becomes_passive_msg_id_and_reference_token():
    conn = Bot()
    conn._v2_remember_reference("m-1", "REFIDX_AAA")
    asyncio.run(conn.send_group_message_segments(
        "G1", [
            {"type": "reply", "data": {"id": "m-1"}},
            {"type": "text", "data": {"text": "收到"}},
        ],
    ))
    body = _sent_body(conn)
    assert body["msg_id"] == "m-1"                 # 被动回复（占被动额度）
    assert body["message_reference"] == {"message_id": "REFIDX_AAA"}  # 可见引用（要令牌）


def test_reference_is_skipped_when_the_token_is_unknown():
    conn = Bot()
    asyncio.run(conn.send_group_message_segments(
        "G1", [{"type": "reply", "data": {"id": "never-seen"}},
               {"type": "text", "data": {"text": "x"}}],
    ))
    body = _sent_body(conn)
    assert "message_reference" not in body          # 编一个引用令牌只会让平台报错
    assert body["msg_id"] == "never-seen"


def test_at_segment_uses_the_documented_tag_and_markdown():
    conn = Bot()
    asyncio.run(conn.send_group_message_segments(
        "G1", [{"type": "at", "data": {"qq": "MEMBER1"}},
               {"type": "text", "data": {"text": "看这里"}}],
    ))
    body = _sent_body(conn)
    assert body["msg_type"] == 2
    assert '<qqbot-at-user id="MEMBER1" />' in body["markdown"]["content"]


def test_at_everyone_tag_for_admin_bots():
    conn = Bot()
    asyncio.run(conn.send_group_message_segments(
        "G1", [{"type": "at", "data": {"qq": "all"}},
               {"type": "text", "data": {"text": "开会"}}],
    ))
    assert "<qqbot-at-everyone />" in _sent_body(conn)["markdown"]["content"]


def test_image_is_uploaded_then_sent_with_a_separate_text_leg():
    """图片带文字必须拆两条：msg_type=7 只有一个 media 字段，硬拼会把话挤掉。"""
    conn = Bot()
    asyncio.run(conn.send_group_message_segments(
        "G1", [{"type": "image", "data": {"file": "https://x/y.png"}},
               {"type": "text", "data": {"text": "看图"}}],
    ))
    assert conn.uploads == [("groups", "G1", FILE_TYPE_IMAGE, "https://x/y.png")]
    assert [r["body"]["msg_type"] for r in conn._http.requests] == [0, 7]
    assert conn._http.requests[-1]["body"]["media"] == {"file_info": "FI-IMAGE"}


def test_voice_upload_uses_file_type_three():
    conn = Bot()
    asyncio.run(conn.send_group_record("G1", "/tmp/a.wav"))
    assert conn.uploads == [("groups", "G1", FILE_TYPE_VOICE, "/tmp/a.wav")]
    body = _sent_body(conn)
    assert body["msg_type"] == 7 and body["media"]["file_info"] == "FI-VOICE"


def test_voice_carries_no_text_or_at_but_keeps_the_passive_window():
    """语音就是一条语音（不夹文字/@），但必须保留 msg_id。

    摘掉 msg_id 这条语音就变成主动消息：用户关掉"接受主动推送"时整条发不出去，
    配额也比被动紧得多。
    """
    conn = Bot()
    asyncio.run(conn.send_group_record("G1", "/tmp/a.wav", reply_message_id="m1", at_user_id="u1"))
    body = _sent_body(conn)
    assert body["msg_type"] == 7 and "content" not in body
    assert body["msg_id"] == "m1"
    assert "keyboard" not in body


def test_keyboard_becomes_button_row_and_markdown():
    conn = Bot()
    asyncio.run(conn.send_group_message_segments(
        "G1", [{"type": "text", "data": {"text": "选一个"}}], keyboard="吃|睡"
    ))
    body = _sent_body(conn)
    buttons = body["keyboard"]["content"]["rows"][0]["buttons"]
    assert [b["render_data"]["label"] for b in buttons] == ["吃", "睡"]
    assert body["msg_type"] == 2 and "content" not in body


def test_card_segment_uses_msg_type_eight_and_https_only():
    conn = Bot()
    asyncio.run(conn.send_group_message_segments(
        "G1", [{"type": "card", "data": {"card": {
            "title": "标题", "description": "说明", "url": "http://a/b", "pic_url": "x.png",
        }}}],
    ))
    card = _sent_body(conn)["card"]
    assert card["type"] == "tuwen"
    assert card["content"]["url"].startswith("https://")
    assert card["content"]["pic_url"].startswith("https://")


def test_file_and_video_senders_exist_and_route_to_the_right_type():
    conn = Bot()
    asyncio.run(conn.send_group_video("G1", "https://x/m.mp4"))
    assert conn.uploads[-1][2] == 2
    asyncio.run(conn.send_private_file("U1", "/tmp/doc.pdf"))
    assert conn.uploads[-1][:3] == ("users", "U1", 4)


def test_ark_card_request_is_translated_not_dropped():
    """reply_pipeline 给的是 NapCat 的 ark kv；连接器要把它换成图文卡片。"""
    conn = Bot()
    ok = asyncio.run(conn.send_group_ark_card("G1", {"ark": {"kv": [
        {"key": "#TITLE#", "value": "今日"}, {"key": "#DESC#", "value": "看点"},
        {"key": "#URL#", "value": "https://a"}, {"key": "#IMG#", "value": "https://i"},
    ]}}))
    body = _sent_body(conn)
    assert ok is True
    assert body["msg_type"] == 8
    assert body["card"]["content"] == {
        "title": "今日", "description": "看点", "url": "https://a", "pic_url": "https://i",
    }


def test_typing_notify_is_group_free_and_c2c_only():
    conn = Bot()
    assert asyncio.run(conn.set_typing("U1", seconds=3)) is True
    assert _sent_body(conn) == {"msg_type": 6, "input_notify": {"input_type": 1, "input_second": 3}}
    assert conn._http.requests[-1]["url"].endswith("/v2/users/U1/messages")


def test_recall_uses_delete_and_needs_a_target():
    conn = Bot()
    assert asyncio.run(conn.recall_message("M1", group_id="G1")) is True
    assert conn._http.requests[-1]["method"] == "DELETE"
    assert conn._http.requests[-1]["url"].endswith("/v2/groups/G1/messages/M1")
    assert asyncio.run(conn.recall_message("M1")) is False   # 不知道在哪个会话就不猜


# ── 错误处理 ────────────────────────────────────────────

def test_expired_passive_reply_resends_once_as_proactive():
    conn = Bot(FakeHttp([
        FakeResponse({"err_code": 40034005, "message": "回复消息msg_id已过期"}, 400),
        FakeResponse({"id": "ok-2"}),
    ]))
    message_id = asyncio.run(conn._v2_send("groups", "G1", content="补一条", msg_id="old"))
    assert message_id == "ok-2"
    bodies = [r["body"] for r in conn._http.requests]
    assert "msg_id" in bodies[0] and "msg_id" not in bodies[1]  # 第二次是主动消息
    assert len(bodies) == 2


def test_token_expired_refreshes_then_retries_once():
    conn = Bot(FakeHttp([
        FakeResponse({"err_code": 40011027}, 401),
        FakeResponse({"id": "ok"}),
    ]))
    assert asyncio.run(conn._v2_send("users", "U1", content="hi")) == "ok"
    assert conn.token_refreshes == 1
    assert len(conn._http.requests) == 2


def test_muted_group_error_is_recorded_and_visible():
    """850018 不记下来，group_send_safety 就会让她一直撞墙。"""
    conn = Bot(FakeHttp([FakeResponse({"err_code": 850018, "message": "群被禁言或者机器人被禁言"}, 400)]))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(conn._v2_send("groups", "G9", content="x"))
    assert "850018" in str(caught.value)      # 如实抛出，不静默当成发成功
    assert conn.is_group_muted("G9") is True  # 并且记下，5 分钟内不再撞


def test_api_error_carries_err_code_and_trace_id():
    conn = Bot(FakeHttp([FakeResponse({"err_code": 11253, "message": "无权限", "trace_id": "T1"}, 400)]))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(conn.get_group_info("G1"))
    assert "11253" in str(caught.value) and "T1" in str(caught.value)


# ── 不支持的操作：要说清楚 ─────────────────────────────

async def _await(coro):
    return await coro


def test_self_member_lookup_uses_bot_state_not_a_404():
    """group_send_safety 每 5 秒查一次"我是不是被禁言了"，用 app 级 id 查成员必 404。"""
    conn = Bot(FakeHttp([FakeResponse({
        "member_openid": "BOTMEMBER", "member_role": "admin", "allow_proactive_msg": False,
        "recv_msg_setting": "all", "joined_at": "2026-01-01T00:00:00+08:00",
    })]))
    data = asyncio.run(conn.get_group_member_info("G1", "bot-app-id"))
    assert data["member_openid"] == "BOTMEMBER" and data["member_role"] == "admin"
    assert conn._http.requests[-1]["url"].endswith("/v2/groups/G1/bot_state")
    assert conn.receives_all_messages is True
    assert conn._v2_bot_member_openid == "BOTMEMBER"


def test_unsupported_operations_raise_instead_of_faking_success():
    conn = Bot()
    for name in ("set_msg_emoji_like", "send_group_poke", "get_group_msg_history",
                 "send_group_forward_msg", "get_cookies", "set_group_whole_ban"):
        with pytest.raises(QQOpenPlatformUnsupported):
            asyncio.run(getattr(conn, name)())


def test_endpoint_table_covers_the_documented_index():
    """``V2_ENDPOINTS`` 就是"官方机器人可用功能"的全部清单。

    它是直通调用的白名单：表少了，入口就少一条能力；表里的路径写错，就会拿
    一个假路径去打平台，报出来的错和真因无关。这里把文档索引里的条目逐个点名。
    """
    documented = {
        # 消息 / 单聊与群聊
        "send_c2c_message", "send_group_message", "recall_c2c_message", "recall_group_message",
        "send_c2c_stream_message",
        # 富媒体（图/视频/语音/文件，单聊与群聊不互通）
        "upload_group_media", "upload_c2c_media", "upload_group_prepare",
        "upload_group_part_finish", "upload_c2c_prepare", "upload_c2c_part_finish",
        # 群资料与成员
        "get_group_info", "get_group_bot_state", "get_group_members", "get_group_member",
        "remove_group_members",
        # 入群申请与自动审批策略
        "list_group_join_requests", "approve_group_join_request",
        "list_join_approval_strategies", "create_join_approval_strategy",
        "update_join_approval_strategy", "delete_join_approval_strategy",
        "execute_join_approval_strategy", "update_strategy_whitelist_users",
        # 黑名单与禁言
        "list_group_member_blacklist", "operate_group_member_blacklist",
        "get_group_mute_setting", "set_group_mute_setting",
        # 互动与菜单面板
        "respond_interaction", "get_menu", "set_menu", "list_panels", "create_panel",
        "get_panel", "update_panel", "delete_panel", "set_panel_target",
        # 机器人与分享
        "get_bot_info", "get_bot_guilds", "get_gateway", "generate_url_link",
    }
    assert documented <= set(V2_ENDPOINTS), sorted(documented - set(V2_ENDPOINTS))
    assert len(V2_ENDPOINTS) >= 47          # 官方接口索引一共 47 条
    for endpoint_id, (method, template, summary) in V2_ENDPOINTS.items():
        assert method in ("GET", "POST", "PUT", "PATCH", "DELETE"), endpoint_id
        assert template.startswith("/"), endpoint_id
        assert summary, endpoint_id
        names = re.findall(r"\{(\w+)\}", template)
        # 花括号成对、占位符不重复：不匹配的话 _v2_format_path 会拼出半截 URL。
        assert template.count("{") == len(names) == template.count("}"), template
        assert len(names) == len(set(names)), template


def test_v2_call_refuses_names_that_are_not_on_the_list():
    conn = Bot()
    with pytest.raises(ValueError):
        asyncio.run(conn.v2_call("delete_everything"))
    with pytest.raises(ValueError):
        asyncio.run(conn.v2_call("get_group_info"))          # 路径参数不能省
    conn2 = Bot(FakeHttp([FakeResponse({"group_name": "读书会"})]))
    data = asyncio.run(conn2.v2_call("get_group_info", path_params={"group_openid": "G1"}))
    assert data["group_name"] == "读书会"
    assert conn2._http.requests[-1]["url"].endswith("/v2/groups/G1/info")


def test_v2_class_is_cached_and_wins_over_the_base_implementation():
    cached = v2_connection_class(FakeBase)
    assert v2_connection_class(FakeBase) is cached       # 每种基类只合成一次
    assert cached.__mro__[:3] == (cached, QQOpenPlatformV2Mixin, FakeBase)
    assert not hasattr(FakeBase, "capability_snapshot")   # 基类本身没被改动
    conn = make_connection()
    assert conn.supports_voice is True
    assert conn.receives_all_messages is False


def test_construction_falls_back_when_the_connector_adds_required_args(monkeypatch):
    """宿主连接器哪天加了必填参数时，宁可少一层能力，也不能把启动卡住。"""
    from plugin.plugins.qq_auto_reply import connector_seam

    class Drifted(FakeBase):
        def __init__(self, *, app_id, client_secret, new_required):
            super().__init__()

    monkeypatch.setattr(connector_seam, "QQOpenPlatformConnection", Drifted)
    fallback = {"called": False}

    def fake_factory(settings, **_kw):
        fallback["called"] = True
        return FakeBase()

    monkeypatch.setattr(connector_seam, "create_onebot_connection", fake_factory)
    from plugin.plugins.qq_auto_reply.qq_open_platform_v2 import create_open_platform_connection

    conn = create_open_platform_connection(
        {"qq_open_app_id": "a", "qq_open_client_secret": "b"},
    )
    assert fallback["called"] is True
    assert type(conn) is FakeBase                          # 退回原实现
    assert not hasattr(conn, "capability_snapshot")

def test_numeric_group_id_error_says_which_id_namespace_is_expected():
    """40011028 的真因通常是"拿 QQ 群号调官方接口"，平台原文会把人带偏。"""
    conn = Bot(FakeHttp([
        FakeResponse({"err_code": 40011028, "message": "请求的资源不存在(用户/群已注销)"}, 400),
        FakeResponse({"err_code": 40011028, "message": "请求的资源不存在"}, 400),
    ]))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(conn.get_group_info("1065604240"))
    assert "group_openid" in str(caught.value) and "40011028" in str(caught.value)

    # 已经是 openid 形态就别给这条提示 —— 那时"群不存在"就是它字面的意思。
    with pytest.raises(RuntimeError) as caught2:
        asyncio.run(conn.get_group_info("A1B2" * 16))
    assert "group_openid" not in str(caught2.value)


# ── 指令面板：平台的必填项是真的必填 ─────────────────────

def test_list_panels_requires_scope_because_the_platform_does():
    """漏传 scope 时平台回 40030011 生效场景不合法 —— 真机撞过一次。"""
    conn = Bot()
    with pytest.raises(ValueError):
        asyncio.run(conn.list_panels())
    conn2 = Bot(FakeHttp([FakeResponse({"records": [], "is_end": True})]))
    asyncio.run(conn2.list_panels("group", limit=10))
    last = conn2._http.requests[-1]
    assert last["method"] == "GET" and last["url"].endswith("/v2/panels?scope=group&limit=10")


def test_panel_target_only_accepts_add_or_del_with_targets():
    conn = Bot()
    with pytest.raises(ValueError):
        asyncio.run(conn.set_panel_target("p1", "toggle"))
    with pytest.raises(ValueError):
        asyncio.run(conn.set_panel_target("p1", "add"))
    conn2 = Bot(FakeHttp([FakeResponse({})]))
    out = asyncio.run(conn2.set_panel_target("p1", "add", group_openids=["G1"]))
    assert out["op"] == "add"
    assert conn2._http.requests[-1]["body"] == {"op": "add", "group_openids": ["G1"]}


# ── 互动召回（可选）──────────────────────────────────

def test_unanchored_private_send_can_use_the_wakeup_channel():
    """开了召回：私聊且没有被动锚点 -> is_wakeup=true；群发与带锚点的不碰它。"""
    conn = Bot()
    conn._v2_wakeup_reader = lambda: True
    asyncio.run(conn._v2_send("users", "U1", content="还没睡吗"))
    assert _sent_body(conn)["is_wakeup"] is True

    asyncio.run(conn._v2_send("users", "U1", content="收到", msg_id="m-1", msg_seq=1))
    assert "is_wakeup" not in _sent_body(conn)

    asyncio.run(conn._v2_send("groups", "G1", content="顺便说一句"))
    assert "is_wakeup" not in _sent_body(conn)

    asyncio.run(conn._v2_send("users", "U1", msg_type=6, input_notify={"input_type": 1}))
    assert "is_wakeup" not in _sent_body(conn)      # 输入状态提示不占召回额度


def test_wakeup_is_off_unless_the_setting_says_otherwise():
    conn = Bot()
    asyncio.run(conn._v2_send("users", "U1", content="普通主动"))
    assert "is_wakeup" not in _sent_body(conn)


def test_bot_member_openid_is_learned_from_a_single_mention_at_event():
    """bot_state 是白名单能力；学不到自己的 member_openid 就会把"@ 她"判成没 @ 她。"""
    conn = Bot()
    at = conn._v2_normalize_message("GROUP_AT_MESSAGE_CREATE", _group_event_payload(
        content="<@BOTMEMBER> 在吗", mentions=[{"member_openid": "BOTMEMBER"}]), "e")
    assert at["is_at_bot"] is True
    assert conn._v2_bot_member_openid == "BOTMEMBER"

    # 之后同一条群消息从**全量**事件推来，也能认出是在 @ 她
    full = conn._v2_normalize_message("GROUP_MESSAGE_CREATE", _group_event_payload(
        content="<@BOTMEMBER> 还在吗", mentions=[{"member_openid": "BOTMEMBER"}]), "e2")
    assert full["is_at_bot"] is True
    assert full["mentions_other_user"] is False


def _group_event_payload(**over):
    data = {
        "id": "m-9", "content": "hi",
        "author": {"member_openid": "MEMBER_A", "username": "小明"},
        "group_openid": "G1", "message_type": 0,
        "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_M9"]},
    }
    data.update(over)
    return data


def test_groups_seen_reports_counts_and_untrusted_groups():
    conn = Bot()
    conn._v2_seen_group("G1")
    conn._v2_seen_group("G1")
    groups = asyncio.run(conn.get_group_list())
    assert groups[0]["group_id"] == "G1" and groups[0]["messages_seen"] == 2
