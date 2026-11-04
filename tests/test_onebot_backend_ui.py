"""Exercise real UI handlers: visibility, per-backend inputs and login copy."""
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"


def function(html, name):
    start = html.index(f"function {name}(")
    end = html.index("\n}", start) + 2
    return html[start:end]


def test_backend_visibility_and_independent_configuration():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for UI behavior tests")
    status = (STATIC / "status.html").read_text(encoding="utf-8")
    console = (STATIC / "napcat.html").read_text(encoding="utf-8")
    source = (STATIC / "onebot-backend.js").read_text(encoding="utf-8")
    harness = r"""
globalThis.window = globalThis;
const assert = require('node:assert/strict');
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id,{style:{},value:'',textContent:'',checked:false,setAttribute(k,v){this[k]=v},removeAttribute(k){delete this[k]}});
    return elements.get(id);
  },
  querySelectorAll(selector) { return [this.getElementById(selector)]; }
};
// Mode cards have a distinct shape from the settings elements.
const query = document.querySelectorAll.bind(document);
document.querySelectorAll = selector => selector === '#modes .mode' ? [] : query(selector);
let lastMode = null, _settingsDirty = false;
let state = {settings:{napcat_directory:'C:/nc',snowluma_directory:'C:/sl',show_napcat_window:true,show_snowluma_window:false}};
function stopBindPoll() {}
function call() {}
function renderBackendControls() {}
function loadRuntimeStatus() {}
function currentMode() { return 'napcat_forward'; }
"""
    checks = r"""
applyMode('napcat');
assert.equal(document.getElementById('onebot-backend-section').style.display,'');
applyMode('open_platform');
assert.equal(document.getElementById('onebot-backend-section').style.display,'none');
assert.equal(document.getElementById('sec-napcat').style.display,'none');
applyMode('napcat_forward');
assert.equal(document.getElementById('onebot-backend-section').style.display,'');
assert.equal(document.getElementById('sec-openplat').style.display,'none');
onBackendChange('snowluma');
assert.equal(document.getElementById('backend-webui-link').style.display,'');
assert.equal(document.getElementById('backend-webui-link')['aria-disabled'],'true');
assert.equal(document.getElementById('backend-webui-link').href,undefined);
assert.equal(document.getElementById('cfg-path').value,'C:/sl');
assert.equal(document.getElementById('cfg-show-napcat').checked,false);
assert.equal(document.getElementById('cfg-url-label').textContent,'SnowLuma WebSocket 服务器地址');
assert.match(document.getElementById('qr-empty').textContent,/本机 QQ/);
assert.equal(document.getElementById('[data-i18n="ui.napcat.config.refresh_qrcode"]').style.display,'none');
assert.match(onebotBackend('snowluma').guides['step-onebot'],/networks.wsClients/);
onBackendChange('napcat');
assert.equal(document.getElementById('cfg-path').value,'C:/nc');
assert.equal(document.getElementById('cfg-show-napcat').checked,true);
assert.equal(document.getElementById('[data-i18n="ui.napcat.config.refresh_qrcode"]').style.display,'');
assert.equal(state.settings.snowluma_directory,'C:/sl');
state.backendWebuiUrl='http://localhost:6099/webui';
applyBackendUI();
assert.equal(document.getElementById('backend-webui-link')['aria-disabled'],'false');
onBackendChange('snowluma');
assert.equal(document.getElementById('backend-webui-link').href,undefined);
state.settings.onebot_backend='snowluma';
state.backendWebuiUrl='http://localhost:5099/';
applyBackendUI();
assert.equal(document.getElementById('backend-webui-link').href,'http://localhost:5099/');
"""
    code = harness + source + function(status, "applyMode") + function(console, "onBackendChange") + function(console, "applyBackendUI") + checks
    result = subprocess.run([node, "-"], input=code, text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_console_open_in_electron_and_browser():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    source = (STATIC / "onebot-backend.js").read_text(encoding="utf-8")
    harness = r"""
const assert = require('node:assert/strict');
globalThis.window=globalThis;
window.parent={};
const realTimer=setTimeout;
const link={style:{},setAttribute(k,v){this[k]=v},removeAttribute(k){delete this[k]}};
(async()=>{
 for(const key of ['napcat','snowluma']) {
  let opened=[];
  window.electronShell={openExternal:async url=>opened.push(url)};
  window.open=()=>{throw Error('Electron must not open an about:blank popup')};
  updateBackendConsoleLink(link,key,'http://localhost:5099/',false,
    async()=>({backend:key,url:'http://localhost:5099/?token=TEST'}));
  await link.onclick({preventDefault(){}});
  assert.deepEqual(opened,['http://localhost:5099/?token=TEST']);
  assert.equal(link.href,'http://localhost:5099/');
  opened=[];
  globalThis.setTimeout=(fn)=>realTimer(fn,1);
  updateBackendConsoleLink(link,key,'http://localhost:5099/',false,()=>new Promise(()=>{}));
  await link.onclick({preventDefault(){}});
  assert.deepEqual(opened,['http://localhost:5099/']);
  globalThis.setTimeout=realTimer;
 }
 delete window.electronShell;
 let destination='';
 window.open=(url)=>{assert.equal(url,'about:blank');return {opener:null,closed:false,location:{replace(url){destination=url}},close(){}}};
 updateBackendConsoleLink(link,'snowluma','http://localhost:5099/',false,
   async()=>({backend:'snowluma',url:'http://localhost:5099/?token=TEST'}));
 await link.onclick({preventDefault(){}});
 assert.equal(destination,'http://localhost:5099/?token=TEST');
 Object.defineProperty(window,'navigator',{value:{userAgent:'Electron/40'},configurable:true});
 window.open=(url)=>{destination=url;return null};
 await link.onclick({preventDefault(){}});
 assert.equal(destination,'http://localhost:5099/?token=TEST');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-"], input="globalThis.window=globalThis;\n" + source + harness,
                            text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_backend_process_controls():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for UI behavior tests")
    source = (STATIC / "backend-controls.js").read_text(encoding="utf-8")
    harness = r"""
const assert = require('node:assert/strict');
function onebotBackend(value) { return {label:value === 'snowluma' ? 'SnowLuma' : 'NapCat'}; }
const elements = new Map();
const el = {querySelector(selector) { if(!elements.has(selector))elements.set(selector,{});return elements.get(selector); }};
(async()=>{
  let calls=[],refreshes=0;
  const call=async(name,args)=>calls.push([name,args]);
  const refresh=async()=>refreshes++;
  for(const backend of ['napcat','snowluma']) {
    const settings={onebot_backend:backend,qq_connection_mode:'napcat'};
    renderBackendControls(el,settings,{},backend,call,refresh);
    assert.equal(el.querySelector('.backend-start').disabled,false);
    assert.equal(el.querySelector('.backend-stop').disabled,true);
    await el.querySelector('.backend-start').onclick();
    assert.deepEqual(calls.at(-1),['runtime',{action:'backend_start',backend}]);
    renderBackendControls(el,settings,{napcat_running:true,napcat_managed:true},backend,call,refresh);
    assert.equal(el.querySelector('.backend-stop').disabled,false);
    await el.querySelector('.backend-stop').onclick();
    assert.deepEqual(calls.at(-1),['runtime',{action:'backend_stop',backend}]);
    renderBackendControls(el,settings,{onebot_connected:true},backend,call,refresh);
    assert.equal(el.querySelector('.backend-start').disabled,true);
    assert.equal(el.querySelector('.backend-stop').disabled,true);
    renderBackendControls(el,settings,{},backend==='napcat'?'snowluma':'napcat',call,refresh);
    assert.equal(el.querySelector('.backend-start').disabled,true);
  }
  assert.equal(refreshes,4);
  renderBackendControls(el,{qq_connection_mode:'open_platform'},{},'napcat',call,refresh);
  assert.equal(el.hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node, "-"], input=source + harness, text=True,
                            encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
