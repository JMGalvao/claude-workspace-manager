#!/usr/bin/env python3
"""Claude Workspace Manager: restore a whole desktop layout of Claude Code sessions with one click."""
import ast, json, os, re, shlex, shutil, signal, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import trust

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "workspace.json"
PORT = int(os.environ.get("CLAUDE_WS_PORT", "8765"))
HOST = "127.0.0.1"
MODES = {"continue": ["--continue"], "resume": ["--resume"], "new": []}
IS_MAC = sys.platform == "darwin"
AUTOSTART = (Path.home() / "Library/LaunchAgents/com.claudeworkspace.restore.plist" if IS_MAC
             else Path.home() / ".config/autostart/claude-workspace.desktop")
SESS_DIR = Path.home() / ".claude/sessions"
BUS = "org.claudews.Bridge"
OBJ = "/org/claudews/Bridge"
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "-bash", "-zsh"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

RESTORE = {"running": False, "done": 0, "total": 0, "current": "", "log": [], "placed": True}
RESTORE_LOCK = threading.Lock()
CFG_LOCK = threading.RLock()   # serialises read-modify-write of workspace.json (HTTP handlers + layout watcher)
OPT_OUT = set()               # pids the user took out of the layout by hand while they keep running
SYNC_HOLD = [0.0]              # the watcher does not read window positions before this time
EXPECTED_GONE = {}             # pid -> when we closed it ourselves (stop/restart/update): not a user closing it
SHUTDOWN = threading.Event()   # set when the server itself is told to stop (logout, shutdown, restart)


# ---------- config ----------
def load():
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text())
            for p in cfg.get("projects", []):
                p.setdefault("workspace", 1)
                p.setdefault("session_id", "")
            cfg.setdefault("settings", {}).setdefault("notify", True)
            cfg["settings"].setdefault("panel_show_stopped", True)
            cfg["settings"].setdefault("auto_trust", False)  # opt-in: pre-accept the "trust this folder" dialog
            cfg["settings"].setdefault("notify_seconds", 20)  # how long the banner stays at the top
            return cfg
        except Exception:
            pass
    return {"profiles": ["Geral"], "projects": [], "settings": {"notify": True}}


def save(cfg):
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    tmp.replace(CONFIG)


def real(path):
    return os.path.realpath(os.path.expanduser(path))


