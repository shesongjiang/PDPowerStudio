"""协议层测试：全部使用 COM48 实测真实帧，无设备也能跑。"""
from __future__ import annotations

import pytest

from pdpower_studio import protocol as p

# ---- 实机抓到的原始响应（USB CDC 模式，无 len、0x0A 结尾） ----
NAME = b"WeAct Studio PD Power Mini V1 BUCK"
RAW_81 = b"\x81" + NAME + b"\x0a"                  # 36 B
RAW_C2 = b"\xc2" + b"V1.0.0.4_0e318123" + b"\x0a"  # 19 B
RAW_C3 = b"\xc3" + b"acde750d9f3d" + b"\x0a"       # 14 B
RAW_82 = bytes.fromhex("82 00 0a")
RAW_83 = bytes.fromhex("83 04 0a")
RAW_84 = bytes.fromhex("84 00 88 13 d0 07 0a")
RAW_85 = bytes.fromhex("85 00 00 00 00 0a")
RAW_86 = bytes.fromhex("86 00 0a")
RAW_87 = bytes.fromhex("87 01 0a")
RAW_88 = bytes.fromhex("88 0a 0a")
RAW_C6 = bytes.fromhex("c6 00 0a")


def decode_one(raw: bytes) -> p.Frame:
    frames = p.FrameDecoder(p.FrameMode.USB).feed(raw)
    assert len(frames) == 1, f"期望 1 帧，实际 {len(frames)}: {raw.hex(' ')}"
    return frames[0]


def test_frame_lengths_match_real_capture():
    assert len(RAW_81) == 36
    assert len(RAW_C2) == 19
    assert len(RAW_C3) == 14
    assert p.FIXED_FRAME_LEN[0x85] == 6


def test_decode_who_am_i_36_bytes_without_length_byte():
    frame = decode_one(RAW_81)
    assert frame.cmd == 0x81
    assert frame.payload == NAME
    assert frame.text == "WeAct Studio PD Power Mini V1 BUCK"


def test_decode_version_frame_c2():
    frame = decode_one(RAW_C2)
    version, suffix, text = p.parse_version(frame)
    assert version == (1, 0, 0, 4)
    assert suffix == "0e318123"
    assert text == "V1.0.0.4_0e318123"


def test_decode_serial_frame_c3():
    frame = decode_one(RAW_C3)
    assert frame.text == "acde750d9f3d"


def test_decode_output_state_82():
    frame = decode_one(RAW_82)
    assert p.parse_output_state(frame) == {"output_enabled": False, "output_mode": 0}


def test_decode_output_id_83():
    assert p.parse_output_id(decode_one(RAW_83)) == 4


def test_decode_output_data_84_m0_5v_2a():
    frame = decode_one(RAW_84)
    assert p.parse_output_id_and_data(frame) == (0, 5000, 2000)


def test_decode_display_85_zero():
    assert p.parse_display(decode_one(RAW_85)) == (0, 0)


def test_decode_switches_86_87_88_and_lcd_c6():
    assert p.parse_bool(decode_one(RAW_86)) is False
    assert p.parse_bool(decode_one(RAW_87)) is True
    assert decode_one(RAW_88).payload == b"\x0a"
    assert decode_one(RAW_C6).payload == b"\x00"


def test_decode_input_state_8a_layout():
    # 0x8A 只在 >=1.0.2.0 有响应；这里按协议表构造 7 字节帧验证字段偏移
    raw = bytes.fromhex("8a 05 84 03 20 03 0a")
    frame = decode_one(raw)
    info = p.parse_input_state(frame)
    assert info == {"input_state": 5, "input_mv": 900, "pd_set_dv": 0x0320}


def test_decoder_tolerates_noise_and_split_chunks():
    decoder = p.FrameDecoder(p.FrameMode.USB)
    assert decoder.feed(b"\xff\xfe") == []
    assert decoder.feed(RAW_81[:10]) == []
    frames = decoder.feed(RAW_81[10:])
    assert [f.cmd for f in frames] == [0x81]
    assert frames[0].payload == NAME


def test_decoder_reads_back_to_back_frames():
    raw = RAW_82 + RAW_83 + RAW_84
    frames = p.FrameDecoder(p.FrameMode.USB).feed(raw)
    assert [f.cmd for f in frames] == [0x82, 0x83, 0x84]


def test_encode_output_enable_usb():
    assert p.encode(p.Command.OUTPUT_ENABLE, [1]) == bytes.fromhex("02 01 0a")
    assert p.encode(p.Command.OUTPUT_ENABLE, [0]) == bytes.fromhex("02 00 0a")


def test_encode_output_data_usb_m0_5v_2000ma():
    payload = p.encode(p.Command.OUTPUT_DATA, [0, 0x88, 0x13, 0xD0, 0x07])
    assert payload == bytes.fromhex("04 00 88 13 d0 07 0a")


def test_encode_upgrade_magic_is_41_a5_aa_0a():
    assert p.encode_upgrade() == bytes.fromhex("41 a5 aa 0a")
    assert p.encode(0x41) == bytes.fromhex("41 0a")  # 普通编码不会得到 A5AA
    assert p.UPGRADE_MAGIC == bytes.fromhex("41 a5 aa 0a")


def test_uart_mode_crc_roundtrip_fixed_frame():
    # UART 模式下固定响应为 [cmd][payload...][crc8]，总长与 USB 的 FIXED_FRAME_LEN 相同
    body = bytes([0x85, 0x00, 0x00, 0x00, 0x00])
    raw = body + bytes([p.crc8(body)])
    assert len(raw) == p.FIXED_FRAME_LEN[0x85]
    frames = p.FrameDecoder(p.FrameMode.UART).feed(raw)
    assert [f.cmd for f in frames] == [0x85]
    assert p.parse_display(frames[0]) == (0, 0)


def test_uart_mode_variable_response_has_length_byte():
    body = bytes([0x81, len(NAME)]) + NAME
    raw = body + bytes([p.crc8(body)])
    frames = p.FrameDecoder(p.FrameMode.UART).feed(raw)
    assert len(frames) == 1
    assert frames[0].payload == NAME


def test_crc_helpers_basic_properties():
    assert p.crc8(b"") == 0xFF
    assert p.crc8([0x81]) != p.crc8([0x82])
    # CRC16-XMODEM 标准校验值："123456789" → 0x31C3
    assert p.crc16_xmodem(b"123456789") == 0x31C3


def test_read_only_whitelist_excludes_all_writes():
    writes = {
        p.Command.OUTPUT_ENABLE, p.Command.OUTPUT_ID, p.Command.OUTPUT_DATA,
        p.Command.OCP_ENABLE, p.Command.COMPENSATE_ENABLE, p.Command.BRIGHTNESS,
        p.Command.PD_VOLTAGE, p.Command.SYSTEM_UPGRADE, p.Command.OUTPUT_CONFIG_SAVE,
        p.Command.FACTORY_DATA_RESET, p.Command.SYSTEM_RESET,
    }
    assert writes.isdisjoint(p.READ_ONLY_COMMANDS)
    assert p.Command.READ_OUTPUT_DISPLAY in p.READ_ONLY_COMMANDS


def test_parser_rejects_bad_payload_length():
    with pytest.raises(p.FrameError):
        p.parse_output_state(p.Frame(0x82, b"\x00\x00", b""))
    with pytest.raises(p.FrameError):
        p.parse_version(p.Frame(0xC2, b"V1.0.0", b""))
