"""表情包收集的三档闸。

为什么这个测试比别的都重要：收集是**唯一一个"她能自己往库里塞东西"的入口**，
而库里的图会被她主动发到群里。闸开错方向的代价不是"功能不好用"，是她在客户群
里发出一张别人家的自拍、一张工作截图。所以每一档的判定都要钉死。

顺带钉住两条容易漏的：
1. **私聊里 group_id 是空串** —— 空串在 ``recent()`` 里表示"不限群"，于是她能拿到
   群里刚发的图。闸必须在 ``check()`` 那一层再挡一次，不能只靠查询过滤。
2. **收藏表情是 ``image`` + ``subType=1``** —— LLBot 把 QQ 的 customFace 降级成
   普通图片段，这个字段是唯一还留着的标记；只认 ``market_face`` 的话这台账号
   一个表情都收不到。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from plugin.plugins.qq_auto_reply.sticker_lab_service import (
    POLICY_ANY,
    POLICY_CURRENT,
    POLICY_OFF,
    QQStickerLab,
    normalize_segment,
)


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class _Plugin:
    """只实现收集侧用到的那几个接口。"""

    def __init__(self, tmp_path: Path, settings: dict | None = None) -> None:
        self._tmp = tmp_path
        self._qq_settings = dict(settings or {})
        self.logger = _Logger()

    def data_path(self, name: str = "") -> str:
        return str(self._tmp / name) if name else str(self._tmp)


def _lab(tmp_path: Path, policy: str | None = POLICY_CURRENT) -> QQStickerLab:
    settings = {} if policy is None else {"sticker_collect_policy": policy}
    return QQStickerLab(_Plugin(tmp_path, settings))


def _face_message(*, group_id: str = "111", sender_id: str = "u1") -> dict:
    """一条带收藏表情的消息（LLBot 把它报成 image + subType=1）。"""
    return {
        "group_id": group_id,
        "user_id": sender_id,
        "message": [{
            "type": "image",
            "data": {
                "subType": 1,
                "url": "https://gchat.qpic.cn/gchatpic_new/0/1-2-ABCDEF0123456789ABCDEF0123456789/0",
                "file": "ABCDEF0123456789ABCDEF0123456789.jpg",
            },
        }],
    }


# ── 策略读取 ────────────────────────────────────────────────


def test_policy_defaults_to_current_when_unset(tmp_path: Path) -> None:
    assert _lab(tmp_path, policy=None).policy() == POLICY_CURRENT


def test_policy_unknown_value_falls_back_to_current(tmp_path: Path) -> None:
    """手改过 config 的乱值不能变成"闸全开"。

    ``settings_service._apply_plain_settings`` 对 str 只去空白、不校验 enum，
    所以这一层兜底不是防御性编程，是唯一的一道校验。
    """
    assert _lab(tmp_path, policy="ANYTHING").policy() == POLICY_CURRENT
    assert _lab(tmp_path, policy="").policy() == POLICY_CURRENT


@pytest.mark.parametrize("policy", [POLICY_OFF, POLICY_CURRENT, POLICY_ANY])
def test_policy_reads_each_valid_value(tmp_path: Path, policy: str) -> None:
    assert _lab(tmp_path, policy=policy).policy() == policy


# ── 闸门判定 ────────────────────────────────────────────────


def test_off_refuses_even_same_group(tmp_path: Path) -> None:
    lab = _lab(tmp_path, POLICY_OFF)
    assert lab.check(item_group="111", current_group="111") != ""


def test_current_allows_same_group(tmp_path: Path) -> None:
    lab = _lab(tmp_path, POLICY_CURRENT)
    assert lab.check(item_group="111", current_group="111") == ""


def test_current_refuses_other_group(tmp_path: Path) -> None:
    lab = _lab(tmp_path, POLICY_CURRENT)
    assert lab.check(item_group="111", current_group="222") != ""
    assert lab.diag["policy_block"] == 1


def test_current_refuses_group_item_inside_private_session(tmp_path: Path) -> None:
    """私聊里 group_id 是空串，不能当成"哪个群都行"。"""
    lab = _lab(tmp_path, POLICY_CURRENT)
    assert lab.check(item_group="111", current_group="") != ""


def test_current_allows_private_item_in_private_session(tmp_path: Path) -> None:
    lab = _lab(tmp_path, POLICY_CURRENT)
    assert lab.check(item_group="", current_group="", item_sender="u1", current_sender="u1") == ""


@pytest.mark.parametrize("sender", ["u2", ""])
def test_current_refuses_other_or_unknown_private_peer(tmp_path, sender):
    lab = _lab(tmp_path, POLICY_CURRENT)
    assert lab.check(item_group="", current_group="", item_sender="u1", current_sender=sender)


@pytest.mark.parametrize("policy,expected", [(POLICY_CURRENT, 0), (POLICY_ANY, 1)])
def test_private_save_never_falls_back_to_another_peer_by_default(tmp_path, monkeypatch, policy, expected):
    from unittest.mock import AsyncMock

    lab = _lab(tmp_path, policy)
    lab.observe(_face_message(group_id="", sender_id="u2"))
    save = AsyncMock(return_value={"desc": "private picture"})
    monkeypatch.setattr(lab, "save", save)
    asyncio.run(lab.execute_save({}, sender_id="u1", group_id=""))
    assert save.await_count == expected


def test_private_save_skips_newer_group_image_from_same_sender(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    lab = _lab(tmp_path, POLICY_CURRENT)
    lab.observe(_face_message(group_id="", sender_id="u1"))
    lab.observe(_face_message(group_id="111", sender_id="u1"))
    save = AsyncMock(return_value={"desc": "private picture"})
    monkeypatch.setattr(lab, "save", save)
    asyncio.run(lab.execute_save({}, sender_id="u1", group_id=""))
    save.assert_awaited_once()
    assert save.call_args.kwargs["group_id"] == ""


def test_any_allows_other_group(tmp_path: Path) -> None:
    lab = _lab(tmp_path, POLICY_ANY)
    assert lab.check(item_group="111", current_group="222") == ""
    assert lab.diag["policy_block"] == 0


# ── 段归一 ──────────────────────────────────────────────────


def test_collected_face_is_recognized_by_sub_type(tmp_path: Path) -> None:
    norm = normalize_segment(_face_message()["message"][0])
    assert norm is not None
    assert norm["mode"] == "local"
    # 文件名就是 md5，用它当去重键——URL 里的 rkey 几小时就换一茬。
    assert norm["md5"] == "abcdef0123456789abcdef0123456789"


def test_normal_photo_is_not_a_sticker() -> None:
    segment = {"type": "image", "data": {"url": "https://x/y.jpg", "file": "y.jpg"}}
    assert normalize_segment(segment) is None


def test_text_segment_is_not_a_sticker() -> None:
    assert normalize_segment({"type": "text", "data": {"text": "hi"}}) is None


def test_market_face_is_recognized() -> None:
    segment = {
        "type": "market_face",
        "data": {"emoji_id": "abc123", "summary": "[猫猫点赞]", "url": "https://x/z.gif"},
    }
    norm = normalize_segment(segment)
    assert norm is not None
    assert norm["mode"] == "mface"
    assert norm["emoji_id"] == "abc123"


# ── 收流记录 ────────────────────────────────────────────────


def test_observe_records_sticker_with_source(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    lab.observe(_face_message(group_id="111", sender_id="u9"))
    assert lab.diag["seen"] == 1
    items = lab.recent(limit=1)
    assert items[0]["group_id"] == "111"
    assert items[0]["sender_id"] == "u9"


def test_observe_ignores_normal_image(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    lab.observe({
        "group_id": "111",
        "user_id": "u1",
        "message": [{"type": "image", "data": {"url": "https://x/a.jpg", "file": "a.jpg"}}],
    })
    assert lab.diag["seen"] == 0
    assert lab.recent() == []


def test_recent_filters_by_group(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    lab.observe(_face_message(group_id="111"))
    lab.observe(_face_message(group_id="222"))
    assert len(lab.recent(group_id="111")) == 1
    assert lab.recent(group_id="111")[0]["group_id"] == "111"
    # 不传 group_id 表示不限，两张都在
    assert len(lab.recent(limit=5)) == 2


def test_recent_returns_newest_first(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    lab.observe(_face_message(sender_id="first"))
    lab.observe(_face_message(sender_id="second"))
    assert lab.recent(limit=1)[0]["sender_id"] == "second"


# ── 工具入口 ────────────────────────────────────────────────


def test_execute_save_off_refuses_without_touching_network(tmp_path: Path) -> None:
    """off 档下必须在**发请求之前**回绝。

    图已经收到也已经在缓冲里了，但"不收"是使用者的决定——先回绝再谈其他。
    """
    lab = _lab(tmp_path, POLICY_OFF)
    lab.observe(_face_message())
    out = asyncio.run(lab.execute_save({}, sender_id="u1", group_id="111"))
    assert "没在收集" in out
    assert lab.diag["policy_block"] == 1
    assert lab.diag["download_fail"] == 0


def test_execute_save_in_other_group_reports_nothing_to_save(tmp_path: Path) -> None:
    """当前会话里没有可收的图时，不能顺手拿去别的群那张。"""
    lab = _lab(tmp_path, POLICY_CURRENT)
    lab.observe(_face_message(group_id="111"))
    out = asyncio.run(lab.execute_save({}, sender_id="u1", group_id="222"))
    assert "没收到图" in out
    assert lab.diag["save_ok"] == 0


def test_stats_exposes_policy_and_counters(tmp_path: Path) -> None:
    lab = _lab(tmp_path, POLICY_ANY)
    stats = lab.stats()
    assert stats["policy"] == POLICY_ANY
    assert stats["recent"] == 0
    assert "policy_block" in stats["diag"]


# ── 这一轮挂不挂工具 ────────────────────────────────────────
#
# ``should_offer_save`` 不是"顺便判一下"：它决定这一轮往 prompt 里塞不塞
# 工具说明、给不给额外超时预算。判错方向的两个后果都不轻 —— 常挂等于每句话
# 都在提醒她"你有工具"（模型会开始凭空调度），永远不挂等于功能根本不存在。


def test_should_offer_is_false_before_any_image(tmp_path: Path) -> None:
    assert _lab(tmp_path, POLICY_CURRENT).should_offer_save() is False


def test_should_offer_is_true_right_after_an_image(tmp_path: Path) -> None:
    lab = _lab(tmp_path, POLICY_CURRENT)
    lab.observe(_face_message())
    assert lab.should_offer_save() is True


def test_should_offer_is_false_when_policy_off(tmp_path: Path) -> None:
    """闸关着就别挂工具：收进来也不许用，挂上只是让她白费一次调用。"""
    lab = _lab(tmp_path, POLICY_OFF)
    lab.observe(_face_message())
    assert lab.should_offer_save() is False


def test_should_offer_expires_with_the_url(tmp_path: Path) -> None:
    """图 URL 带 rkey，过一阵就是死链，不能再为它预留工具和超时预算。"""
    lab = _lab(tmp_path, POLICY_CURRENT)
    lab.observe(_face_message())
    lab._recent[-1]["at"] -= 3600
    assert lab.has_fresh() is False
    assert lab.should_offer_save() is False


# ── 入库 ────────────────────────────────────────────────────


def _patch_io(lab: QQStickerLab, monkeypatch: pytest.MonkeyPatch, *, payload: bytes) -> None:
    """把"下载"和"看图"换成固定结果，别在单测里打网络。"""

    async def _fake_download(locator: str) -> bytes:
        return payload

    async def _fake_describe(path: Path) -> str:
        return "无语的猫"

    monkeypatch.setattr(lab, "_download", _fake_download)
    monkeypatch.setattr(lab, "describe", _fake_describe)


def test_save_writes_file_and_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = _lab(tmp_path, POLICY_CURRENT)
    _patch_io(lab, monkeypatch, payload=b"\xff\xd8\xff" + b"0" * 64)

    entry = asyncio.run(lab.save("https://x/a.jpg", note="无语", origin="u1", group_id="111"))
    assert entry is not None
    assert entry["desc"].startswith("无语")
    assert entry["group"] == "111"
    assert (tmp_path / "sticker" / entry["path"]).exists()
    assert (tmp_path / "sticker.json").exists()


def test_save_dedupes_same_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一张图被转发了十次，库里只能有一份。"""
    lab = _lab(tmp_path, POLICY_CURRENT)
    _patch_io(lab, monkeypatch, payload=b"\x89PNG\r\n\x1a\n" + b"7" * 64)

    first = asyncio.run(lab.save("https://x/a.jpg"))
    second = asyncio.run(lab.save("https://x/b.jpg"))
    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert lab.diag["dup"] == 1


