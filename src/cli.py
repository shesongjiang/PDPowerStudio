"""命令行入口：只读探针 / MCP server / 固件包校验 / 日志体积估算。

只读探针默认 acquire_lock=False（不写锁文件），且 DeviceLink(read_only=True) 会
拦截任何非白名单命令——即使误传写命令也不会下发到设备。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .device_model import Calibration, parse_version_text
from .logging_store import estimate_size
from .transport import DeviceLink, LinkError, PortBusy, list_pdpower_ports

EXPECTED_V1 = {
    "groups": [(5000, 2000), (5000, 3000), (9000, 3000), (9000, 2000), (11000, 3000)],
    "ocp": False,
    "compensate": True,
    "brightness": 10,
    "lcd_name": "HAN",
    "output_enabled": False,
}


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_list(_: argparse.Namespace) -> int:
    ports = list_pdpower_ports()
    if not ports:
        print("未发现 VID 1A86 / PID FE0C 的 PDPower 设备")
        return 0
    for item in ports:
        print(f"{item['port']:<8} {item['model']:<14} SN={item['sn']} {item['description']}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    link = DeviceLink(read_only=True, poll_display_s=args.interval)
    stamps: list[float] = []
    link.on_snapshot = lambda _snap: stamps.append(time.monotonic())
    print(f"[read-only] 打开 {args.port} @115200，禁止一切写命令", file=sys.stderr)
    try:
        link.open(args.port, baud=115200, acquire_lock=args.lock)
    except PortBusy as exc:
        print(f"[错误] 串口被占用：{exc}", file=sys.stderr)
        print("提示：关闭官方上位机/串口助手后重试，或用 list 换一个端口。", file=sys.stderr)
        return 2
    except (LinkError, OSError) as exc:
        print(f"[错误] 无法打开 {args.port}：{exc}", file=sys.stderr)
        print("提示：用 `pdpower-cli list` 确认设备与端口是否存在。", file=sys.stderr)
        return 2
    try:
        handshake = link.handshake()
        t0 = time.monotonic()
        while time.monotonic() - t0 < args.seconds:
            time.sleep(0.02)
        elapsed = time.monotonic() - t0
        snap = link.snapshot_copy()
    finally:
        link.close()

    mv, ma = Calibration().apply(snap["display"]["mv"], snap["display"]["ma"])
    rate = len(stamps) / elapsed if elapsed else 0.0
    intervals = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:])]
    mean_interval = sum(intervals) / len(intervals) if intervals else 0.0

    report = {
        "port": args.port,
        "read_only": True,
        "handshake": {
            "name": snap["name"],
            "model": snap["model"],
            "model_name": snap["model_name"],
            "sn": snap["sn"],
            "version": snap["version"],
            "lcd": snap["lcd_name"],
            "capabilities": snap["capabilities"],
        },
        "groups": snap["groups"],
        "output": {
            "enabled": snap["output_enabled"],
            "mode": snap["output_mode"],
            "group": snap["group"],
        },
        "switches": {"ocp": snap["ocp"], "compensate": snap["compensate"],
                     "brightness": snap["brightness"]},
        "input": {"state": snap["input_state"], "mv": snap["input_mv"],
                  "pd_dv": snap["pd_set_dv"]},
        "display": {"mv": snap["display"]["mv"], "ma": snap["display"]["ma"],
                    "calibrated_mv": mv, "calibrated_ma": ma},
        "polling": {"seconds": round(elapsed, 2), "samples": len(stamps),
                    "hz": round(rate, 2), "mean_interval_ms": round(mean_interval, 1)},
        "frames_seen": snap.get("frames_seen", 0),
        "handshake_frames": handshake.get("frames_seen", 0),
    }

    print("=== 只读握手 ===")
    print(f"name     : {report['handshake']['name']}")
    print(f"model    : {report['handshake']['model']} ({report['handshake']['model_name']})")
    print(f"sn       : {report['handshake']['sn']}")
    print(f"version  : {report['handshake']['version']}")
    print(f"lcd      : {report['handshake']['lcd']}")
    print(f"caps     : {report['handshake']['capabilities']}")
    print("=== 五组设定 ===")
    for item in report["groups"]:
        print(f"M{item['id']}: {item['mv']} mV / {item['ma']} mA")
    print("=== 状态 ===")
    print(f"output   : enabled={report['output']['enabled']} mode={report['output']['mode']} "
          f"group=M{report['output']['group']}")
    print(f"switches : {report['switches']}")
    print(f"input    : {report['input']}")
    print(f"display  : {report['display']['mv']} mV / {report['display']['ma']} mA")
    print("=== 轮询 ===")
    print(f"{report['polling']['samples']} samples in {report['polling']['seconds']}s "
          f"→ {report['polling']['hz']} Hz (mean {report['polling']['mean_interval_ms']} ms)")

    exit_code = 0
    if args.check_defaults:
        print("=== 与实测基线对比（V1 默认值）===")
        checks = []
        got_groups = [(g["mv"], g["ma"]) for g in report["groups"]]
        checks.append(("M0–M4", got_groups == EXPECTED_V1["groups"],
                       f"{got_groups} vs {EXPECTED_V1['groups']}"))
        checks.append(("OCP", report["switches"]["ocp"] == EXPECTED_V1["ocp"],
                       str(report["switches"]["ocp"])))
        checks.append(("补偿", report["switches"]["compensate"] == EXPECTED_V1["compensate"],
                       str(report["switches"]["compensate"])))
        checks.append(("亮度", report["switches"]["brightness"] == EXPECTED_V1["brightness"],
                       str(report["switches"]["brightness"])))
        checks.append(("LCD", report["handshake"]["lcd"] == EXPECTED_V1["lcd_name"],
                       report["handshake"]["lcd"]))
        checks.append(("输出关闭", report["output"]["enabled"] == EXPECTED_V1["output_enabled"],
                       str(report["output"]["enabled"])))
        for name, ok, detail in checks:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
            exit_code |= 0 if ok else 1
        report["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已写入 {args.json}")
    return exit_code


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server.server import McpServer
    from .mcp_server.tools import ToolRouter

    router = ToolRouter(read_only=not args.allow_write)
    print(f"[mcp] stdio ready (read_only={not args.allow_write})", file=sys.stderr)
    McpServer(router).serve_forever()
    return 0


def cmd_upgrade_verify(args: argparse.Namespace) -> int:
    from .updater.fpk import verify_fpk

    current = None
    if args.current:
        current, _ = parse_version_text(args.current)
    report = verify_fpk(args.file, args.model, current, args.allow_downgrade)
    _print_json(report.to_dict())
    return 0 if report.ok else 1


def cmd_log_estimate(args: argparse.Namespace) -> int:
    _print_json(estimate_size(args.hz, args.hours, args.format))
    return 0


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(prog="pdpower-studio", description="PDPower Studio CLI")
    parser.add_argument("--version", "-V", action="version", version=f"PDPower Studio {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出 PDPower 串口设备")
    p_list.set_defaults(func=cmd_list)

    p_probe = sub.add_parser("probe", help="只读握手 + 定时轮询（不下发任何写命令）")
    p_probe.add_argument("--port", default="COM48")
    p_probe.add_argument("--seconds", type=float, default=10.0)
    p_probe.add_argument("--interval", type=float, default=0.2, help="0x85 轮询周期（秒）")
    p_probe.add_argument("--json", default="", help="把结果写入 JSON 文件")
    p_probe.add_argument("--check-defaults", action="store_true", help="与 V1 默认值逐项对比")
    p_probe.add_argument("--lock", action="store_true", help="使用端口锁文件（默认不用）")
    p_probe.set_defaults(func=cmd_probe)

    p_mcp = sub.add_parser("mcp", help="以 stdio MCP server 运行（默认只读）")
    p_mcp.add_argument("--allow-write", action="store_true")
    p_mcp.set_defaults(func=cmd_mcp)

    p_verify = sub.add_parser("upgrade-verify", help="校验 .fpk（不连接设备、不刷写）")
    p_verify.add_argument("--file", required=True)
    p_verify.add_argument("--model", default="PDPOWERMINIV1")
    p_verify.add_argument("--current", default="")
    p_verify.add_argument("--allow-downgrade", action="store_true")
    p_verify.set_defaults(func=cmd_upgrade_verify)

    p_log = sub.add_parser("log-estimate", help="估算挂测日志体积")
    p_log.add_argument("--hz", type=float, default=5.0)
    p_log.add_argument("--hours", type=float, default=24.0)
    p_log.add_argument("--format", default="csv", choices=["csv", "jsonl"])
    p_log.set_defaults(func=cmd_log_estimate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
