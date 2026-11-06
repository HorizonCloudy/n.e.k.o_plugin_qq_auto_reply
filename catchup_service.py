"""断线重连后，把漏掉的消息补回来。

## 为什么需要它

插件收消息只有一条路：OneBot 服务端推过来，`handle_message` 处理。**断线期间
推不过来的消息就是真的没了** —— 服务端不会重发，插件也不知道自己错过什么。
表现是：网络抖一下、或者 NapCat 重启一次，她再开口时会说"你刚才说什么？"，
而对方其实已经说了三句。

这件事上游的 `backlog_service` 管不了：那个是**给人看的群消息汇总**（哪些群
有未读、哪些消息待审核），数据来源同样是推过来的消息 —— 断线期间它一样空着。
补消息必须主动去服务端翻历史，是另一条路。

## 怎么知道"刚重连过"

上游的连接器由 `connector_seam` 构建（可能是宿主第一方包，也可能回退到
`_vendor` 副本），**不能假设它有"连接建立"回调**。所以这里用轮询：
定期读 `client.is_connected()`，抓 False → True 的那一次跳变。

轮询而不是回调，代价是最多晚一个周期才知道；好处是不碰连接器，谁构建的都能用。

## 判据

游标记的是"每个会话处理到哪条了"（`message_seq`）。重连后拉一页历史，
比游标新的就是漏的。三处容易错，都在具体位置写了注释：

* `call_action` 已经拆掉 OneBot 信封，拿到的是 `{"messages": [...]}`；
* 空 `{}` 不等于"真没消息"，可能是一次超时被 `or {}` 吞掉；
* 拿不到 `message_seq` 时**不能拿时间戳凑**，只能标"位置不明"，下次只对齐不注入。

## 去重靠两份东西

游标是"位置"，`recent_ids` 是"名单"。只有位置不够：插件重载时游标可能还没
落盘（写完有防抖），重连后上一轮刚回过的消息会被当成漏消息再补一遍，
她对着同一条消息回两次。名单比位置可靠。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from utils.file_utils import atomic_write_json_async, read_json_async


class QQMessageCatchupService:
    """重连后把断线期间漏掉的 QQ 消息补回正常管道。"""

    CURSOR_FILENAME = "message_cursor.json"
    SCHEMA_VERSION = 1

    #: 一次最多补几个会话。多到没边的话，一次重连要打十几个 QQ 接口，
    #: 既慢又容易被风控，而且补回来的东西她也看不过来。
    MAX_CONVERSATIONS = 8
    #: 每个会话最多翻几页。一页 40 条，三页够覆盖一整夜。
    MAX_PAGES = 3
    #: 每次拉取条数（QQ 端上限附近，实测 60 条能一次拿到 15 小时）
    FETCH_COUNT = 40
    #: 去重名单长度。400 条足够覆盖一次重连前后所有可能重叠的消息。
    MAX_RECENT_IDS = 400
    #: 两次补消息之间的最小间隔，防止连接反复抖动时把接口打爆
    MIN_INTERVAL_SECONDS = 30.0
    #: 补回来的消息之间的注入间隔：让下游缓冲有机会把它们合并成一轮。
    #: 0.12 秒是实测值——更密的话每条都单独开一轮生成，看起来像刷屏。
    DEFAULT_STAGGER_SECONDS = 0.12

    #: ``call_action`` 对不存在的接口会抛 ``RuntimeError``，文案各实现不一样，
    #: 只能靠这几个词认。"1404" 是 NapCat 系对未知 action 的 retcode。
    UNSUPPORTED_MARKS = (
        "1404", "不支持", "not supported", "unknown action",
        "no such action", "未实现", "undefine", "not found",
    )

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.logger = getattr(plugin, "logger", None)

        #: key = "private:<qq>" / "group:<群号>"，value = {seq, at, kind, peer, updated}
        self._cursors: dict[str, dict[str, Any]] = {}
        #: message_id → 处理时间。只留最近 MAX_RECENT_IDS 条。
        self._recent_ids: "OrderedDict[str, float]" = OrderedDict()
        self._loaded = False
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._last_catchup_at: float = 0.0
        self._last_result: dict[str, Any] = {}
        #: 上一次轮询看到的连接状态。None 表示还没看过——第一次采样只记录，
        #: 不触发补消息（插件刚启动时"连上了"不算重连，那时候没有漏的消息）。
        self._was_connected: Optional[bool] = None
        #: 分项计数。吞异常必须留数：接口不支持、真没消息、调用失败三种情况
        #: 处置完全不一样，混成一个"补了 0 条"等于没观测。
        self._diag: dict[str, int] = {
            "catchup_runs": 0,
            "conv_ok": 0,
            "conv_empty": 0,
            "conv_unsupported": 0,
            "conv_failed": 0,
            "injected": 0,
            "skipped_capped": 0,
            "skipped_old": 0,
            "skipped_self": 0,
            "skipped_dup": 0,
            "seq_missing": 0,
        }
        self._consecutive_failures = 0

    # ── 配置 ────────────────────────────────────────────────

    def _cfg(self, key: str, default: Any) -> Any:
        settings = getattr(self.plugin, "_qq_settings", None) or {}
        value = settings.get(key, default)
        return default if value is None else value

    def _enabled(self) -> bool:
        return bool(self._cfg("catchup_enabled", True))

    # ── 持久化 ──────────────────────────────────────────────

    def _path(self) -> Path:
        try:
            return Path(self.plugin.data_path()) / self.CURSOR_FILENAME
        except Exception:
            return Path(self.CURSOR_FILENAME)

    async def _load(self) -> None:
        if self._loaded:
            return
        path = self._path()
        payload: Any = None
        if path.is_file():
            try:
                payload = await read_json_async(path)
            except Exception:
                # 游标坏了不该影响启动——最坏结果就是这次少补一些，下次照常
                if self.logger:
                    self.logger.warning("[Catchup] 游标文件读取失败，按空处理", exc_info=True)
                payload = None
        conversations = (payload or {}).get("conversations") if isinstance(payload, dict) else None
        if isinstance(conversations, dict):
            for key, rec in conversations.items():
                if not isinstance(rec, dict):
                    continue
                try:
                    seq = int(rec.get("seq") or 0)
                except (TypeError, ValueError):
                    seq = 0
                self._cursors[str(key)] = {
                    "seq": seq,
                    "at": int(rec.get("at") or 0),
                    "kind": str(rec.get("kind") or ""),
                    "peer": str(rec.get("peer") or ""),
                    "updated": int(rec.get("updated") or 0),
                    "seq_unknown": bool(rec.get("seq_unknown")),
                }
                if rec.get("pending_seq") is not None:
                    self._cursors[str(key)]["pending_seq"] = max(0, int(rec["pending_seq"]))
        ids = (payload or {}).get("recent_ids") if isinstance(payload, dict) else None
        if isinstance(ids, dict):
            for mid, ts in ids.items():
                try:
                    self._recent_ids[str(mid)] = float(ts or 0)
                except (TypeError, ValueError):
                    continue
        self._loaded = True

    async def _flush(self, *, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        payload = {
            "version": self.SCHEMA_VERSION,
            "saved_at": int(time.time()),
            "conversations": self._cursors,
            "recent_ids": dict(self._recent_ids),
        }
        try:
            await atomic_write_json_async(self._path(), payload)
            self._dirty = False
        except Exception:
            if self.logger:
                self.logger.warning("[Catchup] 游标落盘失败", exc_info=True)

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1.0)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception:
                if self.logger:
                    self.logger.warning("[Catchup] 落盘循环异常", exc_info=True)

    # ── 记录：每收到一条消息就推一次游标 ──────────────────────

    @staticmethod
    def _conversation_key(message: dict[str, Any]) -> str:
        if str(message.get("message_type")) == "group":
            gid = str(message.get("group_id") or "").strip()
            return f"group:{gid}" if gid else ""
        uid = str(message.get("user_id") or "").strip()
        if not uid:
            uid = str((message.get("sender") or {}).get("user_id") or "").strip()
        return f"private:{uid}" if uid else ""

    def note_message(self, message: dict[str, Any]) -> None:
        """记录"处理到哪了"。同步、只动内存，落盘交给后台循环。

        由 ``QQMessageDispatcher.handle_message`` 在入口调用，所以在收消息的
        热路径上——这里绝对不能 await，一 await 整条 WS 流就卡住了。
        """
        try:
            if str(message.get("post_type") or "message") != "message":
                return
            key = self._conversation_key(message)
            if not key:
                return
            # 先记名单再算位置：就算这条算不出 seq，"这条已经处理过"这个事实
            # 也必须留下——名单是防重复注入的最后一道闸。
            self._remember_id(message)

            rec = self._cursors.get(key)
            if rec is None:
                kind, peer = key.split(":", 1)
                rec = {"seq": 0, "at": 0, "kind": kind, "peer": peer, "updated": 0}
                self._cursors[key] = rec
            rec["updated"] = int(time.time())
            ts = int(message.get("time") or 0)
            if ts:
                rec["at"] = ts

            try:
                seq = int(message.get("message_seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > 0:
                # 只在**没被标脏**的时候用 seq 推进。标脏说明之前混进过
                # 不可比的数，得等下次对账重新对齐，否则会跳到一个错误的位置。
                if not rec.get("seq_unknown") and seq > int(rec.get("seq") or 0):
                    previous_seq = int(rec.get("seq") or 0)
                    if previous_seq and seq > previous_seq + 1 and not message.get("_catchup"):
                        # 实时事件可能先于轮询到达。另存缺口起点，不能让最新
                        # 消息的高水位覆盖尚未补回的断线消息。
                        rec.setdefault("pending_seq", previous_seq)
                    rec["seq"] = seq
            else:
                # 拿不到 message_seq 时**绝对不能拿时间戳凑**。
                # 实测踩过：拿 time*1000（一万亿量级）当 seq 写进去，之后所有
                # 历史消息的 seq（一百多）都"比它小"，补消息就永久失效；反过来
                # 写得比真实值小，重连后又会把回过的消息再补一遍。
                # 正确做法是标记"位置不明"，让下次对账只对齐、不注入。
                rec["seq_unknown"] = True
                self._diag["seq_missing"] += 1
            self._dirty = True
        except Exception:
            if self.logger:
                self.logger.warning("[Catchup] 记录游标失败", exc_info=True)

    def _remember_id(self, message: dict[str, Any]) -> None:
        """记住处理过的 message_id（去重名单）。"""
        mid = str(message.get("message_id") or "").strip()
        if not mid:
            return
        self._recent_ids[mid] = time.time()
        self._recent_ids.move_to_end(mid)
        while len(self._recent_ids) > self.MAX_RECENT_IDS:
            self._recent_ids.popitem(last=False)

    # ── 重连检测 ────────────────────────────────────────────

    async def start(self) -> None:
        await self._load()
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        for task in (self._watch_task, self._flush_task):
            if task is not None and not task.done():
                task.cancel()
        self._watch_task = None
        self._flush_task = None
        await self._flush(force=True)

    async def _watch_loop(self) -> None:
        """轮询连接状态，抓 False → True 的那次跳变。

        没用连接器的"连上回调"：连接器可能是宿主第一方包，也可能是 `_vendor`
        副本，两边不保证都有这个钩子。轮询谁构建的都能用，代价是最多晚一个
        周期才知道——对补消息来说这点延迟无所谓。
        """
        interval = max(10, int(self._cfg("catchup_sweep_seconds", 30)))
        while True:
            try:
                await asyncio.sleep(interval)
                await self._watch_tick()
            except asyncio.CancelledError:
                break
            except Exception:
                if self.logger:
                    self.logger.warning("[Catchup] 重连检测循环异常", exc_info=True)

    async def _watch_tick(self) -> None:
        """一次采样。抽成独立方法是为了能单测——直接调它，不用等轮询周期。"""
        if not self._enabled():
            return
        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            return
        try:
            connected = bool(client.is_connected())
        except Exception:
            connected = False
        previous = self._was_connected
        self._was_connected = connected
        if previous is None:
            # 第一次采样只记录。插件刚启动时"连上了"不算重连：
            # 那时候游标是上次运行留下的，本来就该对齐，但没有"漏的消息"
            # 这个概念——真断了之后的重连才是要补的。
            return
        if not connected and previous:
            for rec in self._cursors.values():
                rec.setdefault("pending_seq", int(rec.get("seq") or 0))
            self._dirty = True
        if connected and (not previous or any("pending_seq" in rec for rec in self._cursors.values())):
            await self.catch_up(reason="reconnect" if not previous else "sequence_gap")

    # ── 主流程 ──────────────────────────────────────────────

    def _self_id(self) -> str:
        client = getattr(self.plugin, "qq_client", None)
        return str(getattr(client, "self_id", "") or "").strip()

    def _conversations_to_check(self, limit: Optional[int] = None) -> list[str]:
        """要补哪些会话：按最近活跃排序，外加管理员私聊兜底。"""
        admin = str(getattr(self.plugin, "_admin_qq", "") or "").strip()
        cap = self.MAX_CONVERSATIONS if limit is None else max(1, int(limit))
        ordered = sorted(
            self._cursors.items(),
            key=lambda kv: int(kv[1].get("updated") or 0),
            reverse=True,
        )
        picked = [key for key, _ in ordered[:cap]]
        # 管理员那条永远要补，哪怕它排不进最近的几个：主人发的话漏了最难受
        if admin:
            admin_key = f"private:{admin}"
            if admin_key not in picked:
                picked.insert(0, admin_key)
        return picked

    async def catch_up(self, *, reason: str = "manual", force: bool = False) -> dict[str, Any]:
        """重连后对账补消息。返回这次做了什么，供日志和状态查询用。"""
        await self._load()
        now = time.time()
        # 连接抖动时 30 秒内可能触发好几次，没必要每次都打接口
        if not force and now - self._last_catchup_at < self.MIN_INTERVAL_SECONDS:
            return {"status": "throttled", "reason": reason}
        self._last_catchup_at = now
        self._diag["catchup_runs"] += 1

        window = float(self._cfg("catchup_window_hours", 12)) * 3600
        limit = int(self._cfg("catchup_max_per_conversation", 12))
        total_cap = int(self._cfg("catchup_max_total", 30))
        stagger = int(self._cfg("catchup_stagger_ms", 120)) / 1000.0

        keys = self._conversations_to_check()
        result: dict[str, Any] = {
            "status": "ok", "reason": reason, "conversations": len(keys),
            "injected": 0, "skipped": 0, "details": [],
        }
        remaining = total_cap
        for key in keys:
            if remaining <= 0:
                break
            if not getattr(self.plugin, "_running", False):
                break
            rec = self._cursors.get(key)
            if rec is None:
                continue
            row = await self._catch_up_conversation(
                key, rec, window=window, limit=min(limit, remaining),
                stagger=stagger, force=force,
            )
            result["details"].append({"key": key, **row})
            injected = int(row.get("injected") or 0)
            result["injected"] += injected
            result["skipped"] += int(row.get("skipped") or 0)
            remaining -= injected

        self._last_result = result
        await self._flush()
        if result["injected"]:
            self._emit(
                "INFO",
                f"[Catchup] 重连补消息（{reason}）：补了 {result['injected']} 条，"
                f"跳过 {result['skipped']} 条，涉及 {len(result['details'])} 个会话",
            )
        return result

    async def _catch_up_conversation(
        self,
        key: str,
        rec: dict[str, Any],
        *,
        window: float,
        limit: int,
        stagger: float,
        force: bool = False,
    ) -> dict[str, Any]:
        kind, peer = key.split(":", 1)
        cursor_seq = int(rec.get("pending_seq", rec.get("seq")) or 0)
        self_id = self._self_id()
        now = time.time()

        # 从没见过的会话（首次启动、新加的好友）：**只对齐游标，不补消息**。
        # 不这么做的话，第一次跑起来会把服务端历史里整个窗口的消息全倒进管道，
        # 她会对着几小时前的话一口气回十二条，看起来像抽风。
        #
        # ``seq_unknown`` 同样走这条路：那说明现场事件里取不到 message_seq，
        # 现在这个游标是不可信的，只能拿历史重新对齐一次，不能拿它去判定
        # "哪些是漏的"——判错的代价是把回过的消息再回一遍。
        #
        # 手动触发（reason="manual"）时跳过这道闸，方便验证和救急。
        align_only = (
            (not int(rec.get("updated") or 0) and cursor_seq <= 0)
            or bool(rec.get("seq_unknown"))
        )

        collected: list[dict[str, Any]] = []
        anchor = 0
        pages = 0
        while pages < self.MAX_PAGES:
            fetched, status = await self._fetch_history(kind, peer, anchor)
            pages += 1
            if status != "ok":
                return {"status": status, "injected": 0, "skipped": 0, "pages": pages}
            if not fetched:
                break
            collected.extend(fetched)
            seqs = [self._seq_of(m) for m in fetched]
            oldest = min(seqs) if seqs else 0
            times = [int(m.get("time") or 0) for m in fetched]
            times = [t for t in times if t]
            oldest_time = min(times) if times else 0
            if align_only and not force:
                break
            if oldest <= cursor_seq + 1:
                break
            if oldest_time and now - oldest_time > window:
                # 已经翻到窗口之外，再往前都是"陈年老账"，没必要补
                break
            # 用这一页最老的 seq 当下一页的锚点（get_*_msg_history 的
            # message_seq 是"从这条往前翻"，传 0 表示最新一页）
            if anchor and oldest >= anchor:
                break
            anchor = oldest

        if align_only and not force:
            max_seq = max((self._seq_of(m) for m in collected), default=0)
            if max_seq > cursor_seq:
                rec["seq"] = max_seq
                rec["updated"] = int(now)
            # 对齐完要把脏标记清掉：位置已经重新锚定了
            rec.pop("seq_unknown", None)
            rec.pop("pending_seq", None)
            self._dirty = True
            self._diag["conv_ok"] += 1
            return {"status": "aligned", "injected": 0, "skipped": 0, "pages": pages}

        if not collected:
            self._diag["conv_empty"] += 1
            return {"status": "empty", "injected": 0, "skipped": 0, "pages": pages}

        # 按 seq 升序（服务端返回的是倒序），并按时间窗/自身消息/重复/上限过滤
        candidates = sorted(collected, key=lambda m: self._seq_of(m))
        skipped_capped = skipped_old = skipped_self = skipped_dup = 0
        missed: list[dict[str, Any]] = []
        batch_ids: set[str] = set()
        for msg in candidates:
            seq = self._seq_of(msg)
            mid = str(msg.get("message_id") or "").strip()
            sender = str(
                msg.get("user_id")
                or (msg.get("sender") or {}).get("user_id")
                or ""
            ).strip()
            ts = int(msg.get("time") or 0)
            if seq and cursor_seq and seq <= cursor_seq:
                continue
            if mid and (mid in self._recent_ids or mid in batch_ids):
                skipped_dup += 1
                continue
            if mid:
                batch_ids.add(mid)
            if self_id and sender == self_id:
                # 她自己发的不补：那不是"漏掉的消息"，补进去等于让她自问自答
                skipped_self += 1
                continue
            if ts and now - ts > window:
                skipped_old += 1
                continue
            if len(missed) >= limit:
                skipped_capped += 1
                continue
            missed.append(msg)

        injected = 0
        for msg in missed:
            if not getattr(self.plugin, "_running", False):
                break
            ts = int(msg.get("time") or 0)
            msg["_catchup"] = True
            msg["_catchup_reason"] = "catchup"
            msg["_catchup_age_seconds"] = int(now - ts) if ts else 0
            # 历史接口给的段格式与实时事件一致，但缺 post_type。补上——
            # handle_message 里有些分支会看它。
            msg.setdefault("post_type", "message")
            try:
                await self.plugin.message_dispatcher.handle_message(msg)
                injected += 1
                self._diag["injected"] += 1
                self._remember_id(msg)
            except Exception:
                self._note_failure(key)
                # 入口 note_message 可能已经记下失败消息；允许下次重新注入。
                self._recent_ids.pop(str(msg.get("message_id") or ""), None)
                rec["pending_seq"] = cursor_seq
                self._dirty = True
                if self.logger:
                    self.logger.warning(f"[Catchup] 注入 {key} 消息失败", exc_info=True)
                return {"status": "failed", "injected": injected, "skipped": 0, "pages": pages}
            if stagger:
                await asyncio.sleep(stagger)

        self._diag["skipped_capped"] += skipped_capped
        self._diag["skipped_old"] += skipped_old
        self._diag["skipped_self"] += skipped_self
        self._diag["skipped_dup"] += skipped_dup

        # 游标推到"见过的最大 seq"，包括没注入的（她自己发的、太老的、被限流的）。
        # 不推的话下次重连还会把它们翻出来，白白多打一轮接口。
        max_seq = max((self._seq_of(m) for m in collected), default=0)
        # 翻页/投递期间仍会收到实时消息，不能把高水位倒退，也不能清掉
        # 本次历史快照之后的新缺口；下一次轮询继续补那一段。
        live_seq = int(rec.get("seq") or 0)
        if live_seq > max_seq:
            rec["pending_seq"] = max(cursor_seq, max_seq)
        else:
            rec.pop("pending_seq", None)
        if max_seq > cursor_seq:
            rec["seq"] = max(live_seq, max_seq)
            rec["kind"] = kind
            rec["peer"] = peer
            rec["updated"] = int(now)
            latest_ts = max((int(m.get("time") or 0) for m in collected), default=0)
            if latest_ts:
                rec["at"] = latest_ts
        if self_id:
            rec["self_id"] = self_id

        self._diag["conv_ok"] += 1
        self._consecutive_failures = 0
        self._dirty = True
        return {
            "status": "ok",
            "injected": injected,
            "skipped": skipped_capped + skipped_old + skipped_self + skipped_dup,
            "skipped_capped": skipped_capped,
            "skipped_old": skipped_old,
            "skipped_self": skipped_self,
            "skipped_dup": skipped_dup,
            "pages": pages,
            "cursor": rec.get("seq"),
        }

    @staticmethod
    def _seq_of(msg: dict[str, Any]) -> int:
        try:
            return int(msg.get("message_seq") or 0)
        except (TypeError, ValueError):
            return 0

    async def _fetch_history(
        self, kind: str, peer: str, anchor: int,
    ) -> tuple[list[dict[str, Any]], str]:
        """拉一页历史。返回 (消息列表, 状态)。

        ⚠️ ``call_action`` 已经把 OneBot 信封里的 ``data`` **拆出来**了，所以
        这里拿到的是 ``{"messages": [...]}``，不是 ``{"status","retcode","data"}``。
        按信封解析会永远得到空列表，日志显示"补充 0 条"，看着像"断线期间真没
        消息"，其实是解析错位。

        状态分三种，因为处置完全不一样：
          - ``ok``：拿到了（列表可能是空的，那是真没消息）
          - ``unsupported``：这个 OneBot 实现没有该接口——``call_action`` 对
            ``status=failed`` 会抛 ``RuntimeError``，只能从文案里认。
          - ``failed``：超时/连接断了/返回形状不认识

        **开放平台通道走这条路永远是 ``failed``**：`qq_open_plat.py` 里这两个
        方法是同名空实现，直接返回 ``{}``。空 dict 不能当成"真没消息"，否则
        用户会以为补成功了，而实际上一条都没补。
        """
        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            return [], "failed"
        try:
            if kind == "private":
                data = await client.get_friend_msg_history(
                    peer, message_seq=int(anchor), count=self.FETCH_COUNT
                )
            else:
                data = await client.get_group_msg_history(
                    peer, message_seq=int(anchor), count=self.FETCH_COUNT
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            text = str(exc)
            if any(mark in text for mark in self.UNSUPPORTED_MARKS):
                self._diag["conv_unsupported"] += 1
                if self.logger:
                    self.logger.info(f"[Catchup] {kind}:{peer} 不支持历史接口: {text[:80]}")
                return [], "unsupported"
            self._diag["conv_failed"] += 1
            if self.logger:
                self.logger.warning(f"[Catchup] 拉取 {kind}:{peer} 历史失败: {text[:120]}")
            return [], "failed"

        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            # 常见的三种字段名：messages（LLBot/NapCat）、message_list、纯列表兜底
            messages = data.get("messages")
            if messages is None:
                messages = data.get("message_list")
            if messages is None and not data:
                # 空 dict = call_action 拿到的 data 是空的：可能是超时后被
                # `or {}` 吞掉（NapCat），也可能是开放平台的空实现。
                # 两种情况都**不能算"真没消息"**，否则补消息静默失效。
                self._diag["conv_failed"] += 1
                return [], "failed"
        else:
            self._diag["conv_failed"] += 1
            return [], "failed"

        if not isinstance(messages, list):
            self._diag["conv_failed"] += 1
            return [], "failed"
        return [m for m in messages if isinstance(m, dict)], "ok"

    def _note_failure(self, key: str) -> None:
        self._diag["conv_failed"] += 1
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            self.logger.warning(
                f"[Catchup] 连续 {self._consecutive_failures} 次补消息失败（最近: {key}），"
                "可能是通道不支持历史接口"
            )

    def _emit(self, level: str, text: str) -> None:
        """日志两路都发：`_emit_log` 进 UI 运行日志页，`logger` 进宿主日志文件。"""
        emit = getattr(self.plugin, "_emit_log", None)
        if callable(emit):
            try:
                emit(level, text)
            except Exception:
                pass
        if self.logger:
            try:
                self.logger.info(text)
            except Exception:
                pass

    def describe_state(self) -> dict[str, Any]:
        """给 UI / 状态接口看的快照。"""
        return {
            "enabled": self._enabled(),
            "conversations": len(self._cursors),
            "recent_ids": len(self._recent_ids),
            "last_catchup_at": int(self._last_catchup_at) if self._last_catchup_at else 0,
            "last_result": self._last_result,
            "connected": self._was_connected,
            "diag": dict(self._diag),
        }