def test_save_drops_entry_when_nothing_to_describe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没描述又没备注的条目不该进库：目录里会多出一行空白，还占一个 ID。"""
    lab = _lab(tmp_path, POLICY_CURRENT)
    _patch_io(lab, monkeypatch, payload=b"\xff\xd8\xff" + b"1" * 64)

    async def _empty_describe(path: Path) -> str:
        return ""

    monkeypatch.setattr(lab, "describe", _empty_describe)
    assert asyncio.run(lab.save("https://x/a.jpg")) is None
    assert not (tmp_path / "sticker.json").exists()


def test_save_reports_download_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载失败要留计数——不记的话"收不进来"和"本来就没有"长得一样。"""
    lab = _lab(tmp_path, POLICY_CURRENT)

    async def _boom(locator: str) -> bytes:
        raise OSError("boom")

    monkeypatch.setattr(lab, "_download", _boom)
    assert asyncio.run(lab.save("https://x/a.jpg")) is None
    assert lab.diag["download_fail"] == 1


def test_saved_scope_isolated_with_independent_files(tmp_path, monkeypatch):
    from plugin.plugins.qq_auto_reply.sticker_scope import sticker_allowed

    lab = _lab(tmp_path, POLICY_CURRENT)
    _patch_io(lab, monkeypatch, payload=b"\xff\xd8\xff" + b"0" * 64)
    a = asyncio.run(lab.save("https://x/a.jpg", origin="u1", group_id="111"))
    b = asyncio.run(lab.save("https://x/a.jpg", origin="u1", group_id="222"))
    private = asyncio.run(lab.save("https://x/a.jpg", origin="u1"))
    assert len({entry["id"] for entry in (a, b, private)}) == 3
    assert len({entry["path"] for entry in (a, b, private)}) == 3
    assert sticker_allowed(a, target_type="group", target_id="111")
    assert not sticker_allowed(a, target_type="group", target_id="222")
    assert sticker_allowed(private, target_type="private", target_id="u1")
    assert not sticker_allowed(private, target_type="private", target_id="u2")
    (tmp_path / "sticker" / a["path"]).unlink()
    assert (tmp_path / "sticker" / b["path"]).is_file()
    lab.plugin._qq_settings["sticker_collect_policy"] = POLICY_ANY
    global_entry = asyncio.run(lab.save("https://x/a.jpg", origin="u1"))
    assert global_entry["scope"] == "global"
    assert global_entry["id"] != private["id"]
    assert not sticker_allowed(private, target_type="private", target_id="u2")
    assert sticker_allowed(global_entry, target_type="private", target_id="u2")
