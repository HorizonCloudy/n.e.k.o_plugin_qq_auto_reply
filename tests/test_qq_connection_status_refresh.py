"""Execute connection refresh handlers without a live QQ connection."""

import shutil
import subprocess
from pathlib import Path

import pytest
from _ui_source import fn_body

STATIC = Path(__file__).resolve().parents[1] / "static"


def test_napcat_runtime_refresh_updates_all_status_without_touching_edits():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for UI tests")
    html = (STATIC / "napcat.html").read_text(encoding="utf-8")
    names = [
        "applyRuntimeStatus",
        "updateStatusBar",
        "fetchDashboardSnapshot",
        "loadRuntimeStatus",
        "loadDashboard",
        "startStatusPoll",
    ]
    functions = "\n".join(
        ("async " if name in ("loadRuntimeStatus", "loadDashboard") else "")
        + "function "
        + name
        + "("
        + ("d" if name == "applyRuntimeStatus" else "")
        + "){"
        + fn_body(html, name)
        + "}"
        for name in names
    )
    listeners = "\n".join(
        line
        for line in html.splitlines()
        if line.startswith(("document.addEventListener('visibilitychange'", "window.addEventListener('focus'"))
    )
    harness = r"""
const assert = require('node:assert/strict');
const elements = new Map(), events = {}, timers = [];
const el = id => {if(!elements.has(id))elements.set(id,{style:{},dataset:{},value:'unsaved',innerHTML:''});return elements.get(id)};
const document={hidden:false,getElementById:el,addEventListener:(name,fn)=>events[name]=fn};
const window={addEventListener:(name,fn)=>events[name]=fn};
const state={settings:{custom:'unsaved'},loginOnline:false};
let _settingsDirty=true,_initiativeDirty=true,_dashboardRequest=null,_statusPollTimer=null;
let backendRenders=0,guideRenders=0,fullRenders=0,calls=0,pending=null;
let payload={runtime:{},login:{status:'offline'}};
function t(key,fallback){return fallback}
function escapeHtml(value){return String(value||'').replaceAll('<','&lt;')}
function applyBackendUI(){backendRenders++}
function updateGuide(){guideRenders++}
function applyDashboard(data){fullRenders++;applyRuntimeStatus(data)}
function loadBufferState(){} function loadAttention(){}
function setInterval(fn,ms){timers.push({fn,ms});return timers.length}
async function call(){calls++;return pending||payload}
"""
    checks = r"""
(async()=>{
  await loadRuntimeStatus();
  assert.equal(state.onebotOk,false);
  payload={runtime:{onebot_connected:true,auto_reply_running:true,napcat_pid:321,backend_webui_url:'http://localhost:123',qrcode_url:'/qr.png'},login:{status:'online',self_id:'12345',nickname:'cat'},recent_pipeline_trace_summaries:[{result_kind:'sent',source:'private',delivery_mode:'text'}]};
  await loadRuntimeStatus();
  assert.equal(state.onebotOk,true);assert.equal(state.loginOnline,true);
  assert.equal(state.backendWebuiUrl,'http://localhost:123');
  assert.equal(el('st-self-id').textContent,'12345');
  assert.equal(el('st-nickname').textContent,'cat');
  assert.equal(el('st-napcat-pid').textContent,321);
  assert.equal(el('conn-dot').style.background,'var(--success)');
  assert.equal(el('qr-img').style.display,'none');
  assert.match(el('traces-list').innerHTML,/sent/);
  assert.equal(backendRenders,2);assert.equal(guideRenders,2);
  assert.equal(fullRenders,0);assert.equal(_settingsDirty,true);assert.equal(_initiativeDirty,true);
  assert.equal(state.settings.custom,'unsaved');
  payload={runtime:{qrcode_url:'/new-qr.png'},login:{status:'offline'}};
  await loadRuntimeStatus();
  assert.equal(state.onebotOk,false);assert.equal(state.loginOnline,false);
  assert.equal(el('st-self-id').textContent,'-');assert.equal(el('st-nickname').textContent,'-');
  assert.equal(el('conn-dot').style.background,'var(--danger)');
  assert.equal(el('traces-list').innerHTML,'');assert.equal(el('qr-img').style.display,'');
  assert.match(el('qr-img').src,/new-qr/);
  // Shared in-flight query prevents late overlapping poll responses from winning.
  let release;pending=new Promise(resolve=>release=resolve);
  const before=calls;
  const a=loadRuntimeStatus(),b=loadRuntimeStatus(),c=loadDashboard();
  assert.equal(calls,before+1);
  release(payload);pending=null;await Promise.all([a,b,c]);assert.equal(calls,before+2);
  assert.equal(_dashboardRequest,null);
  pending=Promise.reject(Error('offline'));
  await loadRuntimeStatus();pending=null;assert.equal(_dashboardRequest,null);
  await loadRuntimeStatus();
  startStatusPoll();startStatusPoll();assert.equal(timers.length,1);assert.equal(timers[0].ms,5000);
  document.hidden=true;let count=calls;timers[0].fn();events.visibilitychange();assert.equal(calls,count);
  document.hidden=false;timers[0].fn();await Promise.resolve();await loadRuntimeStatus();assert.ok(calls>count);
  count=calls;events.focus();await loadRuntimeStatus();assert.ok(calls>count);
  count=calls;events.visibilitychange();await loadRuntimeStatus();assert.ok(calls>count);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-"],
        input=harness + functions + listeners + checks,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_status_page_subscribes_to_connection_changes_and_resume():
    html = (STATIC / "status.html").read_text(encoding="utf-8")
    assert "UISSE.on('status', () => { load(); });" in html
    assert "document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });" in html
    assert "window.addEventListener('focus', () => { load(); });" in html


def test_polling_starts_even_if_initial_dashboard_query_fails():
    html = (STATIC / "napcat.html").read_text(encoding="utf-8")
    body = fn_body(html, "bootstrap")
    assert body.index("startStatusPoll()") < body.index("try{")
