"""直接启动 GUI：

    python main.py            （在 pdpower_studio/ 目录内）

也支持从任意目录：python C:\\...\\pdpower_studio\\main.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ROOT.parent))

from pdpower_studio.ui.main_window import main as run_gui

if __name__ == "__main__":
    raise SystemExit(run_gui())