# ---------- live sessions (from Claude Code's own session files) ----------
def _ps(pid, field):
    """macOS has no /proc: ask ps for one field of one process."""
    r = subprocess.run(["ps", "-o", f"{field}=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
    return r.stdout.strip() if r.returncode == 0 else ""


def _cmdline(pid):
    if IS_MAC:
        out = _ps(pid, "args")
        try:
            return shlex.split(out) if out else None
        except ValueError:
            return out.split() or None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    return [x.decode(errors="ignore") for x in raw if x]


def _flags_from(args):
    """Launch flags of a running claude, minus resume/continue and its id."""
    out, skip = [], False
    for x in args[1:]:
        if skip:
            skip = False
            if UUID_RE.match(x):
                continue
        if x in ("--resume", "-r", "--continue", "-c"):
            skip = x in ("--resume", "-r")
            continue
        if x.startswith("-"):
            out.append(x)
    return " ".join(shlex.quote(x) for x in out)


def live_sessions():
    out = []
    if not SESS_DIR.exists():
        return out
    for f in SESS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            pid = int(d["pid"])
        except Exception:
            continue
        args = _cmdline(pid)
        if not args or not any(x == "claude" or x.endswith("/claude") for x in args[:2]):
            continue
        if d.get("kind", "interactive") != "interactive":
            continue
        out.append({"pid": pid, "session_id": d.get("sessionId", ""), "name": d.get("name") or "",
                    "cwd": os.path.realpath(d.get("cwd", "")), "version": d.get("version"),
                    "status": d.get("status", ""), "flags": _flags_from(args)})
    return out


def match_projects(cfg, live):
    """Map project id -> live session. Session id first, then (cwd, name), then folder-only entries."""
    used, res = set(), {}
    projs = cfg["projects"]
    for p in projs:
        sid = p.get("session_id")
        if sid:
            for s in live:
                if s["session_id"] == sid and s["pid"] not in used:
                    res[p["id"]] = s
                    used.add(s["pid"])
                    break
    for p in projs:
        if p["id"] in res or not p.get("session_id"):
            continue
        for s in live:  # session id changed (e.g. /clear): same folder and same name
            if s["pid"] not in used and s["cwd"] == real(p["path"]) and s["name"] and s["name"] == p["name"]:
                res[p["id"]] = s
                used.add(s["pid"])
                p["session_id"] = s["session_id"]
                break
    for p in projs:
        if p["id"] in res or p.get("session_id"):
            continue
        for s in live:
            if s["pid"] not in used and s["cwd"] == real(p["path"]):
                res[p["id"]] = s
                used.add(s["pid"])
                break
    return res


def installed_version():
    exe = shutil.which("claude")
    return os.path.basename(os.path.realpath(exe)) if exe else None


# ---------- closing a session and its terminal ----------
def _alive(pid):
    if IS_MAC:
        st = _ps(pid, "stat")
        return bool(st) and not st.startswith("Z")
    try:
        st = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return st != "Z"
    except OSError:
        return False


def _ppid(pid):
    if IS_MAC:
        try:
            return int(_ps(pid, "ppid"))
        except ValueError:
            return 0
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except Exception:
        return 0


def _comm(pid):
    if IS_MAC:
        return os.path.basename(_ps(pid, "comm"))
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _is_terminal(comm):
    return ("terminal" in comm.lower() or "iterm" in comm.lower()
            or comm in {"xterm", "kitty", "alacritty", "kgx", "tilix", "konsole", "wezterm-gui", "ptyxis", "login", "Terminal"})


def terminal_shell(pid):
    """PID of the interactive shell that hosts this claude inside a terminal window, else None."""
    sh = _ppid(pid)
    if sh > 1 and _comm(sh) in SHELLS and _is_terminal(_comm(_ppid(sh))):
        return sh
    return None


def terminate(pid):
    """Stop claude, then close its terminal tab/window (shell is its parent when started by hand)."""
    shell = terminal_shell(pid)
    EXPECTED_GONE[pid] = time.time()
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    end = time.time() + 6
    while time.time() < end and _alive(pid):
        time.sleep(0.2)
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.3)
    if shell and _alive(shell) and _comm(shell) in SHELLS:
        time.sleep(0.4)
        if _alive(shell):
            try:
                os.kill(shell, signal.SIGHUP)
            except ProcessLookupError:
                pass


def terminate_many(pids):
    ts = [threading.Thread(target=terminate, args=(p,)) for p in pids]
    [t.start() for t in ts]
    [t.join() for t in ts]


def wait_gone_pid(pid, timeout=10):
    end = time.time() + timeout
    while time.time() < end and _alive(pid):
        time.sleep(0.2)


# ---------- GNOME bridge (window placement) ----------
def bridge(method, *args):
    cmd = ["gdbus", "call", "--session", "--dest", BUS, "--object-path", OBJ, "--method", f"{BUS}.{method}", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "bridge error")
    out = r.stdout.strip()
    try:
        return ast.literal_eval(out)
    except Exception:
        return out


def bridge_ok():
    try:
        bridge("WorkspaceCount")
        return True
    except Exception:
        return False


def list_windows():
    try:
        return json.loads(bridge("ListWindows")[0])
    except Exception:
        return []


# ---------- window <-> session ----------
_TITLES = {}


def ai_title(sid):
    """Auto-generated title Claude Code writes to the transcript (what unnamed sessions show in the window title)."""
    base = Path.home() / ".claude/projects"
    for f in base.glob(f"*/{sid}.jsonl"):
        try:
            st = f.stat()
            key = (str(f), st.st_mtime_ns)
            if key in _TITLES:
                return _TITLES[key]
            with f.open("rb") as fh:
                fh.seek(max(0, st.st_size - 400_000))
                chunk = fh.read().decode("utf-8", "ignore")
            m = re.findall(r'"aiTitle":"((?:[^"\\]|\\.)*)"', chunk)
            title = json.loads('"' + m[-1] + '"') if m else ""
            _TITLES[key] = title
            return title
        except Exception:
            continue
    return ""


def norm_title(t):
    return re.sub(r"^[^\w]+", "", t or "").strip().casefold()


def session_windows(live):
    """session_id -> terminal window, matched by title (session name first, then auto title)."""
    wins = [w for w in list_windows() if "terminal" in w.get("cls", "").lower()]
    out, used = {}, set()
    for key in ("name", "title"):
        for s in live:
            if s["session_id"] in out:
                continue
            want = norm_title(s["name"] if key == "name" else ai_title(s["session_id"]))
            if not want:
                continue
            for w in wins:
                if w["id"] not in used and norm_title(w["title"]) == want:
                    out[s["session_id"]] = w
                    used.add(w["id"])
                    break
    return out


def focus_window(w):
    bridge("ActivateWindow", f"uint64 {w['id']}")


def capture_layout(cfg):
    """Read where the session windows are right now and store it as the workspace of each project."""
    live = live_sessions()
    m = match_projects(cfg, live)
    sw = session_windows(live)
    got, miss = [], []
    for p in cfg["projects"]:
        s = m.get(p["id"])
        if not s:
            continue
        w = sw.get(s["session_id"])
        if w and w.get("ws", -1) >= 0:
            p["workspace"] = ws_number(cfg, int(w["ws"]))
            got.append(p["name"])
        else:
            miss.append(p["name"])
    return got, miss


# ---------- "session finished" notifications ----------


NOTIFY_LOG = HERE / "notifier.log"


def nlog(msg):
    """Append one line to notifier.log (kept small) so a missing notification can be explained."""
    try:
        if NOTIFY_LOG.exists() and NOTIFY_LOG.stat().st_size > 100_000:
            NOTIFY_LOG.write_text("")
        with NOTIFY_LOG.open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _applescript_str(x):
    return '"' + str(x).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _notify(name, sid):
    if IS_MAC:  # no click action available through osascript
        try:
            subprocess.run(["osascript", "-e", f"display notification {_applescript_str('Terminou.')} "
                            f"with title {_applescript_str(name)} subtitle \"Claude Workspace\""], timeout=10)
            nlog(f"entregue: {name}")
        except Exception as e:
            nlog(f"ERRO a notificar {name}: {e}")
        return
    try:
        r = subprocess.run(
            ["notify-send", "--app-name=Claude Workspace", "--icon=utilities-terminal",
             "-A", "default=Ir para a sessão", f"{name} terminou", "Clica para ir para a sessão."],
            capture_output=True, text=True, timeout=180)
        nlog(f"entregue: {name} (resposta: {r.stdout.strip() or 'sem clique'})")
        if r.stdout.strip() == "default":  # click on the notification body (GNOME shows no button for it)
            for w in session_windows([s for s in live_sessions() if s["session_id"] == sid]).values():
                focus_window(w)
    except Exception as e:
        nlog(f"ERRO a notificar {name}: {e}")


def notifier_loop():
    seen = {}  # session_id -> (status, busy_since)
    while True:
        time.sleep(2)
        try:
            live = live_sessions()
            enabled = load().get("settings", {}).get("notify", True)
            now = time.time()
            for s in live:
                sid, st = s["session_id"], s["status"]
                prev = seen.get(sid)
                busy_since = prev[1] if prev and prev[0] == "busy" else None
                if st == "busy" and busy_since is None:
                    busy_since = now
                if prev and prev[0] == "busy" and st == "idle":
                    label = s["name"] or os.path.basename(s["cwd"])
                    worked = now - prev[1]
                    if not enabled:
                        nlog(f"ignorado: {label} (notificacoes desligadas)")
                    else:
                        w = session_windows([s]).get(sid)
                        if w and w.get("focus"):
                            nlog(f"ignorado: {label} (janela com o foco, trabalhou {worked:.0f}s)")
                        else:
                            nlog(f"a notificar: {label} (trabalhou {worked:.0f}s, janela {'encontrada' if w else 'NAO encontrada'})")
                            threading.Thread(target=_notify, args=(label, sid), daemon=True).start()
                seen[sid] = (st, busy_since)
            for sid in set(seen) - {s["session_id"] for s in live}:
                seen.pop(sid, None)
        except Exception as e:
            nlog(f"ERRO no notificador: {e!r}")


# ---------- layout = the sessions that are active ----------
# `enabled` means "is in the layout". It follows reality: a new session joins by itself, and one leaves only when
# the user closes it (X, /exit) or stops it in the app. Shutdown, logout, restart and update never remove anything.
def shutting_down():
    if SHUTDOWN.is_set():
        return True
    if IS_MAC:
        return False
    try:  # logind raises this flag as soon as a poweroff/reboot begins, before processes are killed
        r = subprocess.run(["gdbus", "call", "--system", "--dest", "org.freedesktop.login1",
                            "--object-path", "/org/freedesktop/login1", "--method", "org.freedesktop.DBus.Properties.Get",
                            "org.freedesktop.login1.Manager", "PreparingForShutdown"],
                           capture_output=True, text=True, timeout=3)
        return "true" in r.stdout.lower()
    except Exception:
        return False


def layout_loop():
    seen = {}       # pid -> project id, for sessions seen running as part of the layout
    gone = {}       # pid -> consecutive polls without it
    moved = {}      # project id -> (workspace number seen, polls seen there): debounce for the live layout
    placing = {}    # session_id -> first seen, new sessions waiting for their window to learn the workspace
    while True:
        time.sleep(2)
        try:
            now = time.time()
            for pid in [p for p, t in EXPECTED_GONE.items() if now - t > 120]:
                EXPECTED_GONE.pop(pid, None)
            if shutting_down():
                seen.clear(); gone.clear()
                continue
            with CFG_LOCK:
                cfg = load()
                raw = live_sessions()
                OPT_OUT.intersection_update({x["pid"] for x in raw})
                live = [x for x in raw if x["pid"] not in EXPECTED_GONE and x["pid"] not in OPT_OUT]
                before = json.dumps(cfg, sort_keys=True)
                sids, keep = set(), []
                for p in cfg["projects"]:      # one entry per session id
                    if p.get("session_id") and p["session_id"] in sids:
                        continue
                    sids.add(p.get("session_id"))
                    keep.append(p)
                cfg["projects"] = keep
                m = match_projects(cfg, live)
                # a stopped entry whose session is running again is active again
                for p in cfg["projects"]:
                    x = m.get(p["id"])
                    if x:
                        seen[x["pid"]] = p["id"]
                        gone.pop(x["pid"], None)
                        if not p.get("enabled", True):
                            p["enabled"] = True
                        if x["name"] and x["name"] != p["name"]:   # renamed inside Claude Code (/rename)
                            p["name"] = x["name"]
                # sessions that are not tracked yet join the layout
                if untracked(cfg, live):
                    known = {p["id"] for p in cfg["projects"]}
                    import_live(cfg, live)
                    for p in cfg["projects"]:
                        if p["id"] not in known and p.get("session_id"):
                            placing[p["session_id"]] = now
                # new sessions: copy the workspace of the window they are in
                for sid in list(placing):
                    if now - placing[sid] > 30:
                        placing.pop(sid)
                        continue
                    w = session_windows([x for x in live if x["session_id"] == sid]).get(sid)
                    if w and w.get("ws", -1) >= 0:
                        for p in cfg["projects"]:
                            if p.get("session_id") == sid:
                                p["workspace"] = ws_number(cfg, int(w["ws"]))
                        placing.pop(sid)
                # live layout: the workspace of a running session is the one its window is in right now
                if not RESTORE["running"] and now > SYNC_HOLD[0] and now - RESTORE.get("ended", 0) > 15:
                    wins = session_windows(live)
                    for p in cfg["projects"]:
                        x = m.get(p["id"])
                        w = wins.get(x["session_id"]) if x else None
                        if not w or w.get("ws", -1) < 0:
                            moved.pop(p["id"], None)
                            continue
                        num = ws_number(cfg, int(w["ws"]))
                        if num == int(p["workspace"]):
                            moved.pop(p["id"], None)
                        elif moved.get(p["id"], (None, 0))[0] == num:
                            if moved[p["id"]][1] >= 1:     # seen at the new place on two polls in a row
                                p["workspace"] = num
                                moved.pop(p["id"], None)
                            else:
                                moved[p["id"]] = (num, moved[p["id"]][1] + 1)
                        else:
                            moved[p["id"]] = (num, 0)
                # sessions that disappeared: the user closed them
                alive = {x["pid"] for x in live} | set(EXPECTED_GONE)
                for pid in list(seen):
                    if pid in alive:
                        continue
                    gone[pid] = gone.get(pid, 0) + 1
                vanished = [pid for pid in seen if gone.get(pid, 0) >= 3]
                if len(vanished) == 1 or (vanished and len(seen) > len(vanished)):
                    for pid in vanished:
                        p = find(cfg, seen[pid])
                        if p and not any(seen.get(q) == p["id"] for q in alive):
                            p["enabled"] = False
                # several gone at once with none left (logout, crash): keep them in the layout
                for pid in vanished:
                    seen.pop(pid, None)
                    gone.pop(pid, None)
                if json.dumps(cfg, sort_keys=True) != before:
                    save(cfg)
        except Exception as e:
            nlog(f"ERRO no vigia do layout: {e!r}")


# ---------- launching ----------
def build_cmd(proj):
    parts = ["claude"]
    if proj.get("flags"):
        parts += shlex.split(proj["flags"])
    if proj.get("session_id"):
        parts += ["--resume", proj["session_id"]]
    else:
        parts += MODES.get(proj.get("mode", "continue"), [])
    return " ".join(shlex.quote(x) for x in parts)


def terminal_cmd(title, cwd, inner):
    if IS_MAC:  # Terminal.app: new window running the command in a login shell, then zoomed
        script = ('tell application "Terminal"\n activate\n'
                  f' do script {_applescript_str(f"cd {shlex.quote(cwd)} && {inner}")}\n'
                  f' set custom title of selected tab of front window to {_applescript_str(title)}\n'
                  ' set zoomed of front window to true\nend tell')
        return ["osascript", "-e", script]
    if shutil.which("gnome-terminal"):
        return ["gnome-terminal", "--maximize", f"--title={title}", f"--working-directory={cwd}", "--", "bash", "-lc", inner]
    return ["x-terminal-emulator", "-T", title, "-e", "bash", "-lc", inner]


def ws_index_map(cfg, also=()):
    """Workspace numbers of the sessions in the layout are ordered ranks (1,2,5 -> indexes 0,1,2): avoids empty gaps
    in GNOME. Stopped sessions out of the layout do not take a place. `also` adds numbers (a session being started)."""
    nums = sorted({int(p.get("workspace", 1)) for p in cfg["projects"] if p.get("enabled", True)}
                  | {int(n) for n in also})
    return {n: i for i, n in enumerate(nums)}


def ws_number(cfg, index):
    """The workspace number of a window is simply its GNOME workspace index + 1 (no feedback from other numbers)."""
    return int(index) + 1


def apply_live_layout(cfg):
    """Move the windows of running sessions to the workspace the layout says (after a move/reorder in the app)."""
    live = live_sessions()
    m = match_projects(cfg, live)
    wins = session_windows(live)
    imap = ws_index_map(cfg, also={int(p["workspace"]) for p in cfg["projects"] if p["id"] in m})
    for p in cfg["projects"]:
        x = m.get(p["id"])
        w = wins.get(x["session_id"]) if x else None
        want = imap.get(int(p["workspace"]))
        if w and want is not None and w.get("ws", -1) != want:
            try:
                bridge("MoveToWorkspace", f"uint64 {w['id']}", f"int32 {want}")
            except Exception:
                pass
    SYNC_HOLD[0] = time.time() + 5


def open_window(proj, ws_index, placed):
    cwd = real(proj["path"])
    if load().get("settings", {}).get("auto_trust"):  # opt-in: skip the "do you trust this folder" dialog
        trust.ensure(cwd)
    before = {w["id"] for w in list_windows()} if placed else set()
    subprocess.Popen(terminal_cmd(proj["name"], cwd, build_cmd(proj)), cwd=cwd,
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not placed:
        return False
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.25)
        new = [w for w in list_windows() if w["id"] not in before and "terminal" in w["cls"].lower()]
        if new:
            try:
                bridge("MoveToWorkspace", f"uint64 {new[0]['id']}", f"int32 {ws_index}")
                return True
            except Exception as e:
                RESTORE["err"] = str(e).replace("GDBus.Error:", "")[:160]
                return False
    RESTORE["err"] = "janela nova não apareceu em 15 s"
    return False


def start(proj, cfg=None):
    cfg = cfg or load()
    if not os.path.isdir(real(proj["path"])):
        return False, f"Pasta não existe: {real(proj['path'])}"
    if proj["id"] in match_projects(cfg, live_sessions()):
        return True, "já a correr"
    idx = ws_index_map(cfg, also={int(proj.get("workspace", 1))}).get(int(proj.get("workspace", 1)), 0)
    ok = open_window(proj, idx, bridge_ok())
    return True, "iniciado" + (f" no workspace {proj.get('workspace', 1)}" if ok else "")


def stop(proj, cfg=None):
    cfg = cfg or load()
    s = match_projects(cfg, live_sessions()).get(proj["id"])
    if not s:
        return True, "não estava a correr"
    terminate(s["pid"])
    return True, "sessão e terminal fechados"


# ---------- one-click restore ----------
def _restore_job(profile, workspace, ids=None):
    cfg = load()
    imap = ws_index_map(cfg)
    order = {p["id"]: i for i, p in enumerate(cfg["projects"])}
    todo = [p for p in cfg["projects"] if p.get("enabled", True)
            and (not profile or p["profile"] == profile)
            and (workspace is None or int(p["workspace"]) == int(workspace))
            and (ids is None or p["id"] in ids)]
    todo.sort(key=lambda p: (imap[int(p["workspace"])], order[p["id"]]))
    placed = bridge_ok()
    RESTORE.update(running=True, done=0, total=len(todo), current="", log=[], placed=placed)
    running = match_projects(cfg, live_sessions())
    try:
        for p in todo:
            RESTORE["current"] = p["name"]
            if not os.path.isdir(real(p["path"])):
                RESTORE["log"].append(f"{p['name']}: pasta em falta")
            elif p["id"] in running:
                RESTORE["log"].append(f"{p['name']}: já aberto, ignorado")
            else:
                RESTORE["err"] = ""
                ok = open_window(p, imap[int(p["workspace"])], placed)
                note = f" (workspace {p['workspace']})" if ok else (f" SEM workspace: {RESTORE['err']}" if placed else "")
                RESTORE["log"].append(f"{p['name']}: aberto{note}")
            RESTORE["done"] += 1
        if placed and todo:
            try:
                bridge("SwitchWorkspace", "int32 0")
            except Exception:
                pass
    finally:
        RESTORE.update(running=False, current="", ended=time.time())
        try:
            (HERE / "last-restore.json").write_text(json.dumps(
                {"when": time.strftime("%Y-%m-%d %H:%M:%S"), **{k: RESTORE[k] for k in ("done", "total", "placed", "log")}},
                indent=2, ensure_ascii=False))
        except OSError:
            pass


def restore(profile=None, workspace=None, ids=None):
    if not RESTORE_LOCK.acquire(blocking=False):
        return False
    RESTORE["running"] = True

    def run():
        try:
            _restore_job(profile, workspace, ids)
        finally:
            RESTORE_LOCK.release()
    threading.Thread(target=run, daemon=True).start()
    return True


# ---------- discovery / import ----------
def discover():
    """Folders already used with Claude Code."""
    base = Path.home() / ".claude/projects"
    found = {}
    if not base.exists():
        return []
    for d in base.iterdir():
        files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            continue
        cwd = None
        try:
            with files[0].open() as fh:
                for _, line in zip(range(60), fh):
                    try:
                        c = json.loads(line).get("cwd")
                    except Exception:
                        continue
                    if c:
                        cwd = c
                        break
        except Exception:
            continue
        if cwd and os.path.isdir(cwd):
            found[cwd] = max(found.get(cwd, 0), files[0].stat().st_mtime)
    return [{"path": p, "name": os.path.basename(p.rstrip("/")) or p, "last": t}
            for p, t in sorted(found.items(), key=lambda kv: -kv[1])]


def untracked(cfg, live):
    """Live sessions whose session id is not in the workspace yet (folder-only entries do not count).
    Two processes with the same session id (a restart overlapping its predecessor) are one session."""
    known = {p["session_id"] for p in cfg["projects"] if p.get("session_id")}
    seen, out = set(), []
    for x in live:
        if x["session_id"] in known or x["session_id"] in seen:
            continue
        seen.add(x["session_id"])
        out.append(x)
    return out


def import_live(cfg, live=None):
    """Add every running session not yet in the workspace as its own entry. Folder-only entries it covers are replaced."""
    live = live if live is not None else live_sessions()
    new = untracked(cfg, live)
    added = 0
    for s in new:
        folder = next((p for p in cfg["projects"] if not p.get("session_id") and real(p["path"]) == s["cwd"]), None)
        # a folder entry only lends its workspace/profile; the session gets its own entry
        prof = folder["profile"] if folder else cfg["profiles"][0]
        cfg["projects"].append({
            "id": uuid.uuid4().hex[:8], "name": s["name"] or os.path.basename(s["cwd"]) or "sessão",
            "path": s["cwd"], "session_id": s["session_id"], "mode": "continue", "flags": s["flags"],
            "profile": prof, "workspace": folder["workspace"] if folder else 1, "enabled": True})
        added += 1
    # drop folder-only entries now represented by named sessions
    covered = {s["cwd"] for s in new}
    cfg["projects"] = [p for p in cfg["projects"]
                       if not (not p.get("session_id") and real(p["path"]) in covered)]
    return added


# ---------- autostart ----------
def set_autostart(on, profile=None):
    """Login entry: run restore-on-login.sh, which waits for the server/bridge and then restores (no fixed delay)."""
    if not on:
        AUTOSTART.unlink(missing_ok=True)
        return
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    script = str(HERE / "restore-on-login.sh")
    if IS_MAC:
        args = "".join(f"<string>{xml_escape(x)}</string>" for x in ([script] + ([profile] if profile else [])))
        AUTOSTART.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
            '<key>Label</key><string>com.claudeworkspace.restore</string>'
            f'<key>ProgramArguments</key><array>{args}</array>'
            '<key>RunAtLoad</key><true/></dict></plist>\n')
        return
    exe = shlex.quote(script)
    arg = f" {shlex.quote(profile)}" if profile else ""
    AUTOSTART.write_text(
        "[Desktop Entry]\nType=Application\nName=Claude Workspace restore\n"
        f"Exec={exe}{arg}\nX-GNOME-Autostart-enabled=true\n")


