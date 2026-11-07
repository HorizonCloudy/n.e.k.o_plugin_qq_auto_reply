"""Gallery is a view of the managed sticker inventory, not a second store."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin
from plugin.plugins.qq_auto_reply.gallery_service import QQGalleryService
from plugin.plugins.qq_auto_reply.sticker_lab_service import QQStickerLab
from plugin.plugins.qq_auto_reply.sticker_scope import sticker_allowed
from plugin.plugins.qq_auto_reply.sticker_store import StickerStore
from test_qq_gallery import _plugin, _png


def services(tmp_path):
    plugin, _ = _plugin(tmp_path)
    plugin.session_instruction_service = SimpleNamespace(_sticker_catalog_cache="cached")
    return plugin, QQGalleryService(plugin), StickerStore(plugin)


@pytest.mark.asyncio
async def test_collected_image_can_be_listed_previewed_deleted_and_stays_deleted(tmp_path):
    plugin, gallery, store = services(tmp_path)
    gid = await gallery.remember_bytes(_png(), desc="private photo", sender_id="alice")
    sid = gid[1:]
    listing = await QQAutoReplyPlugin._asset_list_stickers(plugin, {})
    assert listing.value["stickers"][0]["id"] == sid
    preview = await QQAutoReplyPlugin._asset_preview_sticker(plugin, {"id": sid})
    assert base64.b64decode(preview.value["data_url"].split(",", 1)[1]) == _png()
    image = gallery.path_of(gallery.resolve(gid)[1])
    assert image.parent == tmp_path / "sticker"
    assert not (tmp_path / "gallery" / "index.json").exists()
    result = await QQAutoReplyPlugin._asset_delete_sticker(plugin, {"id": sid})
    assert result.is_ok and not image.exists()
    await gallery.flush()
    assert gallery.resolve(gid) is None
    fresh = QQGalleryService(plugin)
    await fresh.start()
    assert not fresh.list_items() and not store.read()


@pytest.mark.asyncio
async def test_manual_upload_is_visible_in_gallery_without_reload(tmp_path):
    plugin, gallery, _ = services(tmp_path)
    await gallery.start()
    uploaded = await QQAutoReplyPlugin._asset_upload_sticker(plugin, {
        "filename": "manual.png", "data_base64": base64.b64encode(_png()).decode(), "desc": "manual cat",
    })
    assert uploaded.is_ok
    assert gallery.resolve("manual cat")[1]["file"] == "manual.png"


def seed_legacy(tmp_path, items):
    root = tmp_path / "gallery"
    (root / "blobs").mkdir(parents=True)
    for item in items.values():
        if item.get("file") == "cat.png":
            (root / "blobs" / "cat.png").write_bytes(_png())
    (root / "index.json").write_text(json.dumps({"items": items}), encoding="utf-8")


@pytest.mark.asyncio
async def test_migration_preserves_manual_inventory_and_does_not_resurrect_deleted_images(tmp_path):
    plugin, gallery, store = services(tmp_path)
    store.write({"1": {"path": "manual.png", "desc": "existing"}})
    seed_legacy(tmp_path, {"g1": {
        "file": "cat.png", "desc": "old cat", "source": "incoming", "sender": "alice",
        "kind": "sticker", "times": 9, "avatar_at": 42,
    }})
    await gallery.start()
    data = store.read()
    assert data["1"]["desc"] == "existing"
    assert data["2"]["times"] == 9 and data["2"]["avatar_at"] == 42
    assert data["2"]["scope"] == "restricted"  # Old sender may be a group participant.
    assert not sticker_allowed(data["2"], target_type="private", target_id="alice")
    assert (tmp_path / "gallery" / "index.migrated.json").exists()
    assert (tmp_path / "gallery" / "blobs" / "cat.png").exists()  # Recovery backup.
    await QQAutoReplyPlugin._asset_delete_sticker(plugin, {"id": "2"})
    await QQGalleryService(plugin).start()
    assert set(store.read()) == {"1"}


@pytest.mark.asyncio
async def test_partial_migration_checkpoint_survives_delete_and_retry(tmp_path):
    plugin, gallery, store = services(tmp_path)
    seed_legacy(tmp_path, {"g1": {"file": "cat.png", "source": "upload"}, "g2": {"file": "missing.png"}})
    with pytest.raises(ValueError):
        await gallery.start()
    assert len(store.read()) == 1
    await QQAutoReplyPlugin._asset_delete_sticker(plugin, {"id": "1"})
    (tmp_path / "gallery" / "blobs" / "missing.png").write_bytes(_png(color=(5, 6, 7)))
    await QQGalleryService(plugin).start()
    assert len(store.read()) == 1
    assert all(e.get("source") != "upload" for e in store.read().values())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../outside.png", "missing.png"])
async def test_migration_rejects_unsafe_or_missing_file_without_destroying_legacy(tmp_path, name):
    _, gallery, store = services(tmp_path)
    seed_legacy(tmp_path, {"g1": {"file": name}})
    (tmp_path / "gallery" / "outside.png").write_bytes(_png())
    with pytest.raises(ValueError):
        await gallery.start()
    assert not store.read()
    assert (tmp_path / "gallery" / "index.json").exists()


@pytest.mark.asyncio
async def test_gallery_and_save_sticker_share_content_and_scope(tmp_path):
    plugin, gallery, store = services(tmp_path)
    gid = await gallery.remember_bytes(_png(), desc="cat", sender_id="alice")
    assert not sticker_allowed(store.read()[gid[1:]], target_type="private", target_id="alice")
    lab = QQStickerLab(plugin)

    async def download(_url):
        return _png()

    lab._download = download
    saved = await lab.save("http://stub", origin="alice")
    assert saved["id"] == gid[1:]
    assert len(list(store.directory.iterdir())) == len(store.read()) == 1
    assert sticker_allowed(store.read()[saved["id"]], target_type="private", target_id="alice")
    assert not sticker_allowed(store.read()[saved["id"]], target_type="private", target_id="bob")
    other = await gallery.remember_bytes(_png(), kind="sticker", sender_id="bob")
    assert other != gid and len(store.read()) == 2


@pytest.mark.asyncio
async def test_save_sticker_does_not_lose_concurrent_gallery_write(tmp_path):
    plugin, gallery, store = services(tmp_path)
    lab = QQStickerLab(plugin)

    async def download(_url):
        return _png()

    async def describe(_path):
        await gallery.remember_bytes(_png(color=(7, 8, 9)), source="upload", desc="other")
        return "cat"

    lab._download = download
    lab.describe = describe
    assert await lab.save("stub", origin="alice")
    assert {e["desc"] for e in store.read().values()} == {"cat", "other"}
    assert len(list(store.directory.iterdir())) == 2


@pytest.mark.asyncio
async def test_describing_sticker_preserves_scope_and_concurrent_upload(tmp_path):
    plugin, gallery, store = services(tmp_path)
    gid = await gallery.remember_bytes(_png(), kind="sticker", sender_id="alice")

    async def describe(*args, **kwargs):
        await gallery.remember_bytes(_png(color=(7, 8, 9)), source="upload", desc="other")
        return "cat"

    plugin._vlm_describe_locator = describe
    result = await QQAutoReplyPlugin._asset_describe_sticker(plugin, {"id": gid[1:]})
    assert result.is_ok and len(store.read()) == 2
    assert store.read()[gid[1:]]["scope"] == "private:alice"
    assert not sticker_allowed(store.read()[gid[1:]], target_type="private", target_id="bob")


@pytest.mark.asyncio
@pytest.mark.parametrize("through_gallery", [True, False])
async def test_delete_during_description_is_not_undone(tmp_path, through_gallery):
    plugin, gallery, store = services(tmp_path)
    gid = await gallery.remember_bytes(_png(), sender_id="alice")

    async def describe(*args, **kwargs):
        await QQAutoReplyPlugin._asset_delete_sticker(plugin, {"id": gid[1:]})
        return "late description"

    plugin._vlm_describe_locator = describe
    if through_gallery:
        assert await gallery.describe_missing() == 0
    else:
        assert (await QQAutoReplyPlugin._asset_describe_sticker(plugin, {"id": gid[1:]})).is_err
    await gallery.flush()
    assert not store.read()


@pytest.mark.asyncio
async def test_full_inventory_does_not_evict_manual_entries(tmp_path):
    _, gallery, store = services(tmp_path)
    store.write({"1": {"path": "manual.png", "desc": "manual"}})
    gallery.MAX_ITEMS = 1
    assert await gallery.remember_bytes(_png()) is None
    assert store.read() == {"1": {"path": "manual.png", "desc": "manual"}}


@pytest.mark.asyncio
async def test_corrupt_canonical_index_is_never_overwritten(tmp_path):
    plugin, gallery, _ = services(tmp_path)
    (tmp_path / "sticker.json").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        await gallery.start()
    result = await QQAutoReplyPlugin._asset_upload_sticker(plugin, {
        "filename": "cat.png", "data_base64": base64.b64encode(_png()).decode(), "desc": "cat",
    })
    assert result.is_err
    assert (tmp_path / "sticker.json").read_text() == "broken"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside.png", "missing.png", "bad.svg"])
async def test_preview_rejects_traversal_missing_and_active_content(tmp_path, path):
    plugin, _, store = services(tmp_path)
    store.directory.mkdir()
    store.write({"1": {"path": path}})
    (tmp_path / "outside.png").write_bytes(_png())
    (store.directory / "bad.svg").write_text('<svg onload="alert(1)"/>')
    assert (await QQAutoReplyPlugin._asset_preview_sticker(plugin, {"id": "1"})).is_err


def test_management_pages_offer_preview_on_existing_inventory():
    from _ui_source import code_of, fn_body

    for page in ("napcat.html", "open_platform.html"):
        text = code_of(page)
        assert "previewSticker(" in fn_body(text, "loadStickers")
        body = fn_body(text, "previewSticker")
        assert "action:'preview_sticker'" in body and "id:String(sid)" in body
        assert "image.src=d.data_url" in body
        assert "ui.shared.sticker.unified_hint" in text

@pytest.mark.parametrize("page", ["napcat.html", "open_platform.html"])
def test_preview_button_executes_real_ui_contract_and_resets_modal(page):
    import shutil
    import subprocess

    from _ui_source import code_of, fn_body

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute management UI")
    text = code_of(page)
    preview = "async function previewSticker(sid){" + fn_body(text, "previewSticker") + "}"
    close = "function hideModal(){" + fn_body(text, "hideModal") + "}"
    js = r'''
const assert = require('node:assert/strict');
const els = Object.fromEntries(['modal-title','modal-body','modal-confirm','modal-bg'].map(id => [id,{
    style:{}, children:[], replaceChildren(){this.children=[]}, appendChild(x){this.children.push(x)}
}]));
const document={getElementById:id=>els[id],createElement:tag=>({tag,style:{}})};
const t=(_key,fallback)=>fallback;
let calls=[],errors=[],payload='data:image/png;base64,aW1hZ2U=';
const toast=message=>errors.push(message);
const call=async(entry,args)=>{calls.push({entry,args});return {data_url:payload}};
'''
    js += preview + close + r'''
(async()=>{
 await previewSticker('12');
 assert.deepEqual(calls,[{entry:'asset',args:{action:'preview_sticker',id:'12'}}]);
 assert.equal(els['modal-body'].children[0].src,payload);
 assert.equal(els['modal-bg'].style.display,'flex');
 assert.equal(els['modal-confirm'].style.display,'none');
 hideModal();
 assert.equal(els['modal-bg'].style.display,'none');
 assert.equal(els['modal-confirm'].style.display,'');
 payload='javascript:alert(1)';
 await previewSticker('13');
 assert.equal(errors.length,1);
 assert.equal(els['modal-bg'].style.display,'none');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_failed_inventory_write_does_not_consume_inbox(tmp_path, monkeypatch):
    _, gallery, store = services(tmp_path)
    await gallery.start()
    inbox = gallery.inbox_dir() / "cat.png"
    inbox.write_bytes(_png())

    def failed_write(_data):
        raise OSError("disk full")

    monkeypatch.setattr(store.__class__, "write", lambda self, data: failed_write(data))
    result = await gallery.import_inbox()
    assert result["failed"] == 1 and inbox.exists()
    assert not store.read()
    assert list(store.directory.iterdir()) == []

@pytest.mark.asyncio
async def test_inbox_repairs_missing_duplicate_before_consuming_source(tmp_path):
    _, gallery, store = services(tmp_path)
    gid = await gallery.remember_bytes(_png(), source="upload")
    target = store.directory / store.read()[gid[1:]]["path"]
    target.unlink()
    inbox = gallery.inbox_dir() / "replacement.png"
    inbox.write_bytes(_png())
    assert (await gallery.import_inbox())["imported"] == 1
    assert not inbox.exists()
    assert target.read_bytes() == _png()
    assert len(store.read()) == 1

@pytest.mark.asyncio
async def test_management_listing_migrates_without_starting_qq(tmp_path):
    plugin, gallery, _ = services(tmp_path)
    plugin.gallery_service = gallery
    seed_legacy(tmp_path, {"g1": {"file": "cat.png", "desc": "legacy cat", "source": "upload"}})
    result = await QQAutoReplyPlugin._asset_list_stickers(plugin, {})
    assert result.is_ok
    assert result.value["stickers"][0]["desc"] == "legacy cat"
    assert (tmp_path / "gallery" / "index.migrated.json").is_file()
