# -*- coding: utf-8 -*-
"""官方机器人直通入口（``qq_open_platform_service``）的分派与校验。

这里不管"HTTP 怎么发"（那是 ``test_qq_open_platform_v2.py`` 的事），只管三件事：

1. 参数缺了要当场说清楚，不要拿一个 None 去调接口然后报"发送失败"；
2. 不在官方机器人通道时必须**拒绝**，不能假装成功；
3. 动作表就是入口的全部能力面 —— 表外的动作名进不来。
"""

from __future__ import annotations

import asyncio

import pytest
from plugin.plugins.qq_auto_reply.qq_open_platform_service import ACTIONS, QQOpenPlatformService


class FakeConn:
    is_open_platform = True

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _rec(self, name, *args, **kw):
        self.calls.append((name, args, kw))

    def capability_snapshot(self):
        return {"voice": True, "file": True, "recall": True, "reference": True,
                "rich_card": True, "keyboard": True, "recv_msg_setting": "all",
                "receives_all_messages": True, "bot_member_role": "admin"}

    def v2_endpoint_catalog(self):
        return [{"id": "recall_group_message", "method": "DELETE", "path": "/x", "summary": "撤回"}]

    async def recall_message(self, message_id, *, group_id="", user_id=""):
        self._rec("recall", message_id, group_id=group_id, user_id=user_id)
        return True

    async def get_group_info(self, group_id=""):
        self._rec("group_info", group_id)
        return {"group_name": "读书会", "group_member_num": 256}

    async def get_group_bot_state(self, group_id, *, force=False):
        self._rec("bot_state", group_id, force=force)
        return {"member_role": "admin", "_at": 1.0}

    async def get_group_member_list(self, group_id="", **kw):
        self._rec("members", group_id, kw)
        return [{"member_openid": "M1", "username": "小明"}]

    async def get_group_member_info(self, group_id="", user_id=""):
        self._rec("member", group_id, user_id)
        return {"member_openid": user_id}

    async def set_group_ban(self, group_id="", user_id="", duration=600):
        self._rec("ban", group_id, user_id, duration)
        return {"status": "ok"}

    async def set_group_kick(self, group_id="", user_id="", **kw):
        self._rec("kick", group_id, user_id, **kw)
        return {"status": "ok"}

    async def list_group_join_requests(self, group_id="", cursor="", limit=20):
        self._rec("join_requests", group_id, cursor, limit)
        return {"list": [], "next_cursor": ""}

    async def approve_group_join_request(self, group_id, member_openid, **kw):
        self._rec("approve", group_id, member_openid, **kw)
        return {"status": "ok"}

    async def operate_group_member_blacklist(self, group_id, ids, *, op="add"):
        self._rec("blacklist", group_id, tuple(ids), op)
        return {"fail_openids": []}

    async def list_group_member_blacklist(self, group_id, cursor=""):
        self._rec("blacklist_list", group_id, cursor)
        return {"list": []}

    async def set_typing(self, user_id, *, seconds=3):
        self._rec("typing", user_id, seconds)
        return True

    async def send_group_record(self, group_id, file_uri, **kw):
        self._rec("voice_group", group_id, file_uri)
        return "msg-voice"

    async def send_private_video(self, user_id, file_uri, **kw):
        self._rec("video_private", user_id, file_uri)
        return None

    async def send_group_file(self, group_id, file_uri, **kw):
        self._rec("file_group", group_id, file_uri, **kw)
        return "msg-file"

    async def send_group_ark_card(self, group_id, ark_obj, **kw):
        self._rec("card", group_id, ark_obj)
        return True

    async def get_menu(self):
        self._rec("menu_get")
        return {"menu": {"items": []}}

    async def set_menu(self, menu):
        self._rec("menu_set", menu)
        return {"status": "ok"}

    async def list_panels(self, scope="", cursor="", limit=20):
        # 与真连接器的必填校验保持一致（契约本身在 test_qq_open_platform_v2 里钉）
        if not str(scope or "").strip():
            raise ValueError("scope 只能是 c2c/group/channel/dm（平台必填）")
        self._rec("panels", scope, cursor, limit)
        return {"records": []}

    async def set_panel_target(self, panel_id, op, *, user_openids=None, group_openids=None):
        self._rec("panel_target", panel_id, op, user_openids or [], group_openids or [])
        return {"status": "ok"}

    async def create_panel(self, payload):
        self._rec("panel_create", payload)
        return {"id": "p-1"}

    async def generate_url_link(self, callback_data=""):
        self._rec("link", callback_data)
        return {"url": "https://q.qq.com/..."}

    async def respond_interaction(self, interaction_id, code=0):
        self._rec("interaction", interaction_id, code)
        return {"status": "ok"}

    async def v2_call(self, endpoint_id, *, path_params=None, params=None, body=None):
        self._rec("api", endpoint_id, path_params, params, body)
        return {"ok": True}


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)

    def info(self, msg):
        return None


