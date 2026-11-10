"""Layout changes keep collection and binding settings on the parameter page."""
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from _ui_source import fn_body

STATIC = Path(__file__).resolve().parents[1] / 'static'


class Layout(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.ids = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        node = (tag, attrs.get('id', ''))
        if node[1]:
            if node[1] in {'cfg-sticker-collect', 'config-companion-context', 'config-backend-process-controls'}:
                assert node[1] not in self.ids, f'duplicate id: {node[1]}'
            self.ids[node[1]] = [item[1] for item in self.stack]
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def test_settings_card_locations_and_order():
    html = (STATIC / 'napcat.html').read_text(encoding='utf-8')
    layout = Layout(html)
    for field in ('cfg-sticker-collect', 'config-companion-context'):
        assert 'page-config-params' in layout.ids[field]
    assert 'page-sticker' in layout.ids['sticker-table']
    for field in ('cfg-backend', 'config-backend-process-controls', 'qr-img'):
        assert 'page-config-connection' in layout.ids[field]
    assert html.index('id="cfg-backend"') < html.index('id="config-backend-process-controls"') < html.index('id="qr-img"')
    for field, ancestors in layout.ids.items():
        if field.startswith('page-'):
            assert 'content' in ancestors


@pytest.mark.parametrize('has_params', [True, False])
def test_binding_mount_and_params_entry(has_params):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    script = (STATIC / 'plugin-tools.js').read_text(encoding='utf-8')
    switch_sub = fn_body((STATIC / 'napcat.html').read_text(encoding='utf-8'), 'switchSub')
    harness = r"""
const assert = require('node:assert/strict');
const elements = {};
let mounts = 0;
const host = {set innerHTML(markup) {
  mounts++;
  assert.ok(markup.includes('id="cc-card"'));
  elements['cc-card'] = {parent:host};
  elements['pt-zone-available'] = {addEventListener(){}};
}};
elements['page-plugintools'] = host;
const target = {appendChild(card) {card.parent=this;}};
if (hasParams) elements['config-companion-context'] = target;
global.document = {
  readyState:'loading', addEventListener(){},
  getElementById(id) {return elements[id] || null;},
  querySelectorAll() {return [];}
};
global.window = {};
"""
    checks = r"""
window.ptMountPluginTools();
assert.equal(elements['cc-card'].parent, hasParams ? target : host);
window.ptMountPluginTools();
assert.equal(mounts, 1);
let currentPage='config', currentSub='', loads=0;
function ccLoad(){loads++;}
elements.content={scrollTop:100};
elements['page-config-params']={classList:{add(){}}};
switchSub('params');
assert.equal(loads, 1);
switchSub('connection');
assert.equal(loads, 1);
"""
    result = subprocess.run(
        [node, '-e', 'const hasParams=' + json.dumps(has_params) + ';\n' + harness + script
         + '\nfunction switchSub(sub){' + switch_sub + '}\n' + checks],
        capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr

