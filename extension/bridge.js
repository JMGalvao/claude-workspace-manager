import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Soup from 'gi://Soup?version=3.0';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import {QuickToggle} from 'resource:///org/gnome/shell/ui/quickSettings.js';

const BUS_NAME = 'org.claudews.Bridge';
const OBJ_PATH = '/org/claudews/Bridge';
const API = 'http://127.0.0.1:8765';
const EXT_DIR = `${GLib.get_home_dir()}/.local/share/gnome-shell/extensions/claude-workspace-bridge@local`;
// fallback only: the server reports the real path in /api/state (`manager`)
const MANAGER = `${GLib.get_home_dir()}/claude-workspace/open-manager.sh`;

const XML = `
<node>
  <interface name="org.claudews.Bridge">
    <method name="ListWindows"><arg type="s" direction="out" name="json"/></method>
    <method name="MoveToWorkspace">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="ws"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="ActivateWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="SwitchWorkspace"><arg type="i" direction="in" name="ws"/></method>
    <method name="WorkspaceCount"><arg type="i" direction="out" name="n"/></method>
  </interface>
</node>`;

// How long (seconds) our notification banners stay at the top. GNOME ignores the time an app asks
// for and always uses 4 s, so the bridge lengthens it for Claude Workspace notifications only.
// Updated from the server setting `notify_seconds`.
let notifySeconds = 20;
const isOurNotification = n => !!n && (n.source?.title === 'Claude Workspace' || /terminou$/.test(n.title ?? ''));

const C_OK = '#7fa88a', C_BUSY = '#a99bff', C_OFF = '#8a8697', C_WARN = '#ff7a5c';
const C_BUSY_BG = '#7a5fd1';   // working: muted violet
const C_ACTIVE_BG = '#5f8468';  // active: sage green (replaces the system blue)

// ---- tiny HTTP client for the local Claude Workspace server
class Api {
    constructor() {
        this._s = new Soup.Session({timeout: 6});
    }

    _send(method, path, body) {
        return new Promise((resolve, reject) => {
            const msg = Soup.Message.new(method, `${API}${path}`);
            if (body !== undefined) {
                const bytes = new GLib.Bytes(new TextEncoder().encode(JSON.stringify(body)));
                msg.set_request_body_from_bytes('application/json', bytes);
            }
            this._s.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, null, (s, res) => {
                try {
                    const bytes = s.send_and_read_finish(res);
                    const text = new TextDecoder().decode(bytes.get_data() ?? new Uint8Array());
                    resolve(text ? JSON.parse(text) : {});
                } catch (e) {
                    reject(e);
                }
            });
        });
    }

    get(path) { return this._send('GET', path); }
    post(path, body = {}) { return this._send('POST', path, body); }
    destroy() { this._s.abort(); }
}

const Indicator = GObject.registerClass(
class ClaudeWsIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'Claude Workspace', false);
        this._api = new Api();
        this._sig = '';
        this._act = null;         // session id whose action bar is open
        this._armed = '';         // "r:<id>" / "s:<id>": risky action waiting for a second click
        this._showStopped = true;
        this._icon = new St.Icon({
            gicon: Gio.FileIcon.new(Gio.File.new_for_path(`${EXT_DIR}/icons/claude-ws-symbolic.svg`)),
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);
        this.menu.box.style = 'min-width: 400px;';
        this.menu.connect('open-state-changed', (_m, open) => {
            if (open)
                this._refresh();
        });
        this._poll = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 4, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this._refresh();
    }

    _spawn(cmd) {
        try {
            GLib.spawn_command_line_async(cmd);
        } catch (e) {
            console.error(`claude-workspace: ${e}`);
        }
    }

    _redraw() {
        this._sig = '';
        this._refresh();
    }

    async _do(path, body, toast = false) {
        try {
            const r = await this._api.post(path, body);
            if (r.error)
                Main.notify('Claude Workspace', r.error);
            else if (toast && r.message)
                Main.notify('Claude Workspace', r.message);
        } catch (e) {
            Main.notify('Claude Workspace', `Servidor indisponível (${e.message})`);
        }
        this._redraw();
    }

    _iconButton(iconName, cb, color = null, keepOpen = false) {
        const icon = new St.Icon({icon_name: iconName});
        if (color)
            icon.style = `color: ${color};`;
        const b = new St.Button({
            style_class: 'icon-button', can_focus: true, reactive: true, track_hover: true, child: icon,
        });
        b.connect('clicked', () => {
            if (!keepOpen)
                this.menu.close();
            cb();
        });
        return b;
    }

    _textButton(label, cb, color = null) {
        const b = new St.Button({
            style_class: 'button', label, can_focus: true, reactive: true, x_expand: true, track_hover: true,
        });
        if (color)
            b.style = `color: ${color};`;
        b.connect('clicked', cb);
        return b;
    }

    _arm(key) {
        this._armed = key;
        this._redraw();
        GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 4, () => {
            if (this._armed === key) {
                this._armed = '';
                this._redraw();
            }
            return GLib.SOURCE_REMOVE;
        });
    }

    _tile(p) {
        const state = !p.running ? 'Parada' : p.busy ? 'A trabalhar' : 'Ativa';
        const tile = new QuickToggle({
            title: p.name,
            subtitle: `${state}${p.outdated ? ' · ↑ atualizar' : ''}`,
            iconName: 'utilities-terminal-symbolic',
            checked: !!p.running,
            toggleMode: false,
            x_expand: true,
        });
        tile.button_mask = St.ButtonMask.ONE | St.ButtonMask.TWO | St.ButtonMask.THREE;
        // busy = whole pill turns violet (not just an outline); the open action bar adds a white ring
        const bg = p.busy ? C_BUSY_BG : (p.running ? C_ACTIVE_BG : null);
        tile.style = 'min-width: 180px;' + (bg ? ` background-color: ${bg}; color: #ffffff;` : '') +
            (this._act === p.id ? ' box-shadow: 0 0 0 2px #ffffff;' : '');
        tile.connect('clicked', (_b, btn) => {
            if (btn === 2 || btn === 3) {      // middle / right click: show this session's actions
                this._act = this._act === p.id ? null : p.id;
                this._armed = '';
                this._redraw();
                return;
            }
            this.menu.close();
            this._do(p.running ? '/api/focus' : '/api/start', {id: p.id});
        });
        return tile;
    }

    _actionBar(p) {
        const bar = new St.BoxLayout({x_expand: true, style: 'spacing: 8px;'});
        const done = () => {
            this._act = null;
            this._armed = '';
        };
        if (!p.running) {
            bar.add_child(this._textButton('Iniciar', () => {
                this.menu.close();
                done();
                this._do('/api/start', {id: p.id});
            }));
            return bar;
        }
        bar.add_child(this._textButton('Ir para', () => {
            this.menu.close();
            done();
            this._do('/api/focus', {id: p.id});
        }));
        for (const [key, label, path, msg] of [['r', 'Reiniciar', '/api/restart', true], ['s', 'Parar', '/api/stop', true]]) {
            const armed = this._armed === `${key}:${p.id}`;
            const text = armed ? 'Confirmar?' : (p.busy ? `${label}…` : label);
            bar.add_child(this._textButton(text, () => {
                if (p.busy && !armed) {          // it is working right now: ask twice
                    this._arm(`${key}:${p.id}`);
                    return;
                }
                done();
                this._do(path, {id: p.id}, msg);
            }, armed || key === 's' ? C_WARN : null));
        }
        return bar;
    }

    _build(st) {
        const ps = st.projects ?? [];
        const run = ps.filter(p => p.running).length;
        const old = ps.filter(p => p.outdated).length;
        const rs = st.restore ?? {};
        const show = this._showStopped;
        const nums = [...new Set(ps.filter(p => p.workspace != null).map(p => p.workspace))].sort((a, b) => a - b);
        const rows = ps.map(p => ({...p, rank: nums.indexOf(p.workspace) + 1}));
        const here = global.workspace_manager.get_active_workspace_index();

        const root = new St.BoxLayout({vertical: true, x_expand: true, style: 'spacing: 14px; padding: 6px 4px;'});

        // header
        const head = new St.BoxLayout({x_expand: true, style: 'spacing: 8px;'});
        const title = new St.BoxLayout({vertical: true, x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        title.add_child(new St.Label({text: 'Claude Workspace', style: 'font-weight: bold;'}));
        title.add_child(new St.Label({
            text: rs.running ? `A restaurar ${rs.done}/${rs.total}` : `${run} de ${ps.length} ativas`,
            style: `color: ${rs.running ? C_BUSY : C_OFF}; font-size: 0.9em;`,
        }));
        head.add_child(title);
        if (old)
            head.add_child(this._iconButton('software-update-available-symbolic',
                () => this._do('/api/restart-outdated', {force: false}, true), C_WARN));
        if (run) {
            const armedAll = this._armed === 'all';
            head.add_child(this._iconButton('process-stop-symbolic', () => {
                if (!armedAll)
                    return this._arm('all');
                this._armed = '';
                this._do('/api/stop-all', {}, true);
            }, armedAll ? C_WARN : null, true));
        }
        head.add_child(this._iconButton(show ? 'view-reveal-symbolic' : 'view-conceal-symbolic', () => {
            this._showStopped = !show;
            this._do('/api/settings', {panel_show_stopped: !show});
        }, show ? null : C_BUSY, true));
        head.add_child(this._iconButton('view-refresh-symbolic', () => this._do('/api/restore', {}, true)));
        head.add_child(this._iconButton('document-save-symbolic', () => this._do('/api/capture-layout', {}, true)));
        head.add_child(this._iconButton('window-new-symbolic', () => this._spawn(this._manager ?? MANAGER)));
        root.add_child(head);

        // workspaces
        const grid = new St.BoxLayout({vertical: true, x_expand: true, style: 'spacing: 14px;'});
        let hidden = 0;
        const sections = nums.map((n, i) => ({title: `Workspace ${i + 1}`, i, group: rows.filter(p => p.workspace === n)}));
        sections.push({title: 'Inativas', i: -1, group: rows.filter(p => p.workspace == null)});   // no workspace until started
        sections.forEach(({title, i, group}) => {
            const alive = group.filter(p => p.running);
            const visible = show ? group : alive;
            hidden += group.length - visible.length;
            if (!visible.length)
                return;

            const block = new St.BoxLayout({vertical: true, x_expand: true, style: 'spacing: 8px;'});
            const hd = new St.BoxLayout({x_expand: true, style: 'spacing: 8px;'});
            hd.add_child(new St.Label({
                text: title, y_align: Clutter.ActorAlign.CENTER, style: 'font-weight: bold;',
            }));
            hd.add_child(new St.Label({
                text: `${alive.length}/${group.length}`, y_align: Clutter.ActorAlign.CENTER,
                style: `color: ${C_OFF}; font-size: 0.9em;`,
            }));
            if (i === here)
                hd.add_child(new St.Label({
                    text: 'aqui', y_align: Clutter.ActorAlign.CENTER,
                    style: `color: ${C_BUSY}; font-size: 0.85em; font-weight: bold;`,
                }));
            hd.add_child(new St.Widget({
                x_expand: true, y_align: Clutter.ActorAlign.CENTER,
                style: 'height: 1px; background-color: rgba(255,255,255,0.18); margin-left: 4px;',
            }));
            block.add_child(hd);

            for (let k = 0; k < visible.length; k += 2) {
                const pair = visible.slice(k, k + 2);
                const row = new St.BoxLayout({x_expand: true, style: 'spacing: 8px;'});
                pair.forEach(p => row.add_child(this._tile(p)));
                if (pair.length === 1)
                    row.add_child(new St.Widget({x_expand: true}));
                block.add_child(row);
                const open = pair.find(p => p.id === this._act);
                if (open)
                    block.add_child(this._actionBar(open));
            }
            grid.add_child(block);
        });
        if (!grid.get_n_children())
            grid.add_child(new St.Label({
                text: ps.length ? 'Nenhuma sessão ativa' : 'Sem sessões configuradas',
                style: `color: ${C_OFF};`,
            }));
        root.add_child(grid);

        const foot = hidden
            ? `${hidden} parada${hidden > 1 ? 's' : ''} oculta${hidden > 1 ? 's' : ''} · botão direito numa sessão: ações`
            : 'Botão direito numa sessão: ir para, reiniciar, parar';
        root.add_child(new St.Label({text: foot, style: `color: ${C_OFF}; font-size: 0.85em;`}));
        return root;
    }

    async _refresh() {
        let st;
        try {
            st = await this._api.get('/api/state');
        } catch (e) {
            this._icon.style = 'opacity: 0.5;';
            this._sig = 'down';
            this.menu.removeAll();
            const it = new PopupMenu.PopupMenuItem('Servidor desligado. Abrir gestor');
            it.connect('activate', () => this._spawn(MANAGER));
            this.menu.addMenuItem(it);
            return;
        }
        this._manager = st.manager ?? this._manager;
        notifySeconds = Number(st.settings?.notify_seconds) || notifySeconds;
        const ps = st.projects ?? [];
        const old = ps.filter(p => p.outdated).length;
        const busy = ps.filter(p => p.busy).length;
        const run = ps.filter(p => p.running).length;
        this._icon.style = old ? `color: ${C_WARN};` : busy ? `color: ${C_BUSY};` : !run ? 'opacity: 0.6;' : '';

        this._showStopped = st.settings?.panel_show_stopped !== false;
        const rs = st.restore ?? {};
        const sig = JSON.stringify([ps.map(p => [p.id, p.name, p.workspace, p.running, p.busy, p.outdated]),
            rs.running, rs.done, rs.total, global.workspace_manager.get_active_workspace_index(),
            this._showStopped, this._act, this._armed]);
        if (sig === this._sig)
            return;
        this._sig = sig;
        this.menu.removeAll();
        const holder = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        holder.add_child(this._build(st));
        this.menu.addMenuItem(holder);
    }

    destroy() {
        if (this._poll) {
            GLib.source_remove(this._poll);
            this._poll = 0;
        }
        this._api?.destroy();
        super.destroy();
    }
});

