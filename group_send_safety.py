"""Group mute and burst protection. Blocked replies are discarded, never delayed."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any


class GroupSendSafety:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.cutoff: dict[str, float] = {}
        self.muted: dict[str, bool] = {}
        self.muted_until: dict[str, float] = {}
        self.checked: dict[str, float] = {}
        self.recent: dict[str, list[float]] = {}
        self.last_sticker: dict[str, float] = {}
        self.generating: dict[str, int] = {}
        self.last_question: dict[str, float] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def note_reply(self, group_id: str, *, question: bool):
        if question:
            self.last_question[str(group_id)] = time.time()
        else:
            self.last_question.pop(str(group_id), None)

    def style_hint(self, group_id: str) -> str:
        last = self.last_question.get(str(group_id))
        if last is not None and time.time() - last < 120:
            return "你上一条已用问句收尾。这轮优先回应对方给出的内容，用陈述句结束；仅在缺少完成明确请求必需的信息时再澄清，不要追加闲聊问题。\n"
        return ""

    async def blocked(self, group_id: str, *, created_at: float = 0,
                      force: bool = False) -> bool:
        gid = str(group_id)
        async with self.locks.setdefault(gid, asyncio.Lock()):
            now = time.time()
            client = getattr(self.plugin, "qq_client", None)
            # Capture notice expiry before is_group_muted removes expired entries.
            local_expiries = getattr(client, "_group_muted", None)
            if isinstance(local_expiries, dict) and local_expiries.get(gid):
                self.muted_until[gid] = float(local_expiries[gid])
            check = getattr(client, "is_group_muted", None)
            local_muted = callable(check) and check(gid) is True
            muted = local_muted
            if force or now - self.checked.get(gid, 0) >= 5:
                member = getattr(client, "get_group_member_info", None)
                login = getattr(client, "get_login_info", None)
                if callable(member) and callable(login):
                    try:
                        async with asyncio.timeout(5):
                            info = await login()
                            uid = info.get("user_id") if isinstance(info, dict) else None
                            if uid:
                                data = await member(gid, str(uid), no_cache=True)
                                if isinstance(data, dict):
                                    until = float(data.get("shut_up_timestamp") or 0)
                                    if until > 0:
                                        self.muted_until[gid] = max(
                                            until, self.muted_until.get(gid, 0) if local_muted else 0,
                                        )
                                    muted = muted or until > now
                    except Exception:
                        # Keep an active known mute, but don't extend its expiry.
                        until = self.muted_until.get(gid)
                        muted = muted or (self.muted.get(gid, False) and (until is None or until > now))
                self.checked[gid] = now
            else:
                until = self.muted_until.get(gid)
                muted = muted or (self.muted.get(gid, False) and (until is None or until > now))
            if muted or self.muted.get(gid, False):
                # A late observation of natural expiry must not discard fresh input.
                boundary = now if muted else min(now, self.muted_until.get(gid, now))
                self.cutoff[gid] = max(self.cutoff.get(gid, 0), boundary)
            if muted != self.muted.get(gid, False):
                emit = getattr(self.plugin, "_emit_log", None)
                if callable(emit):
                    emit("INFO", f"[Mute] 群{gid} {'禁言中' if muted else '已解禁'}，此前消息与待发回复不补发")
            self.muted[gid] = muted
            return muted or bool(created_at and created_at <= self.cutoff.get(gid, 0))

    def has_capacity(self, group_id: str) -> bool:
        """Non-consuming preflight; delivery still checks and consumes the quota."""
        now = time.time()
        rows = self.recent.setdefault(str(group_id), [])
        rows[:] = [stamp for stamp in rows if now - stamp < 60]
        return len(rows) < 3

    def admit(self, group_id: str) -> bool:
        """Reserve one outgoing message attempt, not a whole reply plan.

        Failed/uncertain sends keep their reservation to avoid retry bursts.
        """
        if not self.has_capacity(group_id):
            return False
        self.recent[str(group_id)].append(time.time())
        return True

    def admit_sticker(self, group_id: str) -> bool:
        """Reserve before upload so concurrent replies cannot burst stickers."""
        gid = str(group_id)
        now = time.time()
        last = self.last_sticker.get(gid)
        if last is not None and now - last < 30:
            return False
        if not self.admit(gid):
            return False
        self.last_sticker[gid] = now
        return True

    @asynccontextmanager
    async def generation(self, group_id: str):
        """Shared preflight for both model entry points; never queue generation."""
        gid = str(group_id)
        if await self.blocked(gid):
            yield "group_muted"
            return
        self.has_capacity(gid)  # Prune expired send reservations.
        pending = getattr(getattr(self.plugin, "reply_buffer_service", None), "_pending", {})
        draft = pending.get(f"group:{gid}") if isinstance(pending, dict) else None
        # A placeholder has no generated blocks yet. A delivering plan is
        # already accounted for by actual send reservations.
        queued = len(getattr(draft, "first_blocks", ()) or ()) if not getattr(draft, "delivering", False) else 0
        if len(self.recent[gid]) + self.generating.get(gid, 0) + queued >= 3:
            yield "group_rate_limit"
            return
        self.generating[gid] = self.generating.get(gid, 0) + 1
        try:
            yield ""
        finally:
            left = self.generating[gid] - 1
            if left:
                self.generating[gid] = left
            else:
                self.generating.pop(gid, None)


def group_send_safety(plugin: Any) -> GroupSendSafety:
    service = getattr(plugin, "_group_send_safety", None)
    if not isinstance(service, GroupSendSafety):
        service = GroupSendSafety(plugin)
        plugin._group_send_safety = service
    return service
