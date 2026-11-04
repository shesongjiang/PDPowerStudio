"""控制台输出编码：让打包后的 CLI/MCP 在 cp936 控制台下正确显示中文。

MCP 的 stdin/stdout 都是 JSON-RPC 通道，按规范统一为 UTF-8；
否则 Windows 默认的 cp936 会把客户端发来的中文参数解错。
"""
from __future__ import annotations

import ctypes
import sys


def _reconfigure(stream, **kwargs) -> None:
    if stream is not None and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(**kwargs)
        except Exception:
            pass


def setup_console_encoding() -> None:
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # UTF-8
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    _reconfigure(sys.stdin, encoding="utf-8", errors="replace")
    _reconfigure(sys.stdout, encoding="utf-8", errors="replace", line_buffering=True)
    _reconfigure(sys.stderr, encoding="utf-8", errors="replace")
