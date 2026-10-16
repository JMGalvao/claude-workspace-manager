import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// Thin loader: copies bridge.js to a name that includes its mtime and imports that copy,
// because the shell caches ES modules by URL and never re-evaluates an edited file.
export default class ClaudeWsBridgeLoader extends Extension {
    enable() {
        this._on = true;
        this._load().catch(e => console.error(`claude-workspace-bridge: ${e}`));
    }

    async _load() {
        const src = Gio.File.new_for_path(`${this.path}/bridge.js`);
        const info = src.query_info('time::modified', Gio.FileQueryInfoFlags.NONE, null);
        const stamp = info.get_attribute_uint64('time::modified');
        const dir = `${GLib.get_user_cache_dir()}/claude-ws-bridge`;
        GLib.mkdir_with_parents(dir, 0o755);
        const dst = Gio.File.new_for_path(`${dir}/bridge-${stamp}.js`);
        src.copy(dst, Gio.FileCopyFlags.OVERWRITE, null, null);
        const mod = await import(`file://${dst.get_path()}`);
        if (!this._on)
            return;
        this._bridge = new mod.Bridge();
        this._bridge.enable();
    }

    disable() {
        this._on = false;
        if (this._bridge) {
            this._bridge.disable();
            this._bridge = null;
        }
    }
}
