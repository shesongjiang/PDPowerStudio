"""设备状态模型：型号识别、版本门控、能力位、快照与写入服务。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from . import protocol as p

MODEL_V1 = "PDPOWERMINIV1"
MODEL_V11 = "PDPOWERMINIV11"  # 仓库未给出 V1.1 的 user_string，按命名规律推定，未验证

# 版本门槛（README 的 release notes）
MIN_INPUT_STATE_V1 = (1, 0, 2, 0)
MIN_PD_VSET = (1, 0, 1, 0)
MIN_UART_MODE_V1 = (1, 0, 0, 5)


@dataclass(frozen=True)
class Capabilities:
    model: str
    model_name: str
    max_voltage_v: float
    max_current_a: float
    pd_min_v: float
    pd_max_v: float
    input_state: bool
    pd_vset: bool
    uart_mode: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "model_name": self.model_name,
            "max_voltage_v": self.max_voltage_v,
            "max_current_a": self.max_current_a,
            "pd_min_v": self.pd_min_v,
            "pd_max_v": self.pd_max_v,
            "input_state": self.input_state,
            "pd_vset": self.pd_vset,
            "uart_mode": self.uart_mode,
        }

    def clamp(self, voltage_v: float, current_a: float) -> tuple[float, float]:
        if voltage_v < 0.5 or voltage_v > self.max_voltage_v:
            raise ValueError(
                f"电压 {voltage_v}V 超出范围 0.5–{self.max_voltage_v}V（{self.model_name}）"
            )
        if current_a < 0.0 or current_a > self.max_current_a:
            raise ValueError(
                f"电流 {current_a}A 超出范围 0–{self.max_current_a}A（{self.model_name}）"
            )
        return voltage_v, current_a


def detect_model(serial_number: str | None) -> str | None:
    if not serial_number:
        return None
    sn = serial_number.strip().upper()
    if sn.startswith("AC"):
        return MODEL_V1
    if sn.startswith("B3"):
        return MODEL_V11
    return None


def parse_version_text(text: str) -> tuple[tuple[int, int, int, int], str]:
    head, _, suffix = text.strip().partition("_")
    parts = head.lstrip("Vv").split(".")
    if len(parts) != 4 or not all(x.isdigit() for x in parts):
        raise ValueError(f"无法解析版本号: {text!r}")
    return tuple(int(x) for x in parts), suffix  # type: ignore[return-value]


def capabilities_for(model: str | None, version: Sequence[int] | None) -> Capabilities | None:
    if model is None or version is None:
        return None
    v = tuple(int(x) for x in version)
    if model == MODEL_V1:
        return Capabilities(
            model=model,
            model_name="PDPowerMiniV1",
            max_voltage_v=20.0,
            max_current_a=3.0,
            pd_min_v=8.0,
            pd_max_v=24.0,
            input_state=v >= MIN_INPUT_STATE_V1,
            pd_vset=v >= MIN_PD_VSET,
            uart_mode=v >= MIN_UART_MODE_V1,
        )
    if model == MODEL_V11:
        return Capabilities(
            model=model,
            model_name="PDPowerMiniV1.1",
            max_voltage_v=24.0,
            max_current_a=3.0,
            pd_min_v=8.0,
            pd_max_v=28.0,
            input_state=v >= MIN_PD_VSET,   # 仓库未单列 V1.1 门槛，保守取 1.0.1.0
            pd_vset=v >= MIN_PD_VSET,
            uart_mode=False,                # 未验证，默认关闭
        )
    return None


@dataclass
class DeviceSnapshot:
    port: str = ""
    name: str = ""
    version_text: str = ""
    version: tuple[int, int, int, int] | None = None
    version_hash: str = ""
    sn: str = ""
    model: str | None = None
    caps: Capabilities | None = None

    output_enabled: bool = False
    output_mode: int = 0          # 0 正常 / 1 CC / 2 OC
    group: int = 0
    groups: list[tuple[int, int]] = field(default_factory=lambda: [(0, 0)] * 5)
    disp_mv: int = 0
    disp_ma: int = 0

    ocp: bool = False
    compensate: bool = False
    brightness: int = 0
    lcd: int = 0
    lcd_name: str = ""

    input_state: int | None = None
    input_mv: int | None = None
    pd_set_dv: int | None = None

    last_update_ts: float = 0.0
    frames_seen: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "name": self.name,
            "version": self.version_text,
            "version_tuple": list(self.version) if self.version else None,
            "version_hash": self.version_hash,
            "sn": self.sn,
            "model": self.model,
            "model_name": self.caps.model_name if self.caps else "",
            "capabilities": self.caps.to_dict() if self.caps else None,
            "output_enabled": self.output_enabled,
            "output_mode": self.output_mode,
            "group": self.group,
            "groups": [{"id": i, "mv": mv, "ma": ma} for i, (mv, ma) in enumerate(self.groups)],
            "display": {"mv": self.disp_mv, "ma": self.disp_ma,
                        "voltage_v": self.disp_mv / 1000, "current_a": self.disp_ma / 1000,
                        "power_w": self.disp_mv * self.disp_ma / 1e6},
            "ocp": self.ocp,
            "compensate": self.compensate,
            "brightness": self.brightness,
            "lcd": self.lcd,
            "lcd_name": self.lcd_name,
            "input_state": self.input_state,
            "input_mv": self.input_mv,
            "pd_set_dv": self.pd_set_dv,
            "last_update_ts": self.last_update_ts,
        }


@dataclass(frozen=True)
class Calibration:
    """标定系数接口（默认 1.0 / 0 偏移，即完全按设备原始 mV/mA 显示）。"""

    voltage_gain: float = 1.0
    voltage_offset_mv: int = 0
    current_gain: float = 1.0
    current_offset_ma: int = 0

    def apply(self, mv: int, ma: int) -> tuple[int, int]:
        return (
            int(round(mv * self.voltage_gain)) + self.voltage_offset_mv,
            int(round(ma * self.current_gain)) + self.current_offset_ma,
        )


LCD_NAMES = {0: "HAN", 1: "BOE", 2: "?"}
OUTPUT_MODE_NAMES = {0: "normal", 1: "CC", 2: "OC"}
INPUT_STATE_NAMES = {0: "WAIT", 1: "WAIT_PD_OK", 2: "WAIT_QC_OK", 3: "ERR",
                     4: "QC", 5: "PD", 6: "DC"}


def apply_frame(snapshot: DeviceSnapshot, frame: p.Frame) -> None:
    """把一个已校验的响应帧并入快照。未知命令忽略。"""
    snapshot.frames_seen += 1
    snapshot.last_update_ts = time.time()
    cmd = frame.cmd
    try:
        if cmd == p.Command.READ_WHO_AM_I:
            snapshot.name = frame.text
        elif cmd == p.Command.READ_VERSION:
            version, suffix, text = p.parse_version(frame)
            snapshot.version = version
            snapshot.version_text = text
            snapshot.version_hash = suffix
        elif cmd == p.Command.READ_SERIAL_NUM:
            snapshot.sn = frame.text
            snapshot.model = detect_model(snapshot.sn)
        elif cmd == p.Command.READ_OUTPUT_STATE:
            state = p.parse_output_state(frame)
            snapshot.output_enabled = state["output_enabled"]
            snapshot.output_mode = state["output_mode"]
        elif cmd == p.Command.READ_OUTPUT_ID:
            snapshot.group = p.parse_output_id(frame)
        elif cmd == p.Command.READ_OUTPUT_DATA:
            gid, mv, ma = p.parse_output_id_and_data(frame)
            if 0 <= gid < 5:
                snapshot.groups[gid] = (mv, ma)
        elif cmd == p.Command.READ_OUTPUT_DISPLAY:
            snapshot.disp_mv, snapshot.disp_ma = p.parse_display(frame)
        elif cmd == p.Command.READ_OCP_ENABLE:
            snapshot.ocp = p.parse_bool(frame)
        elif cmd == p.Command.READ_COMPENSATE_ENABLE:
            snapshot.compensate = p.parse_bool(frame)
        elif cmd == p.Command.READ_BRIGHTNESS:
            snapshot.brightness = frame.payload[0]
        elif cmd == p.Command.READ_LCD_PANEL_TYPE:
            snapshot.lcd = frame.payload[0]
            snapshot.lcd_name = LCD_NAMES.get(snapshot.lcd, "?")
        elif cmd == p.Command.READ_INPUT_STATE:
            info = p.parse_input_state(frame)
            snapshot.input_state = info["input_state"]
            snapshot.input_mv = info["input_mv"]
            snapshot.pd_set_dv = info["pd_set_dv"]
    except (p.FrameError, ValueError):
        return
    snapshot.caps = capabilities_for(snapshot.model, snapshot.version)


class DeviceService:
    """UI 与 MCP 共用的唯一写入口：范围校验、危险操作二次确认、串口独占。"""

    def __init__(self, link=None, log_store=None) -> None:
        self.link = link
        self.log_store = log_store

    # ---- 只读
    def status(self) -> dict:
        if not self.link:
            raise RuntimeError("未连接设备")
        return self.link.snapshot_copy()

    def device_info(self) -> dict:
        data = self.status()
        return {
            "port": data["port"], "name": data["name"], "version": data["version"],
            "sn": data["sn"], "model": data["model"],
            "model_name": data["model_name"], "capabilities": data["capabilities"],
            "lcd_name": data["lcd_name"],
        }

    def capabilities(self) -> Capabilities:
        snap = self.status()
        caps = capabilities_for(snap.get("model"), snap.get("version_tuple"))
        if caps is None:
            raise RuntimeError("设备能力未知（未完成握手）")
        return caps

    # ---- 写入（默认要求 read_only=False）
    def _write_and_verify(self, cmd: int, args: Sequence[int], read_cmd: int,
                          read_args: Sequence[int], check) -> None:
        """写入后回读校验；轮询暂停，避免把旧快照当成回读结果。

        实测固件 V1.0.2.0 的 0x02 输出使能要 ~200-300 ms 才反映到 0x82，
        固定 sleep 80 ms 会误判为失败，因此在 1 s 内重试回读直到一致。
        """
        self.link.set_polling(False)
        try:
            self._write(cmd, args)
            deadline = time.monotonic() + 1.0
            while True:
                time.sleep(0.1)
                frame = self.link.request(read_cmd, read_args, timeout=0.8)
                if check(frame):
                    return
                if time.monotonic() >= deadline:
                    break
            raise RuntimeError(
                f"写入后回读不一致（0x{read_cmd:02X}，1 s 内重试仍未生效）"
            )
        finally:
            self.link.set_polling(True)

    def set_voltage_current(self, voltage_v: float, current_a: float, group: int | None = None) -> None:
        caps = self.capabilities()
        voltage_v, current_a = caps.clamp(voltage_v, current_a)
        snap = self.status()
        gid = snap["group"] if group is None else int(group)
        if not 0 <= gid <= 4:
            raise ValueError("group 必须为 0–4")
        mv, ma = int(round(voltage_v * 1000)), int(round(current_a * 1000))

        def check(frame: p.Frame) -> bool:
            gid_rb, mv_rb, ma_rb = p.parse_output_id_and_data(frame)
            return gid_rb == gid and mv_rb == mv and ma_rb == ma

        self._write_and_verify(
            p.Command.OUTPUT_DATA, [gid, mv & 0xFF, mv >> 8, ma & 0xFF, ma >> 8],
            p.Command.READ_OUTPUT_DATA, (gid,), check,
        )

    def set_group(self, group: int, confirm: bool = False) -> None:
        if self.status()["output_enabled"] and not confirm:
            raise PermissionError("输出开启时不允许切换组，需先关闭输出并 confirm=true")
        gid = int(group)

        def check(frame: p.Frame) -> bool:
            return p.parse_output_id(frame) == gid

        self._write_and_verify(p.Command.OUTPUT_ID, [gid], p.Command.READ_OUTPUT_ID, (), check)

    def output_enable(self, enabled: bool, confirm: bool = False) -> None:
        if enabled and not confirm:
            raise PermissionError("开启输出需要 confirm=true")

        def check(frame: p.Frame) -> bool:
            state = p.parse_output_state(frame)
            return bool(state["output_enabled"]) == bool(enabled)

        self._write_and_verify(
            p.Command.OUTPUT_ENABLE, [1 if enabled else 0], p.Command.READ_OUTPUT_STATE, (), check
        )

    def save_config(self, confirm: bool = False) -> None:
        if not confirm:
            raise PermissionError("保存配置需要 confirm=true")
        self._write(p.Command.OUTPUT_CONFIG_SAVE, [])

    def set_pd_voltage(self, voltage_v: float, confirm: bool = False) -> None:
        caps = self.capabilities()
        if not caps.pd_vset:
            raise PermissionError(f"当前固件 {self.status()['version']} 不支持 PD 电压设置，需升级到 1.0.2.0")
        if not (caps.pd_min_v <= voltage_v <= caps.pd_max_v):
            raise ValueError(f"PD 电压需在 {caps.pd_min_v}–{caps.pd_max_v}V")
        snap = self.status()
        if snap["output_enabled"] or snap["display"]["voltage_v"] >= 5:
            raise PermissionError("设置 PD 电压需输出关闭且输出电压 < 5V")
        if not confirm:
            raise PermissionError("设置 PD 电压需要 confirm=true")
        dv = int(round(voltage_v * 10))
        self._write(p.Command.PD_VOLTAGE, [dv & 0xFF, dv >> 8])

    def _write(self, cmd: int, args: Sequence[int]) -> None:
        if not self.link:
            raise RuntimeError("未连接设备")
        if cmd == p.Command.SYSTEM_UPGRADE:
            # 0x41 只能由 UpgradeService（持锁 + 9600 + 固定魔数）发送，
            # 普通业务命令路径必须拒绝，避免发出 41 0A 这种无效帧。
            raise PermissionError("0x41 进升级模式只能由固件升级流程触发")
        self.link.send(cmd, args)
