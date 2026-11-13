# -*- coding: utf-8 -*-
"""官方机器人事件流：``_receive_loop``、``_v2_dispatch``、入站归一化。

钉的是"平台推来的东西有没有被扔掉"。基类的接收循环只认 ``GROUP_AT_MESSAGE_CREATE``
和 ``C2C_MESSAGE_CREATE``，其余事件直接丢；上层因此从来不知道有全量群消息、
入群申请、按钮点击这回事。
"""

from __future__ import annotations

import asyncio
import json

from plugin.plugins.qq_auto_reply import enrichment
from plugin.plugins.qq_auto_reply.qq_open_platform_v2 import (
    QQOpenPlatformV2Mixin,
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
    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests = []

    async def request(self, method, url, headers=None, json=None):
        self.requests.append({"method": method, "url": url, "body": json})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return FakeResponse({"id": f"msg-{len(self.requests)}"})


class FakeWs:
    """只放得下给定几帧的网关：第 N+1 次 recv 抛断线，走重连那条分支。"""

    def __init__(self, conn, frames):
        self.conn = conn
        self.frames = list(frames)
        self.sent = []

    async def recv(self):
        if not self.frames:
            raise ConnectionResetError("frames exhausted")
        return self.frames.pop(0)

    async def send(self, text):
        self.sent.append(text)

    async def close(self):
        return None


class FakeBase:
    CHANNEL = "open"
    mode = "open_platform"

    def __init__(self, http=None):
        self._http = http or FakeHttp()
        self._API_BASE = "https://api.bot.qq.com"
        self.logger = None
        self._closing = False
        self._ws = None
        self.ws = None
        self._last_seq = 0
        self._self_id = "bot-app-id"
        self._self_nickname = "猫娘"
        self._access_token = "t"
        self._message_queue = asyncio.Queue(maxsize=8)
        self._sent_message_ids = {}
        self.reconnects = 0

    def _auth_headers(self):
        return {"Authorization": f"QQBot {self._access_token}"}

    async def _ensure_token(self):
        return None

    async def _refresh_token(self):
        return None

    def record_sent_message_id(self, message_id):
        self._sent_message_ids[str(message_id)] = 0.0

    async def _try_reconnect(self):
        # 真实的 _try_reconnect 会重建 self._ws 并继续收帧；测试里不重建 ——
        # 结束条件是"重连过一次"，不是"永远收下去"。
        self.reconnects += 1
        self._closing = True

    def _log_identity_probe(self, event_type, data):
        self.probed = getattr(self, "probed", [])
        self.probed.append(event_type)


class Bot(QQOpenPlatformV2Mixin, FakeBase):
    pass


def run(coro):
    return asyncio.run(coro)


def frame(op, *, t=None, d=None, s=7, event_id="evt-1"):
    payload = {"op": op, "s": s, "d": {} if d is None else d}
    if t is not None:
        payload["t"] = t
    if event_id:
        payload["id"] = event_id
    return json.dumps(payload)


def drive(conn, frames):
    ws = FakeWs(conn, frames)
    conn._ws = ws
    conn.ws = ws
    run(conn._receive_loop())
    return ws


def _group_event(**over):
    data = {
        "id": "m-1",
        "content": "大家早上好",
        "author": {"member_openid": "MEMBER_A", "username": "小明", "bot": False},
        "group_openid": "G1",
        "timestamp": "2026-11-12T09:00:00+08:00",
        "message_type": 0,
        "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_M1", "auth_token=x"]},
    }
    data.update(over)
    return data


# ── 接收循环 ────────────────────────────────────────────

def test_receive_loop_passes_every_dispatch_frame_and_answers_heartbeat():
    conn = Bot()
    ws = drive(conn, [
        frame(0, t="C2C_MESSAGE_CREATE", d={"id": "c1", "content": "在吗",
                                            "author": {"user_openid": "U1"}}),
        frame(1),
        frame(11),
        frame(0, t="GROUP_MESSAGE_CREATE", d=_group_event()),
    ])
    assert ws.sent == [json.dumps({"op": 11, "d": 7})]      # op=1 要按最新 s 回执
    assert conn._last_seq == 7
    queued = [conn._message_queue.get_nowait() for _ in range(2)]
    assert [m["message_type"] for m in queued] == ["private", "group"]
    assert conn.reconnects == 1                             # 帧耗尽 -> 走重连，不崩
    assert conn.probed == ["C2C_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"]  # 取证探针仍在白名单之前


