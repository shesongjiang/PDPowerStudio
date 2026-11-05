"""设备模型 / 能力位 / 写入口测试（用假 link，无设备）。"""
from __future__ import annotations

import pytest

from pdpower_studio import protocol as p
from pdpower_studio.device_model import (
    MODEL_V1, MODEL_V11, Calibration, DeviceService, DeviceSnapshot, apply_frame,
    capabilities_for, detect_model, parse_version_text,
)


def make_snapshot(version=(1, 0, 0, 4), model=MODEL_V1) -> DeviceSnapshot:
    snap = DeviceSnapshot(port="COM48", model=model, version=version,
                          version_text="V1.0.0.4", sn="acde750d9f3d")
    snap.caps = capabilities_for(model, version)
    snap.groups = [(5000, 2000), (5000, 3000), (9000, 3000), (9000, 2000), (11000, 3000)]
    snap.disp_mv, snap.disp_ma = 5000, 2000
    return snap


class FakeLink:
    """带最小回读仿真的假链路：写入后能返回一致的 0x84/0x83/0x82。"""

    def __init__(self, snapshot: DeviceSnapshot) -> None:
        self.snapshot = snapshot
        self.sent: list[tuple[int, list[int]]] = []
        self.polling = True
        self._readback: dict[int, p.Frame] = {}

    def snapshot_copy(self) -> dict:
        return self.snapshot.to_dict()

    def set_polling(self, enabled: bool) -> None:
        self.polling = enabled

    def send(self, cmd: int, args=()) -> None:
        args = list(args)
        self.sent.append((cmd, args))
        if cmd == p.Command.OUTPUT_DATA:
            gid, mvl, mvh, mal, mah = args
            mv, ma = mvl | (mvh << 8), mal | (mah << 8)
            apply_frame(self.snapshot, p.Frame(
                p.Command.READ_OUTPUT_DATA,
                bytes([gid, mv & 0xFF, mv >> 8, ma & 0xFF, ma >> 8]), b""))
            self._readback[p.Command.READ_OUTPUT_DATA] = p.Frame(
                p.Command.READ_OUTPUT_DATA,
                bytes([gid, mv & 0xFF, mv >> 8, ma & 0xFF, ma >> 8]), b"")
        elif cmd == p.Command.OUTPUT_ID:
            gid = args[0]
            self.snapshot.group = gid
            self._readback[p.Command.READ_OUTPUT_ID] = p.Frame(
                p.Command.READ_OUTPUT_ID, bytes([gid]), b"")
        elif cmd == p.Command.OUTPUT_ENABLE:
            state = 1 if args[0] else 0
            self.snapshot.output_enabled = bool(state)
            self._readback[p.Command.READ_OUTPUT_STATE] = p.Frame(
                p.Command.READ_OUTPUT_STATE, bytes([state]), b"")

    def request(self, cmd: int, args=(), timeout: float = 0.4) -> p.Frame:
        if cmd not in self._readback:
            raise AssertionError(f"未预期的回读命令 0x{cmd:02X}")
        return self._readback[cmd]


def test_detect_model_by_sn_prefix():
    assert detect_model("acde750d9f3d") == MODEL_V1
    assert detect_model("ACDE750D9F3D") == MODEL_V1
    assert detect_model("B3AABBCCDDEE") == MODEL_V11
    assert detect_model("") is None
    assert detect_model("ZZ123") is None


def test_parse_version_text_accepts_lowercase_v_and_suffix():
    version, suffix = parse_version_text("V1.0.0.4_0e318123")
    assert version == (1, 0, 0, 4)
    assert suffix == "0e318123"
    with pytest.raises(ValueError):
        parse_version_text("1.0.4")


def test_capabilities_v1_at_1_0_0_4_gate_pd_and_input_state():
    caps = capabilities_for(MODEL_V1, (1, 0, 0, 4))
    assert caps is not None
    assert caps.max_voltage_v == 20.0
    assert caps.max_current_a == 3.0
    assert caps.input_state is False     # 需 >=1.0.2.0
    assert caps.pd_vset is False         # 需 >=1.0.1.0
    assert caps.uart_mode is False       # 需 >=1.0.0.5


def test_capabilities_v1_after_upgrade():
    caps = capabilities_for(MODEL_V1, (1, 0, 2, 0))
    assert caps.input_state is True
    assert caps.pd_vset is True


def test_clamp_rejects_out_of_range():
    caps = capabilities_for(MODEL_V1, (1, 0, 0, 4))
    assert caps.clamp(5.0, 2.0) == (5.0, 2.0)
    with pytest.raises(ValueError):
        caps.clamp(20.5, 1.0)
    with pytest.raises(ValueError):
        caps.clamp(5.0, 3.5)
    v11 = capabilities_for(MODEL_V11, (1, 0, 2, 0))
    assert v11.clamp(24.0, 3.0) == (24.0, 3.0)


