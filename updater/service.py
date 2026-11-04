"""固件升级编排：发现 → 校验 → 确认 → 进 bootloader → Y-Modem → 复核。

安全默认：dry_run=True 且 allow_real_flash=False —— 只做“计划与校验”，
不会发送 0x41，也不会打开 bootloader 端口。真刷必须显式双重解锁 + 确认口令。
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

import serial

from .. import protocol as p
from ..transport import PortLock
from .fpk import FpkInfo, VerifyReport, verify_fpk
from .github_source import FirmwareAsset, GitHubFirmwareSource, UpdateError
from .ymodem import YModemError, YModemSender

CONFIRM_TOKEN = "I-UNDERSTAND"


class UpgradeState(str, Enum):
    IDLE = "idle"
    DISCOVERED = "discovered"
    VERIFIED = "verified"
    ENTERING = "entering_bootloader"
    TRANSFERRING = "transferring"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"


@dataclass
class UpgradePlan:
    port: str
    model_key: str
    current_version: tuple[int, int, int, int]
    asset_path: Path
    allow_downgrade: bool = False
    report: VerifyReport | None = None


class UpgradeBlocked(PermissionError):
    pass


class UpgradeService:
    def __init__(
        self,
        cache_dir: Path | None = None,
        dry_run: bool = True,
        allow_real_flash: bool = False,
        retries: int = 3,
        source_factory: Callable[[str], GitHubFirmwareSource] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir or Path.home() / ".pdpower_studio" / "firmware")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run
        self.allow_real_flash = allow_real_flash
        self.retries = retries
        self.source_factory = source_factory or (lambda key: GitHubFirmwareSource(key, cache_dir=self.cache_dir / "downloads"))
        self.state = UpgradeState.IDLE
        self.last_error: str | None = None

    # ------------------------------------------------------------- 查询
    def discover(self, model_key: str) -> FirmwareAsset:
        source = self.source_factory(model_key)
        asset = source.find_latest()
        self.state = UpgradeState.DISCOVERED
        return asset

    def download(self, model_key: str, asset: FirmwareAsset) -> Path:
        source = self.source_factory(model_key)
        return source.download(asset, self.cache_dir / "downloads")

    # ------------------------------------------------------------- 校验
    def prepare(self, plan: UpgradePlan) -> VerifyReport:
        report = verify_fpk(
            plan.asset_path,
            model_key=plan.model_key,
            current_version=plan.current_version,
            allow_downgrade=plan.allow_downgrade,
        )
        plan.report = report
        self.state = UpgradeState.VERIFIED if report.ok else UpgradeState.FAILED
        if not report.ok:
            self.last_error = "; ".join(report.errors)
        return report

    # ------------------------------------------------------------- 执行
    def execute(
        self,
        plan: UpgradePlan,
        confirm: str,
        progress: Callable[[str, float], None] | None = None,
    ) -> dict:
        report = plan.report or self.prepare(plan)
        if not report.ok or report.info is None:
            return {"executed": False, "reason": "校验未通过", "errors": report.errors}
        if self.dry_run or not self.allow_real_flash:
            return {
                "executed": False,
                "reason": "dry-run（默认不执行真刷）。勾选“允许真实刷写”并输入确认口令后才会发送 0x41",
                "plan": {
                    "port": plan.port,
                    "current_version": ".".join(map(str, plan.current_version)),
                    "target_version": report.info.version_text,
                    "bytes": report.info.size,
                    "sha256": report.info.sha256,
                    "steps": [
                        "0x41 A5 AA 0A @9600 → bootloader",
                        "端口重枚举 → 115200",
                        "Y-Modem 1K/CRC16 传输",
                        "EOT",
                        "重新握手读 0xC2 复核版本",
                    ],
                },
                "firmware": report.info.to_dict(),
            }
        if confirm != CONFIRM_TOKEN:
            raise UpgradeBlocked(f"确认口令不符，需输入 {CONFIRM_TOKEN}")

        attempts = 0
        last_exc: Exception | None = None
        while attempts < self.retries:
            attempts += 1
            if progress:
                progress("attempt", attempts / self.retries)
            try:
                self._flash_once(plan, progress)
                self.state = UpgradeState.VERIFYING
                verify = self._verify_after_flash(plan)
                ok_verify = bool(verify.get("match"))
                self.state = UpgradeState.DONE if ok_verify else UpgradeState.FAILED
                return {
                    "executed": True, "attempts": attempts, "ok": ok_verify,
                    "firmware": report.info.to_dict(), **verify,
                }
            except (OSError, serial.SerialException, TimeoutError, YModemError) as exc:
                last_exc = exc
                self.last_error = str(exc)
                self.state = UpgradeState.FAILED
                time.sleep(0.5)
        return {"executed": True, "attempts": attempts, "ok": False, "error": str(last_exc)}

    def _flash_once(self, plan: UpgradePlan, progress: Callable[[str, float], None] | None) -> None:
        assert plan.report and plan.report.info
        # 整个刷写过程（含 0x41 进 bootloader）都必须持有端口锁：
        # 若 GUI/MCP 正占用同一端口，acquire 会抛 PortBusy，
        # 绝不能在未持锁时直接打开串口发送 0x41。
        lock = PortLock(plan.port)
        lock.acquire()
        try:
            self._enter_bootloader(plan.port)
            self.state = UpgradeState.TRANSFERRING
            ser = serial.Serial(plan.port, 115200, timeout=0.1, write_timeout=1.0)
            try:
                sender = YModemSender(
                    write=lambda payload: ser.write(payload),
                    read=lambda timeout: ser.read(64),
                    timeout=3.0,
                    retries=10,
                    progress=(lambda sent, total: progress("send", sent / max(total, 1))) if progress else None,
                )
                sender.send(plan.asset_path.name, plan.asset_path.read_bytes())
            finally:
                ser.close()
        finally:
            lock.release()

    def _verify_after_flash(self, plan: UpgradePlan) -> dict:
        """真读复核：等待端口重枚举后只读握手，读 0xC2 与目标版本比对。

        旧实现只是 sleep 后假装复核，这里改为实际打开串口读版本。
        """
        expected = plan.report.info.version_text if plan.report and plan.report.info else ""
        expected_tuple = plan.report.info.new_version if plan.report and plan.report.info else ()
        from ..transport import DeviceLink, LinkError, PortBusy

        deadline = time.time() + 20.0
        last_error = ""
        while time.time() < deadline:
            time.sleep(1.0)
            link = DeviceLink(read_only=True)
            try:
                link.open(plan.port, baud=115200, acquire_lock=False)
                try:
                    snap = link.handshake()
                finally:
                    link.close()
                version = snap.get("version") or ""
                version_tuple = tuple(snap.get("version_tuple") or ())
                if version:
                    return {
                        "verified_version": version,
                        "expected": expected,
                        "version_tuple": list(version_tuple),
                        "match": version_tuple == tuple(expected_tuple),
                    }
                last_error = "握手未返回版本"
            except (PortBusy, LinkError, OSError, TimeoutError) as exc:
                last_error = str(exc)
        return {
            "verified_version": "", "expected": expected, "match": False,
            "error": f"20 s 内未能重新握手复核: {last_error}",
        }

    def _enter_bootloader(self, port: str) -> None:
        """0x41 进入升级模式：9600 + 41 A5 AA 0A；若设备已在 bootloader（发 'C'）则不重复发送。

        调用方必须已持有 PortLock。
        """
        self.state = UpgradeState.ENTERING
        with serial.Serial(port, 9600, timeout=0.2, write_timeout=0.5) as ser:
            if b"C" not in ser.read(2):
                ser.write(p.encode_upgrade())
        time.sleep(0.6)

    # ------------------------------------------------------------- 缓存
    def cache_previous(self, current: Path) -> Path:
        """保留上一版 fpk，供降级/回滚。"""
        target = self.cache_dir / "previous"
        target.mkdir(parents=True, exist_ok=True)
        dest = target / current.name
        if current.exists():
            shutil.copy2(current, dest)
        return dest
