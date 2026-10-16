#!/usr/bin/env python3
"""One entry point: picks the right installer for this operating system.

Usage: python3 install.py [--trust-folders]   (the flag is Linux only, see README)
"""
import shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]

if sys.platform.startswith("linux"):
    script = HERE / "install.sh"
elif sys.platform == "darwin":
    script = HERE / "install-macos.sh"
else:
    sys.exit("Windows ainda não é suportado (só Linux/GNOME e macOS experimental). Vê o README, secção Plataformas.")

if not shutil.which("claude"):
    print("Aviso: o comando 'claude' não está no PATH. Instala o Claude Code antes de usar o gestor.")
sys.exit(subprocess.call(["bash", str(script), *args]))
