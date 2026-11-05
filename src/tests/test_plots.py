"""曲线降采样测试：LTTB 保端点、保长度（不创建 QWidget，无需 GUI）。"""
from __future__ import annotations

import math

from pdpower_studio.ui.plots import TIME_WINDOWS, lttb_indices

MAX_POINTS_CHECK = 1200


def test_time_windows_definition():
    assert [seconds for _text, seconds in TIME_WINDOWS] == [30, 300, 1800, 0]


def test_lttb_keeps_endpoints_and_length():
    n = 5000
    xs = [float(i) for i in range(n)]
    ys = [math.sin(i / 50.0) for i in range(n)]
    idx = lttb_indices(xs, ys, MAX_POINTS_CHECK)
    assert len(idx) == MAX_POINTS_CHECK
    assert idx[0] == 0
    assert idx[-1] == n - 1
    assert idx == sorted(idx)
    assert len(set(idx)) == len(idx)


def test_lttb_noop_when_below_threshold():
    xs = [0.0, 1.0, 2.0]
    ys = [0.0, 1.0, 0.0]
    assert lttb_indices(xs, ys, 10) == [0, 1, 2]


def test_lttb_preserves_spike():
    n = 400
    xs = [float(i) for i in range(n)]
    ys = [0.0] * n
    ys[200] = 100.0
    idx = lttb_indices(xs, ys, 50)
    assert any(ys[i] == 100.0 for i in idx)
