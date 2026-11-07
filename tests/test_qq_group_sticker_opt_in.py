"""Per-group sticker storage opt-in: default deny, persistence and all write paths."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from plugin.plugins.qq_auto_reply.config_store import QQAutoReplyConfigStore
from plugin.plugins.qq_auto_reply.dashboard_service import QQDashboardService
from plugin.plugins.qq_auto_reply.group_permission import GroupPermissionManager
from plugin.plugins.qq_auto_reply.settings_service import QQSettingsService
from plugin.plugins.qq_auto_reply.sticker_scope import group_sticker_collection_allowed
from test_qq_gallery import _lab as gallery_lab
from test_qq_gallery import _png
from test_qq_sticker_collect_policy import _face_message, _lab, _patch_io


@pytest.mark.parametrize("value", [None, False, "false", "true", 1, {}, []])
def test_old_and_invalid_settings_are_off(value):
    manager = GroupPermissionManager(["legacy", {"group_id": "g", "sticker_collect_enabled": value}])
    for gid in ("legacy", "g", "missing"):
        assert not manager.is_sticker_collection_enabled(gid)
    manager.add_group("new")
    assert not manager.is_sticker_collection_enabled("new")
    assert not group_sticker_collection_allowed(SimpleNamespace(), "g")


def test_edit_and_reload_preserve_explicit_opt_in():
    manager = GroupPermissionManager([{"group_id": "g", "sticker_collect_enabled": True}])
    manager.add_group("g", "normal", normal_relay_probability=0.25)
    restored = GroupPermissionManager(manager.list_groups())
    assert restored.is_sticker_collection_enabled("g")
    assert restored.get_group_level("g") == "normal"
    assert restored.get_normal_relay_probability("g") == 0.25
    restored.remove_group("g")
    restored.add_group("g")
    assert not restored.is_sticker_collection_enabled("g")


@pytest.mark.parametrize("policy", ["current", "any"])
async def test_group_opt_in_cannot_be_bypassed(tmp_path, policy):
    lab = _lab(tmp_path, policy)
    lab.plugin.group_permission_mgr.set_sticker_collection_enabled("111", False)
    lab._download = AsyncMock()
    lab.observe(_face_message())
    assert not lab.recent()
    assert not lab.should_offer_save(group_id="111")
    assert lab.check(item_group="111", current_group="222")
    assert "没有开启" in await lab.execute_save({}, sender_id="u1", group_id="111")
    assert await lab.save("https://x/cat.jpg", group_id="111") is None
    lab._download.assert_not_awaited()
    assert not (tmp_path / "sticker.json").exists()


async def test_revoking_opt_in_filters_already_observed_media(tmp_path):
    lab = _lab(tmp_path, "any")
    lab.observe(_face_message())
    assert lab.should_offer_save(group_id="111")
    lab.plugin.group_permission_mgr.set_sticker_collection_enabled("111", False)
    assert not lab.has_fresh()
    assert not lab.recent()
    assert not lab.should_offer_save(group_id="222")
    lab._download = AsyncMock()
    # ANY must not pull from a now-disabled source even from an enabled group/private chat.
    await lab.execute_save({}, sender_id="u1", group_id="222")
    await lab.execute_save({}, sender_id="u1")
    lab._download.assert_not_awaited()


@pytest.mark.parametrize("stage", ["download", "describe"])
async def test_revoke_during_collection_leaves_no_saved_entry(tmp_path, monkeypatch, stage):
    lab = _lab(tmp_path)
    _patch_io(lab, monkeypatch, payload=_png())
    method = "_download" if stage == "download" else "describe"
    original = getattr(lab, method)

    async def revoke(*args):
        result = await original(*args)
        lab.plugin.group_permission_mgr.set_sticker_collection_enabled("111", False)
        return result

    monkeypatch.setattr(lab, method, revoke)
    assert await lab.save("https://x/cat.png", group_id="111") is None
    assert not lab._load_index()
    assert not list((tmp_path / "sticker").glob("*"))


@pytest.mark.parametrize("enabled", [False, True])
async def test_gallery_sticker_path_obeys_same_group_switch(tmp_path, enabled):
    gallery, _, blobs = gallery_lab(tmp_path)
    gallery.SAVE_DEBOUNCE_SECONDS = 0
    gallery.plugin.group_permission_mgr = GroupPermissionManager([
        {"group_id": "111", "sticker_collect_enabled": enabled},
    ])
    message = _face_message()
    locator = message["message"][0]["data"]["url"]
    blobs[locator] = _png()
    # The same URL in attachments must not turn a rejected sticker into a normal image.
    message["attachments"] = [{"type": "image", "url": locator}]
    ids = await gallery.remember_message(message)
    assert bool(ids) is enabled
    assert bool(gallery.list_items()) is enabled
    await gallery.flush()


@pytest.mark.parametrize("sticker_first", [False, True])
async def test_gallery_duplicate_segments_cannot_downgrade_sticker(tmp_path, sticker_first):
    gallery, _, _ = gallery_lab(tmp_path)
    gallery.plugin.group_permission_mgr = GroupPermissionManager([{"group_id": "111"}])
    message = _face_message()
    sticker = message["message"][0]
    image = {"type": "image", "data": {"url": sticker["data"]["url"]}}
    message["message"] = [sticker, image] if sticker_first else [image, sticker]
    gallery._fetch_bytes = AsyncMock(return_value=_png())
    assert await gallery.remember_message(message) == []
    gallery._fetch_bytes.assert_not_awaited()
    assert not gallery.list_items()
    await gallery.flush()


async def test_gallery_rechecks_after_download(tmp_path):
    gallery, _, _ = gallery_lab(tmp_path)
    gallery.plugin.group_permission_mgr = GroupPermissionManager([
        {"group_id": "111", "sticker_collect_enabled": True},
    ])

    async def fetch(*args, **kwargs):
        gallery.plugin.group_permission_mgr.set_sticker_collection_enabled("111", False)
        return _png()

    gallery._fetch_bytes = fetch
    assert await gallery.remember("https://x/cat.png", kind="sticker", group_id="111") is None
    assert not gallery.list_items()


def dashboard(tmp_path):
    backlog = SimpleNamespace(ensure_group_placeholder=AsyncMock(), remove_group_placeholder=AsyncMock())
    plugin = SimpleNamespace(
        group_permission_mgr=GroupPermissionManager([{"group_id": "111"}]),
        permission_mgr=None, backlog_store=backlog,
        _create_backlog_store_from_settings=lambda settings: backlog,
        _qq_settings={}, config_store=QQAutoReplyConfigStore(tmp_path),
        logger=SimpleNamespace(error=Mock(), info=Mock()),
    )
    plugin.settings_service = QQSettingsService(plugin)
    service = QQDashboardService(plugin)
    service.build_dashboard_state = AsyncMock(return_value={})
    return service, plugin


async def test_dashboard_opt_in_disk_roundtrip_and_unrelated_edit(tmp_path):
    service, plugin = dashboard(tmp_path)
    result = await service.add_trusted_group(group_id="111", sticker_collect_enabled=True)
    assert result.value["persisted"] is True
    assert plugin.group_permission_mgr.is_sticker_collection_enabled("111")
    await service.add_trusted_group(group_id="111", level="normal", normal_relay_probability=0.2)
    loaded = await plugin.config_store.load()
    restored = GroupPermissionManager(loaded["trusted_groups"])
    assert restored.is_sticker_collection_enabled("111")
    assert restored.get_normal_relay_probability("111") == 0.2
    await service.add_trusted_group(group_id="111", sticker_collect_enabled=False)
    loaded = await plugin.config_store.load()
    assert not GroupPermissionManager(loaded["trusted_groups"]).is_sticker_collection_enabled("111")


@pytest.mark.parametrize("saved", [False, True])
async def test_dashboard_grants_only_after_save(tmp_path, saved):
    service, plugin = dashboard(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()

    async def persist(*, overlay):
        assert overlay["trusted_groups"][0]["sticker_collect_enabled"] is True
        started.set()
        await finish.wait()
        return saved

    plugin.settings_service.persist_business_config = persist
    task = asyncio.create_task(service.add_trusted_group(group_id="111", sticker_collect_enabled=True))
    await started.wait()
    assert not plugin.group_permission_mgr.is_sticker_collection_enabled("111")
    finish.set()
    result = await task
    assert result.value["persisted"] is saved
    assert plugin.group_permission_mgr.is_sticker_collection_enabled("111") is saved


@pytest.mark.parametrize("value", ["false", "true", 1, [], {}])
async def test_dashboard_rejects_non_boolean_values(tmp_path, value):
    service, plugin = dashboard(tmp_path)
    assert (await service.add_trusted_group(group_id="111", sticker_collect_enabled=value)).is_err()
    assert not plugin.group_permission_mgr.is_sticker_collection_enabled("111")


async def test_trust_entry_forwards_opt_in(tmp_path):
    from plugin.plugins.qq_auto_reply import QQAutoReplyPlugin
    from test_qq_entry_dispatch import _plugin

    plugin = _plugin(add_trusted_group=None)
    await QQAutoReplyPlugin.trust(plugin, action="group_add", group_id="111", sticker_collect_enabled=True)
    assert plugin.dashboard_service.add_trusted_group.call_args.kwargs["sticker_collect_enabled"] is True

@pytest.mark.parametrize("page", ["napcat.html", "open_platform.html"])
def test_group_checkbox_render_and_save_contract(page):
    import json
    import re
    import shutil
    import subprocess

    from _ui_source import fn_body, read

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for executable UI checks")
    source = read(page)
    render = fn_body(source, "renderGroups")
    toggle = fn_body(source, "doSetGroupStickerCollect")
    assert render and toggle
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", source, re.S)
    script = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
for(const script of input.scripts)new vm.Script(script);
const state={groups:[{group_id:'111',level:'normal',normal_relay_probability:0.25}]};
const tbody={innerHTML:''};
const document={getElementById:()=>tbody};
const t=(_key,fallback)=>fallback, escapeHtml=String,escapeOnclick=String;
const calls=[], errors=[];let persisted=true,refreshes=0;
async function call(entry,args){calls.push({entry,args});return {persisted};}
async function loadDashboard(){refreshes++;}
function toast(error){errors.push(error);}
const renderGroups=new Function('state','document','t','escapeHtml','escapeOnclick',input.render);
const toggle=new Function('state','call','loadDashboard','toast','t',
  'return async function(gid,checkbox){'+input.toggle+'}')(state,call,loadDashboard,toast,t);
(async()=>{
 renderGroups(state,document,t,escapeHtml,escapeOnclick);
 assert(!tbody.innerHTML.includes(' checked'));
 assert(tbody.innerHTML.includes('doSetGroupStickerCollect'));
 state.groups[0].sticker_collect_enabled=true;
 renderGroups(state,document,t,escapeHtml,escapeOnclick);
 assert(tbody.innerHTML.includes(' checked'));
 const checkbox={checked:false,disabled:false};
 await toggle('111',checkbox);
 assert.deepEqual(calls[0],{entry:'trust',args:{action:'group_add',group_id:'111',level:'normal',
   normal_relay_probability:0.25,sticker_collect_enabled:false}});
 assert.equal(checkbox.disabled,false);
 assert.equal(refreshes,1);
 persisted=false;state.groups[0].sticker_collect_enabled=false;checkbox.checked=true;
 await toggle('111',checkbox);
 assert.equal(checkbox.checked,false);
 assert.equal(errors.length,1);
 assert.equal(refreshes,2);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run(
        [node, "-e", script], input=json.dumps({"render": render, "toggle": toggle, "scripts": scripts}),
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


async def test_global_off_also_blocks_enabled_group_gallery(tmp_path):
    gallery, _, _ = gallery_lab(tmp_path)
    gallery.plugin.group_permission_mgr = GroupPermissionManager([
        {"group_id": "111", "sticker_collect_enabled": True},
    ])
    gallery.plugin._qq_settings = {"sticker_collect_policy": "off"}
    gallery._fetch_bytes = AsyncMock()
    assert await gallery.remember("https://x/cat.png", kind="sticker", group_id="111") is None
    gallery._fetch_bytes.assert_not_awaited()
