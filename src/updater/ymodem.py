"""Y-Modem 发送器（1K 块 + CRC16-XMODEM），与官方 bootloader 行为对齐。

只依赖注入的 write/read 回调，便于用假设备做单元测试。
"""
from __future__ import annotations

import time
from typing import Callable

from ..protocol import crc16_xmodem

SOH, STX, EOT, ACK, NAK, CAN, C = 0x01, 0x02, 0x04, 0x06, 0x15, 0x18, 0x43


class YModemError(RuntimeError):
    pass


class YModemSender:
    def __init__(
        self,
        write: Callable[[bytes], None],
        read: Callable[[float], bytes],
        timeout: float = 3.0,
        retries: int = 10,
        block_size: int = 1024,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        self.write = write
        self.read = read
        self.timeout = timeout
        self.retries = retries
        self.block_size = block_size
        self.progress = progress
        self.retries_used = 0
        self._rx = bytearray()

    # ------------------------------------------------------------- 底层
    def _fill(self, timeout: float) -> None:
        data = self.read(timeout)
        if data:
            self._rx.extend(data)

    def _wait(self, accepted: set[int], timeout: float | None = None) -> int:
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        while True:
            for byte in self._rx:
                if byte in accepted:
                    idx = self._rx.index(byte)
                    del self._rx[:idx + 1]
                    return byte
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("等待 bootloader 应答超时")
            self._fill(min(0.05, remaining))

    # ------------------------------------------------------------- 打包
    @staticmethod
    def _crc(payload: bytes) -> bytes:
        value = crc16_xmodem(payload)
        return bytes([value >> 8, value & 0xFF])

    def _header_packet(self, filename: str, size: int) -> bytes:
        payload = filename.encode("ascii", errors="replace") + b"\x00" + str(size).encode("ascii") + b"\x00"
        payload = payload.ljust(128, b"\x00")
        return bytes([SOH, 0x00, 0xFF]) + payload + self._crc(payload)

    def _block_packet(self, seq: int, chunk: bytes) -> bytes:
        payload = chunk.ljust(self.block_size, b"\x1a")
        return bytes([STX, seq & 0xFF, 0xFF - (seq & 0xFF)]) + payload + self._crc(payload)

    # ------------------------------------------------------------- 主流程
    def send(self, filename: str, data: bytes, handshake_timeout: float = 3.0) -> dict:
        self._wait({C}, handshake_timeout)
        total = len(data)
        sent = 0
        # 文件头 + 等待 ACK + 再等 'C'
        header = self._header_packet(filename, total)
        for attempt in range(self.retries):
            self.write(header)
            try:
                self._wait({ACK})
                break
            except TimeoutError:
                self.retries_used += 1
                self.write(bytes([NAK]))
        else:
            raise YModemError("文件头未被确认")
        self._wait({C}, handshake_timeout)

        seq = 1
        for offset in range(0, total, self.block_size):
            chunk = data[offset:offset + self.block_size]
            packet = self._block_packet(seq, chunk)
            for attempt in range(self.retries):
                self.write(packet)
                try:
                    reply = self._wait({ACK, NAK, C})
                except TimeoutError:
                    reply = NAK
                if reply == ACK:
                    break
                self.retries_used += 1
            else:
                raise YModemError(f"第 {seq} 块重试 {self.retries} 次仍失败")
            sent += len(chunk)
            if self.progress:
                self.progress(sent, total)
            seq = (seq + 1) & 0xFF

        # EOT 两次（第一次收到 NAK，第二次收到 ACK），再发空包收尾
        for _ in range(self.retries):
            self.write(bytes([EOT]))
            try:
                self._wait({NAK, ACK})
                break
            except TimeoutError:
                self.retries_used += 1
        else:
            raise YModemError("EOT 未被确认")
        for _ in range(self.retries):
            self.write(bytes([EOT]))
            try:
                self._wait({ACK})
                break
            except TimeoutError:
                self.retries_used += 1
        self.write(self._header_packet("", 0))
        try:
            self._wait({ACK}, 1.5)
        except TimeoutError:
            pass
        return {"bytes": total, "blocks": seq - 1, "retries": self.retries_used}
