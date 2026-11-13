# -*- coding: utf-8 -*-
"""官方机器人（开放平台 API v2）动作的分派层。

连接器（``qq_open_platform_v2``）负责"怎么调"，这里只负责"入口收到的一句话动作
对应哪次调用、参数怎么校验"。单独一个文件是因为 ``__init__.py`` 已经太厚，
而这张表要能脱离宿主被读、被测。

设计上的两个取舍：

- **动作名 + 参数，不暴露 URL**。``api`` 这个动作确实能调任意接口，但它只认
  ``V2_ENDPOINTS`` 里的名字和路径模板，调用方给不出表外的地址。
- **权限交给平台判**。禁言、踢人、审批入群这些接口，机器人自己在群里是不是
  管理员由 QQ 平台说了算（``11282`` / ``40062003``）；这里不复制一套本地判断，
  因为本地那份一旦和平台不一致，报出来的错就和真因无关。
"""

from __future__ import annotations

from typing import Any, Callable

from .qq_open_platform_v2 import PANEL_SCOPES

#: 动作 -> 中文说明（入口的 input_schema 与错误提示共用这一份，不各写各的）。
GROUP_ONLY = ("mute", "unmute", "kick", "join_requests", "approve_join", "group_info",
              "group_members", "group_member", "bot_state", "blacklist")