def test_apply_frame_merges_real_frames_into_snapshot():
    snap = DeviceSnapshot()
    decoder = p.FrameDecoder(p.FrameMode.USB)
    frames = decoder.feed(
        b"\xc3" + b"acde750d9f3d" + b"\x0a"
        + b"\xc2" + b"V1.0.0.4_0e318123" + b"\x0a"
        + bytes.fromhex("82 00 0a")
        + bytes.fromhex("83 04 0a")
        + bytes.fromhex("84 02 28 23 b8 0b 0a")   # M2 = 9000 mV / 3000 mA
        + bytes.fromhex("85 88 13 d0 07 0a")      # 5000 mV / 2000 mA
        + bytes.fromhex("86 00 0a")
        + bytes.fromhex("87 01 0a")
        + bytes.fromhex("88 0a 0a")
        + bytes.fromhex("c6 00 0a")
    )
    for frame in frames:
        apply_frame(snap, frame)

    assert snap.sn == "acde750d9f3d"
    assert snap.model == MODEL_V1
    assert snap.version == (1, 0, 0, 4)
    assert snap.version_hash == "0e318123"
    assert snap.output_enabled is False
    assert snap.group == 4
    assert snap.groups[2] == (9000, 3000)
    assert snap.groups[4] == (0, 0)          # 未被 0x84 读到的组保持默认占位
    assert (snap.disp_mv, snap.disp_ma) == (5000, 2000)
    assert snap.ocp is False and snap.compensate is True
    assert snap.brightness == 10
    assert snap.lcd_name == "HAN"
    assert snap.caps.input_state is False


def test_snapshot_to_dict_shape():
    data = make_snapshot().to_dict()
    assert data["display"]["voltage_v"] == 5.0
    assert data["display"]["power_w"] == pytest.approx(10.0)
    assert data["capabilities"]["pd_vset"] is False
    assert len(data["groups"]) == 5


def test_calibration_default_is_identity():
    mvg, mag = Calibration().apply(5000, 2000)
    assert (mvg, mag) == (5000, 2000)
    calibrated = Calibration(voltage_gain=1.001, current_gain=0.998).apply(5000, 2000)
    assert calibrated[0] == 5005
    assert calibrated[1] == 1996


def test_service_set_voltage_current_encodes_0x04():
    link = FakeLink(make_snapshot())
    service = DeviceService(link)
    service.set_voltage_current(5.0, 2.0)
    cmd, args = link.sent[-1]
    assert cmd == p.Command.OUTPUT_DATA
    assert args == [0, 0x88, 0x13, 0xD0, 0x07]


def test_service_rejects_out_of_range_before_writing():
    link = FakeLink(make_snapshot())
    service = DeviceService(link)
    with pytest.raises(ValueError):
        service.set_voltage_current(21.0, 1.0)
    assert link.sent == []


def test_service_output_enable_requires_confirm():
    link = FakeLink(make_snapshot())
    service = DeviceService(link)
    with pytest.raises(PermissionError):
        service.output_enable(True)
    service.output_enable(True, confirm=True)
    assert link.sent[-1] == (p.Command.OUTPUT_ENABLE, [1])
    service.output_enable(False)
    assert link.sent[-1] == (p.Command.OUTPUT_ENABLE, [0])


def test_service_save_config_requires_confirm():
    link = FakeLink(make_snapshot())
    service = DeviceService(link)
    with pytest.raises(PermissionError):
        service.save_config()
    service.save_config(confirm=True)
    assert link.sent[-1][0] == p.Command.OUTPUT_CONFIG_SAVE


def test_service_pd_voltage_is_gated_by_firmware():
    link = FakeLink(make_snapshot())
    service = DeviceService(link)
    with pytest.raises(PermissionError) as exc:
        service.set_pd_voltage(12.0, confirm=True)
    assert "1.0.2.0" in str(exc.value)
    assert link.sent == []


def test_service_set_group_blocked_when_output_on():
    snap = make_snapshot()
    snap.output_enabled = True
    link = FakeLink(snap)
    service = DeviceService(link)
    with pytest.raises(PermissionError):
        service.set_group(2)
    service.set_group(2, confirm=True)
    assert link.sent[-1] == (p.Command.OUTPUT_ID, [2])


def test_device_link_read_only_guard_blocks_all_writes():
    from pdpower_studio.transport import DeviceLink, LinkError

    link = DeviceLink(read_only=True)
    with pytest.raises(PermissionError):
        link.send(p.Command.SYSTEM_UPGRADE)          # 0x41
    with pytest.raises(PermissionError):
        link.send(p.Command.OUTPUT_ENABLE, [1])      # 0x02
    with pytest.raises(PermissionError):
        link.send(p.Command.OUTPUT_DATA, [0, 0, 0, 0, 0])   # 0x04
    with pytest.raises(LinkError):
        link.send(p.Command.READ_OUTPUT_DISPLAY)     # 只读命令放行，但端口未打开
