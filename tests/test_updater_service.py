"""升级编排测试：默认 dry-run 绝不触碰串口；未解锁/口令错误一律阻断。"""
from __future__ import annotations

import pytest

from pdpower_studio.updater.service import (
    CONFIRM_TOKEN, UpgradeBlocked, UpgradePlan, UpgradeService,
)
from pdpower_studio.updater.ymodem import YModemError


def make_plan(path, current=(1, 0, 0, 4)) -> UpgradePlan:
    plan = UpgradePlan(port="COM48", model_key="PDPOWERMINIV1",
                       current_version=current, asset_path=path)
    return plan


def test_dry_run_verifies_but_never_enters_bootloader(tmp_path, make_fpk):
    service = UpgradeService(cache_dir=tmp_path / "fw", retries=3)
    plan = make_plan(make_fpk())
    report = service.prepare(plan)
    assert report.ok is True

    called: list[str] = []
    service._enter_bootloader = lambda port: called.append(port)  # type: ignore[method-assign]

    result = service.execute(plan, confirm=CONFIRM_TOKEN)
    assert result["executed"] is False
    assert "dry-run" in result["reason"]
    assert called == []


def test_execute_blocks_on_wrong_token_without_flashing(tmp_path, make_fpk):
    service = UpgradeService(cache_dir=tmp_path / "fw", dry_run=False, allow_real_flash=True)
    plan = make_plan(make_fpk())
    plan.report = service.prepare(plan)
    calls: list[int] = []
    service._flash_once = lambda p, prog: calls.append(1)  # type: ignore[method-assign]
    with pytest.raises(UpgradeBlocked):
        service.execute(plan, confirm="not-the-token")
    assert calls == []


def test_execute_retries_three_times_then_fails(tmp_path, make_fpk):
    service = UpgradeService(cache_dir=tmp_path / "fw", dry_run=False,
                             allow_real_flash=True, retries=3)
    plan = make_plan(make_fpk())
    plan.report = service.prepare(plan)
    attempts: list[int] = []

    def boom(p, prog):
        attempts.append(1)
        raise YModemError("simulated transfer failure")

    service._flash_once = boom  # type: ignore[method-assign]
    result = service.execute(plan, confirm=CONFIRM_TOKEN)
    assert len(attempts) == 3
    assert result["attempts"] == 3
    assert result["ok"] is False


def test_prepare_reports_errors_and_does_not_raise(tmp_path, make_fpk):
    service = UpgradeService(cache_dir=tmp_path / "fw")
    plan = make_plan(make_fpk(user="OTHERMODEL"))
    report = service.prepare(plan)
    assert report.ok is False
    assert service.state.value == "failed"
    assert service.last_error


def test_cache_previous_keeps_one_copy(tmp_path, make_fpk):
    service = UpgradeService(cache_dir=tmp_path / "fw")
    path = make_fpk(name="WeAct-PDPowerMiniV1_V1.0.0.4.fpk")
    saved = service.cache_previous(path)
    assert saved.exists()
    assert saved.name == path.name
    assert saved.parent == tmp_path / "fw" / "previous"