class QQOpenPlatformService:
    """把入口动作翻成连接器调用。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ── 小工具 ──────────────────────────────────────────────

    def _connection(self) -> Any:
        """可用的连接对象，或 ``None``（非官方通道 / 能力层没挂上）。"""
        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            return None
        if getattr(client, "is_open_platform", False):
            return client
        from .qq_open_platform_v2 import is_official_bot

        if is_official_bot(client):
            # 官方通道在，但能力层没挂上（升级被宿主类挡了）：说清是哪一种，
            # 不要报成"当前不是官方机器人通道"。
            self.plugin.logger.warning("[QQ] 官方机器人能力层未挂载，直通动作不可用")
        return None

    def _require_connection(self) -> Any:
        conn = self._connection()
        if conn is None:
            raise RuntimeError("官方机器人未连接，且当前不是官方机器人通道")
        return conn

    @staticmethod
    def _require(params: dict[str, Any], *keys: str) -> tuple[str, ...]:
        missing = [k for k in keys if not str(params.get(k) or "").strip()]
        if missing:
            raise ValueError(f"缺少参数: {', '.join(missing)}")
        return tuple(str(params[k]).strip() for k in keys)

    def untrusted_seen(self, groups: list[dict[str, Any]], conn: Any) -> list[str]:
        """见过的群里，哪些还没进「信任群」名单（那些群她只按概率转达，几乎不回）。

        名单本身在权限管理器里，这里只做只读比对：不自动加信任 —— 那是主人的授权决定。
        """
        mgr = getattr(self.plugin, "group_permission_mgr", None)
        if mgr is None or not hasattr(mgr, "list_groups"):
            return []
        listed = {
            str(row.get("group_id") or "").strip()
            for row in (mgr.list_groups() or []) if isinstance(row, dict)
        }
        return [
            str(g["group_id"]) for g in groups
            if str(g.get("group_id") or "").strip() not in listed
        ]

    async def describe(self) -> dict[str, Any]:
        """当前连接的能力表 + 可调动作。给 UI 和"她自己能查"用。"""
        conn = self._connection()
        if conn is None:
            return {
                "connected": False,
                "hint": "当前不是官方机器人通道（OneBot/NapCat 走的是另一套接口，不经这里）",
            }
        snapshot = conn.capability_snapshot() if hasattr(conn, "capability_snapshot") else {}
        catalog = conn.v2_endpoint_catalog() if hasattr(conn, "v2_endpoint_catalog") else []
        return {
            "connected": True,
            "capabilities": snapshot,
            "actions": sorted(ACTIONS),
            "endpoints": [item["id"] for item in catalog],
        }

    async def call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """执行一个动作。未知动作 / 缺参数 / 平台报错都会抛，由入口转成可读错误。"""
        action = str(action or "").strip()
        handler = ACTIONS.get(action)
        if handler is None:
            raise ValueError(f"未知动作: {action}（可用: {', '.join(sorted(ACTIONS))}）")
        # capabilities 是查询：路不通也要答一句"不通"，所以它不需要连接可用。
        conn = None if handler is _capabilities else self._require_connection()
        return await handler(self, conn, dict(params or {}))


# ── 动作表 ────────────────────────────────────────────────

async def _capabilities(service: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    return await service.describe()


async def _recall(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    message_id, group_id, user_id = (str(p.get("message_id") or "").strip(),
                                     str(p.get("group_id") or "").strip(),
                                     str(p.get("user_id") or "").strip())
    if not message_id:
        raise ValueError("缺少参数: message_id")
    if not group_id and not user_id:
        raise ValueError("撤回要指明 group_id 或 user_id（两个场景是两个接口）")
    ok = await conn.recall_message(message_id, group_id=group_id, user_id=user_id)
    return {"recalled": ok, "message_id": message_id}


async def _group_info(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    (group_id,) = QQOpenPlatformService._require(p, "group_id")
    return await conn.get_group_info(group_id)


async def _bot_state(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    (group_id,) = QQOpenPlatformService._require(p, "group_id")
    state = await conn.get_group_bot_state(group_id, force=bool(p.get("force")))
    state.pop("_at", None)
    return state


async def _group_members(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    (group_id,) = QQOpenPlatformService._require(p, "group_id")
    members = await conn.get_group_member_list(group_id, max_pages=int(p.get("max_pages") or 10))
    return {"members": members, "count": len(members)}


async def _group_member(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    group_id, member_openid = QQOpenPlatformService._require(p, "group_id", "member_openid")
    return await conn.get_group_member_info(group_id, member_openid)


async def _mute(_s: QQOpenPlatformService, conn: Any, p: dict, *, unmute: bool = False) -> dict:
    group_id, member_openid = QQOpenPlatformService._require(p, "group_id", "member_openid")
    seconds = 0 if unmute else max(1, min(int(p.get("seconds") or 600), 30 * 24 * 3600))
    return await conn.set_group_ban(group_id, member_openid, seconds)


async def _unmute(s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    return await _mute(s, conn, p, unmute=True)


async def _kick(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    group_id, member_openid = QQOpenPlatformService._require(p, "group_id", "member_openid")
    return await conn.set_group_kick(group_id, member_openid,
                                     add_to_member_blacklist=bool(p.get("add_to_blacklist")))


async def _join_requests(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    (group_id,) = QQOpenPlatformService._require(p, "group_id")
    return await conn.list_group_join_requests(
        group_id, cursor=str(p.get("cursor") or ""), limit=int(p.get("limit") or 20),
    )


async def _approve_join(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    group_id, member_openid = QQOpenPlatformService._require(p, "group_id", "member_openid")
    approve = p.get("approve")
    if approve is None:
        raise ValueError("缺少参数: approve（true 通过 / false 拒绝）")
    return await conn.approve_group_join_request(
        group_id, member_openid, approve=bool(approve),
        join_request_id=str(p.get("join_request_id") or ""),
        reject_reason=str(p.get("reason") or ""),
        add_to_blacklist=bool(p.get("add_to_blacklist")),
    )


async def _blacklist(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    (group_id,) = QQOpenPlatformService._require(p, "group_id")
    ids = [str(x).strip() for x in (p.get("member_openids") or []) if str(x).strip()][:20]
    op = str(p.get("op") or "add").strip()
    if not ids:
        return await conn.list_group_member_blacklist(group_id, cursor=str(p.get("cursor") or ""))
    if op not in ("add", "del"):
        raise ValueError("op 只能是 add / del")
    return await conn.operate_group_member_blacklist(group_id, ids, op=op)


async def _typing(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    (user_id,) = QQOpenPlatformService._require(p, "user_id")
    return {"shown": await conn.set_typing(user_id, seconds=int(p.get("seconds") or 3))}


async def _send_media(_s: QQOpenPlatformService, conn: Any, p: dict, *, kind: str) -> dict:
    path = str(p.get("path") or p.get("file") or "").strip()
    if not path:
        raise ValueError("缺少参数: path（本地文件或 http(s) 链接）")
    group_id = str(p.get("group_id") or "").strip()
    user_id = str(p.get("user_id") or "").strip()
    if not group_id and not user_id:
        raise ValueError("要指明 group_id 或 user_id")
    target, scope = (group_id, "group") if group_id else (user_id, "private")
    sender = getattr(conn, f"send_{'group' if scope == 'group' else 'private'}_{kind}")
    message_id = await sender(target, path)
    if not message_id:
        return {"sent": False, "reason": f"{kind} 上传或发送失败（详见运行日志）"}
    return {"sent": True, "message_id": message_id, "scope": scope}


async def _send_voice(s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    return await _send_media(s, conn, p, kind="record")


async def _send_video(s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    return await _send_media(s, conn, p, kind="video")


async def _send_file(s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    return await _send_media(s, conn, p, kind="file")


async def _send_card(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    title = str(p.get("title") or "").strip()
    if not title:
        raise ValueError("缺少参数: title")
    card = {
        "title": title[:50],
        "description": str(p.get("description") or "")[:200],
        "url": str(p.get("url") or ""),
        "pic_url": str(p.get("pic_url") or ""),
    }
    group_id = str(p.get("group_id") or "").strip()
    user_id = str(p.get("user_id") or "").strip()
    if group_id:
        ok = await conn.send_group_ark_card(group_id, {"card": {"type": "tuwen", "content": card}})
    elif user_id:
        ok = await conn.send_private_ark_card(user_id, {"card": {"type": "tuwen", "content": card}})
    else:
        raise ValueError("要指明 group_id 或 user_id")
    return {"sent": bool(ok)}


async def _menu_get(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    return await conn.get_menu()


async def _menu_set(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    menu = p.get("menu")
    if not isinstance(menu, dict):
        raise ValueError("缺少参数: menu（对象，items 最多 10 项）")
    return await conn.set_menu(menu)


async def _panels(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    return await conn.list_panels(
        str(p.get("scope") or "").strip(), cursor=str(p.get("cursor") or ""),
        limit=int(p.get("limit") or 20),
    )


async def _panel_create(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    panel = p.get("panel")
    scope = str(p.get("scope") or "").strip()
    if not isinstance(panel, dict) or scope not in PANEL_SCOPES:
        raise ValueError("缺少参数: scope（c2c/group/channel/dm）与 panel（对象）")
    return await conn.create_panel({"scope": scope, "target_type": str(p.get("target_type") or "all"),
                                    "panel": panel})


async def _url_link(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    return await conn.generate_url_link(str(p.get("callback_data") or ""))


async def _panel_target(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    panel_id = str(p.get("panel_id") or "").strip()
    if not panel_id:
        raise ValueError("缺少参数: panel_id")
    return await conn.set_panel_target(
        panel_id, str(p.get("op") or ""),
        user_openids=p.get("user_openids") or [], group_openids=p.get("group_openids") or [],
    )


async def _groups_seen(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    """她真的收到过消息的群。官方机器人的群 id 是 group_openid，不是 QQ 群号。"""
    groups = await conn.get_group_list()
    return {"groups": groups, "count": len(groups), "unclaimed": _s.untrusted_seen(groups, conn)}


async def _respond_interaction(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    interaction_id = str(p.get("interaction_id") or "").strip()
    if not interaction_id:
        raise ValueError("缺少参数: interaction_id")
    return await conn.respond_interaction(interaction_id, int(p.get("code") or 0))


async def _api(_s: QQOpenPlatformService, conn: Any, p: dict) -> dict:
    endpoint_id = str(p.get("endpoint_id") or "").strip()
    if not endpoint_id:
        raise ValueError("缺少参数: endpoint_id（取自 actions=capabilities 的 endpoints 列表）")
    return await conn.v2_call(
        endpoint_id,
        path_params=p.get("path_params") or {},
        params=p.get("params") or {},
        body=p.get("body"),
    )


#: 入口允许的平铺参数名。和动作表一起维护：入口收这些键，其余键留在 params 里透传。
PARAM_KEYS = (
    "group_id", "user_id", "member_openid", "message_id", "seconds", "path", "title",
    "description", "url", "pic_url", "cursor", "limit", "endpoint_id", "path_params",
    "body", "callback_data", "interaction_id", "code", "menu", "panel", "scope",
    "target_type", "approve", "reason", "op", "member_openids", "add_to_blacklist",
    "join_request_id", "max_pages", "force", "scope", "panel_id", "user_openids",
    "group_openids", "cursor",
)

#: 入口只认这张表里的动作。加接口 = 在连接器里加方法，再在这里挂一个动作名。
ACTIONS: dict[str, Callable[..., Any]] = {
    "capabilities": _capabilities,
    "recall": _recall,
    "group_info": _group_info,
    "bot_state": _bot_state,
    "group_members": _group_members,
    "group_member": _group_member,
    "mute": _mute,
    "unmute": _unmute,
    "kick": _kick,
    "join_requests": _join_requests,
    "approve_join": _approve_join,
    "blacklist": _blacklist,
    "groups_seen": _groups_seen,
    "typing": _typing,
    "send_voice": _send_voice,
    "send_video": _send_video,
    "send_file": _send_file,
    "send_card": _send_card,
    "menu_get": _menu_get,
    "menu_set": _menu_set,
    "panels": _panels,
    "panel_create": _panel_create,
    "panel_target": _panel_target,
    "url_link": _url_link,
    "respond_interaction": _respond_interaction,
    "api": _api,
}
