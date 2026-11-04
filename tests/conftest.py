"""pytest 引导：让 `import pdpower_studio` 在项目目录内直接可用（无需安装）。"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pdpower_studio.updater.fpk import HEAD, HEAD_SIZE  # noqa: E402


@pytest.fixture
def make_fpk(tmp_path):
    """生成符合真实 96 字节头布局的假 fpk（载荷任意，用于校验逻辑测试）。"""

    def _make(
        name: str = "test.fpk",
        version: tuple[int, int, int, int] = (1, 0, 2, 0),
        suffix: tuple[int, int, int, int] = (0x23, 0x81, 0x31, 0x0E),
        user: str = "PDPOWERMINIV1",
        part: str = "app",
        payload: bytes = b"\x00" * 64,
    ) -> Path:
        path = tmp_path / name
        old_raw = bytes([1, 0, 0, 4]) + bytes(12)
        new_raw = bytes(version) + bytes(suffix) + bytes(8)
        user_raw = user.encode("ascii").ljust(16, b"\x00")
        part_raw = part.encode("ascii").ljust(16, b"\x00")
        fields = (
            b"fpk\x00", b"\x00" * 4, old_raw, new_raw, user_raw, part_raw,
            len(payload), len(payload) + HEAD_SIZE, 0,
            zlib.crc32(payload), 0x12345678, 0,
        )
        partial = HEAD.pack(*fields)
        head_crc = zlib.crc32(partial[:92])
        head = HEAD.pack(*fields[:-1], head_crc)
        path.write_bytes(head + payload)
        return path

    return _make
