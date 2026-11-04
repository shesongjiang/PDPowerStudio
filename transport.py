"""串口传输层（无线程框架依赖，Qt/MCP/测试共用）。

- 所有权：单写者。打开时可选端口锁文件，避免 UI 与 MCP 抢 COM 口。
- 节流：写队列按 55 ms 间隔发送（与官方工具行为一致）。
- 轮询：0x85 / 0x82 / 0x8A 独立周期；轮询与读取在同一 IO 线程内完成，UI 只读快照。
- 只读模式：read_only=True 时拒绝任何非白名单命令（联调默认开启）。
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterable, Sequence

import serial

from . import protocol as p
from .device_model import DeviceSnapshot, apply_frame, capabilities_for

WRITE_INTERVAL_S = 0.055
DEFAULT_TIMEOUT_S = 0.4


class PortBusy(RuntimeError):
    pass


class LinkError(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class PortLock:
    """跨进程端口锁：文件里存 pid + 时间戳，pid 已死则视为陈旧锁。"""

    def __init__(self, port: str, directory: Path | None = None) -> None:
        base = directory or Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "pdpower_studio"
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / f"port_{port.strip().upper()}.lock"
        self.owned = False

    def acquire(self) -> None:
        if self.path.exists():
            try:
                info = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError):
                info = {}
            pid = int(info.get("pid", 0) or 0)
            if _pid_alive(pid):
                raise PortBusy(f"{self.path.stem} 已被进程 {pid} 占用（串口独占）")
            try:
                self.path.unlink()
            except OSError:
                raise PortBusy(f"{self.path.stem} 存在无法清理的陈旧锁")
        self.path.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
        self.owned = True

    def release(self) -> None:
        if self.owned:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.owned = False


class DeviceLink:
    def __init__(
        self,
        mode: p.FrameMode = p.FrameMode.USB,
        read_only: bool = True,
        poll_display_s: float = 0.2,
        poll_state_s: float = 0.5,
        poll_input_s: float = 1.0,
    ) -> None:
        self.mode = mode
        self.read_only = read_only
        self.poll_display_s = poll_display_s
        self.poll_state_s = poll_state_s
        self.poll_input_s = poll_input_s

        self.snapshot = DeviceSnapshot()
        self.on_snapshot: Callable[[dict], None] | None = None
        self.on_error: Callable[[str], None] | None = None

        self._ser: serial.Serial | None = None
        self.port = ""
        self._lock = PortLock("")  # 打开时重建
        self._snap_lock = threading.Lock()
        self._decoder = p.FrameDecoder(mode)
        self._queue: deque[tuple[int, tuple[int, ...], threading.Event | None, list]] = deque()
        self._pending: dict[int, list[tuple[threading.Event, list]]] = {}
        self._last_write = 0.0
        self._next_poll: dict[int, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self.frames_seen = 0

    # ------------------------------------------------------------ 生命周期
    def open(self, port: str, baud: int = 115200, acquire_lock: bool = True) -> None:
        if self._connected:
            return
        if acquire_lock:
            self._lock = PortLock(port)
            self._lock.acquire()
        try:
            self._ser = serial.Serial(port, baud, timeout=0, write_timeout=1.0)
        except PermissionError as exc:
            self._lock.release()
            raise PortBusy(f"{port} 打开失败（被占用或无权限）: {exc}") from exc
        except serial.SerialException as exc:
            self._lock.release()
            # pyserial 把 Windows 的“拒绝访问”包装成 SerialException，
            # 必须看 __cause__/文案才能区分“被占用”与“设备不存在”。
            cause = exc.__cause__
            if isinstance(cause, PermissionError) or "permission" in str(exc).lower() or "拒绝访问" in str(exc):
                raise PortBusy(f"{port} 打开失败（被其他程序占用或无权限）: {exc}") from exc
            raise LinkError(f"{port} 打开失败: {exc}") from exc
        self.snapshot = DeviceSnapshot(port=port)
        self.port = port
        self._connected = True
        self._decoder.reset()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"pd-link-{port}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self._connected = False
        self.port = ""
        self._lock.release()

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------ 公共 API
    def snapshot_copy(self) -> dict:
        with self._snap_lock:
            return self.snapshot.to_dict()

    def request(self, cmd: int, args: Sequence[int] = (), timeout: float = DEFAULT_TIMEOUT_S) -> p.Frame:
        self._guard(cmd)
        if not self._connected:
            raise LinkError("串口未打开")
        event = threading.Event()
        holder: list = []
        self._pending.setdefault(cmd, []).append((event, holder))
        self._queue.append((cmd, tuple(int(a) for a in args), event, holder))
        if not event.wait(timeout):
            try:
                self._pending.get(cmd, []).remove((event, holder))
            except ValueError:
                pass  # IO 线程刚好已 pop，忽略
            raise TimeoutError(f"0x{cmd:02X} 响应超时（{timeout}s）")
        if not holder:
            raise LinkError(f"0x{cmd:02X} 无响应")
        return holder[0]

    def send(self, cmd: int, args: Sequence[int] = ()) -> None:
        self._guard(cmd)
        if not self._connected:
            raise LinkError("串口未打开")
        self._queue.append((cmd, tuple(int(a) for a in args), None, []))

    def request_stop(self) -> None:
        """请求尽快停止（用于 UI 退出时打断长握手）。"""
        self._stop.set()

    def _guard(self, cmd: int) -> None:
        if self.read_only and cmd not in p.READ_ONLY_COMMANDS:
            raise PermissionError(f"只读模式禁止发送 0x{cmd:02X}")

    def handshake(self, spacing: float = 0.05) -> dict:
        """只读握手：型号/版本/SN/状态/五组设置/显示值/开关项。"""
        steps: list[tuple[int, tuple[int, ...]]] = [
            (p.Command.READ_WHO_AM_I, ()),
            (p.Command.READ_VERSION, ()),
            (p.Command.READ_SERIAL_NUM, ()),
            (p.Command.READ_OUTPUT_STATE, ()),
            (p.Command.READ_OUTPUT_ID, ()),
        ]
        for gid in range(5):
            steps.append((p.Command.READ_OUTPUT_DATA, (gid,)))
        steps += [
            (p.Command.READ_OUTPUT_DISPLAY, ()),
            (p.Command.READ_OCP_ENABLE, ()),
            (p.Command.READ_COMPENSATE_ENABLE, ()),
            (p.Command.READ_BRIGHTNESS, ()),
            (p.Command.READ_LCD_PANEL_TYPE, ()),
        ]
        for cmd, args in steps:
            if self._stop.is_set():
                break
            try:
                self.request(cmd, args)
            except (TimeoutError, LinkError):
                pass
            time.sleep(spacing)
        with self._snap_lock:
            if self.snapshot.caps and self.snapshot.caps.input_state:
                steps_allowed = True
            else:
                steps_allowed = False
        if steps_allowed:
            try:
                self.request(p.Command.READ_INPUT_STATE)
            except (TimeoutError, LinkError):
                pass
        self._schedule_polls()
        return self.snapshot_copy()

    def set_polling(self, enabled: bool) -> None:
        self._polls_enabled = enabled
        if enabled:
            # 写后校验会短暂关闭轮询；恢复时必须重新排期，
            # 否则 _next_poll 已被清空，界面显示值将永久停止更新。
            self._schedule_polls()
        else:
            self._next_poll = {}

    # ------------------------------------------------------------ 内部 IO
    _polls_enabled = True

    def _schedule_polls(self) -> None:
        now = time.time()
        self._next_poll = {
            p.Command.READ_OUTPUT_DISPLAY: now,
            p.Command.READ_OUTPUT_STATE: now,
        }
        with self._snap_lock:
            if self.snapshot.caps and self.snapshot.caps.input_state:
                self._next_poll[p.Command.READ_INPUT_STATE] = now

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_once()
                self._dispatch_queue()
                self._poll()
            except Exception as exc:  # 串口拔出等
                self._connected = False
                if self.on_error:
                    self.on_error(str(exc))
                break
            time.sleep(0.004)

    def _read_once(self) -> None:
        ser = self._ser
        if not ser:
            return
        n = ser.in_waiting
        if not n:
            return
        data = ser.read(n)
        if not data:
            return
        for frame in self._decoder.feed(data):
            self._handle_frame(frame)

    def _handle_frame(self, frame: p.Frame) -> None:
        pending = self._pending.get(frame.cmd)
        waiter = pending.pop(0) if pending else None
        with self._snap_lock:
            apply_frame(self.snapshot, frame)
            snapshot = self.snapshot.to_dict()
        self.frames_seen += 1
        if waiter:
            event, holder = waiter
            holder.append(frame)
            # 先并入快照再唤醒 request()，保证写入回读后拿到的是新快照
            event.set()
        if frame.cmd == p.Command.READ_OUTPUT_DISPLAY and self.on_snapshot:
            self.on_snapshot(snapshot)

    def _dispatch_queue(self) -> None:
        now = time.time()
        if now - self._last_write < WRITE_INTERVAL_S:
            return
        while self._queue:
            cmd, args, _, _ = self._queue.popleft()
            payload = p.encode(cmd, args, self.mode)
            self._ser.write(payload)  # type: ignore[union-attr]
            self._last_write = time.time()
            break

    def _poll(self) -> None:
        if not self._polls_enabled:
            return
        now = time.time()
        for cmd, due in list(self._next_poll.items()):
            if now >= due:
                self._queue.append((cmd, (), None, []))
                interval = {
                    p.Command.READ_OUTPUT_DISPLAY: self.poll_display_s,
                    p.Command.READ_OUTPUT_STATE: self.poll_state_s,
                    p.Command.READ_INPUT_STATE: self.poll_input_s,
                }[cmd]
                self._next_poll[cmd] = now + interval


def list_pdpower_ports() -> list[dict]:
    """按 VID/PID 过滤设备端口（SN 前缀 AC/B3 为 PDPower 系列）。"""
    from serial.tools import list_ports

    found = []
    for port in list_ports.comports():
        if port.vid == 0x1A86 and port.pid == 0xFE0C:
            sn = port.serial_number or ""
            prefix = sn[:2].upper()
            # 同 VID/PID 的其他产品（SN 前缀非 AC/B3）默认不当作 PDPower
            if sn and prefix not in ("AC", "B3"):
                continue
            found.append({
                "port": port.device,
                "sn": sn,
                "model": "PDPOWERMINIV1" if prefix == "AC" else ("PDPOWERMINIV11" if prefix == "B3" else ""),
                "description": port.description,
            })
    return found
