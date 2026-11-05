"""WeAct PDPowerMiniV1.x 帧协议编解码。

两种变体（已用实机 COM48 校对）：
- USB CDC（默认）：帧尾 0x0A，无 CRC；变长响应（0x81/0xC2/0xC3）**不带 length 字节**。
- UART：帧尾改为 CRC8（poly=0x31, init=0xFF）；变长响应格式为 [cmd][len][payload][crc8]。

实机帧样例见 tests/test_protocol.py。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

END = 0x0A
UPGRADE_MAGIC = bytes([0x41, 0xA5, 0xAA, 0x0A])

# 总帧长（含帧头与帧尾）。来自协议表；0xC7 的 66 字节未实机验证，已标注。
FIXED_FRAME_LEN: dict[int, int] = {
    0x82: 3,   # output state
    0x83: 3,   # output id
    0x84: 7,   # output data (mv, ma)
    0x85: 6,   # output display (mv, ma)
    0x86: 3,   # ocp enable
    0x87: 3,   # output offset/comp enable
    0x88: 3,   # brightness
    0x8A: 7,   # input state (仅 >=1.0.2.0)
    0xC6: 3,   # lcd panel type
    0xC7: 66,  # factory data（未验证）
}
VARIABLE_FRAMES = {0x81, 0xC2, 0xC3}
MAX_VARIABLE_PAYLOAD = 128


class FrameMode(str, Enum):
    USB = "usb"
    UART = "uart"


class Command:
    OUTPUT_ENABLE = 0x02
    OUTPUT_ID = 0x03
    OUTPUT_DATA = 0x04
    OCP_ENABLE = 0x06
    COMPENSATE_ENABLE = 0x07
    BRIGHTNESS = 0x08
    DISCHARGE_ENABLE = 0x09
    PD_VOLTAGE = 0x0A
    SYSTEM_RESET = 0x40
    SYSTEM_UPGRADE = 0x41
    SYSTEM_DATA_RESET = 0x42
    SYSTEM_FACTORY_RESET = 0x43
    OUTPUT_CONFIG_SAVE = 0x44
    FACTORY_DATA_RESET = 0x45
    LCD_PANEL_TYPE = 0x46
    FACTORY_DATA_SET = 0x47

    READ_WHO_AM_I = 0x81
    READ_OUTPUT_STATE = 0x82
    READ_OUTPUT_ID = 0x83
    READ_OUTPUT_DATA = 0x84
    READ_OUTPUT_DISPLAY = 0x85
    READ_OCP_ENABLE = 0x86
    READ_COMPENSATE_ENABLE = 0x87
    READ_BRIGHTNESS = 0x88
    READ_INPUT_STATE = 0x8A
    READ_VERSION = 0xC2
    READ_SERIAL_NUM = 0xC3
    READ_LCD_PANEL_TYPE = 0xC6
    READ_FACTORY_DATA = 0xC7


#: 只读命令白名单：用于联调/探针，任何写命令都不在其中。
READ_ONLY_COMMANDS = {
    Command.READ_WHO_AM_I,
    Command.READ_OUTPUT_STATE,
    Command.READ_OUTPUT_ID,
    Command.READ_OUTPUT_DATA,
    Command.READ_OUTPUT_DISPLAY,
    Command.READ_OCP_ENABLE,
    Command.READ_COMPENSATE_ENABLE,
    Command.READ_BRIGHTNESS,
    Command.READ_INPUT_STATE,
    Command.READ_VERSION,
    Command.READ_SERIAL_NUM,
    Command.READ_LCD_PANEL_TYPE,
    Command.READ_FACTORY_DATA,
}


def crc8(data: Iterable[int], init: int = 0xFF, poly: int = 0x31) -> int:
    """WCH UART 协议使用的 CRC8（逐位，poly=0x31，init=0xFF）。"""
    crc = init
    for byte in data:
        crc ^= byte & 0xFF
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def crc16_xmodem(data: Iterable[int], init: int = 0x0000, poly: int = 0x1021) -> int:
    """Y-Modem 使用的 CRC16-XMODEM。"""
    crc = init
    for byte in data:
        crc ^= (byte & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ poly) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class Frame:
    cmd: int
    payload: bytes
    raw: bytes

    @property
    def text(self) -> str:
        return self.payload.decode("ascii", errors="replace").strip("\x00").strip()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Frame(0x{self.cmd:02X}, {self.raw.hex(' ')})"


def encode(cmd: int, args: Sequence[int] = (), mode: FrameMode = FrameMode.USB) -> bytes:
    if not 0 <= cmd <= 0xFF:
        raise ValueError(f"bad cmd: {cmd}")
    body = bytes([cmd, *[int(a) & 0xFF for a in args]])
    if cmd == Command.PD_VOLTAGE:
        # 0x0A 同时是帧尾值，写法上仍然是 [cmd][data][0x0A]。
        pass
    if mode is FrameMode.USB:
        return body + bytes([END])
    return body + bytes([crc8(body)])


def encode_upgrade() -> bytes:
    """0x41 进入升级模式：41 A5 AA 0A（固定 4 字节，官方工具实测用法）。"""
    return UPGRADE_MAGIC


class FrameError(ValueError):
    pass


class FrameDecoder:
    """流式分帧。允许前缀噪声与跨片输入。"""

    def __init__(self, mode: FrameMode = FrameMode.USB) -> None:
        self.mode = mode
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        i = 0
        buf = self._buf
        n = len(buf)
        while i < n:
            cmd = buf[i]
            if cmd in FIXED_FRAME_LEN:
                total = FIXED_FRAME_LEN[cmd]
                if i + total > n:
                    break
                raw = bytes(buf[i:i + total])
                if self.mode is FrameMode.USB:
                    ok = raw[-1] == END
                else:
                    ok = crc8(raw[:-1]) == raw[-1]
                if not ok:
                    i += 1
                    continue
                frames.append(Frame(cmd, raw[1:-1], raw))
                i += total
            elif cmd in VARIABLE_FRAMES:
                if self.mode is FrameMode.USB:
                    end = -1
                    limit = min(n, i + 2 + MAX_VARIABLE_PAYLOAD)
                    for j in range(i + 1, limit):
                        if buf[j] == END:
                            end = j
                            break
                    if end < 0:
                        if n - i > 2 + MAX_VARIABLE_PAYLOAD:
                            # 窗口内找不到帧尾：当前字节不可能是合法帧头，丢弃以重新同步，
                            # 否则噪声会让缓冲区无限增长。
                            i += 1
                            continue
                        break
                    raw = bytes(buf[i:end + 1])
                    frames.append(Frame(cmd, raw[1:-1], raw))
                    i = end + 1
                else:
                    if i + 2 > n:
                        break
                    length = buf[i + 1]
                    total = 3 + length
                    if i + total > n:
                        break
                    raw = bytes(buf[i:i + total])
                    if crc8(raw[:-1]) != raw[-1]:
                        i += 1
                        continue
                    frames.append(Frame(cmd, raw[2:-1], raw))
                    i += total
            else:
                i += 1
        if i:
            del buf[:i]
        return frames


# ---------------------------------------------------------------- 解析辅助

def _u16(payload: bytes, idx: int) -> int:
    if len(payload) < idx + 2:
        raise FrameError("payload too short")
    return payload[idx] | (payload[idx + 1] << 8)


def parse_text(frame: Frame) -> str:
    return frame.text


def parse_output_state(frame: Frame) -> dict:
    if len(frame.payload) != 1:
        raise FrameError("output state expects 1 byte")
    value = frame.payload[0]
    return {
        "output_enabled": bool(value & 0x01),
        "output_mode": (value >> 1) & 0x03,  # 0 normal / 1 CC / 2 OC
    }


def parse_output_id(frame: Frame) -> int:
    if len(frame.payload) != 1:
        raise FrameError("output id expects 1 byte")
    return frame.payload[0]


def parse_output_data(frame: Frame) -> tuple[int, int]:
    if len(frame.payload) != 5:
        raise FrameError("output data expects id + mv + ma")
    return _u16(frame.payload, 1), _u16(frame.payload, 3)


def parse_output_id_and_data(frame: Frame) -> tuple[int, int, int]:
    if len(frame.payload) != 5:
        raise FrameError("output data expects id + mv + ma")
    return frame.payload[0], _u16(frame.payload, 1), _u16(frame.payload, 3)


def parse_display(frame: Frame) -> tuple[int, int]:
    if len(frame.payload) != 4:
        raise FrameError("display expects mv + ma")
    return _u16(frame.payload, 0), _u16(frame.payload, 2)


def parse_bool(frame: Frame) -> bool:
    if len(frame.payload) != 1:
        raise FrameError("bool frame expects 1 byte")
    return bool(frame.payload[0])


def parse_input_state(frame: Frame) -> dict:
    if len(frame.payload) != 5:
        raise FrameError("input state expects state + mv + pd(0.1V)")
    return {
        "input_state": frame.payload[0],
        "input_mv": _u16(frame.payload, 1),
        "pd_set_dv": _u16(frame.payload, 3),
    }


def parse_version(frame: Frame) -> tuple[tuple[int, int, int, int], str, str]:
    text = frame.text
    head, _, suffix = text.partition("_")
    if not head.startswith("V"):
        raise FrameError(f"bad version string: {text!r}")
    parts = head[1:].split(".")
    if len(parts) != 4:
        raise FrameError(f"bad version string: {text!r}")
    return tuple(int(p) for p in parts), suffix, text  # type: ignore[return-value]


def format_version(version: Sequence[int]) -> str:
    return "V" + ".".join(str(int(v)) for v in version)