// Loaded by extension.js from a versioned copy, so edits apply with `gnome-extensions disable/enable`.
export class Bridge {
    _patchBanner() {
        const tray = Main.messageTray;
        if (!tray || this._origShowCompleted)
            return;
        const orig = tray._showNotificationCompleted;
        this._origShowCompleted = orig;
        tray._showNotificationCompleted = function () {
            const n = this._notification;
            if (n && n.urgency !== MessageTray.Urgency.CRITICAL && isOurNotification(n)) {
                this._updateNotificationTimeout(notifySeconds * 1000);
                return;
            }
            orig.call(this);
        };
    }

    _unpatchBanner() {
        if (this._origShowCompleted && Main.messageTray)
            Main.messageTray._showNotificationCompleted = this._origShowCompleted;
        this._origShowCompleted = null;
    }

    enable() {
        this._patchBanner();
        this._impl = Gio.DBusExportedObject.wrapJSObject(XML, this);
        this._impl.export(Gio.DBus.session, OBJ_PATH);
        this._owner = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null);
        try {
            this._indicator = new Indicator();
            Main.panel.addToStatusArea('claude-workspace', this._indicator);
        } catch (e) {
            console.error(`claude-workspace indicator: ${e}`);
        }
    }

    disable() {
        this._unpatchBanner();
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
        if (this._impl) {
            this._impl.unexport();
            this._impl = null;
        }
        if (this._owner) {
            Gio.bus_unown_name(this._owner);
            this._owner = 0;
        }
    }

    _windows() {
        return global.get_window_actors()
            .map(a => a.meta_window)
            .filter(w => w && w.get_window_type() === Meta.WindowType.NORMAL);
    }

    ListWindows() {
        return JSON.stringify(this._windows().map(w => ({
            id: w.get_id(),
            cls: w.get_wm_class() ?? '',
            title: w.get_title() ?? '',
            ws: w.get_workspace() ? w.get_workspace().index() : -1,
            pid: w.get_pid(),
            focus: w.has_focus(),
        })));
    }

    MoveToWorkspace(id, ws) {
        const win = this._windows().find(w => w.get_id() === id);
        if (!win)
            return false;
        const wm = global.workspace_manager;
        while (wm.n_workspaces <= ws)
            wm.append_new_workspace(false, global.get_current_time());
        win.change_workspace_by_index(ws, false);
        return true;
    }

    ActivateWindow(id) {
        const win = this._windows().find(w => w.get_id() === id);
        if (!win)
            return false;
        Main.activateWindow(win);
        return true;
    }

    SwitchWorkspace(ws) {
        const target = global.workspace_manager.get_workspace_by_index(ws);
        if (target)
            target.activate(global.get_current_time());
    }

    WorkspaceCount() {
        return global.workspace_manager.n_workspaces;
    }
}