class FakePlugin:
    def __init__(self, conn):
        self.qq_client = conn
        self.logger = FakeLogger()


def service(conn=FakeConn()):
    return FakePlugin(conn), QQOpenPlatformService(FakePlugin(conn))


def run(coro):
    return asyncio.run(coro)


def _call(action, **params):
    plugin, svc = service()
    return plugin, svc, run(svc.call(action, params))


def test_every_action_is_reachable_and_named_in_lowercase():
    assert "capabilities" in ACTIONS and "api" in ACTIONS
    assert all(name == name.lower() for name in ACTIONS)
    assert len(ACTIONS) >= 20


def test_unknown_action_is_rejected_with_the_list_of_what_exists():
    with pytest.raises(ValueError) as caught:
        run(QQOpenPlatformService(FakePlugin(FakeConn())).call("kick_everybody", {}))
    message = str(caught.value)
    assert "未知动作" in message and "mute" in message


def test_capabilities_reports_the_connection_table():
    _p, _s, out = _call("capabilities")
    assert out["connected"] is True
    assert out["capabilities"]["voice"] is True
    assert out["actions"] == sorted(ACTIONS)
    assert out["endpoints"] == ["recall_group_message"]


def test_actions_refuse_to_run_on_an_onebot_channel():
    conn = FakeConn()
    conn.is_open_platform = False
    conn.CHANNEL = "onebot"
    conn.mode = "napcat"
    svc = QQOpenPlatformService(FakePlugin(conn))
    with pytest.raises(RuntimeError):
        run(svc.call("mute", {"group_id": "G1", "member_openid": "M1", "seconds": 60}))
    # capabilities 是查询：这条路不通也要给出可读的答复，而不是抛
    assert run(svc.call("capabilities", {}))["connected"] is False


def test_capabilities_on_onebot_says_so_instead_of_raising():
    conn = FakeConn()
    conn.is_open_platform = False
    conn.CHANNEL = "onebot"
    out = run(QQOpenPlatformService(FakePlugin(conn)).describe())
    assert out["connected"] is False and "OneBot" in out["hint"]


def test_official_channel_without_the_capability_layer_is_reported_not_faked():
    """官方通道在、能力层没挂上（升级被宿主类挡住）时要说实话。"""
    from types import SimpleNamespace

    conn = SimpleNamespace(CHANNEL="open", mode="open_platform")   # 没有 is_open_platform
    plugin = FakePlugin(conn)
    svc = QQOpenPlatformService(plugin)
    with pytest.raises(RuntimeError):
        run(svc.call("group_info", {"group_id": "G1"}))
    assert any("能力层未挂载" in line for line in plugin.logger.warnings)


@pytest.mark.parametrize(
    ("action", "params", "expected"),
    [
        ("recall", {"message_id": "m1", "group_id": "G1"}, ("recall", ("m1",), {"group_id": "G1", "user_id": ""})),
        ("group_info", {"group_id": "G1"}, ("group_info", ("G1",), {})),
        ("bot_state", {"group_id": "G1"}, ("bot_state", ("G1",), {"force": False})),
        ("group_member", {"group_id": "G1", "member_openid": "M1"}, ("member", ("G1", "M1"), {})),
        ("mute", {"group_id": "G1", "member_openid": "M1", "seconds": 120}, ("ban", ("G1", "M1", 120), {})),
        ("unmute", {"group_id": "G1", "member_openid": "M1"}, ("ban", ("G1", "M1", 0), {})),
        ("kick", {"group_id": "G1", "member_openid": "M1"},
         ("kick", ("G1", "M1"), {"add_to_member_blacklist": False})),
        ("typing", {"user_id": "U1", "seconds": 5}, ("typing", ("U1", 5), {})),
        ("send_voice", {"group_id": "G1", "path": "/tmp/a.wav"}, ("voice_group", ("G1", "/tmp/a.wav"), {})),
        ("send_file", {"group_id": "G1", "path": "/tmp/d.pdf"}, ("file_group", ("G1", "/tmp/d.pdf"), {})),
        ("url_link", {"callback_data": "abc"}, ("link", ("abc",), {})),
        ("respond_interaction", {"interaction_id": "i1", "code": 4}, ("interaction", ("i1", 4), {})),
        ("menu_set", {"menu": {"items": []}}, ("menu_set", ({"items": []},), {})),
        ("api", {"endpoint_id": "get_group_info", "path_params": {"group_openid": "G1"}},
         ("api", ("get_group_info", {"group_openid": "G1"}, {}, None), {})),
    ],
)
def test_action_maps_to_the_right_connector_call(action, params, expected):
    plugin, svc = service()
    run(svc.call(action, params))
    assert plugin.qq_client.calls[-1] == expected


