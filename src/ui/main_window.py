"""主窗口：实时数据优先；设备信息收进右侧抽屉；曲线可回看历史日志。"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSizePolicy, QSlider, QSpinBox, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from ..device_model import (
    INPUT_STATE_NAMES, OUTPUT_MODE_NAMES, Calibration, DeviceService, capabilities_for,
)
from ..i18n import tr
from ..logging_store import LogConfig, LogStore, estimate_size, read_range
from ..serial_worker import SerialWorker
from ..transport import LinkError, PortBusy, list_pdpower_ports
from ..updater.fpk import VerifyReport, verify_fpk
from ..updater.github_source import UpdateError
from ..updater.service import CONFIRM_TOKEN, UpgradeBlocked, UpgradePlan, UpgradeService
from .plots import TIME_WINDOWS, DualAxisPlot

PRESET_VOLTAGE = [5.0, 9.0, 12.0, 15.0, 20.0]
PRESET_CURRENT = [0.5, 1.0, 2.0, 3.0]

_THEME_DIR = Path(__file__).resolve().parent


def load_qss(name: str) -> str:
    """读取 ui/ 下的 QSS；打包后回退到 PyInstaller 的 _MEIPASS 数据目录。"""
    candidates = [_THEME_DIR / name]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "pdpower_studio" / "ui" / name)
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"找不到主题文件 {name}（{candidates}）")


def set_variant(widget: QPushButton, variant: str) -> None:
    """primary / secondary / danger / ghost，QSS 用属性选择器派生。"""
    widget.setProperty("variant", variant)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._bindings: list[tuple[Callable[[str], None], str]] = []
        self._guard = False
        self.worker: SerialWorker | None = None
        self._retired_workers: list[SerialWorker] = []
        self.service: DeviceService | None = None
        self.log_store: LogStore | None = None
        self.snapshot: dict | None = None
        self.avg_buf: deque[int] = deque(maxlen=5)
        self.hold_value: int | None = None
        self.energy_wh = 0.0
        self.last_sample_ts: float | None = None
        self.calibration = Calibration()
        self.settings = QSettings("WeActStudio", "PDPowerStudio")
        # 量程只在变化时才重设控件：10 Hz 快照逐帧 setMaximum 会打断输入并触发反复重排
        self._caps_applied: tuple | None = None
        # 记忆组：仅在组号变化时回填设定值，避免 10 Hz 快照覆盖用户正在编辑的数字
        self._last_group: int | None = None

        lang = self.settings.value("lang", "zh")
        tr.set_lang(str(lang))
        # 主题：默认深色；记录上次选择
        self.light = str(self.settings.value("theme", "dark")) == "light"
        # 首次写操作在本进程内只提示一次
        self._write_confirmed = False
        # 写成功提示的保留时间，避免被下一次轮询的告警逻辑立即清掉
        self._banner_hold_until = 0.0

        self.resize(1280, 860)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.addLayout(self._build_header())

        body = QHBoxLayout()
        root.addLayout(body, 1)
        body.addWidget(self._build_main_panel(), 1)
        self.drawer = self._build_drawer()
        body.addWidget(self.drawer)
        self.drawer_toggle.toggled.connect(self.drawer.setVisible)
        self.drawer_toggle.setChecked(True)

        self._apply_theme()
        self._retranslate()
        self.refresh_ports()
        self._set_connected_ui(False)

    # ------------------------------------------------------------ 通用
    def _tr(self, key: str) -> str:
        return tr.t(key)

    def _bind(self, setter: Callable[[str], None], key: str) -> None:
        self._bindings.append((setter, key))

    def _make_card(self, key: str) -> tuple[QFrame, QLabel, QLabel]:
        frame = QFrame()
        frame.setObjectName("card")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 10, 14, 10)
        title = QLabel()
        title.setObjectName("cardTitle")
        self._bind(title.setText, key)
        value = QLabel("--")
        value.setObjectName("cardValue")
        sub = QLabel("")
        sub.setObjectName("cardSub")
        lay.addWidget(title)
        lay.addWidget(value)
        lay.addWidget(sub)
        return frame, value, sub

    # ------------------------------------------------------------ 顶部栏
    def _build_header(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        title = QLabel()
        title.setObjectName("appTitle")
        self._bind(title.setText, "app.title")
        bar.addWidget(title)
        bar.addStretch(1)

        # 固件升级只是低频维护入口：放顶部工具条、普通 ghost 样式，不占主界面构图
        # （危险提示保留在升级对话框内）
        self.upgrade_link = QToolButton()
        self._bind(self.upgrade_link.setText, "label.firmware")
        set_variant(self.upgrade_link, "ghost")
        self.upgrade_link.setToolTip(self._tr("label.firmware"))
        self.upgrade_link.clicked.connect(self._open_upgrade)
        bar.addWidget(self.upgrade_link)

        self.drawer_toggle = QToolButton()
        self.drawer_toggle.setText(self._tr("label.device"))
        self.drawer_toggle.setCheckable(True)
        bar.addWidget(self.drawer_toggle)

        self.theme_btn = QPushButton()
        set_variant(self.theme_btn, "ghost")
        self.theme_btn.clicked.connect(self._toggle_theme)
        bar.addWidget(self.theme_btn)

        self.lang_combo = QComboBox()
        self.lang_combo.addItem("中文", "zh")
        self.lang_combo.addItem("English", "en")
        self.lang_combo.setCurrentIndex(0 if tr.lang == "zh" else 1)
        self.lang_combo.currentIndexChanged.connect(self._on_lang)
        bar.addWidget(self.lang_combo)

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(140)
        bar.addWidget(self.port_combo)

        self.refresh_btn = QPushButton()
        self._bind(self.refresh_btn.setText, "btn.refresh")
        set_variant(self.refresh_btn, "secondary")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        bar.addWidget(self.refresh_btn)

        self.show_all_check = QCheckBox()
        self._bind(self.show_all_check.setText, "label.show_all")
        self.show_all_check.setToolTip("默认只显示 WeAct PDPower（VID:PID 1A86:FE0C，SN 以 AC/B3 开头）")
        self.show_all_check.setChecked(False)
        self.show_all_check.toggled.connect(lambda _: self.refresh_ports())
        bar.addWidget(self.show_all_check)

        self.connect_btn = QPushButton()
        self.connect_btn.clicked.connect(self.toggle_connect)
        set_variant(self.connect_btn, "primary")
        bar.addWidget(self.connect_btn)

        self.status_label = QLabel()
        self.status_label.setObjectName("statusChip")
        bar.addWidget(self.status_label)
        return bar

    # ------------------------------------------------------------ 主区
    def _build_main_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        cards = QHBoxLayout()
        v_frame, self.v_value, self.v_sub = self._make_card("label.voltage")
        i_frame, self.i_value, self.i_sub = self._make_card("label.current")
        p_frame, self.p_value, self.p_sub = self._make_card("label.power")
        m_frame, self.m_value, self.m_sub = self._make_card("label.mode")
        e_frame, self.e_value, self.e_sub = self._make_card("label.energy")
        for frame in (v_frame, i_frame, p_frame, m_frame, e_frame):
            cards.addWidget(frame)
        lay.addLayout(cards)

        self.banner = QLabel("")
        self.banner.setObjectName("banner")
        self.banner.hide()
        lay.addWidget(self.banner)

        lay.addLayout(self._build_plot_bar())
        self.plot = DualAxisPlot()
        self.plot.followChanged.connect(self._on_follow_changed)
        lay.addWidget(self.plot, 1)
        lay.addWidget(self._build_output_group())
        return panel

    def _build_plot_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        label = QLabel()
        self._bind(label.setText, "label.timeline")
        bar.addWidget(label)
        self.window_group = QButtonGroup(self)
        self.window_buttons: list[QPushButton] = []
        for index, (text, _seconds) in enumerate(TIME_WINDOWS):
            button = QPushButton(text)
            button.setObjectName("rangeBtn")
            button.setCheckable(True)
            # 四个档位文案长度不同，固定宽度避免按钮宽窄不一
            button.setFixedWidth(58)
            self.window_group.addButton(button, index)
            self.window_buttons.append(button)
            bar.addWidget(button)
        self.window_buttons[1].setChecked(True)   # 默认 5 min
        self.window_group.idClicked.connect(self._on_window_preset)

        self.rect_btn = QPushButton()
        self._bind(self.rect_btn.setText, "btn.rect")
        set_variant(self.rect_btn, "ghost")
        self.rect_btn.setCheckable(True)
        self.rect_btn.setToolTip("开启后左键拖拽为框选缩放；关闭时左键拖拽平移")
        self.rect_btn.toggled.connect(lambda on: self.plot.set_rect_mode(on))
        bar.addWidget(self.rect_btn)

        self.follow_label = QLabel("")
        self.follow_label.setObjectName("hint")
        bar.addWidget(self.follow_label)

        self.history_btn = QPushButton()
        self._bind(self.history_btn.setText, "btn.history")
        set_variant(self.history_btn, "ghost")
        self.history_btn.clicked.connect(self._open_history)
        bar.addWidget(self.history_btn)

        self.exit_history_btn = QPushButton()
        self._bind(self.exit_history_btn.setText, "btn.exit_history")
        set_variant(self.exit_history_btn, "ghost")
        self.exit_history_btn.clicked.connect(self._exit_history)
        bar.addWidget(self.exit_history_btn)

        self.sample_label = QLabel("")
        bar.addWidget(self.sample_label)
        bar.addStretch(1)
        return bar

    def _on_window_preset(self, index: int) -> None:
        for i, button in enumerate(self.window_buttons):
            button.setChecked(i == index)
        self.plot.set_window(TIME_WINDOWS[index][1])

    def _on_follow_changed(self, follow: bool) -> None:
        self.follow_label.setText("" if follow else self._tr("status.follow_paused"))

    def _build_output_group(self) -> QGroupBox:
        box = QGroupBox()
        self._bind(box.setTitle, "label.output")
        grid = QGridLayout(box)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        # 滑杆区占 4 列并均分拉伸，右侧预设/输出固定贴右边界，三行共用 9 列
        for column in range(1, 5):
            grid.setColumnStretch(column, 1)

        group_label = QLabel()
        self._bind(group_label.setText, "label.group")
        grid.addWidget(group_label, 0, 0)
        self.group_buttons: list[QPushButton] = []
        self.group_group = QButtonGroup(self)
        group_row = QHBoxLayout()
        group_row.setContentsMargins(0, 0, 0, 0)
        group_row.setSpacing(6)
        for gid in range(5):
            button = QPushButton(f"M{gid}")
            button.setCheckable(True)
            button.setObjectName("groupBtn")
            # 等分铺满左侧 5 列：既消除第 5 个按钮与右侧数字框之间的空洞，
            # 又保证 5 个按钮宽度一致
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.group_group.addButton(button, gid)
            self.group_buttons.append(button)
            group_row.addWidget(button, 1)
        group_holder = QWidget()
        group_holder.setLayout(group_row)
        grid.addWidget(group_holder, 0, 1, 1, 5)
        self.group_group.idClicked.connect(self._on_group_clicked)
        self.group_buttons[0].setChecked(True)

        self.output_btn = QPushButton()
        self.output_btn.setObjectName("outputBtn")
        self.output_btn.setCheckable(True)
        self.output_btn.clicked.connect(self._toggle_output)
        self.output_btn.setChecked(False)
        self.output_btn.setText(self._tr("btn.output_on"))
        self.output_btn.setToolTip("当前输出关闭，点击开启")
        grid.addWidget(self.output_btn, 0, 6, 1, 3)

        self.v_slider = QSlider(Qt.Orientation.Horizontal)
        self.v_slider.setRange(500, 20000)
        self.v_slider.setSingleStep(10)
        self.v_slider.setValue(5000)
        self.v_spin = QDoubleSpinBox()
        self.v_spin.setRange(0.5, 20.0)
        self.v_spin.setDecimals(3)
        self.v_spin.setSingleStep(0.01)
        self.v_spin.setSuffix(" V")
        self.v_spin.setValue(5.0)
        v_label = QLabel()
        self._bind(v_label.setText, "label.voltage")
        grid.addWidget(v_label, 1, 0)
        grid.addWidget(self.v_slider, 1, 1, 1, 4)
        grid.addWidget(self.v_spin, 1, 5)
        self.v_slider.valueChanged.connect(self._v_from_slider)
        self.v_spin.valueChanged.connect(self._v_from_spin)
        self.v_presets = self._preset_row(grid, 1, 6, PRESET_VOLTAGE, self._set_voltage_preset)

        c_label = QLabel()
        self._bind(c_label.setText, "label.current")
        grid.addWidget(c_label, 2, 0)
        self.i_slider = QSlider(Qt.Orientation.Horizontal)
        self.i_slider.setRange(0, 3000)
        self.i_slider.setValue(1000)
        self.i_spin = QDoubleSpinBox()
        self.i_spin.setRange(0.0, 3.0)
        self.i_spin.setDecimals(3)
        self.i_spin.setSingleStep(0.001)
        self.i_spin.setSuffix(" A")
        self.i_spin.setValue(1.0)
        grid.addWidget(self.i_slider, 2, 1, 1, 4)
        grid.addWidget(self.i_spin, 2, 5)
        self.i_slider.valueChanged.connect(self._i_from_slider)
        self.i_spin.valueChanged.connect(self._i_from_spin)
        self.i_presets = self._preset_row(grid, 2, 6, PRESET_CURRENT, self._set_current_preset)

        self.apply_btn = QPushButton()
        self._bind(self.apply_btn.setText, "label.apply")
        set_variant(self.apply_btn, "primary")
        self.apply_btn.clicked.connect(self._apply_setpoint)
        grid.addWidget(self.apply_btn, 3, 0, 1, 2)

        self.save_btn = QPushButton()
        self._bind(self.save_btn.setText, "label.save")
        set_variant(self.save_btn, "secondary")
        self.save_btn.clicked.connect(self._save_config)
        grid.addWidget(self.save_btn, 3, 2, 1, 2)

        mode_label = QLabel()
        self._bind(mode_label.setText, "label.cur_mode")
        grid.addWidget(mode_label, 3, 4)
        self.cur_mode = QComboBox()
        for key in ("label.raw", "label.avg", "label.hold"):
            self.cur_mode.addItem(self._tr(key), key.split(".")[1])
        self.cur_mode.currentIndexChanged.connect(self._on_cur_mode_changed)
        grid.addWidget(self.cur_mode, 3, 5)

        # 告警阈值整体贴右边界，与第 0 行的输出按钮、第 1/2 行的预设三列同宽
        alert_box = QWidget()
        alert_lay = QHBoxLayout(alert_box)
        alert_lay.setContentsMargins(0, 0, 0, 0)
        alert_lay.setSpacing(8)
        alert_label = QLabel()
        self._bind(alert_label.setText, "label.alert")
        alert_lay.addWidget(alert_label)
        self.alert_spin = QDoubleSpinBox()
        self.alert_spin.setRange(0.0, 3.0)
        self.alert_spin.setDecimals(3)
        self.alert_spin.setSingleStep(0.05)
        self.alert_spin.setValue(0.0)
        self.alert_spin.setSuffix(" A")
        alert_lay.addWidget(self.alert_spin, 1)
        grid.addWidget(alert_box, 3, 6, 1, 3)
        return box

    @staticmethod
    def _preset_row(grid: QGridLayout, row: int, col: int, values: list[float],
                    handler: Callable[[float], None]) -> list[QPushButton]:
        holder = QHBoxLayout()
        holder.setContentsMargins(0, 0, 0, 0)
        holder.setSpacing(6)
        buttons: list[QPushButton] = []
        for value in values:
            button = QPushButton(f"{value:g}")
            button.setObjectName("presetBtn")
            # 等分铺满右侧 3 列，与上方“开启输出”按钮同宽、同边界
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _=False, v=value: handler(v))
            holder.addWidget(button, 1)
            buttons.append(button)
        wrapper = QWidget()
        wrapper.setLayout(holder)
        grid.addWidget(wrapper, row, col, 1, 3)
        return buttons

    # ------------------------------------------------------------ 侧栏抽屉
    def _build_drawer(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("drawer")
        frame.setFixedWidth(330)
        lay = QVBoxLayout(frame)

        title = QLabel()
        title.setObjectName("drawerTitle")
        self._bind(title.setText, "label.device")
        lay.addWidget(title)

        form = QFormLayout()
        self.info: dict[str, QLabel] = {}
        rows = [("model", "label.model"), ("sn", "label.sn"),
                ("version", "label.version"), ("lcd", "label.lcd"),
                ("caps", "label.caps"), ("output", "label.output_state"),
                ("switches", "label.switches"), ("input", "label.input"),
                ("input_voltage", "label.input_voltage")]
        for key, tr_key in rows:
            label = QLabel()
            self._bind(label.setText, tr_key)
            value = QLabel("--")
            value.setWordWrap(True)
            form.addRow(label, value)
            self.info[key] = value
        lay.addLayout(form)

        log_box = QGroupBox()
        self._bind(log_box.setTitle, "label.log")
        log_lay = QVBoxLayout(log_box)
        dir_label = QLabel()
        self._bind(dir_label.setText, "label.log_dir")
        log_lay.addWidget(dir_label)
        self.log_dir_edit = QLineEdit(str(Path.cwd() / "logs"))
        log_lay.addWidget(self.log_dir_edit)
        row2 = QHBoxLayout()
        self.log_fmt = QComboBox()
        self.log_fmt.addItem("CSV", "csv")
        self.log_fmt.addItem("JSONL", "jsonl")
        self.log_fmt.currentIndexChanged.connect(lambda _: self._update_log_estimate())
        row2.addWidget(self.log_fmt)
        self.log_hz = QSpinBox()
        self.log_hz.setRange(1, 20)
        self.log_hz.setValue(5)
        self.log_hz.setSuffix(" Hz")
        self.log_hz.valueChanged.connect(lambda _: self._update_log_estimate())
        row2.addWidget(self.log_hz)
        log_lay.addLayout(row2)
        row3 = QHBoxLayout()
        self.log_start_btn = QPushButton()
        self._bind(self.log_start_btn.setText, "label.start_log")
        set_variant(self.log_start_btn, "secondary")
        self.log_start_btn.clicked.connect(self._start_log)
        row3.addWidget(self.log_start_btn)
        self.log_stop_btn = QPushButton()
        self._bind(self.log_stop_btn.setText, "label.stop_log")
        set_variant(self.log_stop_btn, "secondary")
        self.log_stop_btn.clicked.connect(self._stop_log)
        row3.addWidget(self.log_stop_btn)
        log_lay.addLayout(row3)
        self.log_status = QLabel("")
        self.log_status.setWordWrap(True)
        self.log_status.setObjectName("hint")
        log_lay.addWidget(self.log_status)
        self.log_estimate = QLabel("")
        self.log_estimate.setObjectName("hint")
        log_lay.addWidget(self.log_estimate)
        lay.addWidget(log_box)

        lay.addStretch(1)
        return frame

    # ------------------------------------------------------------ 主题
    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is not None:
            # 一次性设置应用级样式表：主窗、抽屉、对话框共用同一套 token
            app.setStyleSheet(load_qss("theme_light.qss" if self.light else "theme_dark.qss"))
        if hasattr(self, "plot"):
            self.plot.apply_theme(self.light)
        if hasattr(self, "theme_btn"):
            self.theme_btn.setText(self._tr("btn.theme_dark") if self.light else self._tr("btn.theme_light"))

    def _toggle_theme(self) -> None:
        self.light = not self.light
        self.settings.setValue("theme", "light" if self.light else "dark")
        self._apply_theme()

    # ------------------------------------------------------------ 语言
    def _on_lang(self, index: int) -> None:
        lang = self.lang_combo.itemData(index) or "zh"
        tr.set_lang(str(lang))
        self.settings.setValue("lang", str(lang))
        self._retranslate()

    def _retranslate(self) -> None:
        self.setWindowTitle(self._tr("app.title"))
        for setter, key in self._bindings:
            setter(self._tr(key))
        self.drawer_toggle.setText(self._tr("label.device"))
        self.upgrade_link.setToolTip(self._tr("label.firmware"))
        self.theme_btn.setText(self._tr("btn.theme_dark") if self.light else self._tr("btn.theme_light"))
        self.output_btn.setText(
            self._tr("btn.output_off") if self.output_btn.isChecked() else self._tr("btn.output_on")
        )
        self.plot.set_axis_labels(
            self._tr("label.voltage"), self._tr("label.current"), self._tr("label.timeline")
        )
        self.cur_mode.blockSignals(True)
        self.cur_mode.setItemText(0, self._tr("label.raw"))
        self.cur_mode.setItemText(1, self._tr("label.avg"))
        self.cur_mode.setItemText(2, self._tr("label.hold"))
        self.cur_mode.blockSignals(False)
        self.connect_btn.setText(self._tr("btn.disconnect") if self.worker else self._tr("btn.connect"))
        self._update_log_estimate()
        if self.snapshot:
            self._render_snapshot(self.snapshot)

    # ------------------------------------------------------------ 端口与连接
    def refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        self.port_combo.clear()
        known = [item["port"] for item in list_pdpower_ports()]
        if self.show_all_check.isChecked():
            try:
                from serial.tools import list_ports

                ports = [p.device for p in list_ports.comports()]
            except Exception:  # pragma: no cover - 串口枚举失败
                ports = []
        else:
            ports = known
        known_set = set(known)
        for port in ports:
            self.port_combo.addItem(("PDPower " if port in known_set else "") + port, port)
        if current:
            index = self.port_combo.findText(current)
            if index < 0:
                index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        if self.port_combo.count() == 0:
            self.port_combo.addItem("--", "")

    def toggle_connect(self) -> None:
        if self.worker:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        port = self.port_combo.currentData() or ""
        if not port:
            QMessageBox.warning(self, self._tr("app.title"), "未找到串口")
            return
        # GUI 只读模式已移除：连接即按可写链路打开（写入仍受设备量程与回读校验约束）
        worker = SerialWorker(port, read_only=False)
        worker.snapshotUpdated.connect(self._on_snapshot)
        worker.errorOccurred.connect(self._on_error)
        worker.connectedChanged.connect(self._on_connected)
        self.worker = worker
        self.status_label.setText(port)
        worker.start()

    def _disconnect(self) -> None:
        if self.worker:
            worker = self.worker
            self.worker = None
            if worker.stop():
                worker.deleteLater()
            else:
                # 极少数情况下线程未在超时内结束，保留引用避免 QThread 被回收崩溃
                self._retired_workers.append(worker)
        self._retired_workers = [w for w in self._retired_workers if w.isRunning()]
        self.service = None
        self._caps_applied = None
        self._last_group = None
        self.status_label.setText(self._tr("status.waiting"))
        self._set_connected_ui(False)

    def _on_connected(self, ok: bool) -> None:
        if not ok or not self.worker:
            return
        self.service = DeviceService(self.worker.link, self.log_store)
        self._set_connected_ui(True)
        if self.log_store and self.log_store.active:
            self.worker.link.on_snapshot = self.log_store.append

    def _on_error(self, message: str) -> None:
        self.banner.setText(message)
        self.banner.show()
        QMessageBox.warning(self, self._tr("app.title"), message)
        self._disconnect()

    def _set_connected_ui(self, connected: bool) -> None:
        self.connect_btn.setText(self._tr("btn.disconnect") if connected else self._tr("btn.connect"))
        self.port_combo.setEnabled(not connected)
        self.refresh_btn.setEnabled(not connected)
        self.show_all_check.setEnabled(not connected)
        if not connected:
            self.output_btn.blockSignals(True)
            self.output_btn.setChecked(False)
            self.output_btn.setText(self._tr("btn.output_on"))
            self.output_btn.setToolTip("当前输出关闭，点击开启")
            self.output_btn.blockSignals(False)
            self.status_label.setText(self._tr("status.waiting"))
        else:
            self.status_label.setText(
                f"{self._tr('label.port')}: {self.port_combo.currentData()}"
            )
        self._update_write_enabled()

    def _update_write_enabled(self) -> None:
        # 只读模式已移除：有 DeviceService 即可写；越界/未连接由各槽函数给出明确提示
        writable = bool(self.service)
        for widget in (self.apply_btn, self.save_btn, self.output_btn):
            widget.setEnabled(writable)
        for button in self.group_buttons:
            button.setEnabled(writable)
        caps = self._caps()
        for button, value in zip(self.v_presets, PRESET_VOLTAGE):
            button.setEnabled(writable and value <= (caps.max_voltage_v if caps else 0))
        for button, value in zip(self.i_presets, PRESET_CURRENT):
            button.setEnabled(writable and value <= (caps.max_current_a if caps else 0))

    def _caps(self):
        if not self.snapshot:
            return None
        return capabilities_for(self.snapshot.get("model"), self.snapshot.get("version_tuple"))

    # ------------------------------------------------------------ 快照渲染
    def _on_snapshot(self, data: dict) -> None:
        self.snapshot = data
        self._render_snapshot(data)
        disp = data.get("display") or {}
        ts = float(data.get("last_update_ts") or time.time())
        self.plot.add(ts, int(disp.get("mv") or 0), int(disp.get("ma") or 0))
        self.sample_label.setText(f"{self._tr('label.samples')}: {len(self.plot.samples)}")

    def _render_snapshot(self, data: dict) -> None:
        caps = self._caps()
        if caps is not None:
            self._apply_caps(caps)
        disp = data.get("display") or {}
        mv_raw, ma_raw = int(disp.get("mv") or 0), int(disp.get("ma") or 0)
        mv, ma = self.calibration.apply(mv_raw, ma_raw)
        # 显示层 clamp 到 0（空载噪声可能给出 -1/-2 mA）；原始值仍写日志
        mv, ma = max(0, mv), max(0, ma)

        self.v_value.setText(f"{mv} mV")
        self.v_sub.setText(f"{mv / 1000:.3f} V")

        self.avg_buf.append(ma)
        mode = self.cur_mode.currentData()
        held = False
        if mode == "hold":
            if self.hold_value is None:
                self.hold_value = ma
            shown = self.hold_value
            held = True
        elif mode == "avg":
            shown = int(round(sum(self.avg_buf) / len(self.avg_buf)))
        else:
            shown = ma
        self.i_value.setText(f"{shown} mA")
        suffix = " · " + self._tr("status.hold") if held else ""
        self.i_sub.setText(f"{shown / 1000:.3f} A{suffix}")

        power_w = mv * ma / 1e6
        self.p_value.setText(f"{power_w:.2f} W")
        self.p_sub.setText(f"{mv} mV × {ma} mA")

        enabled = bool(data.get("output_enabled"))
        mode_name = OUTPUT_MODE_NAMES.get(int(data.get("output_mode") or 0), "?")
        mode_key = {"normal": "status.normal", "CC": "status.cc", "OC": "status.oc"}.get(mode_name, "status.normal")
        self.m_value.setText(self._tr("status.on") if enabled else self._tr("status.off"))
        self.m_sub.setText(f"{self._tr(mode_key)} · M{int(data.get('group') or 0)}")

        ts = float(data.get("last_update_ts") or 0)
        if self.last_sample_ts and 0 < ts - self.last_sample_ts < 5:
            self.energy_wh += power_w * (ts - self.last_sample_ts) / 3600.0
        if ts:
            self.last_sample_ts = ts
        self.e_value.setText(f"{self.energy_wh:.4f} Wh")
        self.e_sub.setText(f"{self.energy_wh * 1000:.1f} mWh")

        self.output_btn.blockSignals(True)
        self.output_btn.setChecked(enabled)
        self.output_btn.setText(self._tr("btn.output_off") if enabled else self._tr("btn.output_on"))
        self.output_btn.setToolTip(
            "当前输出已开启，点击关闭" if enabled else "当前输出关闭，点击开启"
        )
        self.output_btn.blockSignals(False)

        group = int(data.get("group") or 0)
        for gid, button in enumerate(self.group_buttons):
            button.setChecked(gid == group)
        # 记忆组切换后把该组已存设定值回填到滑杆/数字框（0x84 快照），
        # 只在组号变化时做一次，避免 10 Hz 轮询覆盖正在编辑的输入
        if group != self._last_group:
            self._last_group = group
            self._sync_setpoint_from_group(group)

        self._update_alert(ma_raw, int(data.get("output_mode") or 0))
        self._render_info(data)

    def _update_alert(self, ma: int, mode: int) -> None:
        threshold_ma = int(round(self.alert_spin.value() * 1000))
        if threshold_ma > 0 and ma >= threshold_ma:
            self._banner_hold_until = 0.0
            self.banner.setText(self._tr("warn.current").format(ma=ma, thr=threshold_ma))
            self.banner.show()
        elif mode == 2:
            self._banner_hold_until = 0.0
            self.banner.setText(self._tr("warn.oc"))
            self.banner.show()
        elif time.time() < self._banner_hold_until:
            return  # 保留“写入成功+回读值”提示，不被下一次轮询清掉
        else:
            self.banner.hide()

    def _render_info(self, data: dict) -> None:
        caps = self._caps()
        self.info["model"].setText(data.get("model_name") or data.get("name") or "--")
        self.info["sn"].setText(data.get("sn") or "--")
        self.info["version"].setText(data.get("version") or "--")
        self.info["lcd"].setText(data.get("lcd_name") or "--")

        def yes_no(flag: bool) -> str:
            return self._tr("status.supported") if flag else self._tr("status.unsupported")

        if caps is not None:
            self.info["caps"].setText(self._tr("info.caps").format(
                input_state=yes_no(caps.input_state),
                pd_vset=yes_no(caps.pd_vset),
                uart=yes_no(caps.uart_mode),
                vmax=f"{caps.max_voltage_v:g}",
                imax=f"{caps.max_current_a:g}",
            ))
        else:
            self.info["caps"].setText("--")

        disp = data.get("display") or {}
        mv, ma = self.calibration.apply(int(disp.get("mv") or 0), int(disp.get("ma") or 0))
        mv, ma = max(0, mv), max(0, ma)
        state = self._tr("status.on") if data.get("output_enabled") else self._tr("status.off")
        mode_name = OUTPUT_MODE_NAMES.get(int(data.get("output_mode") or 0), "?")
        mode_key = {"normal": "status.normal", "CC": "status.cc", "OC": "status.oc"}.get(
            mode_name, "status.normal"
        )
        self.info["output"].setText(self._tr("info.output").format(
            state=state, mode=self._tr(mode_key), group=int(data.get("group") or 0),
            voltage=f"{mv / 1000:.3f}", current=f"{ma / 1000:.3f}", power=f"{mv * ma / 1e6:.2f}",
        ))

        def on_off(flag: bool) -> str:
            return self._tr("status.enabled") if flag else self._tr("status.disabled")

        self.info["switches"].setText(self._tr("info.switches").format(
            ocp=on_off(bool(data.get("ocp"))),
            comp=on_off(bool(data.get("compensate"))),
            brightness=data.get("brightness"),
        ))

        in_state = data.get("input_state")
        if in_state is None:
            self.info["input"].setText("--")
            self.info["input_voltage"].setText("--")
            return
        state_keys = {
            0: "input.wait", 1: "input.wait_pd", 2: "input.wait_qc",
            3: "input.error", 4: "input.qc", 5: "input.pd", 6: "input.dc",
        }
        state_key = state_keys.get(int(in_state))
        name = self._tr(state_key) if state_key else INPUT_STATE_NAMES.get(int(in_state), str(in_state))
        pd_dv = data.get("pd_set_dv")
        pd_text = f"{float(pd_dv) / 10:.1f} V" if pd_dv is not None else "--"
        self.info["input"].setText(self._tr("info.input").format(state=name, pd=pd_text))
        input_mv = data.get("input_mv")
        self.info["input_voltage"].setText(
            "--" if input_mv is None else f"{int(input_mv)} mV · {int(input_mv) / 1000:.3f} V"
        )

    def _apply_caps(self, caps) -> None:
        key = (caps.max_voltage_v, caps.max_current_a)
        if key == self._caps_applied:
            return
        self._caps_applied = key
        self._guard = True
        self.v_spin.setMaximum(caps.max_voltage_v)
        self.v_slider.setMaximum(int(caps.max_voltage_v * 1000))
        self.i_spin.setMaximum(caps.max_current_a)
        self.i_slider.setMaximum(int(caps.max_current_a * 1000))
        self.alert_spin.setMaximum(caps.max_current_a)
        self._guard = False
        self._update_write_enabled()

    # ------------------------------------------------------------ 输入控件
    def _v_from_slider(self, value: int) -> None:
        if self._guard:
            return
        self._guard = True
        self.v_spin.setValue(value / 1000)
        self._guard = False

    def _v_from_spin(self, value: float) -> None:
        if self._guard:
            return
        self._guard = True
        self.v_slider.setValue(int(round(value * 1000)))
        self._guard = False

    def _i_from_slider(self, value: int) -> None:
        if self._guard:
            return
        self._guard = True
        self.i_spin.setValue(value / 1000)
        self._guard = False

    def _i_from_spin(self, value: float) -> None:
        if self._guard:
            return
        self._guard = True
        self.i_slider.setValue(int(round(value * 1000)))
        self._guard = False

    def _set_voltage_preset(self, value: float) -> None:
        self.v_spin.setValue(value)

    def _set_current_preset(self, value: float) -> None:
        self.i_spin.setValue(value)

    def _on_cur_mode_changed(self) -> None:
        self.hold_value = None

    # ------------------------------------------------------------ 写路径公共
    def _confirm_first_write(self) -> bool:
        """首次写操作确认（每个进程只提示一次，用户拒绝则不写）。"""
        if self._write_confirmed:
            return True
        answer = QMessageBox.question(
            self, self._tr("app.title"),
            "即将向设备写入控制命令（0x02 / 0x03 / 0x04）。\n"
            "这会真实改变输出电压、电流或输出状态，请确认已知晓风险。\n\n"
            "本次运行仅提示一次。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._write_confirmed = True
        return True

    def _show_write_error(self, exc: Exception) -> None:
        """写入失败按来源分类：拒绝写入 / 端口占用 / 设备拒绝（含回读不一致）。"""
        if isinstance(exc, PermissionError):
            text = f"拒绝写入：{exc}"
        elif isinstance(exc, PortBusy):
            text = f"端口占用：{exc}"
        elif isinstance(exc, TimeoutError):
            text = f"设备拒绝（响应超时）：{exc}"
        elif isinstance(exc, LinkError):
            text = f"设备拒绝（链路错误）：{exc}"
        elif isinstance(exc, RuntimeError):
            text = f"设备拒绝（写入后回读不一致）：{exc}"
        else:
            text = f"写入失败（设备拒绝）：{exc}"
        self._banner_hold_until = 0.0
        self.banner.setText(text)
        self.banner.show()
        QMessageBox.warning(self, self._tr("app.title"), text)

    def _refresh_from_link(self) -> None:
        if self.worker:
            self._on_snapshot(self.worker.link.snapshot_copy())

    def _show_write_ok(self, text: str) -> None:
        """写成功后立即用链路快照刷新界面，并保留一条含回读值的提示。"""
        self._refresh_from_link()
        self._banner_hold_until = time.time() + 6.0
        self.banner.setText(text)
        self.banner.show()

    def _sync_group_buttons(self) -> None:
        group = int((self.snapshot or {}).get("group") or 0)
        for gid, button in enumerate(self.group_buttons):
            button.blockSignals(True)
            button.setChecked(gid == group)
            button.blockSignals(False)

    def _readback_groups(self) -> list[dict]:
        return list((self.worker.link.snapshot_copy() if self.worker else {}).get("groups") or [])

    def _sync_setpoint_from_group(self, gid: int) -> None:
        """把当前记忆组的已存设定值回填到电压/电流控件。

        数据来自握手与写后 0x84 回读维护的 ``snapshot["groups"]``；
        只在组号变化时调用，且用 ``_guard`` 阻止回填触发滑杆/数字框互写。
        """
        groups = self._readback_groups()
        if not (0 <= gid < len(groups)):
            return
        mv = int(groups[gid].get("mv") or 0)
        ma = int(groups[gid].get("ma") or 0)
        self._guard = True
        if 500 <= mv <= 20000:
            self.v_spin.setValue(mv / 1000)
        if 0 <= ma <= 3000:
            self.i_spin.setValue(ma / 1000)
        self._guard = False

    def _on_group_clicked(self, gid: int) -> None:
        if not self.service:
            self._sync_group_buttons()
            self.banner.setText("未连接设备，未切换记忆组")
            self.banner.show()
            return
        if not self._confirm_first_write():
            self._sync_group_buttons()
            return
        if (self.snapshot or {}).get("output_enabled"):
            answer = QMessageBox.question(self, self._tr("app.title"),
                                          f"输出开启，切换到 M{gid}？")
            if answer != QMessageBox.StandardButton.Yes:
                self._sync_group_buttons()
                return
        try:
            self.service.set_group(gid, confirm=True)
        except Exception as exc:  # noqa: BLE001 - 面向用户提示
            self._show_write_error(exc)
            self._sync_group_buttons()
            return
        actual = int((self.worker.link.snapshot_copy() if self.worker else {}).get("group") or 0)
        self._sync_setpoint_from_group(actual)
        self._show_write_ok(f"已切换并回读校验：当前组 M{actual}（0x03 → 0x83 一致）")

    def _apply_setpoint(self) -> None:
        if not self.service:
            return
        if not self._confirm_first_write():
            return
        gid = int((self.snapshot or {}).get("group") or 0)
        try:
            self.service.set_voltage_current(self.v_spin.value(), self.i_spin.value())
        except Exception as exc:  # noqa: BLE001
            self._show_write_error(exc)
            return
        groups = self._readback_groups()
        mv, ma = (int(groups[gid]["mv"]), int(groups[gid]["ma"])) if 0 <= gid < len(groups) else (0, 0)
        self._show_write_ok(
            f"写入成功并回读校验：M{gid} = {mv / 1000:.3f} V / {ma / 1000:.3f} A"
            f"（0x04 → 0x84 一致，临时生效；需保存到设备才断电保留）"
        )

    def _save_config(self) -> None:
        if not self.service:
            return
        if not self._confirm_first_write():
            return
        answer = QMessageBox.question(self, self._tr("app.title"),
                                      f"{self._tr('label.save')}? (0x44)")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.save_config(confirm=True)
        except Exception as exc:  # noqa: BLE001
            self._show_write_error(exc)
            return
        self._show_write_ok("已保存到设备 NVM（0x44），断电后保留；该命令无回读，未做校验")

    def _toggle_output(self, enabled: bool) -> None:
        """一键输出：点击后立即按当前设定值下发（0x04 → 0x02），不再弹二次确认。

        保留的安全判断：未连接、量程外的设定值都会明确提示并回退按钮状态，
        不会静默忽略；设备侧的量程钳制与写后回读校验仍由 DeviceService 负责。
        """
        def revert() -> None:
            actual = bool((self.snapshot or {}).get("output_enabled"))
            self.output_btn.blockSignals(True)
            self.output_btn.setChecked(actual)
            self.output_btn.setText(
                self._tr("btn.output_off") if actual else self._tr("btn.output_on")
            )
            self.output_btn.blockSignals(False)

        def notify(text: str) -> None:
            revert()
            self._banner_hold_until = 0.0
            self.banner.setText(text)
            self.banner.show()
            QMessageBox.warning(self, self._tr("app.title"), text)

        if not self.service:
            notify("未连接设备：请先选择串口并连接，再切换输出")
            return

        caps = self._caps()
        voltage = self.v_spin.value()
        current = self.i_spin.value()
        if caps is not None:
            if not (0.5 <= voltage <= caps.max_voltage_v):
                notify(f"设定电压 {voltage:.3f} V 超出量程 0.5–{caps.max_voltage_v:g} V，未下发")
                return
            if not (0.0 <= current <= caps.max_current_a):
                notify(f"设定电流 {current:.3f} A 超出量程 0–{caps.max_current_a:g} A，未下发")
                return

        try:
            if enabled:
                # 用户要求“一键生效”：开启输出前先把当前设定值写入设备，再打开输出
                self.service.set_voltage_current(voltage, current)
            self.service.output_enable(enabled, confirm=enabled)
        except Exception as exc:  # noqa: BLE001
            self._show_write_error(exc)
            revert()
            return
        state = int(bool((self.worker.link.snapshot_copy() if self.worker else {}).get("output_enabled")))
        text = (
            f"输出已开启：{voltage:.3f} V / {current:.3f} A 已下发，0x02 → 0x82 回读 enabled={state}"
            if enabled else
            f"输出已关闭：0x02 → 0x82 回读 enabled={state}"
        )
        self._show_write_ok(text)

    # ------------------------------------------------------------ 日志
    def _update_log_estimate(self) -> None:
        fmt = self.log_fmt.currentData() or "csv"
        info = estimate_size(self.log_hz.value(), 8.0, str(fmt))
        self.log_estimate.setText(
            f"8 h ≈ {info['mib']} MiB / {info['samples']} rows ({fmt})"
        )

    def _start_log(self) -> None:
        if self.log_store and self.log_store.active:
            return
        directory = Path(self.log_dir_edit.text().strip() or "logs")
        config = LogConfig(directory=directory, fmt=self.log_fmt.currentData() or "csv",
                           hz=float(self.log_hz.value()))
        store = LogStore(config)
        path = store.start({"port": (self.snapshot or {}).get("port", ""), "note": "gui"})
        self.log_store = store
        if self.worker and self.worker.link.connected:
            self.worker.link.on_snapshot = store.append
        self.log_status.setText(str(path))

    def _stop_log(self) -> None:
        if self.worker:
            self.worker.link.on_snapshot = None
        if self.log_store:
            self.log_store.stop()
            current = self.log_store.current_path
            self.log_status.setText(f"stopped · {current}" if current else "stopped")
            self.log_store = None

    # ------------------------------------------------------------ 历史回看
    def _open_history(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, self._tr("btn.history"),
                                                     self.log_dir_edit.text())
        if not directory:
            return
        hours, ok = QInputDialog.getDouble(self, self._tr("btn.history"), "小时 / hours",
                                           1.0, 0.1, 720.0, 1)
        if not ok:
            return
        now_ms = int(time.time() * 1000)
        rows = read_range(Path(directory), now_ms - int(hours * 3600 * 1000), now_ms)
        if not rows:
            QMessageBox.information(self, self._tr("app.title"), "该时间段没有日志数据")
            return
        count = self.plot.show_history(rows)
        self.sample_label.setText(f"{self._tr('label.samples')}: {count} (history)")

    def _exit_history(self) -> None:
        self.plot.exit_history()
        self.sample_label.setText(f"{self._tr('label.samples')}: {len(self.plot.samples)}")

    # ------------------------------------------------------------ 固件升级
    def _open_upgrade(self) -> None:
        dialog = UpgradeDialog(self.snapshot, self)
        dialog.exec()

    # ------------------------------------------------------------ 生命周期
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._stop_log()
        if self.worker:
            self.worker.stop()
        super().closeEvent(event)


class UpgradeDialog(QDialog):
    """升级对话框：默认 dry-run，按钮只做发现 / 校验 / 计划，不触发真刷。"""

    def __init__(self, snapshot: dict | None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("upgradeDialog")
        self.setWindowTitle(tr.t("label.firmware"))
        self.resize(560, 640)
        self.snapshot = snapshot or {}
        self.model_key = self.snapshot.get("model") or ""
        self.current = tuple(self.snapshot.get("version_tuple") or (0, 0, 0, 0))
        self.service = UpgradeService(dry_run=True, allow_real_flash=False, retries=3)
        self.plan: UpgradePlan | None = None
        self.report: VerifyReport | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        form = QFormLayout()
        self.model_label = QLabel(self.model_key or "--")
        self.version_label = QLabel(self.snapshot.get("version") or "--")
        self.port_label = QLabel(self.snapshot.get("port") or "--")
        for text, widget in (("型号", self.model_label), ("当前版本", self.version_label),
                             ("端口", self.port_label)):
            form.addRow(text, widget)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.remote_btn = QPushButton("检查 GitHub 最新固件")
        set_variant(self.remote_btn, "secondary")
        self.remote_btn.clicked.connect(self._check_remote)
        row.addWidget(self.remote_btn)
        self.local_btn = QPushButton("选择本地 .fpk")
        set_variant(self.local_btn, "secondary")
        self.local_btn.clicked.connect(self._pick_local)
        row.addWidget(self.local_btn)
        lay.addLayout(row)

        self.allow_downgrade = QCheckBox("允许降级（需保留上一版 fpk）")
        lay.addWidget(self.allow_downgrade)

        self.summary = QLabel("尚未选择固件文件")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("hint")
        lay.addWidget(self.summary)

        self.report_view = QTextEdit()
        self.report_view.setReadOnly(True)
        lay.addWidget(self.report_view, 1)

        self.real_check = QCheckBox("允许真实刷写（危险）")
        self.real_check.toggled.connect(self._update_exec_btn)
        lay.addWidget(self.real_check)

        self.execute_btn = QPushButton("演练升级（dry-run，不发送 0x41）")
        set_variant(self.execute_btn, "primary")
        self.execute_btn.setProperty("uiSize", "lg")
        self.execute_btn.setMinimumHeight(38)
        self.execute_btn.setEnabled(False)
        self.execute_btn.clicked.connect(self._execute)
        lay.addWidget(self.execute_btn)

        self.note = QLabel(
            f"失败自动重试 {self.service.retries} 次；勾选“允许真实刷写”后会再弹一次"
            "是/否确认，确认后才下发 0x41 并真刷，刷写后自动读 0xC2 复核版本。"
        )
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

    def _log(self, text: str) -> None:
        self.report_view.append(text)

    def _update_exec_btn(self) -> None:
        real = self.real_check.isChecked()
        ok = bool(self.report and self.report.ok)
        self.execute_btn.setEnabled(ok)
        # 真刷是危险动作：按钮切成 danger 变体，视觉上与演练区分
        set_variant(self.execute_btn, "danger" if real else "primary")
        self.execute_btn.setText(
            "执行升级（真实刷写）" if real else "演练升级（dry-run，不发送 0x41）"
        )

    def _check_remote(self) -> None:
        if not self.model_key:
            self._log("未识别型号，无法匹配固件")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            asset = self.service.discover(self.model_key)
            self._log(f"发现 {asset.version_text} · {asset.filename}")
            path = self.service.download(self.model_key, asset)
            self._verify(path)
        except UpdateError as exc:
            self._log(f"GitHub 不可用: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"失败: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

    def _pick_local(self) -> None:
        start = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(self, "选择 .fpk", start, "Firmware (*.fpk)")
        if path:
            self._verify(Path(path))

    def _verify(self, path: Path) -> None:
        report = verify_fpk(path, self.model_key or "?", self.current,
                            self.allow_downgrade.isChecked())
        self.report = report
        self.plan = UpgradePlan(
            port=self.snapshot.get("port", ""), model_key=self.model_key,
            current_version=self.current, asset_path=path,
            allow_downgrade=self.allow_downgrade.isChecked(), report=report,
        )
        info = report.info
        if info is not None:
            self.summary.setText(
                f"型号={info.user_string}  版本={info.version_text}  "
                f"大小={info.size} bytes  part={info.part_name}\n"
                f"sha256={info.sha256}\n"
                + ("头部 CRC 校验通过" if report.ok else "拒绝刷写：" + "；".join(report.errors))
            )
        else:
            self.summary.setText("拒绝刷写：" + "；".join(report.errors))
        self._log(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        self._update_exec_btn()

    def _execute(self) -> None:
        if not self.plan:
            return
        real = self.real_check.isChecked()
        if real:
            answer = QMessageBox.question(
                self, "危险操作",
                "即将向设备真实刷写固件：\n"
                "1) 发送 0x41 A5 AA 0A 进入 bootloader\n"
                "2) 端口重枚举后 115200 Y-Modem 1K/CRC16 传输\n"
                "3) EOT 后重新握手，读 0xC2 复核版本\n\n"
                "刷写期间请勿断电。确认继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        service = UpgradeService(dry_run=not real, allow_real_flash=real, retries=3)
        try:
            # 不再要求用户手输口令：确认环节只保留上面这条普通的是/否确认，
            # 服务层 confirm 校验（写入口）原样保留，由程序内部传入常量。
            result = service.execute(self.plan, confirm=CONFIRM_TOKEN)
        except UpgradeBlocked as exc:
            self._log(f"已拒绝：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self._log(f"执行异常：{exc}")
            return
        self._log("执行结果: " + json.dumps(result, ensure_ascii=False, default=str, indent=2))
        if real and result.get("verified_version"):
            verdict = "一致" if result.get("match") else "不一致"
            self._log(
                f"0xC2 复核：设备当前 {result['verified_version']}，目标 "
                f"{result.get('expected')} → {verdict}"
            )


def main() -> int:
    # 必须在 QApplication 创建前设置：Round 让 125% 落回 100%、150% 升到 200%，
    # 避免 Qt 用自己的非整数缩放把 1px 线画成半像素而发虚（125% 的代价是 UI 略小）。
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.Round
    )
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication.instance() or QApplication([])
    # 统一字号：正文 14px（规范 12/14/16 档），曲线轴文字在 plots.py 单独设 12px
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(14)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    window = MainWindow()
    window.show()
    # 打包冒烟用：设置 PDPOWER_SMOKE_EXIT_MS 后窗口显示 N 毫秒即正常退出
    smoke_ms = os.environ.get("PDPOWER_SMOKE_EXIT_MS")
    if smoke_ms:
        QTimer.singleShot(int(smoke_ms), app.quit)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
