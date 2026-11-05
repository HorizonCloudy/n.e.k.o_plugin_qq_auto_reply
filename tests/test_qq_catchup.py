"""断线补消息的判据。

这块代码的价值全在"判得对"上：判错一个方向是**她对着几小时前的话一口气回
十条**，判错另一个方向是**漏掉的话永远补不回来，而且日志一片绿**。所以每个
边界都单独钉一条，包括三个不显眼但真会出事的：

* 拿不到 ``message_seq`` 时不许拿时间戳凑（凑出来的数会让游标永久错位）；
* ``{}`` 不是"真没消息"（超时会被 `or {}` 吞掉，开放平台干脆返回空实现）；
* 第一次遇到的会话只对齐、不注入（否则新加一个好友就"抽风"）。
"""

from __future__ import annotations

import asyncio
import functools
import time
from pathlib import Path
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.catchup_service import QQMessageCatchupService


def _sync(fn):
    """把 async 测试包成同步的。

    仓库里没有 pytest-asyncio（上游测试一直是 ``asyncio.run(...)`` 手写），
    引一个新依赖只为跑这几条断言不值得，CI 也不保证装。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Client:
    """假连接：history 里 key 是 ``group:<id>`` / ``private:<id>``。"""

    def __init__(self) -> None:
        self.connected = True
        self.self_id = "999"
        self.history: dict[str, list[dict]] = {}
        self.empty_payload = False

    def is_connected(self) -> bool:
        return self.connected

    async def get_group_msg_history(self, group_id, message_seq: int = 0, count: int = 40):
        if self.empty_payload:
            # 开放平台的同名空实现 / 超时被 `or {}` 吞掉，长这样
            return {}
        if message_seq:
            # 第二次翻页返回空，让分页循环正常停下
            return {"messages": []}
        return {"messages": list(self.history.get(f"group:{group_id}", []))}

    async def get_friend_msg_history(self, user_id, message_seq: int = 0, count: int = 40):
        if self.empty_payload:
            return {}
        if message_seq:
            return {"messages": []}
        return {"messages": list(self.history.get(f"private:{user_id}", []))}


class _Dispatcher:
    def __init__(self) -> None:
        self.injected: list[dict] = []

    async def handle_message(self, message: dict) -> None:
        self.injected.append(dict(message))


def _plugin(tmp_path: Path, *, settings: dict | None = None):
    client = _Client()
    dispatcher = _Dispatcher()
    plugin = SimpleNamespace(
        data_path=lambda: str(tmp_path),
        logger=_Logger(),
        _qq_settings=dict(settings or {}),
        _running=True,
        qq_client=client,
        message_dispatcher=dispatcher,
        _emit_log=lambda *a, **k: None,
        _admin_qq="",
    )
    return plugin, client, dispatcher


def _msg(seq: int, *, mid: str = "", sender: str = "123", ts: int | None = None) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": "500",
        "user_id": sender,
        "message_id": mid or f"m{seq}",
        "message_seq": seq,
        "time": int(ts if ts is not None else time.time()),
        "message": [{"type": "text", "data": {"text": f"第{seq}条"}}],
    }


# ── 游标 ────────────────────────────────────────────────────


def test_note_message_advances_the_cursor(tmp_path: Path) -> None:
    plugin, _client, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)

    svc.note_message(_msg(5, mid="a"))
    svc.note_message(_msg(7, mid="b"))

    assert svc._cursors["group:500"]["seq"] == 7
    assert "a" in svc._recent_ids and "b" in svc._recent_ids


def test_a_smaller_seq_never_moves_the_cursor_backwards(tmp_path: Path) -> None:
    """乱序到达不该把位置往回拉——拉回去下次就会重补一遍。"""
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)

    svc.note_message(_msg(9))
    svc.note_message(_msg(4))

    assert svc._cursors["group:500"]["seq"] == 9


def test_missing_seq_marks_the_cursor_untrusted_instead_of_faking_one(tmp_path: Path) -> None:
    """拿不到 message_seq 时绝不能拿时间戳凑。

    实测踩过：凑出来的数（time*1000，一万亿量级）比所有历史消息的 seq 都大，
    之后补消息永久失效——判据变成"没有一条比它新"。
    """
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)

    raw = _msg(0, mid="x")
    raw.pop("message_seq")
    svc.note_message(raw)

    rec = svc._cursors["group:500"]
    assert rec["seq"] == 0, "不许编一个位置出来"
    assert rec["seq_unknown"] is True
    assert svc._diag["seq_missing"] == 1


def test_notice_messages_do_not_touch_the_cursor(tmp_path: Path) -> None:
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)

    svc.note_message({"post_type": "notice", "notice_type": "poke", "group_id": "500", "user_id": "123"})

    assert svc._cursors == {}


# ── 首次会话只对齐 ──────────────────────────────────────────


@_sync
async def test_a_conversation_seen_for_the_first_time_only_aligns(tmp_path: Path) -> None:
    """新加的好友 / 首次启动：只记位置，不把历史倒进管道。

    不这么做的话，第一次跑起来她会对着几小时前的话一口气回十二条，像抽风。
    """
    plugin, client, dispatcher = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    client.history["group:500"] = [_msg(100), _msg(101), _msg(102)]
    # 现场事件先来过一条，但没带 seq → 位置不可信
    raw = _msg(0, mid="first")
    raw.pop("message_seq")
    svc.note_message(raw)

    # 不用 force：force=True 是给手动补救留的口子，本来就会跳过这道闸
    result = await svc.catch_up()

    assert dispatcher.injected == []
    assert result["injected"] == 0
    detail = result["details"][0]
    assert detail["status"] == "aligned"
    assert svc._cursors["group:500"]["seq"] == 102
    assert not svc._cursors["group:500"].get("seq_unknown"), "对齐完要把脏标记清掉"


@_sync
async def test_first_sight_of_a_brand_new_group_does_not_replay_history(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    client.history["group:500"] = [_msg(1), _msg(2)]
    svc.note_message(_msg(3))  # 有 seq，但 updated 刚被写入
    # 模拟"从没补过"：updated 为 0 且游标为 0
    svc._cursors["group:500"]["seq"] = 0
    svc._cursors["group:500"]["updated"] = 0

    result = await svc.catch_up()

    assert dispatcher.injected == []
    assert result["details"][0]["status"] == "aligned"


# ── 真补消息 ────────────────────────────────────────────────


@_sync
async def test_messages_newer_than_the_cursor_get_injected(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [_msg(10, mid="seen"), _msg(11, mid="new1"), _msg(12, mid="new2")]

    result = await svc.catch_up(force=True)

    assert [m["message_id"] for m in dispatcher.injected] == ["new1", "new2"]
    assert result["injected"] == 2
    assert all(m["_catchup"] is True for m in dispatcher.injected)
    assert svc._cursors["group:500"]["seq"] == 12


@_sync
async def test_an_already_handled_message_id_is_skipped_even_if_the_cursor_is_behind(tmp_path: Path) -> None:
    """游标慢一拍时靠名单兜住。

    实测事故：插件重载时游标还没落盘（写完有 1 秒防抖），重连后上一轮刚回过的
    消息被当成漏消息又补了一遍，她对着同一条链接回了两次。
    """
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    svc._recent_ids["dup"] = time.time()
    client.history["group:500"] = [_msg(11, mid="dup"), _msg(12, mid="ok")]

    result = await svc.catch_up(force=True)

    assert [m["message_id"] for m in dispatcher.injected] == ["ok"]
    assert result["details"][0]["skipped_dup"] == 1


@_sync
async def test_her_own_messages_are_never_replayed(tmp_path: Path) -> None:
    """自己发的不补：那不是"漏掉的消息"，补进去等于让她自问自答。"""
    plugin, client, dispatcher = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [
        _msg(11, mid="mine", sender=client.self_id),
        _msg(12, mid="theirs"),
    ]

    result = await svc.catch_up(force=True)

    assert [m["message_id"] for m in dispatcher.injected] == ["theirs"]
    assert result["details"][0]["skipped_self"] == 1


@_sync
async def test_messages_older_than_the_window_are_skipped(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(
        tmp_path, settings={"catchup_stagger_ms": 0, "catchup_window_hours": 12},
    )
    svc = QQMessageCatchupService(plugin)
    old_ts = int(time.time()) - 20 * 3600
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [_msg(11, mid="old", ts=old_ts)]

    result = await svc.catch_up(force=True)

    assert dispatcher.injected == []
    assert result["details"][0]["skipped_old"] == 1


@_sync
async def test_per_conversation_cap_is_respected(tmp_path: Path) -> None:
    plugin, client, dispatcher = _plugin(
        tmp_path, settings={"catchup_stagger_ms": 0, "catchup_max_per_conversation": 2},
    )
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [_msg(n, mid=f"n{n}") for n in range(11, 16)]

    result = await svc.catch_up(force=True)

    assert len(dispatcher.injected) == 2
    assert result["details"][0]["skipped_capped"] == 3


# ── 失败分类 ────────────────────────────────────────────────


@_sync
async def test_empty_dict_is_a_failure_not_an_empty_history(tmp_path: Path) -> None:
    """``{}`` 不能算"真没消息"。

    它可能是超时被 `or {}` 吞掉，也可能是开放平台的同名空实现。当成"没消息"
    的话，用户会看到"补完了"，而实际一条都没补——静默失效比报错糟得多。
    """
    plugin, client, dispatcher = _plugin(tmp_path)
    client.empty_payload = True
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))

    result = await svc.catch_up(force=True)

    assert result["details"][0]["status"] == "failed"
    assert svc._diag["conv_failed"] == 1


# ── 重连检测 ────────────────────────────────────────────────


@_sync
async def test_the_first_sample_never_triggers_a_catch_up(tmp_path: Path) -> None:
    """插件刚起来"连上了"不算重连——那时候没有"漏的消息"这个概念。"""
    plugin, client, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    calls: list[str] = []

    async def _fake_catch_up(*, reason: str = "", force: bool = False):
        calls.append(reason)
        return {"status": "ok"}

    svc.catch_up = _fake_catch_up  # type: ignore[assignment]
    client.connected = True

    await svc._watch_tick()

    assert calls == []
    assert svc._was_connected is True


@_sync
async def test_reconnect_after_a_drop_triggers_a_catch_up(tmp_path: Path) -> None:
    plugin, client, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    calls: list[str] = []

    async def _fake_catch_up(*, reason: str = "", force: bool = False):
        calls.append(reason)
        return {"status": "ok"}

    svc.catch_up = _fake_catch_up  # type: ignore[assignment]

    client.connected = True
    await svc._watch_tick()          # 首次采样：只记录
    client.connected = False
    await svc._watch_tick()          # 掉线
    client.connected = True
    await svc._watch_tick()          # 重连 → 补

    assert calls == ["reconnect"]


@_sync
async def test_staying_connected_does_not_keep_triggering(tmp_path: Path) -> None:
    plugin, client, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    calls: list[str] = []

    async def _fake_catch_up(*, reason: str = "", force: bool = False):
        calls.append(reason)
        return {"status": "ok"}

    svc.catch_up = _fake_catch_up  # type: ignore[assignment]

    for _ in range(4):
        client.connected = True
        await svc._watch_tick()

    assert calls == []


@_sync
async def test_disabled_switch_stops_everything(tmp_path: Path) -> None:
    plugin, client, _d = _plugin(tmp_path, settings={"catchup_enabled": False})
    svc = QQMessageCatchupService(plugin)
    calls: list[str] = []

    async def _fake_catch_up(*, reason: str = "", force: bool = False):
        calls.append(reason)
        return {"status": "ok"}

    svc.catch_up = _fake_catch_up  # type: ignore[assignment]

    client.connected = True
    await svc._watch_tick()
    client.connected = False
    await svc._watch_tick()
    client.connected = True
    await svc._watch_tick()

    assert calls == []


# ── 限流 ────────────────────────────────────────────────────


@_sync
async def test_two_catch_ups_within_the_cooldown_are_throttled(tmp_path: Path) -> None:
    """连接抖动时可能一秒触发好几次，没必要每次都打接口。"""
    plugin, client, _d = _plugin(tmp_path, settings={"catchup_stagger_ms": 0})
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(10, mid="seen"))
    client.history["group:500"] = [_msg(11, mid="n")]

    first = await svc.catch_up(force=True)
    second = await svc.catch_up()

    assert first["status"] == "ok"
    assert second["status"] == "throttled"


# ── 持久化 ──────────────────────────────────────────────────


@_sync
async def test_cursor_survives_a_restart(tmp_path: Path) -> None:
    """游标要落盘：插件重载后不该从零开始重新对齐。"""
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(42, mid="keep"))
    await svc._flush(force=True)

    fresh = QQMessageCatchupService(plugin)
    await fresh._load()

    assert fresh._cursors["group:500"]["seq"] == 42
    assert "keep" in fresh._recent_ids


def test_state_snapshot_exposes_diag(tmp_path: Path) -> None:
    plugin, _c, _d = _plugin(tmp_path)
    svc = QQMessageCatchupService(plugin)
    svc.note_message(_msg(1))

    state = svc.describe_state()

    assert state["enabled"] is True
    assert state["conversations"] == 1
    assert "conv_failed" in state["diag"]
