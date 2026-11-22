# Claude Workspace

Local manager for your Claude Code sessions. With one click (or at login) it opens one terminal window per session and **resumes each exact conversation** (`claude --resume <id>`). It has a web UI, and on GNOME it places each window on its own workspace, shows status in the top bar and notifies you when a session finishes.

The UI is in Portuguese.

## Platforms

| | Linux (GNOME, Wayland or X11) | macOS | Windows |
|---|---|---|---|
| Resume sessions (`--resume`) | yes | yes (experimental) | not yet |
| Web UI | yes | yes | not yet |
| Each window on its own workspace | yes (GNOME extension) | no (macOS has no API for this) | n/a |
| Top bar and clickable notification | yes | plain notification | n/a |
| Close sessions and terminals | yes | yes (experimental) | n/a |

Tested on Ubuntu 24.04 with GNOME 46. macOS support was written without access to a Mac: if something breaks, open an issue with the error.

## Install (Linux/GNOME)

Requirements: `python3` (standard library only), `claude` on PATH, `gnome-terminal`, `curl`.

```bash
git clone <this repository URL> claude-workspace
cd claude-workspace
python3 install.py        # picks the right installer for your system
```

(`./install.sh` does the same on Linux; `./install.sh --trust-folders` enables the trusted folders option, see Security.)

Log out and back in once (so GNOME loads the extension). Then open the manager with **Super+W** or from the applications menu.

## Install (macOS, experimental)

Requirements: `python3`, `claude` on PATH, Terminal.app.

```bash
python3 install.py        # or ./install-macos.sh
./open-manager.sh
```

macOS asks for Automation permission to control Terminal: accept it. The "workspaces" in the UI work as groups (the opening order), but windows are not placed into Spaces.

## Usage

1. Open the manager (`http://localhost:8765`).
2. Import the sessions you already have open ("Importar", meaning Import) or add projects by hand (folder, profile, workspace, flags).
3. "Restaurar workspace" (Restore workspace) opens everything. "Restaurar no login" (Restore at login) hooks it into system startup.

Your configuration lives in `workspace.json`, created on first install from `workspace.example.json`. **That file holds your paths and session ids and is in `.gitignore`: do not publish it.**

## The layout follows your active sessions

"No layout" (In layout, the checkbox on each session) is automatic:
- Any new session you open (in any terminal) joins the layout on its own, on the workspace where its window is.
- It only leaves the layout when you close it yourself (window X, `/exit`) or press "Parar" (Stop) in the app.
- Shutting down, logging out, restarting the server, "Reiniciar" (Restart) or "Reiniciar desatualizadas" (Restart outdated) do **not** remove anything from the layout: the next "Restaurar" (Restore) brings them all back.
- Each session's workspace is wherever its window is **right now** (GNOME): if you move it to another workspace, the app picks that up within a few seconds. It works the other way too: moving or reordering in the app moves the windows.
- If you untick a running session, it keeps running but outside the layout.

## Security

- The server only listens on `127.0.0.1` and rejects requests whose `Host` or `Origin` is not its own (this protects against web pages trying to talk to it).
- There is no authentication: **any program running as your user can use the API**, which can launch `claude` in any folder the caller asks for. That is the same trust level as one of your own terminals. Do not expose the port (no `0.0.0.0`, tunnels or port forwarding).
- Arguments are passed to the shell with `shlex.quote`, and session ids are validated as UUIDs.
- **Automatic folder trust** (optional, off by default): Claude Code asks "do you trust this folder?" the first time. With `"auto_trust": true` in `workspace.json` (and `./install.sh --trust-folders` for your regular terminal), this project writes `hasTrustDialogAccepted` to `~/.claude.json` for the folders you manage, skipping that prompt. This removes a Claude Code safeguard: only enable it if you trust the folders you list.
- The `--dangerously-skip-permissions` flag is only used if you add it yourself to a project's flags.
- This project sends nothing to the internet.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## How it works (overview)

- `server.py`: HTTP server (standard library) holding the API and the state. It reads live sessions from `~/.claude/sessions/*.json` (Claude Code's own files).
- `index.html`: the web UI.
- `extension/`: GNOME Shell extension (D-Bus bridge to move windows, top bar indicator, longer-lasting banner).
- `trust.py`: the optional trusted folders feature.
- `install.sh`, `install-macos.sh`, `open-manager.sh`, `restore-on-login.sh`: installation and startup.

## Known limitations

- On GNOME, windows are matched to sessions by window title, so identical names can get mixed up.
- Restarting the server in the middle of a restore leaves that restore incomplete.
- Windows is not supported yet (Windows has no stable public API for placing windows on virtual desktops; minimal support would only cover resuming sessions).

## License

MIT, see [LICENSE](LICENSE).
