"""图库与换头像。

这块最值得钉的不是"能不能存图"，而是**换头像到底有没有换成**。

`set_qq_avatar` 回 ok 只代表 Highway 上传成功，QQ 那边有没有真的换上它不保证
（实测：同样的调用、同样的文件，有一段时间连续两次没落地，过一会儿又连续三次
都成功，中间没改过任何配置）。所以这里每次设完都反查一遍，反查有四种结果，
四种对模型说的话完全不同：

    已落地    → "换上了，已经确认"
    没落地    → "但重新拉头像看还是原来那张"（不能让她去邀功）
    查不了    → "回执成功，没法确认"（不能当成失败）
    调用挂了  → "没换成"
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from types import SimpleNamespace

from plugin.plugins.qq_auto_reply.gallery_service import (
    AVATAR_TOOL_NAME,
    BROWSE_TOOL_NAME,
    QQGalleryService,
)


def _sync(fn):
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


class _Client:
    def __init__(self, *, self_id: str = "12345") -> None:
        self.self_id = self_id
        self.calls: list[str] = []
        self.raise_on_avatar: Exception | None = None

    async def set_qq_avatar(self, file: str):
        if self.raise_on_avatar is not None:
            raise self.raise_on_avatar
        self.calls.append(file)
        return {}


def _plugin(tmp_path: Path, *, client: _Client | None = None):
    client = client or _Client()
    plugin = SimpleNamespace(
        data_path=lambda name="": str(tmp_path / name),
        logger=_Logger(),
        qq_client=client,
        _admin_qq="12345",
        _vlm_describe_locator=None,
    )
    return plugin, client


def _png(size=(32, 32), color=(10, 20, 30)) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _lab(tmp_path: Path, *, client: _Client | None = None, desc_map=None):
    plugin, client = _plugin(tmp_path, client=client)
    svc = QQGalleryService(plugin)
    blobs = {str(k): v for k, v in (desc_map or {}).items()}

    async def _fake_fetch(locator, *, timeout=15.0):
        return blobs.get(str(locator), b"")

    svc._fetch_bytes = _fake_fetch  # type: ignore[assignment]
    return svc, client, blobs


# ── 入库 ────────────────────────────────────────────────────


@_sync
async def test_remember_stores_and_returns_an_id(tmp_path: Path) -> None:
    svc, _c, blobs = _lab(tmp_path)
    blobs["http://x/a.png"] = _png()

    item_id = await svc.remember("http://x/a.png", desc="猫耳少年")

    assert item_id
    items = svc.list_items()
    assert len(items) == 1
    assert items[0]["desc"] == "猫耳少年"
    assert svc.stats()["total"] == 1


@_sync
async def test_the_same_picture_is_stored_once(tmp_path: Path) -> None:
    """按内容 md5 去重：同一张图从两个 URL 收到，只该占一份。"""
    svc, _c, blobs = _lab(tmp_path)
    blobs["http://x/a.png"] = _png()
    blobs["http://x/a-copy.png"] = _png()

    first = await svc.remember("http://x/a.png", desc="猫")
    second = await svc.remember("http://x/a-copy.png", desc="猫")

    assert first == second
    assert len(svc.list_items()) == 1
    assert svc.stats()["total"] == 1


@_sync
async def test_a_failed_download_does_not_create_an_entry(tmp_path: Path) -> None:
    """取不到字节就当没收到：库里不该出现"有记录没文件"的空条目。"""
    svc, _c, _b = _lab(tmp_path)

    item_id = await svc.remember("http://x/missing.png")

    assert item_id is None
    assert svc.list_items() == []
    assert svc.stats()["diag"]["fetch_fail"] >= 1


@_sync
async def test_search_matches_the_description(tmp_path: Path) -> None:
    svc, _c, blobs = _lab(tmp_path)
    blobs["http://x/a.png"] = _png(color=(1, 2, 3))
    blobs["http://x/b.png"] = _png(color=(200, 100, 50))
    await svc.remember("http://x/a.png", desc="猫耳少年一脸无语")
    await svc.remember("http://x/b.png", desc="卡通小狗点赞")

    hits = svc.list_items(query="猫耳")

    assert len(hits) == 1
    assert "猫耳" in hits[0]["desc"]


# ── 换头像 ──────────────────────────────────────────────────


@_sync
async def test_avatar_lands_and_reports_verified(tmp_path: Path) -> None:
    svc, client, blobs = _lab(tmp_path)
    blobs["http://x/a.png"] = _png()
    item_id = await svc.remember("http://x/a.png", desc="猫")

    async def _landed(_client, _path):
        return True

    svc._avatar_landed = _landed  # type: ignore[assignment]

    result = await svc.set_avatar(item_id)

    assert result["ok"] is True
    assert result["verified"] is True
    assert client.calls, "没有真的调 set_qq_avatar"
    # 上传的是本地方形 jpg，不是原始 URL
    assert client.calls[0].startswith("file:///")


@_sync
async def test_avatar_reports_not_landed_and_retries(tmp_path: Path) -> None:
    """回执 ok 但反查没变：重试，最后如实告诉模型"没落地"。"""
    svc, client, blobs = _lab(tmp_path)
    blobs["http://x/a.png"] = _png()
    item_id = await svc.remember("http://x/a.png", desc="猫")

    async def _never_lands(_client, _path):
        return False

    svc._avatar_landed = _never_lands  # type: ignore[assignment]

    result = await svc.set_avatar(item_id)

    assert result["attempts"] == svc.AVATAR_ATTEMPTS
    assert result["verified"] is False
    assert len(client.calls) == svc.AVATAR_ATTEMPTS


@_sync
async def test_unverifiable_avatar_is_not_a_failure(tmp_path: Path) -> None:
    """查不了（没有 uin / 拉不到 CDN）不等于失败，不能报成没换成。"""
    svc, _client, blobs = _lab(tmp_path)
    blobs["http://x/a.png"] = _png()
    item_id = await svc.remember("http://x/a.png", desc="猫")

    async def _cannot_tell(_client, _path):
        return None

    svc._avatar_landed = _cannot_tell  # type: ignore[assignment]

    result = await svc.set_avatar(item_id)

    assert result["ok"] is True
    assert result["verified"] is None
    assert result["attempts"] == 1, "查不了不该触发重试"


@_sync
async def test_avatar_call_failure_reports_not_ok(tmp_path: Path) -> None:
    svc, client, blobs = _lab(tmp_path)
    blobs["http://x/a.png"] = _png()
    item_id = await svc.remember("http://x/a.png", desc="猫")
    client.raise_on_avatar = RuntimeError("No OneBot client connected")

    result = await svc.set_avatar(item_id)

    assert result["ok"] is False
    assert svc.stats()["diag"]["avatar_err"] >= 1


@_sync
async def test_unknown_key_is_refused_before_any_upload(tmp_path: Path) -> None:
    svc, client, blobs = _lab(tmp_path)
    blobs["http://x/a.png"] = _png()
    await svc.remember("http://x/a.png", desc="猫")

    result = await svc.set_avatar("g999")

    assert result["ok"] is False
    assert client.calls == []


@_sync
async def test_avatar_lookup_by_description(tmp_path: Path) -> None:
    """模型可能直接填描述关键词（她不知道编号），要能认出来。"""
    svc, _client, blobs = _lab(tmp_path)
    blobs["http://x/a.png"] = _png()
    item_id = await svc.remember("http://x/a.png", desc="猫耳少年一脸无语")

    async def _landed(_client, _path):
        return True

    svc._avatar_landed = _landed  # type: ignore[assignment]

    result = await svc.set_avatar("猫耳")

    assert result["ok"] is True
    assert result["id"] == item_id


# ── 头像预处理 ──────────────────────────────────────────────


def test_avatar_file_is_cropped_square(tmp_path: Path) -> None:
    """QQ 不做裁切，非方图上去会压扁，所以上传前先裁成正方。"""
    from io import BytesIO

    from PIL import Image

    svc, _c, blobs = _lab(tmp_path)
    buf = BytesIO()
    Image.new("RGB", (400, 200), (30, 60, 90)).save(buf, format="PNG")
    blobs["http://x/wide.png"] = buf.getvalue()

    async def _go():
        item_id = await svc.remember("http://x/wide.png", desc="宽图")
        item = svc.resolve(item_id)[1]
        return svc.prepare_avatar_file(item)

    path = asyncio.run(_go())

    assert path is not None and Path(path).is_file()
    with Image.open(path) as image:
        assert image.width == image.height


# ── 工具定义 ────────────────────────────────────────────────


def test_both_tool_definitions_are_built(tmp_path: Path) -> None:
    svc, _c, _b = _lab(tmp_path)

    browse = svc.build_browse_definition()
    avatar = svc.build_avatar_definition()

    assert browse.name == BROWSE_TOOL_NAME == "browse_gallery"
    assert avatar.name == AVATAR_TOOL_NAME == "set_avatar"
    # 参数名必须是 id：模型自己编成 index 的那次就是没换成的直接原因
    assert "id" in avatar.parameters["properties"]
    assert "index" not in avatar.parameters["properties"]


@_sync
async def test_browse_loads_persisted_index_before_listing(tmp_path):
    from plugin.plugins.qq_auto_reply.reply_generation_service import QQReplyGenerationService

    svc, _client, blobs = _lab(tmp_path)
    svc.SAVE_DEBOUNCE_SECONDS = 0
    blobs["http://x/cat.png"] = _png()
    await svc.remember("http://x/cat.png", desc="saved cat")
    await svc.flush()
    fresh = QQGalleryService(svc.plugin)
    handler = QQReplyGenerationService(svc.plugin)._build_gallery_handlers(fresh, SimpleNamespace(sender_id="12345"))[BROWSE_TOOL_NAME]
    result = await handler(SimpleNamespace(arguments={}, call_id="browse", name=BROWSE_TOOL_NAME))
    assert "saved cat" in result.output
    assert "g1" in result.output


@_sync
async def test_inbound_images_reuse_descriptions_and_deduplicate_attachments(tmp_path):
    from unittest.mock import AsyncMock

    svc, _client, blobs = _lab(tmp_path)
    svc.SAVE_DEBOUNCE_SECONDS = 0
    blobs["http://x/cat.png"] = _png()
    svc.plugin._vlm_describe_locator = AsyncMock()
    message = {
        "user_id": "123", "user_nickname": "Alice",
        "_image_descriptions": {"http://x/cat.png": "a cat"},
        "attachments": [{"type": "image", "url": "http://x/cat.png"}],
    }
    ids = await svc.remember_message(message, segments=[
        {"type": "image", "data": {"url": "http://x/cat.png", "subType": 1}},
    ])
    assert ids == ["g1"]
    assert svc.resolve("g1")[1]["desc"] == "a cat"
    assert svc.resolve("g1")[1]["kind"] == "sticker"
    svc.plugin._vlm_describe_locator.assert_not_awaited()
    await svc.flush()


@_sync
async def test_inbox_is_imported_on_first_browse(tmp_path):
    from plugin.plugins.qq_auto_reply.reply_generation_service import QQReplyGenerationService

    svc, _client, _blobs = _lab(tmp_path)
    svc.SAVE_DEBOUNCE_SECONDS = 0
    image = svc.inbox_dir() / "cat.png"
    image.write_bytes(_png())
    handler = QQReplyGenerationService(svc.plugin)._build_gallery_handlers(svc, SimpleNamespace(sender_id="12345"))[BROWSE_TOOL_NAME]
    result = await handler(SimpleNamespace(arguments={}, call_id="browse", name=BROWSE_TOOL_NAME))
    assert "g1" in result.output
    assert not image.exists()
    await svc.flush()
    fresh = QQGalleryService(svc.plugin)
    await fresh.start()
    assert fresh.resolve("g1") is not None
