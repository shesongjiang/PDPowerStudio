"""MCP server 入口（stdio JSON-RPC）：默认等价于 `cli mcp`。"""
from __future__ import annotations

import sys

from pdpower_studio.cli import main
from pdpower_studio.console_io import setup_console_encoding

setup_console_encoding()

argv = sys.argv[1:]
if not argv:
    argv = ["mcp"]
elif argv[0].startswith("--"):
    argv = ["mcp"] + argv

raise SystemExit(main(argv))
