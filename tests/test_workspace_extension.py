"""Run the real extension placement method with deterministic Mutter objects."""
import shutil
import subprocess
from pathlib import Path
import pytest


def test_extension_session_isolation(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is unavailable')
    source = (Path(__file__).parents[1] / 'gnome-extension/avd-workspaces@avd-feed-connect/extension.js').read_text()
    source = '\n'.join(line for line in source.splitlines() if not line.startswith('import '))
    source = source.replace('export default class', 'class')
    script = '''const assert = require('node:assert/strict');
const Meta = {WindowType: {NORMAL: 0}, prefs_get_dynamic_workspaces: () => true};
class Extension {}
const workspaces = [];
function workspace() {
    const ws = {windows: [], activations: 0, list_windows() {return this.windows},
        activate() {this.activations++}};
    workspaces.push(ws); return ws;
}
const local = workspace();
const trailing = workspace();
const windows = [];
global.get_current_time = () => 1;
global.get_window_actors = () => windows.map(meta_window => ({meta_window}));
global.workspace_manager = {get n_workspaces() {return workspaces.length},
    get_workspace_by_index: i => workspaces[i], get_active_workspace: () => local,
    append_new_workspace: () => workspace()};
function window(title) {
    const w = {ws: local, get_title: () => title, get_window_type: () => 0,
        get_workspace() {return this.ws},
        change_workspace(ws) {this.ws = ws; ws.windows.push(this)}};
    windows.push(w); return w;
}
''' + source + '''
const bridge = new AvdWorkspaces();
bridge._placed = new WeakSet();
const first = 'AVD-' + 'a'.repeat(32), second = 'AVD-' + 'b'.repeat(32);
assert.equal(bridge.PlaceWindow(first), false);
assert.equal(workspaces.length, 2); // Failed/missing window creates nothing.
const unrelated = window('Finance Desktop');
const one = window(first);
assert.equal(bridge.PlaceWindow('Finance Desktop'), false);
assert.equal(bridge.PlaceWindow(first), true);
assert.equal(one.ws, trailing);
assert.equal(unrelated.ws, local);
bridge.PlaceWindow(first); // Idempotent reconnect/focus.
assert.equal(workspaces.length, 2);
assert.equal(trailing.activations, 1);
const two = window(second);
bridge.PlaceWindow(second);
assert.notEqual(two.ws, one.ws);
assert.equal(workspaces.length, 3);
const extraMonitor = window(first);
bridge.PlaceWindow(first);
assert.equal(extraMonitor.ws, one.ws);
assert.equal(workspaces.length, 3);
Meta.prefs_get_dynamic_workspaces = () => false;
const fixed = window('AVD-' + 'c'.repeat(32));
assert.equal(bridge.PlaceWindow(fixed.get_title()), false);
assert.equal(workspaces.length, 3); // Never persist num-workspaces in fixed mode.
const spare = workspace();
assert.equal(bridge.PlaceWindow(fixed.get_title()), true);
assert.equal(fixed.ws, spare);
assert.equal(workspaces.length, 4);
'''
    path = tmp_path / 'test.cjs'
    path.write_text(script)
    subprocess.run([node, str(path)], check=True, capture_output=True, text=True)
