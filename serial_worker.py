"""Qt 适配层：把一个 DeviceLink 放到 QThread 里，按固定频率把快照推给 UI。"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from .transport import DeviceLink, LinkError, PortBusy


class SerialWorker(QThread):
    snapshotUpdated = Signal(dict)
    errorOccurred = Signal(str)
    connectedChanged = Signal(bool)

    def __init__(self, port: str, read_only: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.port = port
        self.read_only = read_only
        self.link = DeviceLink(read_only=read_only)
        self._stop = False
        self._last_error: str | None = None

    def run(self) -> None:  # pragma: no cover - 需要真实串口
        try:
            self.link.open(self.port)
        except (PortBusy, LinkError, OSError) as exc:
            self.errorOccurred.emit(str(exc))
            self.connectedChanged.emit(False)
            return
        # 链路线程出错（如拔线）时记录下来，由下面的循环负责收尾
        self.link.on_error = self._remember_error
        try:
            self.connectedChanged.emit(True)
            self.link.handshake()
            self.link.set_polling(True)
            while not self._stop and self.link.connected:
                self.snapshotUpdated.emit(self.link.snapshot_copy())
                self.msleep(100)
            if not self._stop:
                self.errorOccurred.emit(self._last_error or "设备连接已断开")
        except Exception as exc:
            self.errorOccurred.emit(f"串口工作中断: {exc}")
        finally:
            self.link.close()
            self.connectedChanged.emit(False)

    def _remember_error(self, message: str) -> None:
        self._last_error = message

    def stop(self) -> bool:
        """请求停止；返回是否在超时前真正结束。

        握手最长约 8 s，这里先打断它，再等 4 s；
        没结束时由调用方保留引用，避免 QThread 运行中被回收导致崩溃。
        """
        self._stop = True
        self.link.request_stop()
        return self.wait(4000)
