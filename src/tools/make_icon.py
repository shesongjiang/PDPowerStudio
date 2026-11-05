"""生成 assets/pdpower.ico（纯代码绘制，不引用外部素材）。

用 Qt 离屏渲染 5 个尺寸的 PNG，再手工封装成 ICO（Vista+ 支持 PNG 条目），
避免引入 Pillow 之类仅打包用的依赖。
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen, QPolygonF,
)

ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "assets"
PNG_DIR = ICON_DIR / "png"
SIZES = (256, 64, 48, 32, 16)

BOLT = [(0.62, 0.08), (0.28, 0.55), (0.47, 0.55), (0.38, 0.92), (0.74, 0.40), (0.53, 0.40), (0.62, 0.08)]


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pad = size * 0.03
    body = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    radius = size * 0.18

    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, QColor("#0b1220"))
    gradient.setColorAt(1.0, QColor("#0e3a52"))
    painter.setBrush(QBrush(gradient))
    painter.setPen(QPen(QColor("#38bdf8"), max(1.0, size * 0.015)))
    painter.drawRoundedRect(body, radius, radius)

    painter.setBrush(QBrush(QColor("#7dd3fc")))
    painter.setPen(QPen(QColor("#e0f2fe"), max(1.0, size * 0.008)))
    painter.drawPolygon(QPolygonF([QPointF(x * size, y * size) for x, y in BOLT]))
    painter.end()
    return img


def build_ico(entries: list[tuple[int, bytes]], out: Path) -> None:
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    blob = b""
    for size, png in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blob += png
    out.write_bytes(header + directory + blob)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 - 保持 Qt 初始化
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[int, bytes]] = []
    for size in SIZES:
        path = PNG_DIR / f"icon_{size}.png"
        render(size).save(str(path), "PNG")
        entries.append((size, path.read_bytes()))
    build_ico(entries, ICON_DIR / "pdpower.ico")
    print(f"icon -> {ICON_DIR / 'pdpower.ico'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