def state():
    cfg = load()
    live = live_sessions()
    before = [p.get("session_id") for p in cfg["projects"]]
    m = match_projects(cfg, live)
    if before != [p.get("session_id") for p in cfg["projects"]]:
        save(cfg)
    latest = installed_version()
    for p in cfg["projects"]:
        s = m.get(p["id"])
        p.update(running=bool(s), pid=s["pid"] if s else None, exists=os.path.isdir(real(p["path"])),
                 version=s["version"] if s else None, busy=bool(s and s["status"] == "busy"),
                 outdated=bool(s and s["version"] and latest and s["version"] != latest))
    cfg["untracked"] = [{"name": s["name"] or os.path.basename(s["cwd"]), "cwd": s["cwd"]} for s in untracked(cfg, live)]
    cfg.update(latest=latest, autostart=AUTOSTART.exists(), bridge=bridge_ok(), bridge_expected=not IS_MAC, restore=dict(RESTORE),
               manager=str(HERE / "open-manager.sh"))
    return cfg


def find(cfg, pid):
    return next((p for p in cfg["projects"] if p["id"] == pid), None)


# ---------- http ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok_origin(self):
        if self.headers.get("Host", "") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return False
        o = self.headers.get("Origin")
        return o is None or o in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._ok_origin():
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html")
        if self.path == "/api/state":
            return self._send(200, state())
        if self.path == "/api/discover":
            have = {real(p["path"]) for p in load()["projects"]}
            return self._send(200, [d for d in discover() if real(d["path"]) not in have])
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._ok_origin():
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        with CFG_LOCK:
            self._post(data)

    def _post(self, data):
        cfg = load()
        path, msg = self.path, "ok"
        if path == "/api/project":
            try:
                wsn = max(1, int(data.get("workspace", 1)))
            except (TypeError, ValueError):
                wsn = 1
            sid = str(data.get("session_id", "")).strip()
            if sid and not UUID_RE.match(sid):
                return self._send(400, {"error": "ID de sessão inválido"})
            pr = {"name": str(data.get("name", "")).strip(), "path": str(data.get("path", "")).strip(),
                  "session_id": sid, "mode": data.get("mode") if data.get("mode") in MODES else "continue",
                  "flags": str(data.get("flags", "")).strip(), "profile": data.get("profile") or cfg["profiles"][0],
                  "workspace": wsn, "enabled": bool(data.get("enabled", True))}
            if not pr["name"] or not pr["path"]:
                return self._send(400, {"error": "nome e pasta obrigatórios"})
            if pr["profile"] not in cfg["profiles"]:
                cfg["profiles"].append(pr["profile"])
            ex = find(cfg, data.get("id"))
            trust.ensure(pr["path"])
            if ex:
                ex.update(pr)
            else:
                pr["id"] = uuid.uuid4().hex[:8]
                cfg["projects"].append(pr)
            save(cfg)
        elif path in ("/api/move", "/api/toggle"):
            ids = data.get("ids") or [data.get("id")]
            ps = [p for p in (find(cfg, i) for i in ids) if p]
            if not ps:
                return self._send(404, {"error": "projeto não encontrado"})
            for p in ps:
                if path == "/api/move":
                    p["workspace"] = max(1, int(data.get("workspace", 1)))
                else:
                    p["enabled"] = not p.get("enabled", True)
                    x = match_projects(cfg, live_sessions()).get(p["id"])
                    if x and not p["enabled"]:   # still running: keep it running but out of the layout
                        OPT_OUT.add(x["pid"])
                    elif x:
                        OPT_OUT.discard(x["pid"])
            save(cfg)
            if path == "/api/move":
                apply_live_layout(cfg)
            msg = f"{len(ps)} sessão(ões) atualizada(s)"
        elif path == "/api/ws-order":
            # `order` lists the current workspace numbers in their new left-to-right order
            order = [int(x) for x in data.get("order", [])]
            present = sorted({int(p["workspace"]) for p in cfg["projects"]})
            full = [n for n in order if n in present] + [n for n in present if n not in order]
            mapping = {old: i + 1 for i, old in enumerate(full)}
            for p in cfg["projects"]:
                p["workspace"] = mapping[int(p["workspace"])]
            save(cfg)
            apply_live_layout(cfg)
            msg = "Workspaces reordenados"
        elif path == "/api/focus":
            p = find(cfg, data.get("id"))
            live = live_sessions()
            s = match_projects(cfg, live).get(p["id"]) if p else None
            w = session_windows(live).get(s["session_id"]) if s else None
            if not w:
                return self._send(404, {"error": "não encontrei a janela desta sessão"})
            focus_window(w)
            msg = "janela ativada"
        elif path == "/api/capture-layout":
            got, miss = capture_layout(cfg)
            save(cfg)
            msg = f"{len(got)} sessão(ões) guardadas" + (f", sem janela: {', '.join(miss)}" if miss else "")
        elif path == "/api/settings":
            for k in ("notify", "panel_show_stopped", "auto_trust"):
                if k in data:
                    cfg.setdefault("settings", {})[k] = bool(data[k])
            if "notify_seconds" in data:
                cfg.setdefault("settings", {})["notify_seconds"] = max(4, min(120, int(data["notify_seconds"])))
            save(cfg)
        elif path == "/api/import-live":
            added = import_live(cfg)
            save(cfg)
            msg = f"{added} sessão(ões) importada(s)"
        elif path == "/api/delete":
            cfg["projects"] = [p for p in cfg["projects"] if p["id"] != data.get("id")]
            save(cfg)
        elif path == "/api/profile":
            name = str(data.get("name", "")).strip()
            if name and name not in cfg["profiles"]:
                cfg["profiles"].append(name)
                save(cfg)
        elif path == "/api/profile/delete":
            name = data.get("name")
            if name in cfg["profiles"] and len(cfg["profiles"]) > 1:
                cfg["profiles"].remove(name)
                for p in cfg["projects"]:
                    if p["profile"] == name:
                        p["profile"] = cfg["profiles"][0]
                save(cfg)
        elif path in ("/api/start", "/api/stop", "/api/restart"):
            p = find(cfg, data.get("id"))
            if not p:
                return self._send(404, {"error": "projeto não encontrado"})
            if path == "/api/restart":
                stop(p, cfg)
            fn = {"/api/start": start, "/api/stop": stop, "/api/restart": start}[path]
            ok, msg = fn(p, cfg) if path != "/api/restart" else fn(p)
            if not ok:
                return self._send(500, {"error": msg})
            p["enabled"] = path != "/api/stop"   # stop in the app leaves the layout; start/restart (re)joins it
            save(cfg)
        elif path == "/api/restart-outdated":
            latest = installed_version()
            prof = data.get("profile")
            m = match_projects(cfg, live_sessions())
            old = [(p, m[p["id"]]) for p in cfg["projects"] if p["id"] in m and p.get("enabled", True)
                   and (not prof or p["profile"] == prof)
                   and m[p["id"]]["version"] not in (None, latest)]
            skipped = 0
            if not data.get("force"):
                skipped = sum(1 for _, s in old if s["status"] == "busy")
                old = [(p, s) for p, s in old if s["status"] != "busy"]
            if not old:
                return self._send(200, {"ok": True, "message": "Nada para reiniciar" + (f" ({skipped} ocupadas ignoradas)" if skipped else "")})
            terminate_many([s["pid"] for _, s in old])
            if not restore(prof, None, {p["id"] for p, _ in old}):
                return self._send(409, {"error": "restauro já em curso"})
            msg = f"A reiniciar {len(old)} sessão(ões)" + (f", {skipped} ocupadas ignoradas" if skipped else "")
        elif path == "/api/restore":
            if not restore(data.get("profile"), data.get("workspace")):
                return self._send(409, {"error": "restauro já em curso"})
            msg = "A restaurar o workspace..."
        elif path == "/api/stop-all":
            prof = data.get("profile")
            live = live_sessions()
            m = match_projects(cfg, live)
            if prof:
                cand = [m[p["id"]] for p in cfg["projects"] if p["id"] in m and p["profile"] == prof]
            else:  # everything running, tracked or not
                cand = live
            pids = [x["pid"] for x in cand if x["status"] != "busy"]  # never kill one that is working
            busy = len(cand) - len(pids)
            terminate_many(pids)
            for p in cfg["projects"]:  # closed from the app: out of the layout
                if p["id"] in m and m[p["id"]]["pid"] in pids:
                    p["enabled"] = False
            save(cfg)
            msg = f"{len(pids)} sessão(ões) fechadas" + (f", {busy} a trabalhar ignoradas" if busy else "")
        elif path == "/api/autostart":
            set_autostart(bool(data.get("on")), data.get("profile"))
        else:
            return self._send(404, {"error": "not found"})
        self._send(200, {"ok": True, "message": msg})


def main():
    threading.Thread(target=notifier_loop, daemon=True).start()
    threading.Thread(target=layout_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), H)

    def on_term(*_):  # logout / shutdown / service restart: from now on nothing leaves the layout
        SHUTDOWN.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, on_term)
    print(f"Claude Workspace em http://localhost:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
