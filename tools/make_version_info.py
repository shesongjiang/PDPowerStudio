"""生成 build/version_info.txt（Windows 版本资源），版本号取自 pdpower_studio.__version__。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from pdpower_studio import __version__  # noqa: E402

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({a}, {b}, {c}, 0),
    prodvers=({a}, {b}, {c}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Independent Developer'),
        StringStruct('FileDescription', 'PDPower Studio - unofficial third-party PC tool for WeAct PDPowerMiniV1'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'pdpower_studio'),
        StringStruct('LegalCopyright', 'Third-party PC tool, not affiliated with WeAct Studio'),
        StringStruct('OriginalFilename', 'PDPowerStudio.exe'),
        StringStruct('ProductName', 'PDPower Studio'),
        StringStruct('ProductVersion', '{version}')])
      ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> int:
    parts = [int(p) for p in __version__.split(".")]
    while len(parts) < 3:
        parts.append(0)
    out = ROOT / "build" / "version_info.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        TEMPLATE.format(a=parts[0], b=parts[1], c=parts[2], version=__version__),
        encoding="utf-8",
    )
    print(f"version_info -> {out} ({__version__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
