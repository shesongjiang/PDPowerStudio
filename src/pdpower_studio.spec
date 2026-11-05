# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包定义：同一份 spec 产出三个独立 target。

- PDPowerStudio  : GUI，windowed（无控制台），含 PySide6 + pyqtgraph
- pdpower-cli    : 控制台，含 probe/list/upgrade-verify/log-estimate，不含 Qt
- pdpower-mcp    : 控制台，stdio JSON-RPC server，不含 Qt

默认 one-folder（dist/）；设置 PDPOWER_ONEFILE=1 后改为 one-file（dist-onefile/）。
三种入口分开 Analysis，避免 CLI/MCP 被迫背上 Qt 依赖（GUI 约 130 MiB，CLI/MCP 约 17 MiB）。
"""
import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(SPECPATH)
ICON = os.path.join(ROOT, "assets", "pdpower.ico")
VERSION_FILE = os.path.join(ROOT, "build", "version_info.txt")
ONEFILE = os.environ.get("PDPOWER_ONEFILE", "0") == "1"

# 剔除测试/可选重依赖与无用 Qt 模块，控制体积
EXCLUDES = [
    "tkinter", "_tkinter", "turtle", "pytest", "_pytest", "pluggy",
    "IPython", "jupyter", "notebook", "matplotlib", "scipy", "pandas",
    "PyQt5", "PyQt6", "PySide2", "shiboken2",
    "pygame", "pydoc_data", "lib2to3", "test",
]

# pyqtgraph 对 QtSvg/QtTest 是 try/except 可选导入，可安全排除；
# 但 PySide6.QtOpenGL / QtOpenGLWidgets 是 pyqtgraph.__Qt.OpenGLHelpers 的硬依赖
# （用 importlib.import_module 动态导入，PyInstaller 静态分析看不到），
# 必须保留模块 + DLL 并显式写进 hiddenimports，否则运行时报
# ModuleNotFoundError: No module named 'PySide6.QtOpenGL'。
GUI_EXCLUDES = EXCLUDES + [
    "PySide6.QtTest",
    "PySide6.QtSvg",
    "PySide6.QtNetwork",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
]

SERIAL_HIDDEN = [
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    "serial.tools.list_ports_common",
]

# pyqtgraph 动态导入，静态分析不可见，必须显式声明
GUI_HIDDEN = SERIAL_HIDDEN + [
    "pyqtgraph",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
]

# hook-PySide6 会把整个 PySide6 目录下的 Qt DLL 和插件都收进来，
# 即使上面已经把对应 Python 模块 exclude 掉。这里按目标路径再剪一遍，
# 只保留 QtCore/QtGui/QtWidgets + 必要的平台与图像插件。
# 注意：qwindows.dll（Windows 平台插件）与 imageformats/qico.dll 必须保留。
_DROP_QT = (
    "pyside6/opengl32sw.dll",          # 软件 OpenGL 回退，本程序纯 Raster 绘制
    "pyside6/qt6quick",                # Quick/Qml 全家桶，Widgets 程序不用
    "pyside6/qt6qml",
    "pyside6/qt6pdf",
    "pyside6/qt6network",
    "pyside6/qt6svg",
    "pyside6/qt6virtualkeyboard",
    "pyside6/plugins/generic/",        # QtUiTools / touch，用不到
    "pyside6/plugins/iconengines/",    # 依赖 Qt6Svg，已剪
    "pyside6/plugins/platforminputcontexts/",
    "pyside6/plugins/platforms/qdirect2d.dll",
    "pyside6/plugins/imageformats/qsvg.dll",
    "pyside6/plugins/imageformats/qtiff.dll",
    "pyside6/plugins/imageformats/qwebp.dll",
    "pyside6/plugins/imageformats/qicns.dll",
    "pyside6/plugins/imageformats/qtga.dll",
    "pyside6/plugins/imageformats/qwbmp.dll",
    "pyside6/plugins/imageformats/qgif.dll",
    "pyside6/plugins/imageformats/qpdf.dll",
)


def _drop(dest: str) -> bool:
    norm = dest.replace("\\", "/").lower()
    return any(norm.startswith(prefix) or norm == prefix for prefix in _DROP_QT)


def prune_pyside(analysis):
    kept_binaries = [entry for entry in analysis.binaries if not _drop(entry[0])]
    kept_datas = [entry for entry in analysis.datas if not _drop(entry[0])]
    dropped = (len(analysis.binaries) - len(kept_binaries)) + (len(analysis.datas) - len(kept_datas))
    if dropped:
        print(f"[spec] 剪掉 {dropped} 个未使用的 PySide6 二进制/数据项")
    analysis.binaries = kept_binaries
    analysis.datas = kept_datas
    return analysis


def make(script, name, console, hidden=(), datas=None, excludes=None, manifest=None):
    analysis = Analysis(
        [os.path.join(ROOT, script)],
        pathex=[ROOT],
        binaries=[],
        datas=list(datas or []),
        hiddenimports=list(hidden),
        excludes=list(excludes if excludes is not None else EXCLUDES),
        noarchive=False,
    )
    prune_pyside(analysis)
    pyz = PYZ(analysis.pure)
    common = dict(
        icon=ICON,
        version=VERSION_FILE,
        debug=False,
        strip=False,
        upx=False,
        console=console,
    )
    if manifest:
        common["manifest"] = manifest
    if ONEFILE:
        return EXE(
            pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
            name=name, **common,
        )
    exe = EXE(
        pyz, analysis.scripts, [], exclude_binaries=True, name=name, **common,
    )
    return COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name=name)


# GUI：pyqtgraph 的模板/数据由 PyInstaller contrib 的 hook-pyqtgraph 收集；
# 这里再显式收集一次数据文件，防 hook 版本差异导致缺模板/缺配色表。
gui = make(
    "entry_gui.py", "PDPowerStudio", console=False,
    hidden=GUI_HIDDEN,
    datas=collect_data_files("pyqtgraph", excludes=["**/examples/*"]) + [
        # QSS 是运行时读取的普通文件，必须随 GUI 一起打进 _internal
        (os.path.join(ROOT, "ui", "theme_dark.qss"), os.path.join("pdpower_studio", "ui")),
        (os.path.join(ROOT, "ui", "theme_light.qss"), os.path.join("pdpower_studio", "ui")),
    ],
    excludes=GUI_EXCLUDES,
    manifest=os.path.join(ROOT, "assets", "app.manifest"),
)

cli = make("entry_cli.py", "pdpower-cli", console=True, hidden=SERIAL_HIDDEN)
mcp = make("entry_mcp.py", "pdpower-mcp", console=True, hidden=SERIAL_HIDDEN)
