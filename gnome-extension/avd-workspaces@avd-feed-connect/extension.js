import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.github.shakeelosmani.AvdWorkspaces';
const OBJECT_PATH = '/io/github/shakeelosmani/AvdWorkspaces';
const XML = `<node><interface name="${BUS_NAME}">
<method name="PlaceWindow"><arg type="s" direction="in" name="title"/>
<arg type="b" direction="out" name="placed"/></method>
</interface></node>`;

export default class AvdWorkspaces extends Extension {
    enable() {
        this._placed = new WeakSet();
        this._object = Gio.DBusExportedObject.wrapJSObject(XML, this);
        this._owner = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            connection => this._object.export(connection, OBJECT_PATH), null, null);
    }

    PlaceWindow(title) {
        // Exact random session token: never match user-visible resource names,
        // sandbox PIDs, arbitrary titles, credential prompts, or remote apps.
        if (!/^AVD-[0-9a-f]{32}$/.test(title))
            return false;
        const windows = global.get_window_actors().map(actor => actor.meta_window)
            .filter(window => window.get_title() === title &&
                window.get_window_type() === Meta.WindowType.NORMAL);
        if (windows.length === 0)
            return false;
        if (windows.every(window => this._placed.has(window)))
            return true;
        // Reuse the first session window's workspace for multi-monitor windows.
        let workspace = windows.find(window => this._placed.has(window))?.get_workspace();
        if (!workspace) {
            const manager = global.workspace_manager;
            const last = manager.get_workspace_by_index(manager.n_workspaces - 1);
            if (Meta.prefs_get_dynamic_workspaces()) {
                workspace = last.list_windows().length === 0 && last !== manager.get_active_workspace()
                    ? last : manager.append_new_workspace(false, global.get_current_time());
            } else {
                // Mutter persists num-workspaces when appending in fixed mode.
                // Reserve an existing empty workspace instead of changing it.
                for (let i = manager.n_workspaces - 1; i >= 0; i--) {
                    const candidate = manager.get_workspace_by_index(i);
                    if (candidate !== manager.get_active_workspace() && candidate.list_windows().length === 0) {
                        workspace = candidate;
                        break;
                    }
                }
                if (!workspace)
                    return false;
            }
        }
        for (const window of windows) {
            if (!this._placed.has(window)) {
                window.change_workspace(workspace);
                this._placed.add(window);
            }
        }
        workspace.activate(global.get_current_time());
        return true;
    }

    disable() {
        if (this._owner)
            Gio.bus_unown_name(this._owner);
        this._object?.unexport();
        this._owner = null;
        this._object = null;
        this._placed = null;
        // GNOME handles empty dynamic workspaces. Never delete a workspace:
        // unrelated windows may have been moved into it by the user.
    }
}