def test_reconnect_request_clears_the_socket_for_the_reconnect_path():
    conn = Bot()
    drive(conn, [frame(7)])
    assert conn._ws is None and conn.ws is None
    assert conn.reconnects == 1
    assert conn._closing is True            # 循环交给重连，不在原地打转


# ── 入站归一化 ──────────────────────────────────────────

def test_full_mode_message_uses_the_unified_shape():
    msg = Bot()._v2_normalize_message("GROUP_MESSAGE_CREATE", _group_event(), "evt-9")
    assert msg["message_type"] == "group"
    assert msg["channel"] == "open"
    assert msg["group_id"] == "G1"                        # v2 只给 group_openid
    assert msg["user_id"] == "MEMBER_A"
    assert msg["user_nickname"] == "小明"                  # 昵称平台一直给了
    assert msg["is_at_bot"] is False                      # 全量模式：没 @ 她
    assert msg["event_id"] == "evt-9"


def test_group_id_prefers_group_id_then_group_openid():
    conn = Bot()
    both = conn._v2_normalize_message("GROUP_AT_MESSAGE_CREATE",
                                      _group_event(group_id="G-plain", group_openid="G-openid"), "e")
    assert both["group_id"] == "G-plain"                  # 旧键在时不能换轨（记忆按字节匹配）


def test_at_tags_in_v2_format_become_mentions():
    conn = Bot()
    conn._v2_bot_member_openid = "BOTMEMBER"
    msg = conn._v2_normalize_message("GROUP_MESSAGE_CREATE", _group_event(
        content="<@BOTMEMBER> 在吗 <@MEMBER_B> 你也看看", mentions=[{"member_openid": "MEMBER_B"}]), "e")
    assert msg["is_at_bot"] is True
    assert msg["mentioned_user_ids"] == ["MEMBER_B", "BOTMEMBER"]
    assert msg["mentions_other_user"] is True
    assert "<@" not in msg["content"]


def test_at_everyone_is_recorded_separately():
    msg = Bot()._v2_normalize_message("GROUP_MESSAGE_CREATE", _group_event(content="<@all> 集合"), "e")
    assert msg["mentions_all"] is True and msg["content"] == "集合"


def test_reference_is_resolved_through_the_ref_idx_maps():
    conn = Bot()
    conn._v2_send_ref_map = {"REFIDX_BOT": "bot-msg-7"}
    conn._sent_message_ids["bot-msg-7"] = 0.0
    msg = conn._v2_normalize_message("GROUP_MESSAGE_CREATE", _group_event(
        message_type=103, message_scene={
            "ext": ["msg_idx=REFIDX_M1", "ref_msg_idx=REFIDX_BOT"]}), "e")
    assert msg["quoted_message_id"] == "bot-msg-7"
    assert msg["is_reply_to_bot"] is True
    assert conn._v2_reference_token_for("m-1") == "REFIDX_M1"   # 回引用它要用这个令牌


def test_attachments_keep_name_size_and_the_platform_voice_transcript():
    conn = Bot()
    msg = conn._v2_normalize_message("GROUP_MESSAGE_CREATE", _group_event(content="", attachments=[
        {"content_type": "voice", "url": "https://x/a.silk", "filename": "a.silk",
         "voice_wav_url": "https://x/a.wav", "asr_refer_text": "今晚八点开会", "size": 12},
        {"content_type": "image/png", "url": "https://x/p.png", "width": 10, "height": 20},
        {"content_type": "video/mp4", "url": "https://x/v.mp4"},
        {"content_type": "file", "url": "https://x/d.pdf", "filename": "d.pdf"},
    ]), "e")
    assert [a["type"] for a in msg["attachments"]] == ["voice", "image", "video", "file"]
    voice = msg["attachments"][0]
    assert voice["wav_url"] == "https://x/a.wav" and voice["asr_text"] == "今晚八点开会"
    assert msg["attachments"][1]["width"] == 10
    assert msg["attachments"][3]["name"] == "d.pdf"
    assert msg["content"] == "今晚八点开会"                # 平台识别好的文字直接进正文


