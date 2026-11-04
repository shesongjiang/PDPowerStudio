"""打包自检：确认 exe 内的 Qt / pyqtgraph / 串口依赖链完整。

GUI 是 windowed 模式，没有控制台，因此结果同时写入 JSON 报告文件，
退出码 0=全部通过、1=有失败项；构建脚本据此判断打包是否可用。

用法：
  PDPowerStudio.exe --selftest [报告路径]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _default_report() -> Path:
    return Path(tempfile.gettempdir()) / "pdpower_selftest.json"


def _check(name: str, fn, results: list[dict]) -> None:
    try:
        detail = fn()
        results.append({"check": name, "ok": True, "detail": str(detail)})
    except Exception as exc:  # noqa: BLE001 - 自检需要捕获全部异常
        results.append({
            "check": name, "ok": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })


def run_selftest(report: str | None = None) -> int:
    # 自检不需要真实屏幕：离屏平台插件必须存在（qoffscreen.dll）
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    started = time.perf_counter()
    results: list[dict] = []

    def qt_modules():
        # QtOpenGL / QtOpenGLWidgets 是 pyqtgraph 的硬依赖，必须能导入
        import PySide6.QtCore  # noqa: F401
        import PySide6.QtGui  # noqa: F401
        import PySide6.QtOpenGL  # noqa: F401
        import PySide6.QtOpenGLWidgets  # noqa: F401
        import PySide6.QtWidgets  # noqa: F401
        import PySide6
        return f"PySide6 {PySide6.__version__}: Core/Gui/Widgets/OpenGL/OpenGLWidgets"

    def qt_app():
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        return f"QApplication ok, platform={app.platformName()}"

    def pyqtgraph_plot():
        import pyqtgraph as pg

        widget = pg.PlotWidget()
        widget.plot([0.0, 1.0, 2.0], [0.0, 1.0, 4.0])
        widget.close()
        return f"pyqtgraph {pg.__version__}: PlotWidget + plot ok"

    def plots_module():
        from pdpower_studio.ui.plots import DualAxisPlot, lttb_indices

        plot = DualAxisPlot()
        plot.add(time.time(), 5002, 818)
        plot.add(time.time() + 0.2, 5001, 820)
        sampled = len(lttb_indices(list(range(100)), list(range(100)), 10))
        plot.close()
        return f"DualAxisPlot ok, samples={len(plot.samples)}, lttb={sampled}"

    def main_window():
        from pdpower_studio.ui.main_window import MainWindow

        window = MainWindow()
        window.close()
        return "MainWindow 构造成功"

    def serial_stack():
        import serial
        from serial.tools import list_ports

        ports = [p.device for p in list_ports.comports()]
        return f"pyserial {serial.__version__}: {len(ports)} 个串口"

    def app_chain():
        from pdpower_studio import __version__
        from pdpower_studio import protocol
        from pdpower_studio.device_model import capabilities_for
        from pdpower_studio.logging_store import estimate_size
        from pdpower_studio.updater.fpk import verify_fpk  # noqa: F401

        frame = protocol.encode(0x81, (0x01,))
        assert frame[0] == 0x81
        caps = capabilities_for("PDPOWERMINIV1", (1, 0, 0, 4))
        size = estimate_size(5.0, 8.0, "csv")
        return (
            f"v{__version__}, protocol 0x81 ok, "
            f"caps={caps.max_voltage_v:g}V/{caps.max_current_a:g}A, "
            f"8h/5Hz={size['mib']} MiB"
        )

    _check("qt_modules", qt_modules, results)
    _check("qt_app", qt_app, results)
    _check("pyqtgraph_plot", pyqtgraph_plot, results)
    _check("plots_module", plots_module, results)
    _check("main_window", main_window, results)
    _check("serial_stack", serial_stack, results)
    _check("app_chain", app_chain, results)

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    ok = all(item["ok"] for item in results)
    payload = {
        "ok": ok,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "python": sys.version.split()[0],
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "elapsed_ms": elapsed_ms,
        "results": results,
    }

    target = Path(report) if report else _default_report()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    for item in results:
        marker = "PASS" if item["ok"] else "FAIL"
        _emit(f"[{marker}] {item['check']}: {item['detail']}")
    _emit(f"[selftest] ok={ok} elapsed={elapsed_ms} ms report={target}")
    return 0 if ok else 1


def _emit(text: str) -> None:
    """windowed exe 下 sys.stdout 可能为 None。"""
    try:
        if sys.stdout is not None:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except Exception:  # pragma: no cover
        pass
