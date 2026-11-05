"""曲线区：双 Y 轴实时图 + LTTB 降采样 + 历史日志回看。

长时段策略：
- 内存只保留 WINDOW 内的样本（另有硬上限 MAX_SAMPLES），避免无限增长；
- 绘制前按可视宽度用 LTTB 降采样到 MAX_POINTS，保留峰谷形态；
- 更早的数据不留在内存里，而是从 CSV/JSONL 日志按时间段回读（read_range）。
"""
from __future__ import annotations

import math
from typing import Sequence

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QVBoxLayout, QWidget

TIME_WINDOWS: list[tuple[str, int]] = [("30 s", 30), ("5 min", 300), ("30 min", 1800), ("All", 0)]


def curve_pen(color: str, width: int = 2, dashed: bool = False) -> pg.QtGui.QPen:
    """整数像素 + cosmetic，避免 DPR 1.25/1.75 下被画成半像素灰线。"""
    pen = pg.mkPen(color, width=width, style=Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine)
    pen.setCosmetic(True)
    return pen


def lttb_indices(xs: Sequence[float], ys: Sequence[float], threshold: int) -> list[int]:
    """LTTB（Largest-Triangle-Three-Buckets）降采样，返回保留点的下标。"""
    n = len(xs)
    if threshold >= n or threshold < 3 or n < 3:
        return list(range(n))
    sampled = [0]
    every = (n - 2) / (threshold - 2)
    a = 0
    for i in range(threshold - 2):
        start = int(i * every) + 1
        end = min(int((i + 1) * every) + 1, n - 1)
        if start >= end:
            start, end = min(start, n - 2), min(start + 1, n - 1)
        nxt_start = int((i + 1) * every) + 1
        nxt_end = min(int((i + 2) * every) + 1, n)
        if nxt_start >= nxt_end:
            nxt_start, nxt_end = n - 1, n
        avg_x = sum(xs[nxt_start:nxt_end]) / (nxt_end - nxt_start)
        avg_y = sum(ys[nxt_start:nxt_end]) / (nxt_end - nxt_start)
        ax, ay = xs[a], ys[a]
        best, best_area = start, -1.0
        for j in range(start, end):
            area = abs((ax - avg_x) * (ys[j] - ay) - (ax - xs[j]) * (avg_y - ay))
            if area > best_area:
                best_area, best = area, j
        sampled.append(best)
        a = best
    sampled.append(n - 1)
    return sampled


