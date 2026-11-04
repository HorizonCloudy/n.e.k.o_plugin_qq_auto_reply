"""Execute the status page's login rendering and refresh code with a minimal DOM."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_login_display_and_qrcode_refresh():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to execute frontend behavior tests")
    html = (Path(__file__).resolve().parents[1] / "static/status.html").read_text(encoding="utf-8")
    backend = (Path(__file__).resolve().parents[1] / "static/onebot-backend.js").read_text(encoding="utf-8")
    script = html.split("let dashboardLoad = null;", 1)[1].split("/* ── 开放平台扫码绑定", 1)[0]
    harness = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {style:{}, dataset:{}, textContent:'', src:'',
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; }});
  return elements.get(id);
}};
let payload, calls = [], syncFailure = false, queryFailure = false;
async function call(entry, args) {
  calls.push(args.action);
  if (args.action === 'dashboard' && queryFailure) throw Error('disconnected');
  if (args.action === 'qrcode_sync') {
    if (syncFailure) throw Error('sync unavailable');
    return {...payload, runtime:{...payload.runtime, qrcode_url:'/fresh.png'}};
  }
  return payload;
}
function applyMode(mode) { return mode === 'open_platform'; }
function renderLists() {}
function showErr() {}
let dashboardLoad = null;
"""
    checks = r"""
(async () => {
  payload = {settings:{qq_connection_mode:'napcat'}, runtime:{qrcode_url:'/stale.png',auto_reply_running:true},
    login:{status:'online',self_id:'123',nickname:'测试猫'}};
  await load();
  assert.match(document.getElementById('napcat-login-title').textContent, /已登录.*测试猫.*123/);
  assert.equal(document.getElementById('qr').style.display, 'none');
  assert.equal(document.getElementById('qr').src, undefined);
  assert.equal(document.getElementById('btn-qr').disabled, true);
  assert.deepEqual(calls, ['dashboard']);
  assert.equal(document.getElementById('napcat-account-state').textContent, 'QQ 登录：已登录');
  assert.equal(document.getElementById('napcat-connection-state').textContent, '插件连接：已连接');
  payload.login = {status:'offline'};
  calls = [];
  await Promise.all([load(), load()]);
  assert.deepEqual(calls, ['dashboard','qrcode_sync']);
  assert.ok(document.getElementById('qr').src.startsWith('/fresh.png'));
  assert.equal(document.getElementById('napcat-login-title').textContent, '等待扫码登录');
  assert.equal(document.getElementById('btn-qr').disabled, false);
  syncFailure = true;
  await load();
  assert.match(document.getElementById('napcat-login-detail').textContent, /同步失败/);
  queryFailure = true;
  await load();
  assert.match(document.getElementById('napcat-login-title').textContent, /无法获取/);
  assert.equal(document.getElementById('qr').style.display, 'none');
  queryFailure = syncFailure = false;
  payload.settings.onebot_backend = 'snowluma';
  payload.runtime.login_method = 'native_qq';
  payload.runtime.backend_webui_url = 'http://127.0.0.1:5099/';
  calls = [];
  await load();
  assert.deepEqual(calls, ['dashboard']);
  assert.equal(document.getElementById('qr').style.display, 'none');
  assert.equal(document.getElementById('btn-qr').style.display, 'none');
  assert.match(document.getElementById('qr-empty').textContent, /本机 QQ/);
  assert.equal(document.getElementById('backend-webui').href, 'http://127.0.0.1:5099/');
  payload.settings.onebot_backend = 'napcat';
  payload.runtime.login_method = 'qrcode';
  payload.settings.qq_connection_mode = 'open_platform';
  calls = [];
  await load();
  assert.deepEqual(calls, ['dashboard']);
  renderNapcatLogin({napcat_running:true}, {status:'online', self_id:''});
  assert.equal(document.getElementById('napcat-login').dataset.state, 'pending');
  assert.match(document.getElementById('napcat-login-title').textContent, /正在确认/);
  assert.equal(document.getElementById('napcat-connection-state').textContent, '插件连接：等待连接');
  renderNapcatLogin({onebot_connected:true,auto_reply_running:true}, {status:'error',last_error:'查询超时'});
  assert.equal(document.getElementById('napcat-connection-state').textContent, '插件连接：已连接');
  assert.equal(document.getElementById('napcat-account-state').textContent, 'QQ 登录：尚未确认');
  assert.match(document.getElementById('napcat-login-detail').textContent, /查询超时/);
  renderNapcatLogin({}, {status:'offline'});
  assert.equal(document.getElementById('napcat-connection-state').textContent, '插件连接：未连接');
  assert.equal(document.getElementById('napcat-reply-state').textContent, '自动回复：未启动');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-"], input="globalThis.window = globalThis;\n" + backend + harness + script + checks,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
