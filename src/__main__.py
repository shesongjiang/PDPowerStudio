"""`python -m pdpower_studio` 直接进入 CLI（无参数时打印帮助）。"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
