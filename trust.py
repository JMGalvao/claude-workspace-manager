#!/usr/bin/env python3
"""Mark folders as trusted in ~/.claude.json so Claude Code never shows the trust dialog for them.

Usage: trust.py [DIR ...]   (default: current directory)
Only writes when something actually changes; replaces the file atomically and keeps its mode.
"""
import json, os, stat, sys

CFG = os.path.expanduser("~/.claude.json")


def ensure(*paths):
    """Return the list of paths that had to be marked (empty if all were already trusted)."""
    try:
        raw = open(CFG).read()
        d = json.loads(raw)
    except (OSError, ValueError):
        return []
    projects = d.setdefault("projects", {})
    todo = []
    for p in paths:
        p = os.path.realpath(os.path.expanduser(p))
        if os.path.isdir(p) and not projects.get(p, {}).get("hasTrustDialogAccepted"):
            todo.append(p)
    if not todo:
        return []
    for p in todo:
        projects.setdefault(p, {})["hasTrustDialogAccepted"] = True
    tmp = CFG + ".ws-tmp"
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(d, indent=2, ensure_ascii=False) + ("\n" if raw.endswith("\n") else ""))
        os.chmod(tmp, stat.S_IMODE(os.stat(CFG).st_mode))
        json.load(open(tmp))
        os.replace(tmp, CFG)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return []
    return todo


if __name__ == "__main__":
    ensure(*(sys.argv[1:] or [os.getcwd()]))
