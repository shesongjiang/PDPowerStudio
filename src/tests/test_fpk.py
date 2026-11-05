"""`.fpk` 解析与校验测试（用合成头，验证 CRC/型号/分区/降级规则）。"""
from __future__ import annotations

import pytest

from pdpower_studio.updater.fpk import FpkError, parse_fpk, verify_fpk


def test_parse_fpk_header_fields(make_fpk):
    path = make_fpk()
    info = parse_fpk(path)
    assert info.new_version == (1, 0, 2, 0)
    assert info.new_version_suffix == "0e318123"
    assert info.version_text == "V1.0.2.0_0e318123"
    assert info.user_string == "PDPOWERMINIV1"
    assert info.part_name == "app"
    assert info.header_crc_ok is True
    assert info.size == 96 + 64


def test_verify_accepts_matching_package(make_fpk):
    report = verify_fpk(make_fpk(), "PDPOWERMINIV1", (1, 0, 0, 4))
    assert report.ok is True
    assert report.errors == []
    assert report.info is not None
    assert report.to_dict()["firmware"]["version"] == "V1.0.2.0_0e318123"


def test_verify_rejects_wrong_model(make_fpk):
    report = verify_fpk(make_fpk(user="PDPOWERMINIV11"), "PDPOWERMINIV1", (1, 0, 0, 4))
    assert report.ok is False
    assert any("型号" in e for e in report.errors)


def test_verify_rejects_non_app_partition(make_fpk):
    report = verify_fpk(make_fpk(part="boot"), "PDPOWERMINIV1", (1, 0, 0, 4))
    assert report.ok is False
    assert any("分区" in e for e in report.errors)


def test_verify_blocks_downgrade_by_default_and_allows_with_flag(make_fpk):
    path = make_fpk(version=(1, 0, 0, 3))
    blocked = verify_fpk(path, "PDPOWERMINIV1", (1, 0, 0, 4))
    assert blocked.ok is False
    assert blocked.downgrade is True
    allowed = verify_fpk(path, "PDPOWERMINIV1", (1, 0, 0, 4), allow_downgrade=True)
    assert allowed.ok is True


def test_verify_warns_on_same_version(make_fpk):
    report = verify_fpk(make_fpk(), "PDPOWERMINIV1", (1, 0, 2, 0))
    assert report.ok is True
    assert any("相同" in w for w in report.warnings)


def test_verify_detects_corrupted_header_crc(make_fpk):
    path = make_fpk()
    data = bytearray(path.read_bytes())
    data[40] ^= 0xFF                 # 破坏头部（不改 head_crc32 字段）
    path.write_bytes(bytes(data))
    report = verify_fpk(path, "PDPOWERMINIV1")
    assert report.ok is False
    assert any("CRC32" in e for e in report.errors)


def test_parse_fpk_rejects_bad_magic_or_short_file(tmp_path):
    bad = tmp_path / "bad.fpk"
    bad.write_bytes(b"XXXX" + b"\x00" * 100)
    with pytest.raises(FpkError):
        parse_fpk(bad)
    short = tmp_path / "short.fpk"
    short.write_bytes(b"fpk\x00")
    with pytest.raises(FpkError):
        parse_fpk(short)
