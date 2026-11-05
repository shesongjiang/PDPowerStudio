"""`.fpk` 固件包解析与校验（只校验头部，载荷为加密数据，无法验证内容）。

头部 96 字节布局（实机 1.0.2.0 包已验证）：
  name(4) cfg(4) old_ver(16) new_ver(16) user(16) part(16)
  raw_size(4) pkg_size(4) timestamp(4) raw_crc32(4) pkg_crc32(4) head_crc32(4)
head_crc32 = zlib.crc32(header[:92])（已实测匹配）。
版本高 4 字节 = major/minor/patch/build，后 4 字节 = pkg_crc32，与设备 0xC2 的后缀一致。
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

HEAD = struct.Struct("<4s4s16s16s16s16sIIIIII")
HEAD_SIZE = HEAD.size


class FpkError(ValueError):
    pass


def decode_version(raw: bytes) -> tuple[tuple[int, int, int, int], str]:
    if len(raw) != 16:
        raise FpkError("version field must be 16 bytes")
    version = (raw[0], raw[1], raw[2], raw[3])
    suffix = f"{raw[7]:02x}{raw[6]:02x}{raw[5]:02x}{raw[4]:02x}"
    return version, suffix


@dataclass
class FpkInfo:
    path: Path
    size: int
    name: bytes
    config: bytes
    old_version: tuple[int, int, int, int]
    new_version: tuple[int, int, int, int]
    new_version_suffix: str
    user_string: str
    part_name: str
    raw_size: int
    pkg_size: int
    timestamp: int
    raw_crc32: int
    pkg_crc32: int
    head_crc32: int
    header_crc_ok: bool
    sha256: str
    payload_bytes: int

    @property
    def version_text(self) -> str:
        return "V" + ".".join(str(v) for v in self.new_version) + f"_{self.new_version_suffix}"

    def to_dict(self) -> dict:
        return {
            "path": str(self.path), "size": self.size, "version": self.version_text,
            "version_tuple": list(self.new_version), "old_version": list(self.old_version),
            "user_string": self.user_string, "part": self.part_name,
            "raw_size": self.raw_size, "pkg_size": self.pkg_size,
            "header_crc_ok": self.header_crc_ok, "sha256": self.sha256,
        }


def parse_fpk(path: str | Path) -> FpkInfo:
    path = Path(path)
    data = path.read_bytes()
    if len(data) < HEAD_SIZE:
        raise FpkError("文件过小，不是合法 fpk")
    head = data[:HEAD_SIZE]
    (name, config, old_raw, new_raw, user_raw, part_raw,
     raw_size, pkg_size, timestamp, raw_crc, pkg_crc, head_crc) = HEAD.unpack(head)
    if name[:3] != b"fpk":
        raise FpkError(f"magic 不匹配: {name!r}")
    old_version, _ = decode_version(old_raw)
    new_version, suffix = decode_version(new_raw)
    return FpkInfo(
        path=path, size=len(data), name=name, config=config,
        old_version=old_version, new_version=new_version, new_version_suffix=suffix,
        user_string=user_raw.split(b"\x00")[0].decode("ascii", errors="replace"),
        part_name=part_raw.split(b"\x00")[0].decode("ascii", errors="replace"),
        raw_size=raw_size, pkg_size=pkg_size, timestamp=timestamp,
        raw_crc32=raw_crc, pkg_crc32=pkg_crc, head_crc32=head_crc,
        header_crc_ok=zlib.crc32(head[:92]) == head_crc,
        sha256=hashlib.sha256(data).hexdigest(),
        payload_bytes=len(data) - HEAD_SIZE,
    )


@dataclass
class VerifyReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: FpkInfo | None = None
    downgrade: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "errors": self.errors, "warnings": self.warnings,
            "downgrade": self.downgrade,
            "firmware": self.info.to_dict() if self.info else None,
        }


def verify_fpk(
    path: str | Path,
    model_key: str,
    current_version: tuple[int, int, int, int] | None = None,
    allow_downgrade: bool = False,
) -> VerifyReport:
    report = VerifyReport(ok=False)
    try:
        info = parse_fpk(path)
    except (FpkError, OSError) as exc:
        report.errors.append(str(exc))
        return report

    report.info = info
    if not info.header_crc_ok:
        report.errors.append("header CRC32 校验失败，文件损坏或被篡改")
    if info.user_string != model_key:
        report.errors.append(f"固件型号 {info.user_string!r} 与设备型号 {model_key!r} 不匹配，拒绝刷写")
    if info.part_name != "app":
        report.errors.append(f"分区 {info.part_name!r} 非 app，拒绝刷写")
    if info.size > 8 * 1024 * 1024:
        report.errors.append("文件体积异常（>8 MiB）")
    if info.raw_size <= 0 or info.raw_size > info.size:
        report.warnings.append("raw_size 与文件体积不一致（包结构可能变化）")
    if current_version is not None:
        if info.new_version < tuple(current_version):
            report.downgrade = True
            if not allow_downgrade:
                report.errors.append(
                    f"目标版本 {info.version_text} 低于当前版本 "
                    f"{'.'.join(map(str, current_version))}，默认禁止降级"
                )
        elif info.new_version == tuple(current_version):
            report.warnings.append("目标版本与当前版本相同，无需升级")
    if info.old_version == (0, 0, 0, 0):
        report.warnings.append("包内 old_ver 为空，包本身未限制来源版本")

    report.ok = not report.errors
    return report
