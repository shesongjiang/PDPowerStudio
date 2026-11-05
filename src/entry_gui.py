"""GUI 入口（windowed，无控制台）。

支持两个自检参数，便于打包后不打开窗口就能判断依赖是否完整：
  PDPowerStudio.exe --version
  PDPowerStudio.exe --selftest [报告 JSON 路径]   # 退出码 0=通过
"""
from __future__ import annotations

import sys

from pdpower_studio import __version__
from pdpower_studio.selfcheck import run_selftest


def _emit(text: str) -> None:
    try:
        if sys.stdout is not None:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except Exception:  # pragma: no cover - windowed 模式下可能没有 stdout
        pass


args = sys.argv[1:]
if "--version" in args or "-V" in args:
    _emit(f"PDPower Studio {__version__}")
    raise SystemExit(0)
if "--selftest" in args:
    index = args.index("--selftest")
    report = args[index + 1] if index + 1 < len(args) and not args[index + 1].startswith("-") else None
    raise SystemExit(run_selftest(report))

from pdpower_studio.ui.main_window import main

raise SystemExit(main())
