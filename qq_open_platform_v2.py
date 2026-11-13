# -*- coding: utf-8 -*-
r"""QQ 官方机器人（开放平台 API v2）能力层。

**为什么要有这个文件**：官方机器人（``mode == "open_platform"``）的连接实现来自宿主第一方
连接器（``utils.connection.qq.open_platform``，回退 ``_vendor`` 那份副本），那份实现把
API v2 的能力面写窄了。三处最要命：

1. **能力声明是错的**：``supports_voice=False``（v2 的富媒体 ``file_type=3`` 就是语音）、
   ``supports_ark_cards=True``（v2 发送侧**没有** ark，图文卡片是 ``msg_type=8``）、
   ``receives_all_messages=False``（v2 有 ``GROUP_MESSAGE_CREATE`` 全量模式）。
   插件全篇拿这些旗标决定"要不要试"，旗标错 = 功能整条被关掉。
2. **事件只认两种**：``_receive_loop`` 里 ``if event_type in ("GROUP_AT_MESSAGE_CREATE",
   "C2C_MESSAGE_CREATE")``，其余全部丢弃。入群/退群、入群申请、按钮点击
   （``INTERACTION_CREATE``）、全量群消息……平台推了，连接层扔了。
3. **约 50 个方法返回空桩** ``async def delete_msg(**kw) -> dict: return {}``。
   调用方拿到"没报错"就当成功了：撤回、群成员查询、禁言检测全部静默失效
   （``group_send_safety.blocked()`` 读 ``get_group_member_info`` 的
   ``shut_up_timestamp``，桩返回 ``{}``，于是被禁言也照样发，一直撞错误）。

**这里只补能力，不重写连接器**：本模块是一个 Mixin，:func:`v2_connection_class` 把它和
当前解析到的连接器实现（宿主的或内置副本的）合成一个子类，:func:`create_open_platform_connection`
负责按设置构造。宿主连接器改实现、改类名都不用跟；真遇到"构造函数加了必填参数"这种漂移，
就退回连接器原实现并记一行 —— 宁可少一层能力，也不要启动连不上。

**边界**（照文档说的做，不猜）：

- 表情表态只在**频道**消息上可用（``/develop/api-v2/server-inter/message/trans/emoji``：
  "目前表情表态仅支持在频道内使用"），戳一戳、群公告、精华、合并转发在 v2 群/单聊面**没有**
  对应接口 —— 这些走 :meth:`_v2_unsupported`，明确报错而不是回空桩骗调用方。
- ``bot_state`` / ``members`` 等平台接口标注"仅白名单机器人可用（11253）"，拿不到时
  退化成"按已观测到的行为判断"，不硬失败。
- 频控（主动/被动消息、单群每日上限）由平台判定，本层只把 ``err_code`` 如实抛出。
- **不动 Identify**：连接器订阅 ``(1<<25) GROUP_AND_C2C_EVENT | (1<<12) DIRECT_MESSAGE``。
  群成员进出与入群申请事件要 ``(1<<24)``、按钮/菜单回调要 ``(1<<26)``，这两位在平台是申请制，
  文档还写明"传无权限的 intents 会被直接断连"，所以这里不加位：入群申请改成**主动拉取**，
  按钮与菜单项一律用指令型（不需要回调权限）。:meth:`respond_interaction` 留给"宿主哪天
  订上了"的情况，插件侧不用再改。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, Optional

#: 富媒体类型（``/v2/{groups|users}/{openid}/files`` 的 ``file_type``）。
FILE_TYPE_IMAGE = 1
FILE_TYPE_VIDEO = 2
FILE_TYPE_VOICE = 3
FILE_TYPE_FILE = 4
FILE_TYPE_NAMES = {
    FILE_TYPE_IMAGE: "图片",
    FILE_TYPE_VIDEO: "视频",
    FILE_TYPE_VOICE: "语音",
    FILE_TYPE_FILE: "文件",
}

#: 消息类型（发送侧）。群/单聊一致；``6`` 只有单聊有（文档：群消息不支持流式参数）。
MSG_TEXT = 0
MSG_MARKDOWN = 2
MSG_INPUT_NOTIFY = 6
MSG_MEDIA = 7
MSG_CARD = 8

#: 指令面板的生效场景（``/v2/panels`` 必填 ``scope`` 的取值域）。
PANEL_SCOPES = ("c2c", "group", "channel", "dm")

#: 判定用的 ``err_code``（文档："不要依据 message 判定失败，按 err_code"）。
ERR_REPLY_EXPIRED = 40034005          # 被动回复的 msg_id 过期 -> 降级成主动消息重试一次
ERR_GROUP_MUTED = 850018              # 群被禁言或机器人被禁言 -> 记下禁言，别再撞
ERR_TOKEN_INVALID = 40011027          # AccessToken 无效/过期 -> 刷新后重试一次
ERR_APP_FORBIDDEN = 11253             # 该接口未开权限（白名单能力）
ERR_NOT_FOUND = 40011028              # 请求的资源不存在（用户/群已注销，或根本是另一个命名空间的 id）

#: 引用索引 ref_idx -> message_id 的映射上限。平台侧同一消息只认有限个 ref_idx，
#: 超过就是老得没人引用了；再多留着只是占内存。
REF_IDX_CACHE_SIZE = 512

#: 发言者 id 的取值顺序，和连接器里的约定一致（群内是 member_openid，单聊是 user_openid/id）。
_C2C_ACTOR_ID_KEYS = ("user_openid", "id")
_GROUP_ACTOR_ID_KEYS = ("member_openid", "id")


def pick_actor_id(author: Any, keys: tuple[str, ...]) -> str:
    """按 ``keys`` 顺序取第一个非空的发言人标识；纯函数，便于单独测试。"""
    if not isinstance(author, dict):
        return ""
    for key in keys:
        value = str(author.get(key) or "").strip()
        if value:
            return value
    return ""


#: 官方接口索引（``/develop/api-v2/autogen/`` 的 47 条）+ 索引页里以旧式路径列出的
#: 频道/网关条目。``id -> (HTTP 方法, 路径模板, 一句话用途)``。
#: 这是**校验用的白名单**：入口只认表里的 id，路径由模板拼，参数不接受表外的
#: 自由 URL —— 否则等于给模型一个"以机器人身份调任意接口"的能力。
V2_ENDPOINTS: dict[str, tuple[str, str, str]] = {
    # 消息 / 单聊
    "send_c2c_message": ("POST", "/v2/users/{user_openid}/messages", "发送单聊消息"),
    "recall_c2c_message": ("DELETE", "/v2/users/{user_openid}/messages/{message_id}", "撤回单聊消息"),
    "send_c2c_stream_message": ("POST", "/v2/users/{user_openid}/stream_messages", "流式发送单聊消息"),
    # 消息 / 群聊
    "send_group_message": ("POST", "/v2/groups/{group_openid}/messages", "发送群聊消息"),
    "recall_group_message": ("DELETE", "/v2/groups/{group_openid}/messages/{message_id}", "撤回群聊消息"),
    # 消息 / 富媒体
    "upload_group_media": ("POST", "/v2/groups/{group_openid}/files", "群聊富媒体上传"),
    "upload_c2c_media": ("POST", "/v2/users/{user_openid}/files", "单聊富媒体上传"),
    "upload_group_prepare": ("POST", "/v2/groups/{group_id}/upload_prepare", "群聊富媒体预上传"),
    "upload_group_part_finish": ("POST", "/v2/groups/{group_id}/upload_part_finish", "群聊分片上传完成"),
    "upload_c2c_prepare": ("POST", "/v2/users/{user_id}/upload_prepare", "单聊富媒体预上传"),
    "upload_c2c_part_finish": ("POST", "/v2/users/{user_id}/upload_part_finish", "单聊分片上传完成"),
    # QQ 群 / 群信息
    "get_group_info": ("GET", "/v2/groups/{group_openid}/info", "获取群基本信息"),
    "get_group_bot_state": ("GET", "/v2/groups/{group_openid}/bot_state", "获取机器人群内状态"),
    # QQ 群 / 群成员
    "get_group_members": ("GET", "/v2/groups/{group_openid}/members", "获取群成员列表"),
    "get_group_member": ("GET", "/v2/groups/{group_openid}/members/{member_openid}", "获取群成员信息"),
    "remove_group_members": ("POST", "/v2/groups/{group_openid}/batch_remove_members", "群成员批量移除"),
    # QQ 群 / 入群申请
    "list_group_join_requests": ("GET", "/v2/groups/{group_openid}/join_request_list", "入群申请列表"),
    "approve_group_join_request": (
        "POST", "/v2/groups/{group_openid}/approval_join_request/{member_openid}", "入群申请审批",
    ),
    "list_join_approval_strategies": ("GET", "/v2/groups/join_approval_strategy", "入群自动审批策略列表"),
    "create_join_approval_strategy": ("POST", "/v2/groups/join_approval_strategy", "创建入群自动审批策略"),
    "update_join_approval_strategy": ("PATCH", "/v2/groups/join_approval_strategy/{strategy_id}", "修改策略"),
    "delete_join_approval_strategy": ("DELETE", "/v2/groups/join_approval_strategy/{strategy_id}", "删除策略"),
    "execute_join_approval_strategy": ("POST", "/v2/groups/join_approval_strategy/{strategy_id}/execute", "执行策略"),
    "update_strategy_whitelist_users": (
        "POST", "/v2/groups/join_approval_strategy/{strategy_id}/whitelist_users", "修改策略白名单",
    ),
    # QQ 群 / 黑名单、禁言
    "list_group_member_blacklist": ("GET", "/v2/groups/{group_openid}/member_blacklist", "群黑名单查询"),
    "operate_group_member_blacklist": ("POST", "/v2/groups/{group_openid}/member_blacklist", "群黑名单操作"),
    "get_group_mute_setting": ("GET", "/v2/groups/{group_openid}/restrict_chat_setting", "查询群禁言状态"),
    "set_group_mute_setting": ("POST", "/v2/groups/{group_openid}/restrict_chat_setting", "设置群成员禁言"),
    # 互动
    "respond_interaction": ("PUT", "/interactions/{interaction_id}", "互动事件响应"),
    "get_menu": ("GET", "/v2/menu", "查询全局自定义菜单"),
    "set_menu": ("PUT", "/v2/menu", "修改全局自定义菜单"),
    "list_panels": ("GET", "/v2/panels", "查询指令面板列表"),
    "create_panel": ("POST", "/v2/panels", "创建指令面板"),
    "get_panel": ("GET", "/v2/panels/{panel_id}", "查询指令面板详情"),
    "update_panel": ("PUT", "/v2/panels/{panel_id}", "修改指令面板"),
    "delete_panel": ("DELETE", "/v2/panels/{panel_id}", "删除指令面板"),
    "set_panel_target": ("PUT", "/v2/panels/{panel_id}/target", "修改指令面板关联对象"),
    # 机器人 / 网关 / 分享
    "get_bot_info": ("GET", "/users/@me", "获取机器人详情"),
    "get_bot_guilds": ("GET", "/users/@me/guilds", "获取机器人频道列表"),
    "get_gateway": ("GET", "/gateway", "获取通用 WSS 接入点"),
    "generate_url_link": ("POST", "/v2/generate_url_link", "生成分享链接"),
    # 频道（只有具备频道能力的机器人可用；表内即可调用，能力由平台判定）
    "get_guild": ("GET", "/guilds/{guild_id}", "获取频道详情"),
    "list_guild_channels": ("GET", "/guilds/{guild_id}/channels", "获取子频道列表"),
    "create_guild_channel": ("POST", "/guilds/{guild_id}/channels", "创建子频道"),
    "get_channel": ("GET", "/channels/{channel_id}", "获取子频道详情"),
    "update_channel": ("PATCH", "/channels/{channel_id}", "修改子频道"),
    "delete_channel": ("DELETE", "/channels/{channel_id}", "删除子频道"),
}

#: 事件类型 -> 本层的处理族（``message`` 进消息队列；``notice`` 只发给监听器）。
V2_MESSAGE_EVENTS = frozenset({
    "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE",
})
V2_NOTICE_EVENTS = frozenset({
    "GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_JOIN_REQUEST",
    "GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE",
    "GROUP_MSG_RECEIVE", "GROUP_MSG_REJECT", "C2C_MSG_RECEIVE", "C2C_MSG_REJECT",
    "FRIEND_ADD", "FRIEND_DEL", "SUBSCRIBE_MESSAGE_STATUS", "INTERACTION_CREATE",
})


def is_official_bot(client: Any) -> bool:
    """这条连接是不是官方机器人（开放平台）。

    以前全篇拿 ``not needs_attention`` 反推身份。那在"@ 模式"下刚好成立，
    可一旦这个群开了全量消息，``needs_attention`` 变成 True，官方机器人就会被
    当成 NapCat：键盘、称呼策略、管理员引导、身份命名空间一起错位。**身份要用
    身份判**，不能用副作用判。老测试桩只有 ``needs_attention`` 时退回旧判据。
    """
    if client is None:
        return False
    if str(getattr(client, "CHANNEL", "") or "").strip() == "open":
        return True
    if str(getattr(client, "mode", "") or "").strip() == "open_platform":
        return True
    if getattr(client, "is_open_platform", None) is True:
        return True
    if getattr(client, "CHANNEL", None) is None and getattr(client, "mode", None) is None:
        return not bool(getattr(client, "needs_attention", True))
    return False


class QQOpenPlatformAPIError(RuntimeError):
    """一次 v2 接口调用失败。带 ``err_code`` / ``trace_id``，供上层按码判断而不是按文案猜。"""

    def __init__(
        self, *, method: str, path: str, http_status: int = 0,
        err_code: int = 0, message: str = "", trace_id: str = "",
    ) -> None:
        self.method = method
        self.path = path
        self.http_status = int(http_status or 0)
        self.err_code = int(err_code or 0)
        self.api_message = str(message or "")
        self.trace_id = str(trace_id or "")
        super().__init__(
            f"{method} {path} 失败: http={self.http_status} "
            f"err_code={self.err_code} {self.api_message}"
            + (f" trace={self.trace_id}" if self.trace_id else "")
        )


class QQOpenPlatformUnsupported(RuntimeError):
    """这个操作官方机器人做不到（平台没有对应接口），不是"实现没写完"。"""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"官方机器人不支持 {name}: {reason}")


class QQOpenPlatformV2Mixin:
    """官方机器人连接器的 API v2 能力层（只加方法与惰性状态）。

    新状态一律走 ``getattr(self, "_v2_x", default)`` 惰性挂到实例上，不新增 ``__init__``：
    构造永远交给连接器自己，Mixin 只负责方法。这样合成子类之后，宿主 ``__init__`` 里
    赋过什么、按什么顺序赋，都和本层无关。
    """

    # ── 身份与能力面 ─────────────────────────────────────────
    #
    # 为什么要重写这些旗标：插件全篇用它们决定"要不要试"。写错一个 False，
    # 对应功能在官方机器人上就整条消失（语音、全量群消息、引用、撤回都是这样没的）。

    #: 明确的身份旗标。以前"这是不是官方机器人"是拿 ``not needs_attention`` 反推的，
    #: 而全量模式一旦打开，那个推断就会把官方机器人误认成 NapCat（键盘、昵称策略、
    #: 管理员引导、身份命名空间全跟着错位）。身份要用身份，不要用副作用。
    is_open_platform = True

    @property
    def supports_voice(self) -> bool:  # noqa: D401 - 覆盖基类
        return True

    async def can_send_record(self) -> bool:
        """基类是 ``async def``，这里必须保持同形状（上层有 ``await client.can_send_record()``）。"""
        return True

    @property
    def supports_files(self) -> bool:
        return True

    @property
    def supports_video(self) -> bool:
        return True

    @property
    def supports_recall(self) -> bool:
        return True

    @property
    def supports_markdown(self) -> bool:
        return True

    @property
    def supports_keyboard(self) -> bool:
        return True

    @property
    def supports_reference(self) -> bool:
        return True

    @property
    def supports_typing_notify(self) -> bool:
        """输入中状态（``msg_type=6``）只有单聊有。"""
        return True

    @property
    def supports_cards(self) -> bool:
        """本连接能把卡片请求投递出去（v2 走图文卡片 ``msg_type=8``）。"""
        return True

    @property
    def supports_rich_cards(self) -> bool:
        """图文卡片 ``msg_type=8``（2026-10-28 起群/单聊都能发）。"""
        return True

    @property
    def supports_ark_cards(self) -> bool:
        """发送侧 ark 在 v2 群/单聊已经没有了（消息类型总览：结构化卡片 发 ❌ / 收 ✅）。"""
        return False

    @property
    def supports_poke(self) -> bool:
        return False  # v2 群/单聊没有戳一戳接口

    @property
    def supports_reactions(self) -> bool:
        """表情表态仅频道消息可用，群/单聊没有接口。"""
        return False

    @property
    def recv_msg_setting(self) -> str:
        """平台告知的本群收信范围：``all`` / ``only_mention`` / ``mention_and_context`` / 空=未知。"""
        return str(getattr(self, "_v2_recv_setting", "") or "")

    @property
    def bot_member_role(self) -> str:
        """机器人在群里的角色（``member`` / ``admin`` / ``owner`` / 空=未知）。"""
        return str(getattr(self, "_v2_bot_role", "") or "")

    @property
    def allow_proactive_msg(self) -> Optional[bool]:
        """本群是否允许主动推送；``None`` = 没探到（白名单接口拿不到）。"""
        return getattr(self, "_v2_allow_proactive", None)

    @property
    def receives_all_messages(self) -> bool:
        """是否真的收到全量群消息。

        两条依据，任一成立即为真：``bot_state`` 里 ``recv_msg_setting == "all"``
        （要白名单权限），或者**确实收到过** ``GROUP_MESSAGE_CREATE``。
        后者是硬证据：平台没开权限就不会推这个事件。
        """
        if self.recv_msg_setting == "all":
            return True
        return bool(getattr(self, "_v2_full_mode_seen", False))

    @property
    def needs_attention(self) -> bool:
        """收到全量群消息之后，才需要"谁在跟谁说话"的注意力判定。

        只在 @ 模式时保持 ``False``：那时每条进来的消息都是对机器人说的，
        竞争与判定都没有意义（判了也只能回）。
        """
        return self.receives_all_messages

    def capability_snapshot(self) -> dict[str, Any]:
        """给 UI / 状态页看的一张能力表（v2 文档口径 + 本群实测到的开关）。"""
        return {
            "api": "v2",
            "is_open_platform": True,
            "voice": self.supports_voice,
            "image": True,
            "video": self.supports_video,
            "file": self.supports_files,
            "markdown": self.supports_markdown,
            "rich_card": self.supports_rich_cards,
            "ark_card": self.supports_ark_cards,
            "keyboard": self.supports_keyboard,
            "reference": self.supports_reference,
            "recall": self.supports_recall,
            "typing_notify": self.supports_typing_notify,
            "poke": self.supports_poke,
            "reactions": self.supports_reactions,
            "recv_msg_setting": self.recv_msg_setting,
            "receives_all_messages": self.receives_all_messages,
            "bot_member_role": self.bot_member_role,
            "allow_proactive_msg": self.allow_proactive_msg,
        }

    # ── REST 核心 ────────────────────────────────────────────

    def _v2_log(self, level: str, message: str) -> None:
        """连接器自带的三条日志通道哪个在就用哪个（副本没有 ``_media_log``）。"""
        for name in ("_media_log", "_emit_log"):
            hook = getattr(self, name, None)
            if callable(hook):
                try:
                    if name == "_media_log":
                        hook(level, message)
                    else:
                        hook(level.upper(), message)
                except Exception:
                    pass
        logger = getattr(self, "logger", None)
        if logger:
            getattr(logger, level if level in ("info", "warning", "error") else "info")(
                f"[QQOpenPlatformV2] {message}"
            )

    def _v2_format_path(self, template: str, path_params: dict[str, Any]) -> str:
        missing = [k for k in re.findall(r"\{(\w+)\}", template) if not str(path_params.get(k) or "").strip()]
        if missing:
            raise ValueError(f"缺少路径参数: {','.join(missing)}")
        return re.sub(
            r"\{(\w+)\}",
            lambda m: str(path_params[m.group(1)]).strip(),
            template,
        )

    async def _v2_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        retry_on_token: bool = True,
    ) -> dict[str, Any]:
        """一次 v2 调用。成功返回响应体（DELETE 类无包体时返回 ``{}``），失败抛
        :class:`QQOpenPlatformAPIError`。

        三件基类没做的事：

        1. **按 ``err_code`` 判失败**（文档明说 ``message`` 会随时改，不能拿来判定）；
           基类各发送路径是 ``resp.json()`` 取 ``id``，取不到就返回 ``None``，
           错误码原样丢掉 —— 上层只知道"没发出去"，不知道为什么。
        2. **token 过期刷新一次再试**：``access_token`` 生命期 7200s，基类只在
           ``_ensure_token`` 里按到期时间刷；跨天挂着的连接撞上 40011027 时，
           以前是一次失败、下一次才恢复。
        3. **禁言回写**：撞上 850018（群或机器人被禁言）时按 OneBot 侧同一约定记进
           ``_group_muted``，``group_send_safety`` 才拦得住，不再反复撞墙。
        """
        from urllib.parse import urlencode

        await self._ensure_token()
        http = getattr(self, "_http", None)
        if http is None:
            raise QQOpenPlatformAPIError(method=method, path=path, message="HTTP 客户端尚未建立")
        query = {k: v for k, v in (params or {}).items() if v is not None and str(v) != ""}
        url = f"{self._API_BASE}{path}" + (f"?{urlencode(query)}" if query else "")
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            response = await http.request(method, url, headers=headers, json=body)
        except Exception as exc:
            raise QQOpenPlatformAPIError(method=method, path=path, message=f"网络异常: {exc}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        data: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
        trace_id = str(
            data.get("trace_id")
            or (dict(getattr(response, "headers", {}) or {}).get("X-Tps-trace-ID", ""))
            or ""
        )
        err_code = _as_int(data.get("err_code") or data.get("code"))
        if status == 200 and err_code == 0:
            self._v2_observe_success(method, path, data)
            return data
        error = QQOpenPlatformAPIError(
            method=method, path=path, http_status=status, err_code=err_code,
            message=_v2_explain(path, err_code, str(data.get("message") or "")),
            trace_id=trace_id,
        )
        if err_code == ERR_TOKEN_INVALID and retry_on_token:
            self._v2_log("warning", f"token 失效，刷新后重试一次（{trace_id or '无 trace'}）")
            self._access_token = ""
            self._token_expires_at = 0
            await self._refresh_token()
            return await self._v2_request(
                method, path, params=params, body=body, retry_on_token=False,
            )
        if err_code == ERR_GROUP_MUTED:
            self._v2_note_group_muted(_group_id_from_path(path))
        raise error

    def _v2_observe_success(self, method: str, path: str, data: dict[str, Any]) -> None:
        """把发送成功回包里的 ``ext_info.ref_idx`` 记下来。

        引用回复要的是 ``REFIDX_...`` 这个索引，不是消息 id；平台只在**发消息的响应**里
        给机器人自己消息的 ref_idx。没有这张表，官方机器人在全量模式下就永远判断不出
        "这条消息是在引用我说过的话"。
        """
        if method != "POST":
            return
        ref_idx = str(((data.get("ext_info") or {}) if isinstance(data.get("ext_info"), dict) else {}).get("ref_idx") or "")
        message_id = str(data.get("id") or "")
        if not ref_idx or not message_id:
            return
        cache = getattr(self, "_v2_ref_idx_map", None)
        if cache is None:
            cache = self._v2_ref_idx_map = {}
        cache[ref_idx] = message_id
        while len(cache) > REF_IDX_CACHE_SIZE:
            cache.pop(next(iter(cache)))

    def _v2_note_group_muted(self, group_id: str) -> None:
        """按 OneBot 侧同一约定记禁言（``group_id -> 到期的单调时刻``）。

        时长是估的：v2 的禁言接口只给"到期时间"，而错误码 850018 不告诉具体多久，
        也可能压根是**全群禁言**。5 分钟之后 ``group_send_safety`` 会重新尝试发送，
        再撞上就再记一次 —— 宁可贵一轮，不要一直撞。
        """
        if not group_id:
            return
        muted = getattr(self, "_group_muted", None)
        if muted is None:
            muted = self._group_muted = {}
        muted[str(group_id)] = time.time() + 300
        self._v2_log("warning", f"官方机器人返回禁言（群 {group_id}），5 分钟内不再向该群发送")

    def is_group_muted(self, group_id: str) -> bool:
        """与 ``OneBotClient.is_group_muted`` 同语义：本地记账是否仍在禁言期内。"""
        gid = str(group_id or "").strip()
        if not gid:
            return False
        until = float((getattr(self, "_group_muted", {}) or {}).get(gid, 0) or 0)
        if until and until > 0:
            if time.time() < until:
                return True
            getattr(self, "_group_muted").pop(gid, None)
        return False


    # ── 发送：一条 POST 站点 ────────────────────────────────

    async def _v2_send(
        self,
        scope: str,
        owner_id: str,
        *,
        msg_type: int = MSG_TEXT,
        content: str = "",
        markdown: str = "",
        media_file_info: str = "",
        card: Optional[dict[str, Any]] = None,
        input_notify: Optional[dict[str, Any]] = None,
        keyboard: Optional[dict[str, Any]] = None,
        msg_id: str = "",
        event_id: str = "",
        msg_seq: int = 0,
        reference_message_id: str = "",
        is_wakeup: bool = False,
        record_sent: bool = True,
        fallback_to_proactive: bool = True,
    ) -> str:
        """发一条消息，返回消息 id（空串=没发出去）。

        ``scope`` 是 ``"groups"`` / ``"users"``。所有发送最终都走这里，所以
        "被动回复过期 → 降级主动消息重试一次" 和 ``record_sent`` 记账只需要写一遍。

        降级只在平台**明说** msg_id 过期（``40034005``）时发生：那时第一条确定没发出去，
        重发不会重复；其他错误码一律原样抛出，避免"不知道有没有发成功就再发一遍"。
        """
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            return ""
        body: dict[str, Any] = {"msg_type": int(msg_type)}
        if content:
            body["content"] = content
        if markdown:
            body["markdown"] = {"content": markdown}
        if media_file_info:
            body["media"] = {"file_info": media_file_info}
        if card:
            body["card"] = card
        if input_notify:
            body["input_notify"] = input_notify
        if keyboard:
            body["keyboard"] = keyboard
        if reference_message_id:
            body["message_reference"] = {"message_id": reference_message_id}
        if event_id:
            body["event_id"] = event_id
        elif msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = int(msg_seq or 0) or self._next_msg_seq(msg_id)
        if is_wakeup:
            body["is_wakeup"] = True
            body.pop("msg_id", None)
            body.pop("event_id", None)
        elif (
            scope == "users" and msg_type != MSG_INPUT_NOTIFY
            and not msg_id and not event_id
            and _v2_wakeup_enabled(self)
        ):
            # 私聊、没有被动锚点 = 平台语义里的主动触达。开了召回就走 is_wakeup，
            # 这样"她隔了几小时再来说一句"不会因为被动窗口过期而整个发不出去。
            body["is_wakeup"] = True

        path = f"/v2/{scope}/{owner_id}/messages"
        try:
            data = await self._v2_request("POST", path, body=body)
        except QQOpenPlatformAPIError as exc:
            if not (fallback_to_proactive and msg_id and exc.err_code == ERR_REPLY_EXPIRED):
                raise
            # 被动窗口（群 5 分钟 / 单聊 60 分钟）已过：改成主动消息再发一次。
            # 用户可以在客户端关掉"接受主动消息"，那种情况这里会再抛一个错误码上来，
            # 由调用方按"没送达"处理——不会假装成功。
            self._v2_log("warning", f"被动回复已过期，改用主动消息重发（{scope} {owner_id}）")
            # 换一份新 body，不在原对象上删：同一轮里"发了什么"要能事后核对。
            retry_body = {k: v for k, v in body.items() if k not in ("msg_id", "msg_seq")}
            data = await self._v2_request("POST", path, body=retry_body)
        message_id = str(data.get("id") or "")
        if message_id and record_sent:
            self.record_sent_message_id(message_id)
        return message_id

    async def _v2_upload_media(
        self, source: str, *, scope: str, owner_id: str, file_type: int
    ) -> str:
        """上传富媒体拿 ``file_info``（失败返回空串，由调用方降级）。

        图片沿用 ``media_seam`` 那条真机验证过的通路；其余类型走
        ``media_seam.upload_media``（URL 直传 → 分片 → 二进制，同一套代码）。
        """
        if not str(source or "").strip():
            return ""
        from .media_seam import upload_image, upload_media

        if int(file_type) == FILE_TYPE_IMAGE:
            return await upload_image(self, scope=scope, owner_id=owner_id, source=source)
        return await upload_media(
            self, scope=scope, owner_id=owner_id, source=source, file_type=int(file_type),
        )

    def _v2_keyboard(self, keyboard: str) -> Optional[dict[str, Any]]:
        """把插件里通用的 ``"按钮1|按钮2"`` 字符串换成 v2 的 ``keyboard`` 结构。

        指令按钮（``action.type=2``）点击后把 ``data`` 填进输入框；``reply=True``
        带上对本条消息的引用，机器人随后收到的就是一条带上下文的指令。最多 4 个按钮，
        与基类一致。按钮只在 ``msg_type=2``（markdown）上有效，调用方负责升格。
        """
        labels = [b.strip() for b in str(keyboard or "").split("|") if b.strip()][:4]
        if not labels:
            return None
        return {
            "content": {
                "rows": [{
                    "buttons": [
                        {
                            "id": f"btn_{i}",
                            "render_data": {"label": label[:20], "visited_label": label[:20], "style": 1},
                            "action": {
                                "type": 2,
                                "permission": {"type": 2},
                                "data": label,
                                "unsupport_tips": "请升级 QQ 版本后使用",
                            },
                        }
                        for i, label in enumerate(labels)
                    ]
                }]
            }
        }

    _MD_MARK_RE = re.compile(
        r"(\*\*.+?\*\*|__.+?__|~~.+?~~|^#{1,6} .+|\* .+|- .+|\[\[.+?\]\]\(.+?\)|`[^`\n]+`)",
        re.MULTILINE,
    )

    def _v2_looks_like_markdown(self, text: str) -> bool:
        return bool(self._MD_MARK_RE.search(str(text or "")))

    async def _v2_send_segments(
        self, scope: str, owner_id: str, segments: list[dict[str, Any]],
        *, record_sent: bool = True, keyboard: str = "", event_id: str = "",
    ) -> Optional[str]:
        """OneBot 风格的消息段 -> v2 一条消息。群/单聊共用。

        与基类的差别全在"不再丢东西"：语音/视频/文件段真上传真发（以前压成
        ``[语音]`` 之类的占位文本），``reply`` 段除了当被动回复的 ``msg_id`` 还带
        **可见引用**（``message_reference``，以前只有引用编号、消息上看不出引用了谁），
        ``at`` 段用文档现用的 ``<qqbot-at-user id="" />``（``<@userid>`` 已宣布弃用）。
        """
        expand = getattr(self, "_expand_cq_segments", None)
        parts = list(expand(segments)) if callable(expand) else list(segments or [])

        texts: list[str] = []
        media: list[tuple[int, str]] = []
        at_ids: list[str] = []
        at_all = False
        reply_id = ""
        card: Optional[dict[str, Any]] = None

        for seg in parts:
            seg_type = str(seg.get("type") or "").strip()
            data = seg.get("data") or {}
            if seg_type == "text":
                texts.append(str(data.get("text") or ""))
            elif seg_type in ("image", "img"):
                media.append((FILE_TYPE_IMAGE, str(data.get("file") or "")))
            elif seg_type == "record":
                media.append((FILE_TYPE_VOICE, str(data.get("file") or "")))
            elif seg_type == "video":
                media.append((FILE_TYPE_VIDEO, str(data.get("file") or "")))
            elif seg_type == "file":
                media.append((FILE_TYPE_FILE, str(data.get("file") or data.get("url") or "")))
            elif seg_type == "reply":
                reply_id = str(data.get("id") or "")
            elif seg_type == "at":
                who = str(data.get("qq") or data.get("id") or "").strip()
                if who in ("all", "everyone"):
                    at_all = True
                elif who:
                    at_ids.append(who)
            elif seg_type == "card":
                card = data.get("card") if isinstance(data.get("card"), dict) else data
            elif seg_type in ("face", "mface", "json", "music", "contact", "rps", "dice"):
                texts.append(_v2_segment_placeholder(seg_type, data))

        content = "".join(texts).strip()
        if not content and not media and card is None:
            return None

        # 一条消息只能带一个富媒体（msg_type=7 只有一个 media 字段）：第一样按原类型发，
        # 其余降级成文字说明，不悄悄丢掉。
        file_type, file_source = ("", "")
        for kind, src in media:
            if not str(src or "").strip():
                continue
            if file_type:
                content = " ".join(
                    x for x in (content, f"[还有 1 个{FILE_TYPE_NAMES.get(kind, '文件')}没能带出]") if x
                )
                continue
            file_type, file_source = kind, src

        # @ 只能用 markdown 承载（群聊）；单聊没有 @ 的概念，保留可读占位。
        mention_prefix = ""
        if at_ids and scope == "groups":
            mention_prefix = " ".join(f'<qqbot-at-user id="{i}" />' for i in at_ids[:5]) + " "
        elif at_ids:
            mention_prefix = " ".join(f"[@{i}]" for i in at_ids[:5]) + " "
        if at_all:
            mention_prefix += "<qqbot-at-everyone /> "

        keyboard_struct = self._v2_keyboard(keyboard) if keyboard else None
        use_markdown = (
            bool(mention_prefix and scope == "groups")
            or keyboard_struct is not None
            or (card is None and not file_type and self._v2_looks_like_markdown(content))
        )

        common: dict[str, Any] = {"record_sent": record_sent, "fallback_to_proactive": True}
        if event_id:
            common["event_id"] = event_id
        elif reply_id:
            common["msg_id"] = reply_id
            # 可见引用要的是平台给的 REFIDX 令牌（在事件 ext.msg_idx 里），不是消息 id。
            common["reference_message_id"] = self._v2_reference_token_for(reply_id)

        if file_type:
            file_info = await self._v2_upload_media(
                file_source, scope=scope, owner_id=owner_id, file_type=file_type,
            )
            if file_info:
                if file_type == FILE_TYPE_IMAGE and content:
                    # 富媒体消息不能同时带正文：图片说明拼在图片之外单独发一条，
                    # 免得把 "看图" 这种话挤掉，或者把说明错当文件名。
                    await self._v2_send(scope, owner_id, msg_type=MSG_TEXT, content=content, **common)
                    common["record_sent"] = False
                return await self._v2_send(
                    scope, owner_id, msg_type=MSG_MEDIA,
                    media_file_info=file_info,
                    content="" if (file_type == FILE_TYPE_IMAGE) else content,
                    **common,
                )
            self._v2_log("warning", f"{FILE_TYPE_NAMES.get(file_type, '富媒体')}上传失败，降级为文字发送")
            content = " ".join(x for x in (content, _v2_media_placeholder(file_type, file_source)) if x)

        if card is not None:
            return await self._v2_send(
                scope, owner_id, msg_type=MSG_CARD,
                content=content, card=self._v2_normalize_card(card), **common,
            )

        if use_markdown and content:
            return await self._v2_send(
                scope, owner_id, msg_type=MSG_MARKDOWN,
                markdown=mention_prefix + content, keyboard=keyboard_struct, **common,
            )
        if use_markdown and not content:
            # 只有按钮/只有 @：markdown 内容为空会被平台拒，退回文本 + 键盘。
            return await self._v2_send(
                scope, owner_id, msg_type=MSG_TEXT,
                content=(mention_prefix or "").strip(), keyboard=keyboard_struct, **common,
            )
        return await self._v2_send(scope, owner_id, msg_type=MSG_TEXT, content=content, **common)

    async def send_group_message_segments(
        self, group_id: str, segments: list[dict[str, Any]],
        *, record_sent: bool = True, keyboard: str = "", **_: Any,
    ) -> Optional[str]:
        return await self._v2_send_segments(
            "groups", group_id, segments, record_sent=record_sent, keyboard=keyboard,
        )

    async def send_private_message_segments(
        self, user_id: str, segments: list[dict[str, Any]],
        *, record_sent: bool = True, **_: Any,
    ) -> Optional[str]:
        return await self._v2_send_segments("users", user_id, segments, record_sent=record_sent)

    async def send_group_message(self, group_id: str, message: str) -> Optional[str]:
        return await self.send_group_message_segments(
            group_id, [{"type": "text", "data": {"text": message}}],
        )

    async def send_message(self, user_id: str, message: str) -> Optional[str]:
        return await self.send_private_message_segments(
            user_id, [{"type": "text", "data": {"text": message}}],
        )

    # ── 发送：具名动作 ──────────────────────────────────────

    def _v2_normalize_card(self, card: dict[str, Any]) -> dict[str, Any]:
        """把两种卡片形状统一成 v2 的 ``card={type:"tuwen", content:{...}}``。

        收 NapCat 那套 ``{"ark": {"kv": [{"key": "#TITLE#", ...}]}}`` 是必要的：
        ``reply_pipeline`` 组装的就是这个形状，连接器不认就只能降级成纯文本，
        卡片里的链接和配图一起丢。
        """
        kv = ((card.get("ark") or {}).get("kv")) if isinstance(card.get("ark"), dict) else None
        if isinstance(kv, list):
            fields = {
                str(item.get("key") or "").strip("#").upper(): str(item.get("value") or "")
                for item in kv if isinstance(item, dict)
            }
            content = {
                "title": fields.get("TITLE") or "卡片",
                "description": fields.get("DESC") or fields.get("SUBTITLE") or "",
                "url": fields.get("URL") or "",
                "pic_url": fields.get("IMG") or "",
            }
        else:
            raw = card.get("content") if isinstance(card.get("content"), dict) else card
            content = {
                "title": str(raw.get("title") or "卡片"),
                "description": str(raw.get("description") or raw.get("desc") or ""),
                "url": str(raw.get("url") or ""),
                "pic_url": str(raw.get("pic_url") or raw.get("img") or ""),
            }
        if not content["description"]:
            content["description"] = content["title"]
        if content["url"] and not content["url"].startswith("https://"):
            content["url"] = "https://" + content["url"].lstrip("/")
        if content["pic_url"] and not content["pic_url"].startswith("https://"):
            content["pic_url"] = "https://" + content["pic_url"].lstrip("/")
        return {"type": "tuwen", "content": content}

    async def send_group_ark_card(self, group_id: str, ark_obj: dict[str, Any], **_: Any) -> bool:
        """群卡片。v2 没有 ark，这里翻译成图文卡片（``msg_type=8``）。"""
        if not isinstance(ark_obj, dict) or not str(group_id or "").strip():
            return False
        try:
            return bool(await self._v2_send(
                "groups", group_id, msg_type=MSG_CARD,
                card=self._v2_normalize_card(ark_obj),
            ))
        except Exception as exc:
            self._v2_log("warning", f"图文卡片发送失败: {exc}")
            return False

    async def send_private_ark_card(self, user_id: str, ark_obj: dict[str, Any], **_: Any) -> bool:
        if not isinstance(ark_obj, dict) or not str(user_id or "").strip():
            return False
        try:
            return bool(await self._v2_send(
                "users", user_id, msg_type=MSG_CARD,
                card=self._v2_normalize_card(ark_obj),
            ))
        except Exception as exc:
            self._v2_log("warning", f"单聊图文卡片发送失败: {exc}")
            return False

    async def send_group_record(
        self, group_id: str, file_uri: str, *,
        reply_message_id: str = "", at_user_id: str = "", **_: Any,
    ) -> Optional[str]:
        """群语音：上传 ``file_type=3``（silk/mp3/wav）后按富媒体发出。"""
        return await self._v2_send_media(
            "groups", group_id, FILE_TYPE_VOICE, file_uri,
            reply_message_id=reply_message_id, at_user_id=at_user_id,
        )

    async def send_private_record(
        self, user_id: str, file_uri: str, *,
        reply_message_id: str = "", **_: Any,
    ) -> Optional[str]:
        return await self._v2_send_media(
            "users", user_id, FILE_TYPE_VOICE, file_uri,
            reply_message_id=reply_message_id,
        )

    async def send_group_ai_record(self, group_id: str, file_uri: str = "", **kw: Any) -> Optional[str]:
        if not str(file_uri or "").strip():
            return None
        return await self.send_group_record(group_id, file_uri, **kw)

    async def send_group_video(self, group_id: str, file_uri: str, **kw: Any) -> Optional[str]:
        return await self._v2_send_media("groups", group_id, FILE_TYPE_VIDEO, file_uri, **kw)

    async def send_private_video(self, user_id: str, file_uri: str, **kw: Any) -> Optional[str]:
        return await self._v2_send_media("users", user_id, FILE_TYPE_VIDEO, file_uri, **kw)

    async def send_group_file(self, group_id: str, file_uri: str, **kw: Any) -> Optional[str]:
        return await self._v2_send_media("groups", group_id, FILE_TYPE_FILE, file_uri, **kw)

    async def send_private_file(self, user_id: str, file_uri: str, **kw: Any) -> Optional[str]:
        return await self._v2_send_media("users", user_id, FILE_TYPE_FILE, file_uri, **kw)

    async def upload_group_file(self, file: str = "", name: str = "", **kw: Any) -> dict[str, Any]:
        """OneBot 同名接口：发一个群文件。

        v2 的 ``srv_send_msg=true`` 会直接占用主动消息频次，所以这里不用它 ——
        先拿 ``file_info`` 再按普通发送走，记账和降级和其它富媒体一条路。
        """
        group_id = str(kw.get("group_id") or kw.get("group_openid") or "").strip()
        source = str(file or kw.get("file_uri") or "").strip()
        if not group_id or not source:
            return {}
        message_id = await self.send_group_file(group_id, source, file_name=str(name or ""))
        return {"file_id": message_id, "id": message_id} if message_id else {}

    async def upload_private_file(self, file: str = "", name: str = "", **kw: Any) -> dict[str, Any]:
        user_id = str(kw.get("user_id") or kw.get("openid") or "").strip()
        source = str(file or kw.get("file_uri") or "").strip()
        if not user_id or not source:
            return {}
        message_id = await self.send_private_file(user_id, source, file_name=str(name or ""))
        return {"file_id": message_id, "id": message_id} if message_id else {}

    async def _v2_send_media(
        self, scope: str, owner_id: str, file_type: int, source: str, *,
        content: str = "", reply_message_id: str = "", at_user_id: str = "",
        file_name: str = "", event_id: str = "", record_sent: bool = True,
    ) -> Optional[str]:
        file_info = await self._v2_upload_media(
            source, scope=scope, owner_id=owner_id, file_type=file_type,
        )
        if not file_info:
            self._v2_log(
                "warning",
                f"{FILE_TYPE_NAMES.get(file_type, '富媒体')}上传失败: {file_name or str(source)[-40:]}",
            )
            return None
        if file_type == FILE_TYPE_VOICE:
            # 语音就是一条语音：不夹文字、不夹 @（与 onebot_client 里 send_*_record 的
            # 修正同一条理由）。但 **保留** msg_id —— 摘掉它这条语音就变成主动消息，
            # 用户关掉"接受主动推送"时语音会整个发不出去。
            at_user_id = ""
            content = ""
        common: dict[str, Any] = {"record_sent": record_sent}
        if reply_message_id:
            common["msg_id"] = reply_message_id
            common["reference_message_id"] = self._v2_reference_token_for(reply_message_id)
        mention = ""
        if at_user_id and scope == "groups":
            mention = f'<qqbot-at-user id="{at_user_id}" /> '
        # 图片配文字：平台只让一个 media 字段生效，文字必须单独一条，否则话会被图挤掉。
        if file_type == FILE_TYPE_IMAGE and (content or mention):
            # 富媒体不带正文时，先把文字单独发出去，避免"图到了、话没了"。
            await self._v2_send(
                scope, owner_id,
                msg_type=MSG_MARKDOWN if mention else MSG_TEXT,
                markdown=(mention + content) if mention else "",
                content="" if mention else content,
                msg_id=reply_message_id,
                event_id=event_id,
                reference_message_id=self._v2_reference_token_for(reply_message_id),
                record_sent=False,
            )
        return await self._v2_send(
            scope, owner_id, msg_type=MSG_MEDIA, media_file_info=file_info, **common,
        )

    async def set_typing(self, user_id: str, *, seconds: int = 3) -> bool:
        """单聊"对方正在输入"提示（``msg_type=6``）。不占消息频次，只占一次调用。"""
        try:
            await self._v2_send(
                "users", user_id, msg_type=MSG_INPUT_NOTIFY,
                input_notify={"input_type": 1, "input_second": max(1, min(int(seconds or 3), 60))},
                record_sent=False,
            )
        except Exception as exc:
            self._v2_log("warning", f"输入状态提示发送失败: {exc}")
            return False
        return True

    async def recall_message(
        self, message_id: str, *, group_id: str = "", user_id: str = ""
    ) -> bool:
        """撤回消息。``DELETE`` 成功无响应体，所以按 HTTP 200 判定。"""
        message_id = str(message_id or "").strip()
        if not message_id:
            return False
        if str(group_id or "").strip():
            path = f"/v2/groups/{group_id.strip()}/messages/{message_id}"
        elif str(user_id or "").strip():
            path = f"/v2/users/{user_id.strip()}/messages/{message_id}"
        else:
            return False
        await self._v2_request("DELETE", path)
        return True

    async def delete_msg(self, message_id: str = "", **kw: Any) -> dict[str, Any]:
        """OneBot 同名接口：撤回。失败抛 :class:`QQOpenPlatformAPIError`。"""
        mid = str(message_id or kw.get("message_id") or "").strip()
        group_id = str(kw.get("group_id") or kw.get("message_sent") or "").strip()
        user_id = str(kw.get("user_id") or "").strip()
        if await self.recall_message(mid, group_id=group_id, user_id=user_id):
            return {"status": "ok", "message_id": mid}
        return {}

    async def delete_group_msg(self, group_id: str = "", message_id: str = "", **_: Any) -> dict[str, Any]:
        return await self.delete_msg(message_id=message_id, group_id=group_id)

    async def delete_friend_msg(self, user_id: str = "", message_id: str = "", **_: Any) -> dict[str, Any]:
        return await self.delete_msg(message_id=message_id, user_id=user_id)

    # ── 读：群与成员 ────────────────────────────────────────

    async def get_group_info(self, group_id: str = "", **_: Any) -> dict[str, Any]:
        """群名称/简介/人数。``bot_state`` 之外这几个字段是 ``info`` 独有的。"""
        gid = str(group_id or "").strip()
        if not gid:
            return {}
        return await self._v2_request("GET", f"/v2/groups/{gid}/info")

    async def get_group_bot_state(self, group_id: str, *, force: bool = False) -> dict[str, Any]:
        """机器人在本群的状态：``recv_msg_setting`` / ``member_role`` / ``allow_proactive_msg``。

        这是全量模式的**权威判据**（``bot_state`` 直接告诉你是 ``all`` 还是
        ``only_mention``），60 QPM 且有白名单门槛，所以按群缓存 10 分钟，
        拿不到（11253）时记一次并别再反复要——那时退回"按已观测事件判断"。
        """
        gid = str(group_id or "").strip()
        if not gid:
            return {}
        cache = getattr(self, "_v2_bot_state", None)
        if cache is None:
            cache = self._v2_bot_state = {}
        hit = cache.get(gid)
        now = time.time()
        if hit and not force and now - float(hit.get("_at", 0)) < 600:
            return hit
        try:
            data = await self._v2_request("GET", f"/v2/groups/{gid}/bot_state")
        except QQOpenPlatformAPIError as exc:
            if hit:
                return hit
            if exc.err_code == ERR_APP_FORBIDDEN:
                self._v2_log(
                    "info",
                    f"bot_state 未开权限（群 {gid}）：收信范围改由收到的事件推断",
                )
            else:
                self._v2_log("warning", f"获取机器人群内状态失败: {exc}")
            cache[gid] = {"_at": now, "_failed": True}
            return {}
        data["_at"] = now
        cache[gid] = data
        self._v2_apply_bot_state(data)
        return data

    def _v2_apply_bot_state(self, state: dict[str, Any]) -> None:
        setting = str(state.get("recv_msg_setting") or "").strip()
        role = str(state.get("member_role") or "").strip()
        allow = state.get("allow_proactive_msg")
        if setting:
            self._v2_recv_setting = setting
        if role:
            self._v2_bot_role = role
        if isinstance(allow, bool):
            self._v2_allow_proactive = allow
        if state.get("member_openid"):
            self._v2_bot_member_openid = str(state["member_openid"])

    async def get_group_member_info(
        self, group_id: str = "", user_id: str = "", **_: Any
    ) -> dict[str, Any]:
        """单个群成员（``member_openid`` 口径）。"""
        gid = str(group_id or "").strip()
        member = str(user_id or "").strip()
        if not gid or not member:
            return {}
        if member == str(self._self_id or ""):
            # 问的是机器人自己：平台的成员接口只认 member_openid，而登录身份是 app 级 id，
            # 直接查必然 404。改问 bot_state（自带 member_openid / 角色 / 收信范围）。
            state = await self.get_group_bot_state(gid)
            return {
                "member_openid": str(state.get("member_openid") or ""),
                "username": self._self_nickname,
                "member_role": str(state.get("member_role") or ""),
                "bot": True,
                "joined_at": str(state.get("joined_at") or ""),
            }
        return await self._v2_request("GET", f"/v2/groups/{gid}/members/{member}")

    async def get_group_member_list(
        self, group_id: str = "", **kw: Any
    ) -> list[dict[str, Any]]:
        """群成员列表（自动翻页到 ``max_pages`` 页）。

        官方接口每页最多 30 条并回 ``next_cursor``，空串到底。列表接口标"内邀"，
        没权限时返回空表 —— 调用方（称呼、点名判定）退回原有行为，不报错。
        """
        gid = str(group_id or kw.get("group_openid") or "").strip()
        if not gid:
            return []
        limit = max(1, min(int(kw.get("max_pages") or 10), 20))
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(limit):
            try:
                data = await self._v2_request(
                    "GET", f"/v2/groups/{gid}/members", params={"cursor": cursor},
                )
            except QQOpenPlatformAPIError as exc:
                self._v2_log("warning", f"获取群成员列表失败: {exc}")
                break
            out.extend(data.get("members") or [])
            cursor = str(data.get("next_cursor") or "")
            if not cursor:
                break
        return out

    async def get_group_list(self) -> list[dict[str, Any]]:
        """返回连接期间实际收到过消息的群（官方平台没有"我所在的群列表"接口）。

        空列表比编一个假群好；`group_name` 要靠 `GET /v2/groups/{openid}/info`，
        那个接口是白名单能力（11253），所以不在这儿顺手调，留给按群的动作。
        """
        seen = getattr(self, "_v2_seen_groups", {}) or {}
        out = []
        for gid in sorted(seen):
            stamp, count = seen[gid] if isinstance(seen[gid], tuple) else (seen[gid], 0)
            out.append({
                "group_id": gid, "group_name": "",
                "messages_seen": int(count), "last_seen_at": float(stamp),
            })
        return out

    async def get_login_info(self) -> dict[str, Any]:
        """机器人身份。``GET /users/@me`` 能给正式名称，握手里的 READY 只给 app 级 id。"""
        cached = getattr(self, "_v2_bot_profile", None)
        if cached:
            return cached
        info = {"user_id": self._self_id, "nickname": self._self_nickname}
        try:
            data = await self._v2_request("GET", "/users/@me")
        except Exception as exc:
            self._v2_log("warning", f"获取机器人详情失败，沿用握手身份: {exc}")
            return info
        if isinstance(data, dict):
            info = {
                "user_id": str(data.get("id") or self._self_id),
                "nickname": str(data.get("username") or self._self_nickname),
                "avatar": str(data.get("avatar") or ""),
            }
            self._v2_bot_profile = info
        return info

    # ── 写：群管理（全部要求机器人在该群是管理员/群主）──────

    async def set_group_ban(
        self, group_id: str = "", user_id: str = "", duration: int = 600, **_: Any
    ) -> dict[str, Any]:
        """禁言单个成员。v2 用"到期时间"而不是"时长"，``duration=0`` 即解除。"""
        gid = str(group_id or "").strip()
        if not gid or not str(user_id or "").strip():
            return {}
        return await self.set_group_mute_setting(gid, [{
            "op": "del" if int(duration or 0) <= 0 else "add",
            "member_openid": str(user_id).strip(),
            "mute_expire_at": "" if int(duration or 0) <= 0 else _rfc3339_after(int(duration)),
        }])

    async def set_group_mute_setting(
        self, group_id: str, members: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """``POST /v2/groups/{gid}/restrict_chat_setting``，单次上限 20 人。"""
        gid = str(group_id or "").strip()
        payload = [m for m in (members or []) if isinstance(m, dict)][:20]
        if not gid or not payload:
            return {}
        await self._v2_request(
            "POST", f"/v2/groups/{gid}/restrict_chat_setting", body={"members": payload},
        )
        return {"status": "ok", "count": len(payload)}

    async def get_group_mute_setting(
        self, group_id: str = "", **kw: Any
    ) -> dict[str, Any]:
        gid = str(group_id or kw.get("group_openid") or "").strip()
        if not gid:
            return {}
        return await self._v2_request("GET", f"/v2/groups/{gid}/restrict_chat_setting")

    async def set_group_kick(
        self, group_id: str = "", user_id: str = "", **kw: Any
    ) -> dict[str, Any]:
        """移出成员（可同时拉黑，v2 是一条接口两件事）。"""
        gid = str(group_id or "").strip()
        if not gid or not str(user_id or "").strip():
            return {}
        body: dict[str, Any] = {"member_openids": [str(user_id).strip()]}
        if kw.get("add_to_member_blacklist") or kw.get("reject_add_request"):
            body["add_to_member_blacklist"] = True
        data = await self._v2_request("POST", f"/v2/groups/{gid}/batch_remove_members", body=body)
        return data or {"status": "ok"}

    async def remove_group_members(
        self, group_id: str, member_openids: list[str], *, add_to_blacklist: bool = False
    ) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        ids = [str(x).strip() for x in (member_openids or []) if str(x).strip()][:20]
        if not gid or not ids:
            return {}
        return await self._v2_request(
            "POST", f"/v2/groups/{gid}/batch_remove_members",
            body={"member_openids": ids, "add_to_member_blacklist": bool(add_to_blacklist)},
        )

    async def operate_group_member_blacklist(
        self, group_id: str, member_openids: list[str], *, op: str = "add"
    ) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        ids = [str(x).strip() for x in (member_openids or []) if str(x).strip()][:20]
        if not gid or not ids or op not in ("add", "del"):
            return {}
        return await self._v2_request(
            "POST", f"/v2/groups/{gid}/member_blacklist",
            body={"op": op, "member_openids": ids},
        )

    async def list_group_member_blacklist(self, group_id: str, cursor: str = "") -> dict[str, Any]:
        gid = str(group_id or "").strip()
        if not gid:
            return {}
        return await self._v2_request(
            "GET", f"/v2/groups/{gid}/member_blacklist", params={"cursor": cursor},
        )

    async def list_group_join_requests(
        self, group_id: str = "", cursor: str = "", limit: int = 20, **_: Any
    ) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        if not gid:
            return {}
        return await self._v2_request(
            "GET", f"/v2/groups/{gid}/join_request_list",
            params={"cursor": cursor, "limit": max(1, min(int(limit or 20), 50))},
        )

    async def approve_group_join_request(
        self, group_id: str, member_openid: str, *, approve: bool = True,
        join_request_id: str = "", reject_reason: str = "", add_to_blacklist: bool = False,
    ) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        mid = str(member_openid or "").strip()
        if not gid or not mid:
            return {}
        body: dict[str, Any] = {"op": "approve" if approve else "decline"}
        if join_request_id:
            body["join_request_id"] = str(join_request_id)
        if not approve:
            if reject_reason:
                body["reject_reason"] = str(reject_reason)[:100]
            if add_to_blacklist:
                body["add_to_member_blacklist"] = True
        await self._v2_request("POST", f"/v2/groups/{gid}/approval_join_request/{mid}", body=body)
        return {"status": "ok", "op": body["op"], "member_openid": mid}

    #: ``set_group_add_request`` 的 OneBot 名字映射：approve=True 通过，否则拒绝。
    async def set_group_add_request(
        self, flag: str = "", approve: bool = True, reason: str = "", **kw: Any
    ) -> dict[str, Any]:
        gid = str(kw.get("group_id") or kw.get("group_openid") or "").strip()
        mid = str(kw.get("user_id") or kw.get("member_openid") or "").strip()
        if not gid or not mid:
            return {}
        return await self.approve_group_join_request(
            gid, mid, approve=bool(approve), join_request_id=str(flag or ""),
            reject_reason=str(reason or ""),
        )

    async def generate_url_link(self, callback_data: str = "") -> dict[str, Any]:
        """生成"加好友"分享链接；``callback_data`` 最长 32 字符，会透传给后台。"""
        body = {"callback_data": str(callback_data or "")[:32]}
        return await self._v2_request("POST", "/v2/generate_url_link", body=body)

    # ── 互动：按钮回调 / 菜单 / 指令面板 ────────────────────

    async def respond_interaction(
        self, interaction_id: str, code: int = 0
    ) -> dict[str, Any]:
        """给 ``INTERACTION_CREATE`` 回执行结果。**3 秒内必须回**，否则用户端无反馈。

        ``code``：0 成功 / 1 操作失败 / 2 频繁 / 3 重复 / 4 无权限 / 5 仅管理员。
        """
        iid = str(interaction_id or "").strip()
        if not iid:
            return {}
        await self._v2_request("PUT", f"/interactions/{iid}", body={"code": int(code or 0)})
        return {"status": "ok", "interaction_id": iid}

    async def get_menu(self) -> dict[str, Any]:
        return await self._v2_request("GET", "/v2/menu")

    async def set_menu(self, menu: dict[str, Any]) -> dict[str, Any]:
        """整体覆盖全局自定义菜单（``items`` 最多 10 项）。"""
        if not isinstance(menu, dict):
            return {}
        await self._v2_request("PUT", "/v2/menu", body={"menu": menu})
        return {"status": "ok"}

    async def list_panels(
        self, scope: str = "", cursor: str = "", limit: int = 20
    ) -> dict[str, Any]:
        """按场景查指令面板列表。``scope`` 是平台必填项，缺了它 40030011。"""
        scope = str(scope or "").strip()
        if scope not in PANEL_SCOPES:
            raise ValueError(f"scope 只能是 {'/'.join(PANEL_SCOPES)}（平台必填）")
        return await self._v2_request(
            "GET", "/v2/panels",
            params={"scope": scope, "cursor": cursor, "limit": max(1, min(int(limit or 20), 50))},
        )

    async def create_panel(self, panel: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(panel, dict):
            return {}
        return await self._v2_request("POST", "/v2/panels", body=panel)

    async def get_panel(self, panel_id: str) -> dict[str, Any]:
        return await self._v2_request("GET", f"/v2/panels/{str(panel_id).strip()}")

    async def update_panel(self, panel_id: str, panel: dict[str, Any]) -> dict[str, Any]:
        await self._v2_request("PUT", f"/v2/panels/{str(panel_id).strip()}", body=panel or {})
        return {"status": "ok"}

    async def delete_panel(self, panel_id: str) -> dict[str, Any]:
        await self._v2_request("DELETE", f"/v2/panels/{str(panel_id).strip()}")
        return {"status": "ok"}

    async def set_panel_target(
        self, panel_id: str, op: str,
        *, user_openids: Optional[list[str]] = None, group_openids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """改面板的关联对象。``op`` 只有 add / del；全局面板（target_type=all）不支持。"""
        panel_id = str(panel_id or "").strip()
        if not panel_id:
            return {}
        op = str(op or "").strip()
        if op not in ("add", "del"):
            raise ValueError("op 只能是 add / del")
        body: dict[str, Any] = {"op": op}
        users = [str(x).strip() for x in (user_openids or []) if str(x).strip()][:20]
        groups = [str(x).strip() for x in (group_openids or []) if str(x).strip()][:20]
        if users:
            body["user_openids"] = users
        if groups:
            body["group_openids"] = groups
        if not users and not groups:
            raise ValueError("要给出 user_openids 或 group_openids")
        await self._v2_request("PUT", f"/v2/panels/{panel_id}/target", body=body)
        return {"status": "ok", "panel_id": panel_id, "op": op}

    # ── 通用调用（白名单）──────────────────────────────────

    def v2_endpoint_catalog(self) -> list[dict[str, str]]:
        return [
            {"id": eid, "method": method, "path": path, "summary": summary}
            for eid, (method, path, summary) in sorted(V2_ENDPOINTS.items())
        ]

    async def v2_call(
        self, endpoint_id: str, *,
        path_params: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """按名字调一个**表内**接口。表外的 id 直接拒绝。

        这条路是给"官方机器人有、插件还没包一层"的能力留的出口：入口只接受
        ``endpoint_id`` + 参数，路径永远来自 :data:`V2_ENDPOINTS`，不会被调用方
        拼成任意 URL（那等于给机器人一个万能 HTTP 代理）。
        """
        spec = V2_ENDPOINTS.get(str(endpoint_id or "").strip())
        if not spec:
            raise ValueError(f"未知的官方接口: {endpoint_id}")
        method, template, _ = spec
        path = self._v2_format_path(template, {str(k): v for k, v in (path_params or {}).items()})
        return await self._v2_request(method, path, params=params, body=body)

    # ── 官方平台确实没有的操作：说清楚，不要回空桩 ──────────

    async def _v2_unsupported(self, name: str, reason: str) -> Any:
        raise QQOpenPlatformUnsupported(name, reason)

    async def set_msg_emoji_like(self, **kw: Any) -> Any:
        await self._v2_unsupported(
            "表情表态", "v2 的表情表态只有频道消息接口（PUT /channels/{channel_id}/messages/"
            "{message_id}/reactions/{type}/{id}），群聊与单聊没有对应接口",
        )

    async def send_group_poke(self, group_id: str = "", user_id: str = "", **kw: Any) -> Any:
        await self._v2_unsupported("戳一戳", "v2 群/单聊没有戳一戳接口（OneBot 私有动作）")

    async def group_poke(self, **kw: Any) -> Any:
        await self._v2_unsupported("戳一戳", "v2 群/单聊没有戳一戳接口（OneBot 私有动作）")

    async def friend_poke(self, **kw: Any) -> Any:
        await self._v2_unsupported("戳一戳", "v2 没有好友戳一戳接口")

    async def send_like(self, **kw: Any) -> Any:
        await self._v2_unsupported("点赞", "v2 没有点赞接口")

    async def get_profile_like(self, **kw: Any) -> Any:
        await self._v2_unsupported("点赞资料", "v2 没有点赞相关接口")

    async def mark_msg_as_read(self, **kw: Any) -> Any:
        await self._v2_unsupported("标记已读", "v2 没有已读标记接口")

    async def mark_private_msg_as_read(self, **kw: Any) -> Any:
        await self._v2_unsupported("标记已读", "v2 没有已读标记接口")

    async def mark_group_msg_as_read(self, **kw: Any) -> Any:
        await self._v2_unsupported("标记已读", "v2 没有已读标记接口")

    async def _mark_all_as_read(self, **kw: Any) -> Any:
        await self._v2_unsupported("标记已读", "v2 没有已读标记接口")

    async def send_group_forward_msg(self, **kw: Any) -> Any:
        await self._v2_unsupported("合并转发", "v2 群/单聊没有合并转发接口")

    async def send_private_forward_msg(self, **kw: Any) -> Any:
        await self._v2_unsupported("合并转发", "v2 群/单聊没有合并转发接口")

    async def send_forward_msg(self, **kw: Any) -> Any:
        await self._v2_unsupported("合并转发", "v2 群/单聊没有合并转发接口")

    async def forward_friend_single_msg(self, **kw: Any) -> Any:
        await self._v2_unsupported("合并转发", "v2 没有单条转发接口")

    async def forward_group_single_msg(self, **kw: Any) -> Any:
        await self._v2_unsupported("合并转发", "v2 没有单条转发接口")

    async def get_forward_msg(self, **kw: Any) -> Any:
        await self._v2_unsupported("转发记录", "v2 没有读取转发消息的接口")

    async def get_group_msg_history(self, **kw: Any) -> Any:
        await self._v2_unsupported(
            "群历史消息", "v2 没有消息列表接口；机器人只能收到事件推来的消息（插件的 backlog 就是为此存在）",
        )

    async def get_friend_msg_history(self, **kw: Any) -> Any:
        await self._v2_unsupported("单聊历史消息", "v2 没有消息列表接口")

    async def set_group_whole_ban(self, **kw: Any) -> Any:
        await self._v2_unsupported("全群禁言", "v2 的 restrict_chat_setting 只按成员逐个设置")

    async def set_group_admin(self, **kw: Any) -> Any:
        await self._v2_unsupported("设置群管理员", "v2 没有该接口")

    async def set_group_card(self, **kw: Any) -> Any:
        await self._v2_unsupported("群名片", "v2 没有设置群名片接口")

    async def set_group_name(self, **kw: Any) -> Any:
        await self._v2_unsupported("群名称", "v2 没有修改群名接口")

    async def set_group_leave(self, **kw: Any) -> Any:
        await self._v2_unsupported("退群", "v2 没有让机器人退群的接口（需在开放平台后台操作）")

    async def set_group_special_title(self, **kw: Any) -> Any:
        await self._v2_unsupported("群专属头衔", "v2 没有该接口")

    async def set_group_sign(self, **kw: Any) -> Any:
        await self._v2_unsupported("群签到", "v2 没有该接口")

    async def send_group_sign(self, **kw: Any) -> Any:
        await self._v2_unsupported("群签到", "v2 没有该接口")

    async def set_group_portrait(self, **kw: Any) -> Any:
        await self._v2_unsupported("群头像", "v2 没有该接口")

    async def get_group_at_all_remain(self, **kw: Any) -> Any:
        await self._v2_unsupported("@全体余量", "v2 不返回余量；发了才知道（管理员且每群每自然日 1 次）")

    async def get_group_ignore_add_request(self, **kw: Any) -> Any:
        await self._v2_unsupported("忽略的入群申请", "v2 没有该接口")

    async def get_group_system_msg(self, **kw: Any) -> Any:
        await self._v2_unsupported("系统消息", "v2 没有该接口")

    async def _send_group_notice(self, **kw: Any) -> Any:
        await self._v2_unsupported("群公告", "v2 没有群公告接口")

    async def _get_group_notice(self, **kw: Any) -> Any:
        await self._v2_unsupported("群公告", "v2 没有群公告接口")

    async def _del_group_notice(self, **kw: Any) -> Any:
        await self._v2_unsupported("群公告", "v2 没有群公告接口")

    async def get_group_honor_info(self, **kw: Any) -> Any:
        await self._v2_unsupported("群荣誉", "v2 没有该接口")

    async def get_group_shut_list(self, group_id: str = "", **kw: Any) -> list[dict[str, Any]]:
        """当前被禁言的成员（OneBot 同名接口的形状：一个列表）。"""
        gid = str(group_id or kw.get("group_id") or "").strip()
        if not gid:
            return []
        data = await self._v2_request("GET", f"/v2/groups/{gid}/restrict_chat_setting")
        members = data.get("members")
        return members if isinstance(members, list) else []

    async def get_essence_msg_list(self, **kw: Any) -> Any:
        await self._v2_unsupported("精华消息", "v2 没有该接口")

    async def set_essence_msg(self, **kw: Any) -> Any:
        await self._v2_unsupported("精华消息", "v2 没有该接口")

    async def delete_essence_msg(self, **kw: Any) -> Any:
        await self._v2_unsupported("精华消息", "v2 没有该接口")

    async def get_cookies(self, **kw: Any) -> Any:
        await self._v2_unsupported("登录 Cookie", "官方机器人没有网页登录态，凭据只有 AppID/AppSecret")

    async def get_csrf_token(self, **kw: Any) -> Any:
        await self._v2_unsupported("CSRF Token", "官方机器人没有网页登录态")

    async def get_credentials(self, **kw: Any) -> Any:
        await self._v2_unsupported("登录凭据", "官方机器人没有网页登录态")

    async def get_stranger_info(self, **kw: Any) -> Any:
        await self._v2_unsupported("陌生人资料", "v2 只有群成员资料（按 member_openid）")

    async def get_image(self, **kw: Any) -> Any:
        await self._v2_unsupported("读取本地图片", "官方机器人没有本地图片缓存，附件一律走事件里的 URL")

    async def download_file(self, **kw: Any) -> Any:
        await self._v2_unsupported("下载群文件", "v2 只有上传接口；下载用事件里的 URL")

    async def get_file(self, **kw: Any) -> Any:
        await self._v2_unsupported("读取群文件", "v2 只有上传接口；下载用事件里的 URL")

    async def delete_group_file(self, **kw: Any) -> Any:
        await self._v2_unsupported("删除群文件", "v2 没有该接口")

    async def create_group_file_folder(self, **kw: Any) -> Any:
        await self._v2_unsupported("群文件夹", "v2 没有该接口")

    async def delete_group_folder(self, **kw: Any) -> Any:
        await self._v2_unsupported("群文件夹", "v2 没有该接口")

    async def get_group_file_system_info(self, **kw: Any) -> Any:
        await self._v2_unsupported("群文件系统", "v2 没有该接口")

    async def get_group_root_files(self, **kw: Any) -> Any:
        await self._v2_unsupported("群文件系统", "v2 没有该接口")

    async def get_group_files_by_folder(self, **kw: Any) -> Any:
        await self._v2_unsupported("群文件系统", "v2 没有该接口")

    async def get_group_file_url(self, **kw: Any) -> Any:
        await self._v2_unsupported("群文件系统", "v2 没有该接口")

    async def set_friend_add_request(self, **kw: Any) -> Any:
        await self._v2_unsupported("好友申请", "v2 没有审批好友申请的接口")

    async def delete_friend(self, **kw: Any) -> Any:
        await self._v2_unsupported("删除好友", "v2 没有该接口")

    async def get_friends_with_category(self, **kw: Any) -> Any:
        await self._v2_unsupported("好友分组", "v2 没有好友列表接口")

    # ── 事件：把平台推的东西都接下来 ────────────────────────

    # ── 接管网关帧 ─────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """网关接收循环：``op=0`` 的**每一个**事件都要往下走。

        基类这一段的写法是 ``if event_type in ("GROUP_AT_MESSAGE_CREATE",
        "C2C_MESSAGE_CREATE")`` —— 其余事件（全量群消息、入群申请、按钮点击、
        收信开关、入群/退群）平台推了，这里直接扔掉。上层因此从来不知道有这些事。

        这里是重写而不是复用：opcode 的处理顺序照基类，差别只在 dispatch 分支交给
        :meth:`_v2_dispatch`。断线一律走基类的 ``_try_reconnect``，异常不往外冒。
        """
        while not self._closing:
            if not self._ws:
                await asyncio.sleep(1)
                continue
            try:
                payload = json.loads(await self._ws.recv())
                op = payload.get("op")
                if op == 0:  # Dispatch
                    self._last_seq = payload.get("s", self._last_seq)
                    event_type = str(payload.get("t") or "")
                    data = payload.get("d")
                    # 取证探针要在白名单之前跑：没配受信群也要看得见平台推了什么。
                    probe = getattr(self, "_log_identity_probe", None)
                    if callable(probe):
                        probe(event_type, data)
                    await self._v2_dispatch(event_type, data, str(payload.get("id") or ""))
                    continue
                if op == 1:  # 服务端催心跳
                    await self._ws.send(json.dumps({"op": 11, "d": self._last_seq}))
                    continue
                if op == 11:  # 心跳回执
                    continue
                if op == 7:  # 要求重连：拆掉当前 ws，交给下面的重连
                    self._v2_log("warning", "服务端要求重连")
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    self._ws = None
                    self.ws = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 不单独接 websockets.ConnectionClosed：那条分支也只是记一行再重连，
                # 少一个顶层 import 就少一个装不上依赖时的崩点。
                self._v2_log("warning", f"接收循环中断: {exc}")
            if not self._closing:
                await self._try_reconnect()

    def add_event_listener(self, callback: Callable[[str, dict[str, Any]], Any]) -> None:
        """注册一个非消息事件的监听器（入群申请、按钮点击、收信开关……）。

        返回值：协程 ``True`` 表示"这个事件我处理过了"。``INTERACTION_CREATE``
        在没人处理时补一个 ``code=0`` 应答 —— 平台要求 3 秒内响应，不理会用户端
        就一直转圈。
        """
        listeners = getattr(self, "_v2_listeners", None)
        if listeners is None:
            listeners = self._v2_listeners = []
        if callback not in listeners:
            listeners.append(callback)

    def remove_event_listener(self, callback: Callable[[str, dict[str, Any]], Any]) -> None:
        listeners = getattr(self, "_v2_listeners", None) or []
        if callback in listeners:
            listeners.remove(callback)

    async def _v2_emit_notice(self, event_type: str, payload: dict[str, Any]) -> bool:
        handled = False
        for listener in list(getattr(self, "_v2_listeners", None) or []):
            try:
                result = listener(event_type, payload)
                if asyncio.iscoroutine(result):
                    result = await result
            except Exception as exc:
                self._v2_log("warning", f"事件监听器异常（{event_type}）: {exc}")
                continue
            handled = handled or result is True
        return handled

    async def _v2_dispatch(
        self, event_type: str, data: dict[str, Any], envelope_id: str
    ) -> None:
        """一个 Dispatch 帧 -> 消息队列 / 监听器。"""
        if not isinstance(data, dict):
            return
        if event_type in V2_MESSAGE_EVENTS:
            if event_type == "GROUP_MESSAGE_CREATE" and self._v2_note_full_mode():
                await self._v2_emit_notice(
                    "OPEN_PLATFORM_FULL_MESSAGES_ON", {"channel": self.CHANNEL},
                )
            msg = self._v2_normalize_message(event_type, data, envelope_id)
            if msg:
                self._v2_remember_group(msg)
                try:
                    self._message_queue.put_nowait(msg)
                except asyncio.QueueFull:
                    # 与基类一致：丢最旧的一条，保证最新上下文能进管线。
                    try:
                        self._message_queue.get_nowait()
                    except Exception:
                        pass
                    self._message_queue.put_nowait(msg)
            return

        if event_type == "GROUP_ADD_ROBOT":
            gid = str(data.get("group_openid") or data.get("group_id") or "").strip()
            if gid:
                self._v2_seen_group(gid)
                # 新群：探一次 bot_state，才知道这个群是 @ 模式还是全量模式。
                await self.get_group_bot_state(gid, force=True)
        elif event_type in ("GROUP_MSG_RECEIVE", "GROUP_MSG_REJECT"):
            gid = str(data.get("group_openid") or "").strip()
            if gid:
                await self.get_group_bot_state(gid, force=True)

        if event_type not in V2_NOTICE_EVENTS:
            # 没见过的事件名要说得出来：平台加事件时，静默丢弃等于"功能不存在"。
            self._v2_log("info", f"未处理的官方事件: {event_type}")
            return
        payload = self._v2_notice(event_type, data, envelope_id)
        handled = await self._v2_emit_notice(event_type, payload)
        if event_type == "INTERACTION_CREATE" and not handled:
            iid = str(payload.get("interaction_id") or "")
            if iid:
                try:
                    await self.respond_interaction(iid, 0)
                except Exception as exc:
                    self._v2_log("warning", f"互动应答失败: {exc}")

    def _v2_notice(self, event_type: str, data: dict[str, Any], envelope_id: str) -> dict[str, Any]:
        """非消息事件的统一形状（和消息一样带 ``channel``，上层按通道过滤）。"""
        group_id = str(data.get("group_openid") or data.get("group_id") or "")
        actor = str(
            data.get("op_member_openid") or data.get("member_openid")
            or data.get("user_openid") or ""
        )
        out: dict[str, Any] = {
            "event_type": event_type,
            "channel": self.CHANNEL,
            "group_id": group_id,
            "user_id": actor,
            "timestamp": int(time.time()),
            "raw": data,
            "event_id": envelope_id,
        }
        if event_type == "INTERACTION_CREATE":
            out["interaction_id"] = str(data.get("id") or "")
            out["type"] = _as_int(data.get("type"))
            resolved = (data.get("data") or {}).get("resolved") if isinstance(data.get("data"), dict) else None
            button = resolved.get("button") if isinstance(resolved, dict) else None
            if isinstance(button, dict):
                # 真正有用的是按钮的 data（我们的指令按钮把动作写在里面），
                # 不是整个 resolved。
                out["button_id"] = str(button.get("id") or "")
                out["button_data"] = str(button.get("data") or "")
            else:
                out["button_data"] = str(resolved or "")
            out["author"] = str((data.get("author") or {}).get("user_openid", "")) if isinstance(data.get("author"), dict) else actor
        if event_type == "GROUP_JOIN_REQUEST":
            out["join_request_id"] = str(data.get("join_request_id") or "")
            out["username"] = str(data.get("username") or "")
        return out

    def _v2_seen_group(self, group_id: str) -> None:
        """记下"这个群我们真的收到过消息"，并计数。

        这是官方机器人上唯一可靠的群名单来源：平台不给"我所在的群列表"接口，
        而信任群名单里的 QQ 群号在这条通道上永远匹配不上（id 命名空间不同）。
        """
        seen = getattr(self, "_v2_seen_groups", None)
        if seen is None:
            seen = self._v2_seen_groups = {}
        gid = str(group_id)
        stamp, count = seen.get(gid) or (0.0, 0)
        seen[gid] = (time.time(), count + 1)

    def _v2_remember_group(self, msg: dict[str, Any]) -> None:
        gid = str(msg.get("group_id") or "")
        if gid:
            self._v2_seen_group(gid)

    # ── 入站归一化 ──────────────────────────────────────────

    _AT_OPENID_RE = re.compile(r"<@([0-9A-Za-z_-]{8,})>")
    _AT_LEGACY_RE = re.compile(r"<@!(\d+)>")

    def _v2_normalize_message(
        self, event_type: str, data: dict[str, Any], envelope_id: str
    ) -> Optional[dict[str, Any]]:
        """v2 消息事件 -> 插件的统一消息形状（基类形状，字段一个不多一个不少）。"""
        is_private = event_type == "C2C_MESSAGE_CREATE"
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        content_raw = str(data.get("content") or "")

        mentioned: list[str] = []
        for user in data.get("mentions") or []:
            if isinstance(user, dict):
                who = str(user.get("member_openid") or user.get("id") or "").strip()
                if who:
                    mentioned.append(who)
        for match in self._AT_OPENID_RE.finditer(content_raw):
            who = match.group(1)
            if who not in mentioned:
                mentioned.append(who)
        for match in self._AT_LEGACY_RE.finditer(content_raw):
            who = match.group(1)
            if who not in mentioned:
                mentioned.append(who)
        mentions_all = "<@all>" in content_raw
        clean_content = self._AT_OPENID_RE.sub("", self._AT_LEGACY_RE.sub("", content_raw))
        clean_content = clean_content.replace("<@all>", "").strip()

        msg: dict[str, Any] = {
            "message_type": "private" if is_private else "group",
            "channel": self.CHANNEL,
            "user_id": pick_actor_id(author, _C2C_ACTOR_ID_KEYS if is_private else _GROUP_ACTOR_ID_KEYS),
            "user_nickname": str(author.get("username") or "") or None,
            "content": clean_content if not is_private else content_raw.strip(),
            "message_id": str(data.get("id") or ""),
            "timestamp": int(time.time()),
            "is_at_bot": True if (is_private or event_type == "GROUP_AT_MESSAGE_CREATE") else False,
            "is_reply_to_bot": False,
            "group_id": "" if is_private else str(
                data.get("group_id") or data.get("group_openid") or ""
            ),
            "quoted_message_id": "",
            "mentioned_user_ids": mentioned,
            "mentions_other_user": False,
            "mentions_all": mentions_all,
            "raw": data,
            "attachments": self._v2_extract_attachments(data),
            "event_id": envelope_id,
            "message_kind": _as_int(data.get("message_type")),
        }
        if not is_private:
            self_ids = {str(self._self_id or "")}
            bot_member = str(getattr(self, "_v2_bot_member_openid", "") or "")
            if bot_member:
                self_ids.add(bot_member)
            if not msg["is_at_bot"]:
                msg["is_at_bot"] = bool(self_ids & set(mentioned)) or any(
                    f"<@{sid}>" in content_raw for sid in self_ids if sid
                )
            elif len(mentioned) == 1 and event_type == "GROUP_AT_MESSAGE_CREATE":
                # 机器人自己的**群内** member_openid 只有两个来源：白名单接口
                # `bot_state`（很多机器人没这权限），或者从"只 @ 了一个人"的 @ 事件里学。
                # 学不到就只能把全量事件里的 @ 她判成没 @ 她，于是接话判定把该醒的群
                # 判成睡着 —— 现象是"群里 @ 了她也不回"。
                self._v2_bot_member_openid = mentioned[0]
            msg["mentions_other_user"] = any(who not in self_ids for who in mentioned)
        # 平台自带的语音识别：asr_refer_text 是白送的，不用它就只能等一个空的
        # "[语音]" 标记。shortcut: 没有 asr 结果时不回落 STT（要改 enrichment 的
        # 取音频路径）；哪天平台不给了，再把 wav_url 接进 voice_transcriber。
        if not msg["content"]:
            for att in msg["attachments"]:
                if att.get("type") == "voice" and str(att.get("asr_text") or "").strip():
                    msg["content"] = str(att["asr_text"]).strip()
                    break
        msg["quoted_message_id"] = self._v2_resolve_reference(data)
        msg["is_reply_to_bot"] = bool(
            msg["quoted_message_id"]
            and msg["quoted_message_id"] in (getattr(self, "_sent_message_ids", {}) or {})
        )
        # 记下这条消息的引用令牌，之后引用它要用（不是消息 id）。
        msg_idx = _v2_ext_value(data, "msg_idx")
        if msg_idx and msg["message_id"]:
            self._v2_remember_reference(msg["message_id"], msg_idx)
        if msg["group_id"]:
            self._v2_seen_group(msg["group_id"])
        return msg

    def _v2_note_full_mode(self) -> bool:
        """收到过 GROUP_MESSAGE_CREATE = 这个群确实在推全量消息（硬证据）。"""
        """返回 True 表示"这一次刚刚变成全量模式"，由调用方去通知上层。"""
        if getattr(self, "_v2_full_mode_seen", False):
            return False
        self._v2_full_mode_seen = True
        self._v2_log(
            "info",
            "全量群消息模式已生效（收到 GROUP_MESSAGE_CREATE）：接话判定与注意力竞争开始工作",
        )
        return True

    def _v2_resolve_reference(self, data: dict[str, Any]) -> str:
        """把事件里的 ``ref_msg_idx`` 换成"被引用消息的消息 id"（只认识机器人自己发过的）。"""
        ref = _v2_ext_value(data, "ref_msg_idx")
        if not ref:
            return ""
        return str((getattr(self, "_v2_send_ref_map", None) or {}).get(ref) or "")

    def _v2_remember_reference(self, message_id: str, ref_idx: str) -> None:
        cache = getattr(self, "_v2_inbound_ref_map", None)
        if cache is None:
            cache = self._v2_inbound_ref_map = {}
        cache[str(message_id)] = str(ref_idx)
        while len(cache) > REF_IDX_CACHE_SIZE:
            cache.pop(next(iter(cache)))

    def _v2_reference_token_for(self, message_id: str) -> str:
        """发消息时引用某条消息要用的令牌（``REFIDX_...``）。查不到就只做被动回复。"""
        return str((getattr(self, "_v2_inbound_ref_map", None) or {}).get(str(message_id or "")) or "")

    @staticmethod
    def _v2_extract_attachments(data: dict[str, Any]) -> list[dict[str, Any]]:
        """附件：图片/视频/**语音**/文件，并带上文件名、大小、语音的 WAV 与 ASR 文本。

        基类那份只分 "image/" 前缀与 "file"，语音和视频因此和普通文件混在一起，
        而且把平台给的两个免费礼物丢了：``voice_wav_url``（平台已经把 silk 转成
        WAV）和 ``asr_refer_text``（平台已经识别出的文字）。
        """
        out: list[dict[str, Any]] = []
        for att in data.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            url = str(att.get("url") or "")
            if not url:
                continue
            ctype = str(att.get("content_type") or "")
            if ctype == "voice" or url.endswith((".silk", ".amr")):
                kind = "voice"
            elif ctype.startswith("video/") or ctype == "video/mp4":
                kind = "video"
            elif ctype.startswith("image/"):
                kind = "image"
            else:
                kind = "file"
            item: dict[str, Any] = {
                "type": kind, "url": url, "content_type": ctype,
                "name": str(att.get("filename") or ""),
                "size": _as_int(att.get("size")),
            }
            if kind == "voice":
                item["wav_url"] = str(att.get("voice_wav_url") or "")
                item["asr_text"] = str(att.get("asr_refer_text") or "")
            if att.get("width") is not None:
                item["width"] = _as_int(att.get("width"))
            if att.get("height") is not None:
                item["height"] = _as_int(att.get("height"))
            out.append(item)
        return out

    # 兼容基类调用点（``_convert_event`` 内部用的就是这个名字）。
    def _extract_attachments(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        return self._v2_extract_attachments(data)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _v2_explain(path: str, err_code: int, message: str) -> str:
    """把平台的原文加上"下一步该做什么"。

    ``40011028`` 最常见的真因不是"群注销了"，而是**拿 OneBot 时代的 QQ 群号去调官方
    接口**：官方平台只认 ``group_openid``（32/64 位十六进制），两套 id 不能互换。
    原话"请求的资源不存在(用户/群已注销)"会把人往错方向带，所以这里补一句。
    """
    if int(err_code or 0) == ERR_NOT_FOUND and re.match(r"^/v2/groups/\d+", str(path or "")):
        return (
            f"{message} —— 这个 group_id 是纯数字的 QQ 群号，官方平台只认 group_openid；"
            "让她先在群里收到一条消息，日志里就会出现真正的 group_openid"
        )
    return message


def _group_id_from_path(path: str) -> str:
    m = re.match(r"/v2/groups/([^/]+)", str(path or ""))
    return m.group(1) if m else ""

def _v2_wakeup_enabled(conn: Any) -> bool:
    """互动召回开关：读设置在**调用时**，不在构造时定格。"""
    reader = getattr(conn, "_v2_wakeup_reader", None)
    return bool(reader()) if callable(reader) else False


def _v2_ext_value(data: dict[str, Any], key: str) -> str:
    """``message_scene.ext`` 是 ``["msg_idx=...", "auth_token=..."]`` 这种 k=v 列表。"""
    scene = data.get("message_scene") if isinstance(data.get("message_scene"), dict) else {}
    prefix = f"{key}="
    for item in scene.get("ext") or []:
        text = str(item or "")
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def _rfc3339_after(seconds: int) -> str:
    """RFC3339 的"从现在起 ``seconds`` 秒后"（v2 禁言接口要的是到期时间，不是时长）。"""
    from datetime import datetime, timedelta, timezone

    local = datetime.now(timezone(timedelta(hours=8)))
    return (local + timedelta(seconds=int(seconds))).replace(microsecond=0).isoformat()


def _v2_segment_placeholder(seg_type: str, data: dict[str, Any]) -> str:
    """OneBot 专有段在 v2 没有对应物，转成可读文本而不是悄悄丢掉（和基类一致）。"""
    if seg_type == "face":
        return f"[表情{data.get('id', '')}]"
    if seg_type == "mface":
        return "[动画表情]"
    if seg_type == "json":
        return "[卡片消息]"
    if seg_type == "music":
        return "[音乐分享]"
    if seg_type == "contact":
        return "[推荐联系人]"
    if seg_type == "rps":
        return "[猜拳]"
    if seg_type == "dice":
        return "[骰子]"
    return ""


def _v2_media_placeholder(file_type: int, source: str) -> str:
    label = FILE_TYPE_NAMES.get(int(file_type or 0), "文件")
    tail = str(source or "").rsplit("/", 1)[-1][:40]
    return f"[{label}未发送成功 {tail}]".strip()


#: 基类 -> 已生成的能力层子类。宿主连接器换了实现就再生成一个，不共用。
_V2_SUBCLASSES: dict[type, type] = {}


def v2_connection_class(base: type) -> type:
    """把能力层与某个连接器实现合成一个子类（按基类缓存）。

    Mixin 排在前面，所以这里的方法**覆盖**基类的同名实现 —— 这正是目的：
    基类那几个能力旗标和空桩是错的。

    刻意不用 ``instance.__class__ = 子类`` 那招"给现成对象换类"：连接器基类是
    ``ABC``，实测（vendored 实现 + CPython 3.14/3.15）会抛
    ``__class__ assignment: object layout differs``。换类看着省一步，真机上直接
    挂不上，还只在挂的时候报一行 warning —— 不如老实生成子类再构造。
    """
    subclass = _V2_SUBCLASSES.get(base)
    if subclass is None:
        # 用基类自己的元类建子类：连接器基类是 ABC（元类 ABCMeta），
        # 直接写 type(...) 会撞 "metaclass conflict"。
        subclass = _V2_SUBCLASSES[base] = base.__class__(
            f"{base.__name__}V2", (QQOpenPlatformV2Mixin, base), {},
        )
    return subclass


def create_open_platform_connection(
    settings_or_reader: Any, *, logger: Any = None, emit_log: Any = None
) -> Any:
    """按开放平台的设置造一个**带能力层**的连接对象。

    这里自己拼构造参数，而不是复用 ``create_onebot_connection``：那个工厂写死了
    它自己的类名，插件没法把子类塞进去；而给它抄一份"参数表"是最省事、也最容易
    被宿主漂移打脸的地方。所以：先照工厂的形状构造，构造报 ``TypeError``
    （宿主加了必填参数）就退回工厂的原实现并明确记一行 —— 宁可少一层能力，
    也不要启动就连不上。

    参数取值与工厂一致：``identity_probe`` / ``sandbox`` 都传**回调**，
    这样改设置立刻生效，不需要重连。
    """
    from .connector_seam import QQOpenPlatformConnection

    get_settings = settings_or_reader if callable(settings_or_reader) else (lambda: settings_or_reader)
    settings = get_settings() or {}
    kwargs = {
        "app_id": str(settings.get("qq_open_app_id") or "").strip(),
        "client_secret": str(settings.get("qq_open_client_secret") or "").strip(),
        "logger": logger,
        "identity_probe": lambda: bool(get_settings().get("qq_open_identity_probe_enabled", False)),
        "emit_log": emit_log,
        "sandbox": lambda: bool(get_settings().get("qq_open_sandbox_enabled", False)),
    }
    try:
        connection = v2_connection_class(QQOpenPlatformConnection)(**kwargs)
        # 召回开关不在构造参数里（那是宿主的签名，插件不该往上加）；挂成回调，
        # 与 sandbox / identity_probe 同一套"改设置立刻生效"的约定。
        connection._v2_wakeup_reader = lambda: bool(
            (get_settings() or {}).get("qq_open_proactive_wakeup", False)
        )
        return connection
    except TypeError as exc:
        emit = emit_log or (lambda *_a: None)
        message = f"官方机器人能力层构造失败，退回连接器原实现: {exc}"
        if logger:
            logger.warning(f"[QQOpenPlatformV2] {message}")
        emit("WARNING", message)
        from .connector_seam import create_onebot_connection

        return create_onebot_connection(get_settings(), logger=logger, emit_log=emit_log)
