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
    "independent_dirty", "input_and_refresh", "invalid_greeting_time", "midnight_greeting",
    "multiple_windows", "remove_all", "legacy_migration", "escape_sample", "limit", "slot_dirty",
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
  el('cfg-initiative-greeting-enabled').checked = enabled;
  initiativeSlots=[{id:'morning',enabled:true,start:'06:35',end:'07:00',mode:'greeting',sample:'早安呀',topic:''}];
  el('cfg-initiative-quiet-start').value = '22';
  el('cfg-initiative-quiet-end').value = '7';
  el('cfg-initiative-max-per-day').value = '4';
  el('cfg-initiative-min-gap-hours').value = '0';
  listeners.input({target: el('cfg-initiative-on')});
}
"""
    checks = r"""
(async () => {
  if (scenario === 'multiple_windows') {
    edit(true);addInitiativeSlot();addInitiativeSlot();
    updateInitiativeSlot(1,'start','12:00');updateInitiativeSlot(1,'end','12:30');
    updateInitiativeSlot(1,'mode','search');updateInitiativeSlot(1,'topic','科技新闻');
    updateInitiativeSlot(2,'start','23:30');updateInitiativeSlot(2,'end','00:30');
    updateInitiativeSlot(2,'mode','video');updateInitiativeSlot(2,'sample','晚上好');
    updateInitiativeSlot(2,'topic','B站猫咪视频');
    const ids=initiativeSlots.map(x=>x.id);
    await saveInitiativeSettings();await loadDashboard();
    assert.equal(initiativeSlots.length,3);
    assert.deepEqual(initiativeSlots.map(x=>x.id),ids);
    assert.equal(initiativeSlots[1].topic,'科技新闻');
    assert.equal(initiativeSlots[2].mode,'video');
    assert.equal(initiativeSlots[2].end,'00:30');
    assert.equal(initiativeSlots[2].sample,'晚上好');
  } else if (scenario === 'remove_all') {
    edit(true);removeInitiativeSlot(0);await saveInitiativeSettings();await loadDashboard();
    assert.deepEqual(saved.proactive_initiative_greeting_slots,[]);
    assert.equal(initiativeSlots.length,0);
  } else if (scenario === 'legacy_migration') {
    applyInitiativeSettings({proactive_initiative_greeting_hour:23,proactive_initiative_greeting_minute:58});
    assert.equal(initiativeSlots[0].id,'legacy');
    assert.equal(initiativeSlots[0].start,'23:58');
    assert.equal(initiativeSlots[0].end,'00:03');
  } else if (scenario === 'escape_sample') {
    edit(true);updateInitiativeSlot(0,'sample','</textarea><script>alert(1)</script>');
    updateInitiativeSlot(0,'topic','" onfocus="alert(1)');renderInitiativeSlots();
    const html=el('initiative-slots').innerHTML;
    assert.ok(!html.includes('<script>'));
    assert.ok(html.includes('&lt;/textarea&gt;'));
    assert.ok(html.includes('&quot; onfocus=&quot;'));
  } else if (scenario === 'limit') {
    initiativeSlots=[];for(let i=0;i<13;i++)addInitiativeSlot();
    assert.equal(initiativeSlots.length,12);
    assert.equal(new Set(initiativeSlots.map(x=>x.id)).size,12);
  } else if (scenario === 'slot_dirty') {
    applyInitiativeSettings({});updateInitiativeSlot(0,'sample','不要覆盖我的编辑');
    applyInitiativeSettings({proactive_initiative_greeting_slots:[]});
    assert.equal(_initiativeDirty,true);assert.equal(_settingsDirty,false);
    assert.equal(initiativeSlots[0].sample,'不要覆盖我的编辑');
    removeInitiativeSlot(0);assert.equal(_initiativeDirty,true);
  } else if (scenario === 'invalid_greeting_time') {
    edit(true);
    updateInitiativeSlot(0,'start','');
    await saveInitiativeSettings();
    assert.equal(requests.length, 0);
    assert.equal(_initiativeDirty, true);
    assert.equal(notices.at(-1).ok, false);
  } else if (scenario === 'midnight_greeting') {
    edit(true);
    updateInitiativeSlot(0,'start','00:00');
    await saveInitiativeSettings();
    assert.equal(requests[0].proactive_initiative_greeting_slots[0].start, '00:00');
    assert.equal(initiativeSlots[0].start, '00:00');
  } else if (scenario === 'general_failure') {
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
    assert.equal(request.proactive_initiative_greeting_enabled, enabled);
    assert.equal(request.proactive_initiative_greeting_slots[0].start, '06:35');
    assert.equal(initiativeSlots[0].start, '06:35');
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
    escape = "function escapeHtml(s){" + fn_body(html, "escapeHtml") + "}"
    source = harness + escape + initiative + save + listener + "\nconst scenario=" + json.dumps(scenario) + ";\n" + checks
    result = subprocess.run([node, "-"], input=source, text=True, encoding="utf-8",
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_dashboard_updates_the_initiative_form_without_a_separate_fetch():
    html = PAGE.read_text(encoding="utf-8")
    body = fn_body(html, "applySettings")
    assert "applyInitiativeSettings(s)" in body
    assert body.index("applyInitiativeSettings(s)") < body.index("if(_settingsDirty)")


@pytest.mark.parametrize("trigger,label", [("online", "段内上线触发"), ("random", "段内随机触发")])
def test_greeting_status_shows_server_plan(trigger, label):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for UI handler tests")
    body = fn_body(PAGE.read_text(encoding="utf-8"), "renderInitiativeStatus")
    source = r"""
const assert=require('node:assert/strict');
const el={style:{}};
const document={getElementById:()=>el};
function t(k,f){return f;}
function tf(k,f,values){return f.replace(/\{(\w+)\}/g,(_,key)=>values[key]);}
""" + "function renderInitiativeStatus(d){" + body + "}\n"
    snapshot = {"enabled": True, "has_admin": True, "greeting": {"enabled": True, "slots": [
        {"start": "09:00", "end": "10:00", "trigger": trigger, "planned_at": 1794101400,
         "blocked_reason": "等待时间段内的随机时间"}]}}
    source += "renderInitiativeStatus(" + json.dumps(snapshot) + ");\n"
    source += "assert.ok(el.textContent.includes(" + json.dumps(label) + "));\n"
    source += "assert.ok(el.textContent.includes('09:00–10:00')); assert.ok(el.textContent.includes('计划'));\n"
    source += "assert.ok(!el.textContent.includes('undefined')); assert.ok(!el.textContent.includes('Invalid Date'));"
    result = subprocess.run([node, "-"], input=source, text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
