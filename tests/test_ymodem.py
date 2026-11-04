"""Y-Modem 发送器测试：用假 bootloader 走完 头包 → 数据块 → EOT → 空包。"""
from __future__ import annotations

from collections import deque

from pdpower_studio.protocol import crc16_xmodem
from pdpower_studio.updater.ymodem import ACK, EOT, NAK, YModemSender

C = 0x43
SOH = 0x01
STX = 0x02


class FakeBootloader:
    """按 1K Y-Modem 接收方的应答行为建模；所有写包都会校验 CRC16。"""

    def __init__(self) -> None:
        self.responses: deque[bytes] = deque([b"C"])
        self.headers: list[bytes] = []
        self.seqs: list[int] = []
        self.eot_count = 0
        self.bad_crc = 0

    def write(self, payload: bytes) -> None:
        kind = payload[0]
        if kind == SOH:
            body = payload[3:-2]
            if int.from_bytes(payload[-2:], "big") != crc16_xmodem(body):
                self.bad_crc += 1
            if body.strip(b"\x00"):
                self.headers.append(body)
                self.responses.append(bytes([ACK]))
                self.responses.append(b"C")
            else:
                self.responses.append(bytes([ACK]))
        elif kind == STX:
            body = payload[3:-2]
            if int.from_bytes(payload[-2:], "big") != crc16_xmodem(body):
                self.bad_crc += 1
                self.responses.append(bytes([NAK]))
                return
            self.seqs.append(payload[1])
            self.responses.append(bytes([ACK]))
        elif kind == EOT:
            self.eot_count += 1
            self.responses.append(bytes([NAK]) if self.eot_count == 1 else bytes([ACK]))

    def read(self, timeout: float) -> bytes:
        return self.responses.popleft() if self.responses else b""


def test_sends_header_blocks_and_eot():
    boot = FakeBootloader()
    sender = YModemSender(boot.write, boot.read)
    data = bytes(range(256)) * 6          # 1536 B → 2 个 1K 块
    result = sender.send("firmware.fpk", data)

    assert result["bytes"] == len(data)
    assert result["blocks"] == 2
    assert result["retries"] == 0
    assert boot.bad_crc == 0
    assert boot.seqs == [1, 2]
    assert boot.eot_count == 2
    assert boot.headers and b"firmware.fpk" in boot.headers[0]


def test_single_short_block_padded():
    boot = FakeBootloader()
    sender = YModemSender(boot.write, boot.read)
    result = sender.send("small.fpk", b"\x01\x02\x03")
    assert result["blocks"] == 1
    assert boot.seqs == [1]


def test_crc16_xmodem_standard_vector():
    assert crc16_xmodem(b"123456789") == 0x31C3
