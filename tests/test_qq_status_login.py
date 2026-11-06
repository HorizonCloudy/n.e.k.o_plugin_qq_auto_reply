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
    deploy_handler = html.split("const DEPLOY_TIMEOUT_MS =", 1)[1].split("document.getElementById('btn-apply').addEventListener", 1)[0]
    deploy_handler = "const DEPLOY_TIMEOUT_MS =" + deploy_handler
    harness = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {style:{}, dataset:{}, textContent:'', src:'',
    addEventListener(name, fn) { this[name] = fn; }, focus() {}, scrollIntoView() {},
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; }});
  return elements.get(id);
}};
let payload, calls = [], syncFailure = false, queryFailure = false, deployResponse;
async function call(entry, args) {
  calls.push(args.action);
  if (args.action === 'one_click') return await deployResponse;
  if (args.action === 'dashboard' && queryFailure) throw Error('disconnected');
  if (args.action === 'qrcode_sync') {
    if (syncFailure) throw Error('sync unavailable');
    return {...payload, runtime:{...payload.runtime, qrcode_url:'/fresh.png'}};
  }
  return payload;
}
function applyMode(mode) { return mode === 'open_platform'; }
function renderLists() {}
function renderBackendControls() {}
function showErr() {}
function stopLoginPoll() {}
function startLoginPoll() {}
function busy() {}
function deployLog() {}
let dashboardLoad = null;
"""
    checks = r"""
(async () => {
  const cards = ['backend-login-card', 'trust-list-card', 'sticker-upload-card', 'btn-qr'];
  function visible(expected) {
    for (const id of cards) assert.equal(document.getElementById(id).style.display, expected ? '' : 'none', id);
  }
  // Cached QR, installed files, or a started reply listener do not imply deployment success.
  payload = {settings:{}, runtime:{qrcode_url:'/stale.png',auto_reply_running:true}, login:{status:'offline'}};
  await load();
  visible(false);
  assert.deepEqual(calls, ['dashboard']);
  assert.equal(document.getElementById('qr').src, undefined);
  calls = [];
  payload = {settings:{qq_connection_mode:'napcat'}, runtime:{deployment_complete:true,qrcode_url:'/stale.png',auto_reply_running:true},
    login:{status:'online',self_id:'123',nickname:'测试猫'}};
  await load();
  visible(true);
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
  visible(true); // Ready before QQ login: the user needs the QR to finish login.
  beginDeploymentTransition();
  visible(false);
  calls = [];
  await load();
  visible(false);
  assert.deepEqual(calls, ['dashboard']);
  payload.runtime.deployment_complete = false; // Failed attempt must stay hidden on refresh.
  await endDeploymentTransition();
  visible(false);
  payload.runtime.deployment_complete = true;
  await load();
  visible(true);
  // A poll already in flight must not reveal the previous backend after a switch.
  const originalCall = call;
  let release;
  call = async () => new Promise(resolve => { release = resolve; });
  const oldPoll = load();
  beginDeploymentTransition();
  release(payload);
  await oldPoll;
  visible(false);
  call = originalCall;
  await endDeploymentTransition();
  visible(true);
  syncFailure = true;
  await load();
  assert.match(document.getElementById('napcat-login-detail').textContent, /同步失败/);
  queryFailure = true;
  await load();
  assert.match(document.getElementById('napcat-login-title').textContent, /无法获取/);
  assert.equal(document.getElementById('qr').style.display, 'none');
  visible(false);
  queryFailure = syncFailure = false;
  payload.settings.onebot_backend = 'snowluma';
  payload.runtime.login_method = 'native_qq';
  payload.runtime.backend_webui_url = 'http://127.0.0.1:5099/';
  calls = [];
  await load();
  assert.deepEqual(calls, ['dashboard']);
  assert.equal(document.getElementById('qr').style.display, 'none');
  assert.equal(document.getElementById('btn-qr').style.display, 'none');
  assert.equal(document.getElementById('backend-login-card').style.display, '');
  assert.equal(document.getElementById('trust-list-card').style.display, '');
  assert.match(document.getElementById('qr-empty').textContent, /本机 QQ/);
  assert.equal(document.getElementById('backend-webui').href, 'http://127.0.0.1:5099/');
  payload.settings.onebot_backend = 'napcat';
  payload.runtime.login_method = 'qrcode';
  payload.settings.qq_connection_mode = 'open_platform';
  calls = [];
  await load();
  assert.deepEqual(calls, ['dashboard']);
  visible(false); // Official binding card is separate and must remain reachable.
  payload.runtime.onebot_connected = true;
  await load();
  assert.equal(document.getElementById('trust-list-card').style.display, '');
  assert.equal(document.getElementById('sticker-upload-card').style.display, '');
  assert.equal(document.getElementById('btn-qr').style.display, 'none');
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
  // Exercise the actual deploy click handler, not only the rendering helpers.
  payload = {settings:{onebot_backend:'napcat'}, runtime:{deployment_complete:false}, login:{status:'offline'}};
  document.getElementById('uin').value = '123';
  await load();
  let finishDeploy;
  deployResponse = new Promise(resolve => { finishDeploy = resolve; });
  const deployment = document.getElementById('btn-deploy').click();
  visible(false);
  // Even a ready-looking poll cannot expose cards until the one_click call returns.
  payload.runtime.deployment_complete = true;
  await load();
  visible(false);
  finishDeploy({ok:true,logged_in:false,qrcode_ready:false});
  await deployment;
  visible(true);
  assert.equal(deploymentTransition, false);
  payload.runtime.deployment_complete = false;
  deployResponse = Promise.reject(Error('start failed'));
  await document.getElementById('btn-deploy').click();
  visible(false);
  await load();
  visible(false);
  assert.equal(deploymentTransition, false);

})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-"], input="globalThis.window = globalThis;\n" + backend + harness + script + deploy_handler + checks,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_post_deployment_cards_are_hidden_before_first_refresh():
    from html.parser import HTMLParser

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id") in expected:
                assert attrs.get("style") == "display:none"
                found.add(attrs["id"])

    expected = {"backend-login-card", "trust-list-card", "sticker-upload-card", "btn-qr"}
    found = set()
    Tags().feed((Path(__file__).resolve().parents[1] / "static/status.html").read_text(encoding="utf-8"))
    assert found == expected