def test_group_members_flattens_the_paged_result():
    plugin, svc = service()
    out = run(svc.call("group_members", {"group_id": "G1"}))
    assert out == {"members": [{"member_openid": "M1", "username": "小明"}], "count": 1}


def test_blacklist_switches_between_list_and_operate():
    plugin, svc = service()
    run(svc.call("blacklist", {"group_id": "G1"}))
    assert plugin.qq_client.calls[-1][0] == "blacklist_list"
    run(svc.call("blacklist", {"group_id": "G1", "member_openids": ["M1"], "op": "del"}))
    assert plugin.qq_client.calls[-1] == ("blacklist", ("G1", ("M1",), "del"), {})


def test_join_request_approval_passes_the_reason_and_blacklist():
    plugin, svc = service()
    run(svc.call("approve_join", {
        "group_id": "G1", "member_openid": "M1", "approve": False,
        "reason": "看简介", "add_to_blacklist": True, "join_request_id": "JR1",
    }))
    _name, args, kw = plugin.qq_client.calls[-1]
    assert args == ("G1", "M1")
    assert kw == {"approve": False, "join_request_id": "JR1", "reject_reason": "看简介",
                  "add_to_blacklist": True}


def test_card_action_builds_a_tuwen_card():
    plugin, svc = service()
    out = run(svc.call("send_card", {"group_id": "G1", "title": "标题", "description": "说明",
                                     "url": "https://a", "pic_url": "https://i"}))
    assert out == {"sent": True}
    assert plugin.qq_client.calls[-1][1] == ("G1", {"card": {"type": "tuwen", "content": {
        "title": "标题", "description": "说明", "url": "https://a", "pic_url": "https://i"}}})


@pytest.mark.parametrize(
    ("action", "params", "why"),
    [
        ("recall", {"message_id": "m1"}, "没说在哪个会话"),
        ("recall", {"group_id": "G1"}, "没有消息 id"),
        ("mute", {"group_id": "G1"}, "没有成员"),
        ("typing", {}, "没有对象"),
        ("send_voice", {"group_id": "G1"}, "没有文件路径"),
        ("send_card", {"group_id": "G1"}, "没有标题"),
        ("menu_set", {}, "菜单不是对象"),
        ("panel_create", {"panel": {}}, "没写生效场景"),
        ("api", {}, "没给接口名"),
        ("approve_join", {"group_id": "G1", "member_openid": "M1"}, "没说是通过还是拒绝"),
    ],
)
def test_missing_or_malformed_params_are_named_not_silently_dropped(action, params, why):
    with pytest.raises(ValueError):
        _call(action, **params)


def test_send_that_the_platform_refused_reports_why():
    plugin, svc = service()
    out = run(svc.call("send_video", {"user_id": "U1", "path": "https://x/v.mp4"}))
    assert out["sent"] is False and "上传或发送失败" in out["reason"]

def test_panels_action_forwards_scope_and_fails_without_it():
    """scope 是平台必填项：入口没给就要拿到错误，不能悄悄发一个空查询。"""
    plugin, svc = service()
    with pytest.raises(ValueError):
        run(svc.call("panels", {}))
    out = run(svc.call("panels", {"scope": "c2c", "limit": 10}))
    assert out == {"records": []}
    assert plugin.qq_client.calls[-1][1] == ("c2c", "", 10)


def test_panel_target_action_passes_openid_lists_through():
    plugin, svc = service()
    run(svc.call("panel_target", {
        "panel_id": "p1", "op": "del", "user_openids": ["U1"],
    }))
    name, args, _kwargs = plugin.qq_client.calls[-1]
    assert name == "panel_target"
    assert args == ("p1", "del", ["U1"], [])
