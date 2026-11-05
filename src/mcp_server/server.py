"""最小 MCP server：stdlib stdio + newline-delimited JSON-RPC 2.0。

用法：python -m pdpower_studio.cli mcp [--allow-write]
默认只读：所有写入类工具都会返回错误，直到以 --allow-write 启动。
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any, TextIO

from .. import __version__
from .tools import ToolError, ToolRouter

PROTOCOL_VERSION = "2024-11-05"


class McpServer:
    def __init__(self, router: ToolRouter, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        self.router = router
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    def serve_forever(self) -> None:
        for line in self.stdin:
            # 有些宿主（如 PowerShell 5.1）会给管道首行注入 UTF-8 BOM
            line = line.lstrip("\ufeff").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._send({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}})
                continue
            response = self.handle(message)
            if response is not None:
                self._send(response)

    def _send(self, payload: dict) -> None:
        self.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.stdout.flush()

    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message
        try:
            if method == "initialize":
                response = self._ok(msg_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "pdpower-studio", "version": __version__},
                })
            elif method in ("notifications/initialized", "notifications/cancelled"):
                response = None
            elif method == "ping":
                response = self._ok(msg_id, {})
            elif method == "tools/list":
                response = self._ok(msg_id, {"tools": ToolRouter.list_tools()})
            elif method == "tools/call":
                response = self._call_tool(msg_id, message.get("params") or {})
            else:
                response = self._error(msg_id, -32601, f"Method not found: {method}")
        except Exception:  # pragma: no cover - 兜底
            traceback.print_exc(file=sys.stderr)
            response = self._error(msg_id, -32603, "Internal error")
        # JSON-RPC 通知（无 id）不允许回包
        return None if is_notification and response is not None else response

    def _call_tool(self, msg_id: Any, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return self._error(msg_id, -32602, "Invalid params: missing tool name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return self._error(msg_id, -32602, "Invalid params: arguments must be an object")
        try:
            result = self.router.call(name, args)
            text = json.dumps(result, ensure_ascii=False, default=str)
            return self._ok(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except ToolError as exc:
            return self._ok(msg_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except KeyError as exc:
            # 必填参数缺失属于参数错误（-32602），不是内部错误
            return self._error(msg_id, -32602, f"Invalid params: missing {exc}")
        except (PermissionError, ValueError, RuntimeError, TimeoutError) as exc:
            return self._ok(msg_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                                     "isError": True})

    @staticmethod
    def _ok(msg_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