def test_image_only_message_keeps_content_empty():
    msg = Bot()._v2_normalize_message("GROUP_MESSAGE_CREATE", _group_event(
        content="", attachments=[{"content_type": "image/png", "url": "https://x/p.png"}]), "e")
    assert msg["content"] == ""


# ── 非消息事件 ──────────────────────────────────────────

def test_full_mode_is_announced_exactly_once():
    conn = Bot()
    calls = []
    conn.add_event_listener(lambda et, p: calls.append(et) or False)
    run(conn._v2_dispatch("GROUP_MESSAGE_CREATE", _group_event(), "e"))
    assert calls == ["OPEN_PLATFORM_FULL_MESSAGES_ON"]
    run(conn._v2_dispatch("GROUP_MESSAGE_CREATE", _group_event(id="m2"), "e2"))
    assert calls == ["OPEN_PLATFORM_FULL_MESSAGES_ON"]      # 不重复播报
    assert conn.receives_all_messages is True


def test_group_add_robot_probes_state_and_remembers_the_group():
    conn = Bot(FakeHttp([FakeResponse({
        "member_openid": "BOTM", "member_role": "admin",
        "recv_msg_setting": "all", "allow_proactive_msg": True,
    })]))
    seen = []
    conn.add_event_listener(lambda et, p: seen.append((et, p.get("group_id"))) or False)
    run(conn._v2_dispatch("GROUP_ADD_ROBOT", {"group_openid": "G7", "op_member_openid": "OP"}, "e1"))
    assert conn._http.requests[-1]["url"].endswith("/v2/groups/G7/bot_state")
    assert conn.bot_member_role == "admin"
    assert conn.allow_proactive_msg is True
    assert conn.receives_all_messages is True
    assert seen == [("GROUP_ADD_ROBOT", "G7")]
    assert [g["group_id"] for g in run(conn.get_group_list())] == ["G7"]


def test_msg_receive_notice_re_probes_the_setting():
    conn = Bot(FakeHttp([FakeResponse({"recv_msg_setting": "all"})]))
    run(conn._v2_dispatch("GROUP_MSG_RECEIVE", {"group_openid": "G3"}, "e"))
    assert conn._http.requests[-1]["url"].endswith("/v2/groups/G3/bot_state")


def test_interaction_is_acked_with_the_button_data_exposed():
    conn = Bot()
    got = {}
    conn.add_event_listener(lambda et, p: got.update(p) or False)
    run(conn._v2_dispatch("INTERACTION_CREATE", {
        "id": "int-1", "type": 12,
        "data": {"resolved": {"button": {"id": "btn_0", "data": "/吃饭"}}},
        "author": {"user_openid": "U1"}, "group_openid": "G1",
    }, "e1"))
    assert got["button_data"] == "/吃饭" and got["button_id"] == "btn_0"
    ack = conn._http.requests[-1]
    assert ack["method"] == "PUT" and ack["url"].endswith("/interactions/int-1")
    assert ack["body"] == {"code": 0}


def test_a_handler_that_took_the_interaction_stops_the_auto_ack():
    conn = Bot()
    conn.add_event_listener(lambda et, p: True)
    run(conn._v2_dispatch("INTERACTION_CREATE", {"id": "int-2"}, "e1"))
    assert conn._http.requests == []


def test_join_request_notice_carries_what_the_approver_needs():
    conn = Bot()
    got = {}
    conn.add_event_listener(lambda et, p: got.update(p) or False)
    run(conn._v2_dispatch("GROUP_JOIN_REQUEST", {
        "group_openid": "G1", "member_openid": "M9", "username": "新人",
        "join_request_id": "JR-1", "apply_source": "self_apply",
    }, "e1"))
    assert got["join_request_id"] == "JR-1" and got["username"] == "新人"
    assert got["group_id"] == "G1" and got["event_id"] == "e1"


