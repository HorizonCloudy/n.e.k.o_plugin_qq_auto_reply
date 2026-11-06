"""Exercise the shared actual JS in Node, without changing user settings."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "scenario", ["load", "dirty_refresh", "failure", "success", "inflight_edit", "transport_failure", "initial_edit", "partial_binding", "unbind"]
)
def test_companion_ui(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    script = (Path(__file__).resolve().parents[1] / "static/plugin-tools.js").read_text(encoding="utf-8")
    harness = r"""
const assert = require('node:assert/strict');
const elements = {};
function el(id) {return elements[id] ||= {value:'',checked:false,textContent:'',disabled:false};}
global.window = {};
global.document = {readyState:'loading', addEventListener(){}, getElementById:el};
let saved = {enabled:false, owner_id:'123', bot_id:'456', character:'YUI', presence_enabled:true, mood_enabled:false};
let persisted = true, transportFailure = false, hold = null, requests = [];
global.call = async (entry, args) => {
  requests.push(args);
  if (entry === 'config') {
    if (transportFailure) throw new Error('offline');
    if (hold) await hold;
    if (persisted) saved = {...args.companion_context};
    return {persisted, settings:{companion_context:saved}};
  }
  return {settings:{companion_context:saved}};
};
"""
    checks = r"""
(async () => {
  if (scenario === 'initial_edit') {
    el('cc-owner_id').value = '789';
    el('cc-bot_id').value = '456';
    el('cc-character').value = 'YUI';
    window.ccEdit();
    await window.ccLoad();
    assert.equal(el('cc-owner_id').value, '789');
    await window.ccSave();
    assert.equal(saved.owner_id, '789');
    return;
  }
  await window.ccLoad();
  assert.equal(el('cc-owner_id').value, '123');
  assert.ok(el('cc-status').textContent.includes('尚未迁移'));
  if (scenario === 'load') return;
  if (scenario === 'partial_binding') {
    el('cc-owner_id').value = ''; window.ccEdit(); await window.ccSave();
    assert.equal(requests.length, 1);
    assert.ok(el('cc-status').textContent.includes('完整绑定'));
    return;
  }
  if (scenario === 'unbind') {
    ['owner_id', 'bot_id', 'character'].forEach(k => {el('cc-' + k).value = '';});
    window.ccEdit(); await window.ccSave();
    assert.equal(saved.enabled, false);
    assert.equal(saved.source_mode, 'plugin_tools');
    return;
  }
  el('cc-owner_id').value = '789';
  window.ccEdit();
  if (scenario === 'dirty_refresh') {
    await window.ccLoad();
    assert.equal(el('cc-owner_id').value, '789');
    assert.equal(requests.length, 1);
    return;
  }
  if (scenario === 'failure') persisted = false;
  if (scenario === 'transport_failure') transportFailure = true;
  if (scenario === 'inflight_edit') {
    let resolve;
    hold = new Promise(r => {resolve = r;});
    const saving = window.ccSave();
    el('cc-character').value = 'NEW'; window.ccEdit();
    resolve(); await saving; await window.ccLoad();
    assert.equal(el('cc-character').value, 'NEW');
    assert.ok(el('cc-status').textContent.includes('未保存'));
    return;
  }
  await window.ccSave();
  assert.equal(requests[1].companion_context.enabled, true);
  assert.equal(requests[1].companion_context.source_mode, 'plugin_tools');
  assert.equal('presence_enabled' in requests[1].companion_context, false);
  if (scenario === 'success') {
    assert.equal(saved.enabled, true);
    assert.ok(el('cc-status').textContent.includes('已保存'));
    saved.owner_id = '123'; await window.ccLoad();
    assert.equal(el('cc-owner_id').value, '123');
  } else {
    assert.ok(el('cc-status').textContent.includes('保存失败'));
    await window.ccLoad();
    assert.equal(el('cc-owner_id').value, '789');
  }
})().catch(e => {console.error(e); process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", harness + script + "\nconst scenario=" + json.dumps(scenario) + ";\n" + checks],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("success", [True, False])
def test_tier_save_requires_persistence_and_serializes_edits(success):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    script = (Path(__file__).resolve().parents[1] / "static/plugin-tools.js").read_text(encoding="utf-8")
    assert 'id="cc-presence_enabled"' not in script
    assert 'id="cc-mood_enabled"' not in script
    assert 'id="cc-enabled"' not in script
    harness = r"""
const assert = require('node:assert/strict');
const elements = {};
const el = id => elements[id] ||= {value:'',checked:false,textContent:'',style:{}};
global.window = {};
global.document = {readyState:'loading',addEventListener(){},getElementById:el,querySelectorAll(){return [];}};
global.t = (key,fallback) => fallback;
global.tf = (key,fallback,params) => fallback;
global.escapeHtml = s => String(s); global.escapeOnclick = s => String(s);
const toasts = [], requests = [];
global.toast = (text,ok) => toasts.push({text,ok});
let resolve;
global.call = async (entry,args) => {requests.push(args); await new Promise(r => {resolve=r;}); return {persisted:success};};
"""
    checks = r"""
(async () => {
  const first = window.ptSetTier('neko_presence', 'admin');
  await window.ptSetTier('forever_companion', 'admin');
  assert.equal(requests.length, 1);
  resolve(); await first;
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].ok === true, success);
  const retry = window.ptSetTier('forever_companion', 'admin');
  assert.equal(requests.length, 2);
  assert.equal('neko_presence' in requests[1].qq_open_plugin_tools, success);
  resolve(); await retry;
})().catch(e => {console.error(e); process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", "const success=" + json.dumps(success) + ";\n" + harness + script + checks],
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
