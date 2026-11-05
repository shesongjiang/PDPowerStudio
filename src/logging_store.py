"""挂测日志：CSV / JSONL、按天或按大小滚动、崩溃后续写、区间回读。

落盘在独立 writer 线程，采集线程只做 append()，两者完全解耦。
"""
from __future__ import annotations

import csv
import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Iterable

FIELDS = ["ts", "unix_ms", "v_mv", "i_ma", "mode", "out_en", "in_state", "in_mv", "pd_dv"]


@dataclass
class LogConfig:
    directory: Path
    fmt: str = "csv"              # csv | jsonl
    hz: float = 5.0               # 建议 5（官方 200ms）；实测可到 ~17Hz
    fsync: bool = False
    fsync_interval_s: float = 5.0
    roll_daily: bool = True
    max_file_mb: float = 32.0
    prefix: str = ""


def estimate_size(hz: float, hours: float, fmt: str = "csv") -> dict:
    lines = int(hz * 3600 * hours)
    per_line = 78 if fmt == "csv" else 132
    total = lines * per_line
    return {"samples": lines, "bytes": total, "mib": round(total / 1024 / 1024, 2)}


class LogStore:
    def __init__(self, config: LogConfig) -> None:
        self.config = config
        self._queue: queue.Queue = queue.Queue(maxsize=10000)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._current: Path | None = None
        self._fh = None
        self._writer: csv.writer | None = None
        self._lines_since_flush = 0
        self._last_fsync = 0.0
        self._index = 0
        self._meta: dict[str, Any] = {}
        self.dropped = 0

    # ---------------------------------------------------------- 生命周期
    def start(self, meta: dict[str, Any] | None = None) -> Path:
        self.config.directory.mkdir(parents=True, exist_ok=True)
        self._meta = dict(meta or {})
        self._stop.clear()
        self._index = 0
        self._open_for(date.today())
        self._thread = threading.Thread(target=self._run, name="pd-log-writer", daemon=True)
        self._thread.start()
        return self._current or self.config.directory

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=3.0)
            if thread.is_alive():
                # 写入线程还在收尾（队列未排空），由它自己关闭文件，
                # 主线程提前 close 会让写入抛 ValueError 并静默丢数据。
                return
        self._close()

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_path(self) -> Path | None:
        return self._current

    # ---------------------------------------------------------- 采集侧
    def append(self, snapshot: dict) -> None:
        if not self.active:
            return
        disp = snapshot.get("display") or {}
        row = {
            "ts": datetime.fromtimestamp(snapshot.get("last_update_ts") or time.time()).isoformat(timespec="milliseconds"),
            "unix_ms": int((snapshot.get("last_update_ts") or time.time()) * 1000),
            "v_mv": int(disp.get("mv", 0)),
            "i_ma": int(disp.get("ma", 0)),
            "mode": int(snapshot.get("output_mode", 0)),
            "out_en": int(bool(snapshot.get("output_enabled"))),
            "in_state": snapshot.get("input_state"),
            "in_mv": snapshot.get("input_mv"),
            "pd_dv": snapshot.get("pd_set_dv"),
        }
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    # ---------------------------------------------------------- 写入侧
    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                row = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._maybe_fsync()
                continue
            self._write_row(row)
        self._maybe_fsync(force=True)
        self._close()

    def _open_for(self, day: date) -> None:
        self._close()
        stamp = day.strftime("%Y%m%d")
        stem = f"{self.config.prefix or 'pdpower'}_{stamp}"
        ext = "csv" if self.config.fmt == "csv" else "jsonl"
        path = self.config.directory / f"{stem}.{ext}"
        while path.exists() and path.stat().st_size > self.config.max_file_mb * 1024 * 1024:
            self._index += 1
            path = self.config.directory / f"{stem}_{self._index}.{ext}"
        new = not path.exists() or path.stat().st_size == 0
        if not new:
            self._repair_tail(path)
        self._fh = open(path, "a", encoding="utf-8", newline="")
        self._current = path
        if self.config.fmt == "csv":
            self._writer = csv.DictWriter(self._fh, fieldnames=FIELDS)
            if new:
                self._writer.writeheader()
                self._fh.flush()
        else:
            self._writer = None
        self._write_meta(path, new)

    def _write_meta(self, path: Path, new: bool) -> None:
        meta_path = path.with_suffix(".session.json")
        if new or not meta_path.exists():
            payload = dict(self._meta)
            payload.setdefault("started_at", datetime.now().isoformat(timespec="seconds"))
            payload["format"] = self.config.fmt
            payload["hz"] = self.config.hz
            meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _repair_tail(path: Path) -> None:
        """崩溃后修复：末行不完整则截断到最后一个换行，避免污染后续解析。"""
        try:
            with open(path, "rb+") as fh:
                fh.seek(0)
                data = fh.read()
                if not data or data.endswith(b"\n"):
                    return
                idx = data.rfind(b"\n")
                if idx >= 0:
                    fh.seek(idx + 1)
                    fh.truncate()
        except OSError:
            pass

    def _write_row(self, row: dict) -> None:
        if self._fh is None:
            return
        try:
            if self.config.fmt == "csv":
                self._writer.writerow(row)  # type: ignore[union-attr]
            else:
                self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._lines_since_flush += 1
            if self._lines_since_flush >= 20:
                self._fh.flush()
                self._lines_since_flush = 0
            self._maybe_fsync()
        except (OSError, ValueError):
            # 磁盘满/文件被关闭：停止记录，不要让 writer 线程静默死亡
            self._stop.set()

    def _maybe_fsync(self, force: bool = False) -> None:
        if self._fh is None or not self.config.fsync:
            return
        now = time.time()
        if force or now - self._last_fsync >= self.config.fsync_interval_s:
            os.fsync(self._fh.fileno())
            self._last_fsync = now

    def _close(self) -> None:
        if self._fh:
            try:
                self._fh.flush()
                if self.config.fsync:
                    os.fsync(self._fh.fileno())
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._current = None
        self._thread = None


def read_range(directory: Path, start_ms: int, end_ms: int) -> list[dict]:
    """按时间段回读（用于长时段曲线回看）。支持 csv/jsonl，按行过滤。"""
    rows: list[dict] = []
    for path in sorted(Path(directory).glob("*")):
        if path.suffix.lower() not in (".csv", ".jsonl"):
            continue
        try:
            if path.suffix.lower() == ".csv":
                with open(path, newline="", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        ts = int(float(row.get("unix_ms") or 0))
                        if start_ms <= ts <= end_ms:
                            rows.append(_coerce(row))
            else:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if start_ms <= int(row.get("unix_ms", 0)) <= end_ms:
                            rows.append(_coerce(row))
        except OSError:
            continue
    rows.sort(key=lambda r: r["unix_ms"])
    return rows


def _coerce(row: dict) -> dict:
    out = dict(row)
    for key in ("unix_ms", "v_mv", "i_ma", "mode", "out_en"):
        try:
            out[key] = int(float(out.get(key) or 0))
        except (TypeError, ValueError):
            out[key] = 0
    for key in ("in_state", "in_mv", "pd_dv"):
        value = out.get(key)
        out[key] = None if value in (None, "", "None") else int(float(value))
    return out