def test_unknown_event_is_logged_not_swallowed():
    conn = Bot()
    logs = []
    conn._emit_log = lambda level, msg: logs.append(msg)
    run(conn._v2_dispatch("SOME_NEW_PLATFORM_EVENT", {"x": 1}, "e1"))
    assert any("SOME_NEW_PLATFORM_EVENT" in line for line in logs)


def test_listener_exception_does_not_take_the_connection_down():
    conn = Bot()

    def boom(event_type, payload):
        raise RuntimeError("监听器坏了")

    conn.add_event_listener(boom)
    logs = []
    conn._emit_log = lambda level, msg: logs.append(msg)
    run(conn._v2_dispatch("GROUP_DEL_ROBOT", {"group_openid": "G1"}, "e"))   # 不抛
    assert any("监听器异常" in line for line in logs)


def test_queue_overflow_keeps_the_newest_message():
    conn = Bot()
    conn._message_queue = asyncio.Queue(maxsize=1)
    for mid, text in (("a", "第一句"), ("b", "第二句")):
        run(conn._v2_dispatch("C2C_MESSAGE_CREATE",
                              {"id": mid, "content": text, "author": {"user_openid": "U"}}, "e"))
    assert conn._message_queue.get_nowait()["content"] == "第二句"


def test_malformed_dispatch_payload_is_ignored():
    conn = Bot()
    run(conn._v2_dispatch("GROUP_MESSAGE_CREATE", "not-a-dict", "e"))
    assert conn._message_queue.empty()


def test_video_and_file_attachments_reach_the_file_renderer():
    """能力层分完档以后，视频仍然要走文件渲染链路。

    连接器以前把视频笼统算成 ``file``、渲染器只认 ``file``；能力层给了它正确的
    ``type="video"`` 之后，渲染器不改就会让"对方发了个视频"重新变成看到空气。
    语音反过来**不该**进这条链路：平台给的 ASR 文本已经写进正文了。
    """
    conn = Bot()
    msg = conn._v2_normalize_message("GROUP_MESSAGE_CREATE", _group_event(
        content="", attachments=[
            {"content_type": "video/mp4", "url": "https://x/v.mp4", "filename": "v.mp4"},
            {"content_type": "file", "url": "https://x/d.pdf", "filename": "d.pdf"},
            {"content_type": "voice", "url": "https://x/a.silk", "asr_refer_text": "开会"},
        ]), "e")
    files = enrichment.QQMessageEnricher._attachment_files(msg)
    assert {f["name"] for f in files} == {"v.mp4", "d.pdf"}
    assert all(f["url"].startswith("https://") for f in files)
    assert msg["content"] == "开会"


def test_unnamed_video_attachment_still_gets_a_readable_label():
    files = enrichment.QQMessageEnricher._attachment_files(
        {"attachments": [{"type": "video", "url": "https://x/no-name"}]}
    )
    assert files and files[0]["name"] in ("no-name", "视频")


def test_the_real_connector_class_can_be_upgraded():
    """真的连接器实现（宿主或内置副本，随 connector_seam 解析）也要能合成子类。

    换类那招在 ABC 基类上会直接抛 ``object layout differs``，所以这里改验"合成子类"：
    元类必须跟着基类（ABCMeta），否则 metaclass conflict 让能力层永远挂不上。
    """
    from plugin.plugins.qq_auto_reply import connector_seam

    base = connector_seam.QQOpenPlatformConnection
    subclass = v2_connection_class(base)
    assert isinstance(subclass, type(base).__class__) or issubclass(type(subclass), type(base).__class__)
    assert QQOpenPlatformV2Mixin in subclass.__mro__
    conn = subclass(app_id="a", client_secret="b")
    assert conn.supports_voice is True and conn.supports_ark_cards is False
    assert conn.needs_attention is False
