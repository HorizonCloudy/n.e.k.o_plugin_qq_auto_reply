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
    if (!elements.has(id)) elements.set(id,{style:{},value:'',textContent:'',checked:false});
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
"""
    code = harness + source + function(status, "applyMode") + function(console, "onBackendChange") + function(console, "applyBackendUI") + checks
    result = subprocess.run([node, "-"], input=code, text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
