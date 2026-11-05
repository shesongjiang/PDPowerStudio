"""MCP server 测试：JSON-RPC 形状、只读拒绝、危险操作确认字段。"""
from __future__ import annotations

import json

from pdpower_studio.mcp_server.server import McpServer
from pdpower_studio.mcp_server.tools import ToolRouter


def make_server() -> McpServer:
    return McpServer(ToolRouter(read_only=True))


def test_initialize_and_tools_list():
    server = make_server()
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["serverInfo"]["name"] == "pdpower-studio"

    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    for required in ("list_devices", "get_status", "get_device_info", "set_voltage_current",
                     "set_output", "set_group", "output_enable", "save_config",
                     "start_log", "stop_log"):
        assert required in names

    output_tool = next(t for t in listed["result"]["tools"] if t["name"] == "output_enable")
    assert "confirm" in output_tool["inputSchema"]["properties"]
    assert "enabled" in output_tool["inputSchema"]["required"]


def test_read_only_rejects_every_write_tool():
    server = make_server()
    for name, args in (("output_enable", {"enabled": True, "confirm": True}),
                       ("save_config", {"confirm": True}),
                       ("set_group", {"group": 2, "confirm": True}),
                       ("set_voltage_current", {"voltage_v": 5, "current_a": 1, "confirm": True})):
        response = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})
        assert response["result"]["isError"] is True, name
        assert "只读" in response["result"]["content"][0]["text"]


def test_list_devices_returns_list():
    server = make_server()
    response = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "list_devices", "arguments": {}}})
    payload = json.loads(response["result"]["content"][0]["text"])
    assert isinstance(payload, list)
    assert response["result"]["isError"] is False


def test_get_status_requires_device_is_error_but_structured():
    server = make_server()
    response = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "get_status", "arguments": {}}})
    assert response["result"]["isError"] is True
    assert "未连接" in response["result"]["content"][0]["text"]


def test_unknown_method_and_unknown_tool():
    server = make_server()
    bad_method = server.handle({"jsonrpc": "2.0", "id": 5, "method": "does/not/exist"})
    assert bad_method["error"]["code"] == -32601

    bad_tool = server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                              "params": {"name": "nope", "arguments": {}}})
    assert bad_tool["result"]["isError"] is True


def test_notification_returns_no_response():
    server = make_server()
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_estimate_log_size_tool():
    server = make_server()
    response = server.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                              "params": {"name": "estimate_log_size",
                                         "arguments": {"hz": 5, "hours": 8, "format": "csv"}}})
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["samples"] == 5 * 3600 * 8
    assert payload["mib"] > 0
