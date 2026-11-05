"""日志落盘测试：CSV / JSONL、区间回读、崩溃后末行修复、体积估算。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from pdpower_studio.logging_store import (
    FIELDS, LogConfig, LogStore, estimate_size, read_range,
)


def snapshot(ts: float, mv: int, ma: int) -> dict:
    return {
        "display": {"mv": mv, "ma": ma},
        "output_mode": 0,
        "output_enabled": False,
        "input_state": None,
        "input_mv": None,
        "pd_set_dv": None,
        "last_update_ts": ts,
    }


def test_csv_append_and_range_read(tmp_path):
    config = LogConfig(directory=tmp_path, fmt="csv", hz=5)
    store = LogStore(config)
    path = store.start({"note": "unit-test"})
    assert path.suffix == ".csv"
    base = 1_700_000_000
    for i in range(3):
        store.append(snapshot(base + i, 5000 + i, 1000 + i))
    store.stop()

    rows = read_range(tmp_path, base * 1000, (base + 10) * 1000)
    assert len(rows) == 3
    assert rows[0]["v_mv"] == 5000
    assert rows[2]["i_ma"] == 1002
    assert (tmp_path / path.name).with_suffix(".session.json").exists()

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == FIELDS


def test_jsonl_format_and_filtering(tmp_path):
    config = LogConfig(directory=tmp_path, fmt="jsonl")
    store = LogStore(config)
    path = store.start()
    for i in range(4):
        store.append(snapshot(1_700_000_000 + i, 5000, 1000))
    store.stop()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    assert json.loads(lines[0])["v_mv"] == 5000

    # 只回读中间两行
    rows = read_range(tmp_path, 1_700_000_001 * 1000, 1_700_000_002 * 1000)
    assert len(rows) == 2


def test_repairs_partial_tail_after_crash(tmp_path):
    from datetime import date

    path = tmp_path / f"pdpower_{date.today():%Y%m%d}.csv"
    path.write_text(",".join(FIELDS) + "\n" + "1700000000000,1,5000", encoding="utf-8")

    store = LogStore(LogConfig(directory=tmp_path))
    store.start()
    store.append(snapshot(1_700_000_001, 5000, 2000))
    store.stop()

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["v_mv"] == "5000"


def test_estimate_size_scales_linearly():
    csv_info = estimate_size(5, 24, "csv")
    assert csv_info["samples"] == 5 * 3600 * 24
    assert csv_info["bytes"] == csv_info["samples"] * 78
    jsonl_info = estimate_size(5, 1, "jsonl")
    assert jsonl_info["bytes"] == 5 * 3600 * 132
    assert jsonl_info["mib"] > 0


def test_append_before_start_is_noop(tmp_path):
    store = LogStore(LogConfig(directory=tmp_path))
    store.append(snapshot(1, 0, 0))          # 未 start，应被忽略
    assert not store.active
    assert not any(tmp_path.iterdir())
