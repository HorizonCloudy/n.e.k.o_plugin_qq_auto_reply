"""Run the real initiative form handlers in Node (no live QQ or writes to user config)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from _ui_source import fn_body

PAGE = Path(__file__).resolve().parents[1] / "static" / "napcat.html"


@pytest.mark.parametrize("scenario", [
    "general_enable", "general_disable", "dedicated_failure", "transport_failure",
    "dedicated_success", "dedicated_disable", "general_failure",
    "independent_dirty", "input_and_refresh",
])
def test_initiative_settings_handlers(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for UI handler tests")
    html = PAGE.read_text(encoding="utf-8")
    initiative = html[html.index("function initiativeHours"):]
    initiative = initiative[:initiative.index("(function(){")]
    save = "async function doSave(){" + fn_body(html, "doSave") + "}"
    listener = next(line for line in html.splitlines()
                    if line.startswith("document.addEventListener('input'"))
    harness = r"""
const assert = require('node:assert/strict');
let _settingsDirty = false, _initiativeDirty = false, currentPage = 'config';
let state = {settings: {}, backend: 'napcat', replyMode: 'text'};
const elements = new Map(), listeners = {}, notices = [], requests = [];
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {id, value:'', checked:false, tagName:'INPUT', style:{}});
    return elements.get(id);
  },
  addEventListener(type, callback) {listeners[type] = callback;}
};
const el = id => document.getElementById(id);
let persisted = true, failTransport = false, saved = {};
async function call(entry, args) {
  if (entry === 'query') return {enabled: !!saved.proactive_initiative_enabled};
  requests.push(args);
  if (failTransport) throw new Error('network failed');
  if (persisted) saved = {...saved, ...args};
  return {persisted, settings: {...saved}};
}
function applyDashboard(d) {state.settings=d.settings; applyInitiativeSettings(d.settings);}
async function loadDashboard() {applyDashboard({settings: saved});}
function toast(message, ok) {notices.push({message, ok: !!ok});}
function t(key, fallback) {return fallback;}
function tf(key, fallback) {return fallback;}
function intVal(id, fallback) {return fallback;}
function floatVal(id, fallback) {return fallback;}
function currentMode() {return 'napcat';}
function onebotBackend() {return {directoryKey:'napcat_directory', windowKey:'show_napcat_window'};}
function isMaskedTokenPlaceholder() {return false;}
function edit(enabled) {
  el('cfg-initiative-on').checked = enabled;
  el('cfg-initiative-quiet-start').value = '22';
  el('cfg-initiative-quiet-end').value = '7';
  el('cfg-initiative-max-per-day').value = '4';
  el('cfg-initiative-min-gap-hours').value = '0';
  listeners.input({target: el('cfg-initiative-on')});
}
"""
    checks = r"""
(async () => {
  if (scenario === 'general_failure') {
    edit(true);
    persisted = false;
    await doSave();
    assert.equal(notices.some(n => n.ok), false);
    assert.equal(_initiativeDirty, true);
    assert.equal(el('cfg-initiative-on').checked, true);
  } else if (scenario.startsWith('general_')) {
    const enabled = scenario === 'general_enable';
    saved = {proactive_initiative_enabled: !enabled};
    edit(enabled);
    await doSave();
    const request = requests[0];
    assert.equal(request.proactive_initiative_enabled, enabled, 'general save omitted the checkbox');
    assert.equal(request.proactive_initiative_quiet_start_hour, 22);
    assert.equal(request.proactive_initiative_quiet_end_hour, 7);
    assert.equal(request.proactive_initiative_max_per_day, 4);
    assert.equal(request.proactive_initiative_min_gap_seconds, 0);
    assert.equal(_initiativeDirty, false);
    await loadDashboard();
    assert.equal(el('cfg-initiative-on').checked, enabled);
  } else if (scenario === 'dedicated_failure' || scenario === 'transport_failure') {
    edit(true);
    persisted = false;
    failTransport = scenario === 'transport_failure';
    await saveInitiativeSettings();
    assert.equal(notices.some(n => n.ok), false, 'failed persistence must not report success');
    assert.equal(_initiativeDirty, true, 'failed save must retain the dirty form');
    await loadDashboard();
    assert.equal(el('cfg-initiative-on').checked, true);
  } else if (scenario === 'dedicated_success' || scenario === 'dedicated_disable') {
    const enabled = scenario === 'dedicated_success';
    saved = {proactive_initiative_enabled: !enabled};
    edit(enabled);
    await saveInitiativeSettings();
    assert.equal(state.settings.proactive_initiative_enabled, enabled, 'save response must refresh the cached settings');
    assert.equal(_initiativeDirty, false);
    applyInitiativeSettings();
    assert.equal(el('cfg-initiative-on').checked, enabled);
    assert.equal(notices.at(-1).ok, true);
  } else if (scenario === 'independent_dirty') {
    listeners.input({target: el('cfg-url')});
    edit(true);
    await saveInitiativeSettings();
    assert.equal(_settingsDirty, true, 'card-only save must not discard other unsaved edits');
    assert.equal(_initiativeDirty, false);
  } else if (scenario === 'input_and_refresh') {
    edit(true);
    assert.equal(_settingsDirty, false, 'initiative edits use their own dirty flag');
    assert.equal(_initiativeDirty, true);
    applyInitiativeSettings({proactive_initiative_enabled: false});
    assert.equal(el('cfg-initiative-on').checked, true);
    _initiativeDirty = false;
    _settingsDirty = true;
    applyInitiativeSettings({proactive_initiative_enabled: false});
    assert.equal(el('cfg-initiative-on').checked, false, 'other edits must not prevent card hydration');
  }
})().catch(error => {console.error(error); process.exitCode=1;});
"""
    source = harness + initiative + save + listener + "\nconst scenario=" + json.dumps(scenario) + ";\n" + checks
    result = subprocess.run([node, "-"], input=source, text=True, encoding="utf-8",
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_dashboard_updates_the_initiative_form_without_a_separate_fetch():
    html = PAGE.read_text(encoding="utf-8")
    body = fn_body(html, "applySettings")
    assert "applyInitiativeSettings(s)" in body
    assert body.index("applyInitiativeSettings(s)") < body.index("if(_settingsDirty)")