class DualAxisPlot(QWidget):
    MAX_SAMPLES = 20000
    MAX_POINTS = 1200
    # 采样是 10 Hz，但重绘压到约 15 FPS：多个样点合并成一帧，避免每个样点都触发一次全量布局
    REFRESH_MS = 66
    # 用户手动缩放/平移后置 False；点击档位按钮恢复 True
    followChanged = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 抗锯齿 + 关闭 OpenGL：Raster 渲染在 125%/150% 缩放下更锐利
        pg.setConfigOptions(antialias=True, useOpenGL=False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(background="#121E33")
        layout.addWidget(self.plot)

        self.pi = self.plot.getPlotItem()
        # 给左右刻度与底部标签留出固定空间（规范：56/12/56/32）
        self.pi.setContentsMargins(56, 12, 56, 32)
        self.pi.showGrid(x=True, y=True, alpha=0.18)
        self.pi.showAxis("right")
        self.right_axis = self.pi.getAxis("right")
        self.vb_right = pg.ViewBox()
        self.pi.scene().addItem(self.vb_right)
        self.right_axis.linkToView(self.vb_right)
        self.vb_right.setXLink(self.pi.vb)

        # 坐标轴：字号 12px（规范轴文字档），刻度 6px，避免默认 9px 偏小
        tick_font = QFont("Microsoft YaHei UI")
        tick_font.setPixelSize(12)
        tick_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        for axis in (self.pi.getAxis("left"), self.pi.getAxis("bottom"), self.right_axis):
            axis.setStyle(tickFont=tick_font, tickLength=6, tickTextOffset=4)
            axis.setPen(pg.mkPen("#52708F", width=1))
            axis.setTextPen(pg.mkPen("#A9BAD2"))
            # 关闭自动 SI 前缀，否则电流轴会显示 700 并挂上“(×0.001)”，与标签单位混淆
            axis.enableAutoSIPrefix(False)

        # 曲线 2px 整数宽 + cosmetic（不随缩放变粗变细）
        self.curve_v = self.pi.plot(pen=curve_pen("#38BDF8"), name="voltage")
        self.curve_i = pg.PlotDataItem(pen=curve_pen("#FBBF24"))
        self.vb_right.addItem(self.curve_i)

        self.hist_v = self.pi.plot(pen=curve_pen("#52708F", width=1, dashed=True))
        self.hist_i = pg.PlotDataItem(pen=curve_pen("#52708F", width=1, dashed=True))
        self.vb_right.addItem(self.hist_i)

        # 只绘制可视范围内的点：每次重绘的图元更少，滚动时不会出现整条线重画造成的闪动
        for curve in (self.curve_v, self.curve_i, self.hist_v, self.hist_i):
            curve.setClipToView(True)
            curve.setDownsampling(auto=False)

        # 交互：主视图左键拖拽平移、滚轮缩放 X；右侧电流轴不单独平移
        self.pi.vb.setMouseEnabled(x=True, y=False)
        self.vb_right.setMouseEnabled(x=False, y=False)
        # 兜底：坐标轴下界锁 0，任何自动范围都不可能冒出负值
        self.pi.vb.setLimits(yMin=0)
        self.vb_right.setLimits(yMin=0)
        self.pi.vb.sigRangeChangedManually.connect(self._on_manual_range)

        self.pi.vb.sigResized.connect(self._sync_views)
        self._sync_views()

        self.follow = True
        self.samples: list[tuple[float, int, int]] = []
        self.window_s = 300
        self.history_mode = False
        # 时间原点固定为第一个样本：X 值不会随窗口滑动被整体重算
        self.t_origin: float | None = None
        self._dirty = False
        self._x_view: tuple[float, float] | None = None
        self._y_view: tuple[float, float] | None = None

        # 定频重绘：add() 只入队，由定时器合并成一帧
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    def _on_manual_range(self, *_args) -> None:
        """用户滚轮/拖拽后停止自动跟随，避免每帧被拉回。"""
        if self.history_mode or not self.follow:
            return
        self.follow = False
        self.followChanged.emit(False)

    def set_rect_mode(self, enabled: bool) -> None:
        """左键框选缩放（关闭时为拖拽平移）。"""
        self.pi.vb.setMouseMode(
            pg.ViewBox.RectMode if enabled else pg.ViewBox.PanMode
        )

    def apply_theme(self, light: bool) -> None:
        """深/浅色主题：背景、坐标轴、曲线与网格一起换，用规范里的色板 token。"""
        bg = "#FFFFFF" if light else "#121E33"
        axis_pen = "#8494A8" if light else "#52708F"
        axis_text = "#475569" if light else "#A9BAD2"
        v_color = "#0369A1" if light else "#38BDF8"
        i_color = "#B45309" if light else "#FBBF24"
        hist = "#8494A8" if light else "#52708F"
        self.plot.setBackground(bg)
        for axis in (self.pi.getAxis("left"), self.pi.getAxis("bottom"), self.right_axis):
            axis.setPen(pg.mkPen(axis_pen, width=1))
            axis.setTextPen(pg.mkPen(axis_text))
        self.pi.showGrid(x=True, y=True, alpha=0.18)
        self.curve_v.setPen(curve_pen(v_color))
        self.curve_i.setPen(curve_pen(i_color))
        self.hist_v.setPen(curve_pen(hist, width=1, dashed=True))
        self.hist_i.setPen(curve_pen(hist, width=1, dashed=True))

    # ------------------------------------------------------------ 轴
    def set_axis_labels(self, voltage: str, current: str, time_label: str) -> None:
        self.pi.setLabel("left", voltage)
        self.pi.setLabel("bottom", time_label)
        self.right_axis.setLabel(current)

    def _sync_views(self) -> None:
        self.vb_right.setGeometry(self.pi.vb.sceneBoundingRect())
        self.vb_right.linkedViewChanged(self.pi.vb, self.vb_right.XAxis)

    # ------------------------------------------------------------ 实时
    def add(self, ts: float, mv: int, ma: int) -> None:
        if self.t_origin is None:
            self.t_origin = ts
        self.samples.append((ts, mv, ma))
        if len(self.samples) > self.MAX_SAMPLES:
            del self.samples[: len(self.samples) - self.MAX_SAMPLES]
        if not self.history_mode:
            # 只标记脏，真正重绘交给定时器，10 Hz 的采样不会逐点触发重排
            self._dirty = True

    def _on_tick(self) -> None:
        if self._dirty and not self.history_mode:
            self._dirty = False
            self._refresh_live()

    def set_window(self, seconds: int) -> None:
        self.window_s = seconds
        self.follow = True
        self.followChanged.emit(True)
        self._x_view = None
        self._refresh_live()

    def clear(self) -> None:
        self.samples.clear()
        self.hist_v.setData([], [])
        self.hist_i.setData([], [])
        self.t_origin = None
        self._x_view = None
        self._y_view = None
        self.history_mode = False
        self.follow = True
        self._refresh_live()

    @staticmethod
    def _stable_top(target: float, current: float | None) -> float:
        """Y 轴上界的滞回：只涨或明显回落（>20%）才变，避免每帧上下抖动。"""
        if current is None:
            return target
        if target > current:
            return target
        if target < current * 0.8:
            return target
        return current

    def _set_y_ranges(self, yv: Sequence[float], yi: Sequence[float]) -> None:
        """Y 轴固定从 0 起，上界量化到固定刻度（V 0.1 / A 0.01）后再下发。"""
        vmax = max([v for v in yv if v > 0] or [0.0])
        imax = max([i for i in yi if i > 0] or [0.0])
        v_target = math.ceil(max(vmax * 1.08, 0.5) / 0.1) * 0.1
        i_target = math.ceil(max(imax * 1.08, 0.05) / 0.01) * 0.01
        v_top = self._stable_top(round(v_target, 2), None if self._y_view is None else self._y_view[0])
        i_top = self._stable_top(round(i_target, 3), None if self._y_view is None else self._y_view[1])
        if self._y_view == (v_top, i_top):
            return
        self.pi.setYRange(0, v_top, padding=0)
        self.vb_right.setYRange(0, i_top, padding=0)
        self._y_view = (v_top, i_top)

    def _refresh_live(self) -> None:
        if not self.samples:
            self.curve_v.setData([], [])
            self.curve_i.setData([], [])
            return
        now = self.samples[-1][0]
        pts = self.samples
        if self.window_s:
            pts = [s for s in self.samples if s[0] >= now - self.window_s] or self.samples[-1:]
        origin = self.t_origin if self.t_origin is not None else pts[0][0]
        xs = [p[0] - origin for p in pts]
        # 显示层 clamp 到 0；原始负读数仍保存在 self.samples / 日志中
        yv = [max(0, p[1]) / 1000.0 for p in pts]
        yi = [max(0, p[2]) / 1000.0 for p in pts]
        if len(xs) > self.MAX_POINTS:
            idx = lttb_indices(xs, yv, self.MAX_POINTS)
            xs = [xs[i] for i in idx]
            yv = [yv[i] for i in idx]
            yi = [yi[i] for i in idx]
        self.curve_v.setData(xs, yv)
        self.curve_i.setData(xs, yi)
        self._set_y_ranges(yv, yi)
        if self.follow:
            # X 轴按量化步长滚动：5 min 窗每 1 s 才动一次，不再每个样点都重算刻度
            if self.window_s:
                step = max(self.window_s / 300.0, 0.05)
                right = math.ceil(float(xs[-1]) / step) * step
                left = max(0.0, right - self.window_s)
            else:
                right = math.ceil(float(xs[-1]))
                left = float(xs[0])
            if self._x_view != (left, right):
                self.pi.setXRange(left, max(right, left + 0.1), padding=0)
                self._x_view = (left, right)

    # ------------------------------------------------------------ 历史
    def show_history(self, rows: Sequence[dict]) -> int:
        if not rows:
            return 0
        t0 = rows[0]["unix_ms"] / 1000.0
        xs = [r["unix_ms"] / 1000.0 - t0 for r in rows]
        yv = [max(0, r.get("v_mv", 0)) / 1000.0 for r in rows]
        yi = [max(0, r.get("i_ma", 0)) / 1000.0 for r in rows]
        if len(xs) > self.MAX_POINTS:
            idx = lttb_indices(xs, yv, self.MAX_POINTS)
            xs = [xs[i] for i in idx]
            yv = [yv[i] for i in idx]
            yi = [yi[i] for i in idx]
        self.curve_v.setData([], [])
        self.curve_i.setData([], [])
        self.hist_v.setData(xs, yv)
        self.hist_i.setData(xs, yi)
        self.history_mode = True
        self.follow = False
        self._x_view = None
        self._set_y_ranges(yv, yi)
        self.pi.setXRange(float(xs[0]), max(float(xs[-1]), float(xs[0]) + 0.1), padding=0)
        return len(rows)

    def exit_history(self) -> None:
        self.history_mode = False
        self.follow = True
        self._x_view = None
        self.hist_v.setData([], [])
        self.hist_i.setData([], [])
        self._refresh_live()
