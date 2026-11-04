"""CLI 入口（PyInstaller console target）。"""
from __future__ import annotations

import sys

from pdpower_studio.cli import main
from pdpower_studio.console_io import setup_console_encoding

setup_console_encoding()
raise SystemExit(main(sys.argv[1:]))
