import json
import shutil
import subprocess
from pathlib import Path

import pytest
from _ui_source import fn_body
from plugin.plugins.qq_auto_reply import reply_presets, settings_schema

ROOT = Path(__file__).resolve().parents[1]


def test_real_controller_and_save_handlers():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required')
    html = (ROOT / 'static/napcat.html').read_text(encoding='utf-8')
    js = (ROOT / 'static/reply-presets.js').read_text(encoding='utf-8-sig')
    assert 'src="reply-presets.js?v=1"' in html
    assert '_settingsDirty=true;QQReplyPresets.select(this.value)' in html
    assert '[data-reply-custom-only][hidden]{display:none!important}' in html
    assert html.index('id="reply-preset-card"') < html.index('id="memory-card"')
    harness = r'''
const assert = require('node:assert/strict');
const elements = new Map(), sections = [{hidden:true}, {hidden:true}];
const document = {
    getElementById(id) {
        if (!elements.has(id)) elements.set(id, {value:'', checked:false, disabled:true, style:{}, setAttribute(){}});
        return elements.get(id);
    },
    querySelectorAll(selector) {return selector === '[data-reply-custom-only]' ? sections : [];}
};
const el = id => document.getElementById(id);
let _settingsDirty=false, _initiativeDirty=false;
let state={}, requests=[], persisted=false;
const applyInitiativeSettings=()=>{}, syncGroupMemoryDeps=()=>{}, applyAttentionHints=()=>{},
      updateDirectionUI=()=>{}, initiativeSettingsPayload=()=>({}),
      currentMode=()=> 'napcat', isMaskedTokenPlaceholder=()=>true,
      onebotBackend=()=>({directoryKey:'napcat_directory',windowKey:'show_napcat_window'}),
      toast=()=>{}, t=(key,fallback)=>fallback;
const floatVal=(id,fallback)=>el(id).value===''?fallback:Number(el(id).value);
const intVal=floatVal;
async function call(name, args) {requests.push(args);return {persisted};}
async function loadDashboard() {applySettings(saved);}
'''
    harness += '\n' + js + '\n'
    harness += 'function applySettings(s){' + fn_body(html, 'applySettings') + '}\n'
    harness += 'async function doSave(){' + fn_body(html, 'doSave') + '}\n'
    harness += 'const metadata=' + json.dumps(reply_presets.ui_metadata()) + ';\n'
    harness += 'const saved=' + json.dumps(settings_schema.defaults()) + ';\n'
    harness += r'''
(async()=>{
QQReplyPresets.configure(metadata);
applySettings(saved);
assert.equal(el('cfg-reply-preset').value,'balanced');
assert.equal(el('cfg-reply-preset').disabled,false);
assert(sections.every(s=>s.hidden));
QQReplyPresets.select('quiet'); _settingsDirty=true;
assert.equal(el('cfg-buffer-collect').value,10);
applySettings(saved); // Live polling must not undo the unsaved preview.
assert.equal(el('cfg-reply-preset').value,'quiet');
let payload=QQReplyPresets.payload({buffer_collect_window_seconds:99,group_memory_enabled:false});
assert.equal(payload.reply_behavior_preset,'quiet');
assert(!Object.hasOwn(payload,'buffer_collect_window_seconds'));
assert.equal(payload.group_memory_enabled,false);
QQReplyPresets.select('custom');
assert(sections.every(s=>!s.hidden));
assert.equal(QQReplyPresets.payload({}).attention_fall_rate,0.003);
el('cfg-buffer-collect').value='17';
el('cfg-att-emotion-multipliers').value='{bad json';
await doSave();
assert.equal(requests.length,0); // Custom validation still rejects malformed JSON.
QQReplyPresets.select('fast');
assert.equal(el('cfg-buffer-group').checked,false);
QQReplyPresets.select('custom');
assert.equal(el('cfg-buffer-collect').value,'17');
assert.equal(el('cfg-att-emotion-multipliers').value,'{bad json');
assert.equal(QQReplyPresets.payload({}).attention_fall_rate,0.003);
QQReplyPresets.select('fast');
await doSave(); // Hidden custom draft must not prevent saving a valid preset.
assert.equal(requests.length,1);
assert.equal(requests[0].reply_behavior_preset,'fast');
assert.equal(_settingsDirty,true); // Disk failure must keep the user's selection.
assert.equal(el('cfg-reply-preset').value,'fast');
QQReplyPresets.select('custom');
assert.equal(el('cfg-buffer-collect').value,'17');
QQReplyPresets.select('quiet');
persisted=true;
Object.assign(saved,metadata.values.quiet,{reply_behavior_preset:'quiet'});
await doSave();
assert.equal(_settingsDirty,false);
assert.equal(el('cfg-reply-preset').value,'quiet');
QQReplyPresets.select('custom'); // Successful save discards old, unsaved draft.
assert.equal(el('cfg-buffer-collect').value,10);
// Existing custom settings load without resetting internal or visible knobs.
_settingsDirty=false;
applySettings({...saved,reply_behavior_preset:'custom',attention_fall_rate:0.012,buffer_collect_window_seconds:9});
assert.equal(el('cfg-reply-preset').value,'custom');
assert.equal(el('cfg-buffer-collect').value,9);
assert.equal(QQReplyPresets.payload({}).attention_fall_rate,0.012);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run([node, '-'], input=harness, text=True, encoding='utf-8',
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
