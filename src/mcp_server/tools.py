"""MCP 工具路由：最小工具集 + 量程校验 + 危险操作确认字段 + 串口独占。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..device_model import DeviceService
from ..logging_store import LogConfig, LogStore, estimate_size
from ..transport import DeviceLink, LinkError, PortBusy, list_pdpower_ports

CONFIRM_FIELD_DESC = "危险操作必须传 confirm=true"


class ToolError(RuntimeError):
    pass


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
    }


class ToolRouter:
    def __init__(self, service: DeviceService | None = None, read_only: bool = True) -> None:
        self.service = service or DeviceService()
        self.read_only = read_only
        self.link: DeviceLink | None = None
        self.log_store: LogStore | None = None

    # ------------------------------------------------------------ 定义
    @staticmethod
    def list_tools() -> list[dict]:
        confirm = {"type": "boolean", "description": CONFIRM_FIELD_DESC}
        return [
            _tool("list_devices", "列出 PDPower 系列串口设备（VID 1A86 / PID FE0C）", {}, []),
            _tool("connect", "独占打开串口并只读握手", {
                "port": {"type": "string"}, "read_only": {"type": "boolean"}}, ["port"]),
            _tool("disconnect", "关闭串口并释放端口锁", {}, []),
            _tool("get_device_info", "型号 / SN / 版本 / 能力位", {}, []),
            _tool("get_status", "输出状态、显示值、组设置、输入状态", {}, []),
            _tool("set_voltage_current", "设置组内电压电流（mV/mA 由服务换算，范围严格校验）", {
                "voltage_v": {"type": "number"}, "current_a": {"type": "number"},
                "group": {"type": "integer", "minimum": 0, "maximum": 4},
                "confirm": confirm}, ["voltage_v", "current_a"]),
            _tool("set_output", "set_voltage_current 的别名", {
                "voltage_v": {"type": "number"}, "current_a": {"type": "number"}, "confirm": confirm},
                ["voltage_v", "current_a"]),
            _tool("set_group", "切换记忆组 M0–M4（输出开启时需 confirm）", {
                "group": {"type": "integer", "minimum": 0, "maximum": 4}, "confirm": confirm}, ["group"]),
            _tool("output_enable", "开关输出；开启必须 confirm=true", {
                "enabled": {"type": "boolean"}, "confirm": confirm}, ["enabled"]),
            _tool("save_config", "保存配置到设备 NVM（必须 confirm=true）", {"confirm": confirm}, ["confirm"]),
            _tool("start_log", "开始挂测记录（CSV/JSONL，按天滚动）", {
                "directory": {"type": "string"}, "format": {"type": "string", "enum": ["csv", "jsonl"]},
                "hz": {"type": "number"}}, ["directory"]),
            _tool("stop_log", "停止记录并 flush", {}, []),
            _tool("estimate_log_size", "估算挂测体积", {
                "hz": {"type": "number"}, "hours": {"type": "number"},
                "format": {"type": "string", "enum": ["csv", "jsonl"]}}, ["hz", "hours"]),
        ]

    # ------------------------------------------------------------ 调用
    def call(self, name: str, args: dict | None = None) -> Any:
        args = args or {}
        handler: Callable[[dict], Any] = getattr(self, f"_tool_{name}", None)  # type: ignore[assignment]
        if handler is None:
            raise ToolError(f"未知工具: {name}")
        return handler(args)

    # ------------------------------------------------------------ 实现
    def _tool_list_devices(self, args: dict) -> list[dict]:
        return list_pdpower_ports()

    def _tool_connect(self, args: dict) -> dict:
        if self.link and self.link.connected:
            raise ToolError("已连接，先 disconnect")
        port = args["port"]
        read_only = True if self.read_only else bool(args.get("read_only", False))
        link = DeviceLink(read_only=read_only)
        try:
            link.open(port)
            data = link.handshake()
        except (PortBusy, LinkError, PermissionError, OSError) as exc:
            link.close()
            raise ToolError(str(exc)) from exc
        self.link = link
        self.service.link = link
        link.on_snapshot = self._on_snapshot
        return {"connected": True, "read_only": read_only, **data}

    def _tool_disconnect(self, args: dict) -> dict:
        self.stop_log()
        if self.link:
            self.link.close()
            self.link = None
            self.service.link = None
        return {"connected": False}

    def _tool_get_device_info(self, args: dict) -> dict:
        return self.service.device_info()

    def _tool_get_status(self, args: dict) -> dict:
        return self.service.status()

    def _tool_set_voltage_current(self, args: dict) -> dict:
        if self.read_only:
            raise ToolError("本进程/工具处于只读模式，拒绝写入")
        voltage = float(args["voltage_v"])
        current = float(args["current_a"])
        status = self.service.status()
        if status["output_enabled"] and not args.get("confirm"):
            raise ToolError("输出开启时修改设定需 confirm=true")
        self.service.set_voltage_current(voltage, current, args.get("group"))
        return {"ok": True, "voltage_v": voltage, "current_a": current}

    def _tool_set_output(self, args: dict) -> dict:
        return self._tool_set_voltage_current(args)

    def _tool_set_group(self, args: dict) -> dict:
        if self.read_only:
            raise ToolError("只读模式，拒绝写入")
        group = int(args["group"])
        status = self.service.status()
        if status["output_enabled"] and not args.get("confirm"):
            raise ToolError("输出开启时切换组需 confirm=true")
        self.service.set_group(group, confirm=bool(args.get("confirm")))
        return {"ok": True, "group": group}

    def _tool_output_enable(self, args: dict) -> dict:
        if self.read_only:
            raise ToolError("只读模式，拒绝写入")
        enabled = bool(args["enabled"])
        if enabled and not args.get("confirm"):
            raise ToolError("开启输出必须 confirm=true")
        self.service.output_enable(enabled, confirm=bool(args.get("confirm")))
        return {"ok": True, "output_enabled": enabled}

    def _tool_save_config(self, args: dict) -> dict:
        if self.read_only:
            raise ToolError("只读模式，拒绝写入")
        if not args.get("confirm"):
            raise ToolError("保存配置必须 confirm=true")
        self.service.save_config(confirm=True)
        return {"ok": True, "saved": True}

    def _tool_start_log(self, args: dict) -> dict:
        if self.log_store and self.log_store.active:
            raise ToolError("已在记录中")
        config = LogConfig(
            directory=Path(args["directory"]),
            fmt=args.get("format", "csv"),
            hz=float(args.get("hz", 5.0)),
        )
        store = LogStore(config)
        meta = {"port": (self.link.port if self.link else ""), "note": "mcp"}
        path = store.start(meta)
        self.log_store = store
        return {"logging": True, "path": str(path), "config": {"format": config.fmt, "hz": config.hz}}

    def _tool_stop_log(self, args: dict) -> dict:
        self.stop_log()
        return {"logging": False}

    def _tool_estimate_log_size(self, args: dict) -> dict:
        return estimate_size(float(args["hz"]), float(args["hours"]), args.get("format", "csv"))

    # ------------------------------------------------------------ 内部
    def _on_snapshot(self, snapshot: dict) -> None:
        if self.log_store and self.log_store.active:
            self.log_store.append(snapshot)

    def stop_log(self) -> None:
        if self.log_store:
            self.log_store.stop()
            self.log_store = None
