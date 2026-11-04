"""GitHub 固件发现（stdlib urllib，无需第三方依赖）。

该仓库没有 Releases / tag，因此走 Contents API 列目录后按四段版本号取最大。
降级策略：ETag 缓存 → raw 直链 → 手动选择本地 .fpk。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPOS = {
    "PDPOWERMINIV1": "WeActStudio/WeActStudio.PDPowerMiniV1-Buck",
    "PDPOWERMINIV11": "WeActStudio/WeActStudio.PDPowerMiniV1.1-Buck",
}
ASSET_PATTERNS = {
    "PDPOWERMINIV1": re.compile(r"^WeAct-PDPowerMiniV1_V(\d+)\.(\d+)\.(\d+)\.(\d+)\.fpk$", re.I),
    "PDPOWERMINIV11": re.compile(r"^WeAct-PDPowerMiniV1\.1_V(\d+)\.(\d+)\.(\d+)\.(\d+)\.fpk$", re.I),
}


class UpdateError(RuntimeError):
    def __init__(self, message: str, *, offline: bool = False, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.offline = offline
        self.rate_limited = rate_limited


@dataclass
class FirmwareAsset:
    version: tuple[int, int, int, int]
    filename: str
    download_url: str
    size: int
    sha256_remote: str | None = None

    @property
    def version_text(self) -> str:
        return "V" + ".".join(str(v) for v in self.version)


class GitHubFirmwareSource:
    def __init__(
        self,
        model_key: str,
        token: str | None = None,
        cache_dir: Path | None = None,
        timeout: float = 10.0,
        user_agent: str = "PDPowerStudio/0.1",
    ) -> None:
        self.model_key = model_key
        self.repo = REPOS[model_key]
        self.token = token
        self.timeout = timeout
        self.user_agent = user_agent
        self.cache_dir = cache_dir or Path.home() / ".pdpower_studio" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / f"{self.repo.replace('/', '_')}.json"

    # ------------------------------------------------------------- 查询
    def _request(self, url: str, accept: str = "application/vnd.github+json") -> bytes:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", self.user_agent)
        req.add_header("Accept", accept)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise UpdateError("GitHub API 限流（未认证 60 次/小时）", rate_limited=True) from exc
            if exc.code == 404:
                raise UpdateError(f"仓库或路径不存在: {url}") from exc
            raise UpdateError(f"GitHub HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateError(f"网络不可用: {exc}", offline=True) from exc

    def _list_firmware(self) -> list[dict]:
        url = f"https://api.github.com/repos/{self.repo}/contents/Firmware"
        raw = self._request(url)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise UpdateError("GitHub 返回内容不是 JSON") from exc

    def find_latest(self, use_cache: bool = True) -> FirmwareAsset:
        cached = self._load_cache() if use_cache else None
        try:
            entries = self._list_firmware()
        except UpdateError as exc:
            if exc.offline or exc.rate_limited:
                if cached:
                    return cached["asset"]
            raise
        pattern = ASSET_PATTERNS[self.model_key]
        best: FirmwareAsset | None = None
        for entry in entries:
            if entry.get("type") != "file":
                continue
            match = pattern.match(entry.get("name", ""))
            if not match:
                continue
            version = tuple(int(x) for x in match.groups())
            if best is None or version > best.version:
                best = FirmwareAsset(
                    version=version,  # type: ignore[arg-type]
                    filename=entry["name"],
                    download_url=entry.get("download_url") or "",
                    size=int(entry.get("size") or 0),
                )
        if best is None:
            raise UpdateError("未在仓库 Firmware/ 中找到匹配当前型号的 .fpk")
        self._save_cache(best)
        return best

    def download(self, asset: FirmwareAsset, dest_dir: Path | None = None) -> Path:
        dest_dir = Path(dest_dir or self.cache_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / asset.filename
        if dest.exists() and dest.stat().st_size == asset.size > 0:
            return dest
        try:
            with urllib.request.urlopen(
                urllib.request.Request(asset.download_url, headers={"User-Agent": self.user_agent}),
                timeout=self.timeout,
            ) as resp:
                data = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateError(f"固件下载失败: {exc}", offline=True) from exc
        if asset.size and len(data) != asset.size:
            raise UpdateError(f"下载体积不符：期望 {asset.size}，实际 {len(data)}")
        dest.write_bytes(data)
        return dest

    # ------------------------------------------------------------- 缓存
    def _load_cache(self) -> dict | None:
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - payload.get("checked_at", 0) > 7 * 24 * 3600:
            return None
        asset = payload.get("asset") or {}
        try:
            return {"asset": FirmwareAsset(
                version=tuple(asset["version"]), filename=asset["filename"],
                download_url=asset["download_url"], size=asset["size"],
            )}
        except (KeyError, TypeError):
            return None

    def _save_cache(self, asset: FirmwareAsset) -> None:
        payload = {
            "checked_at": time.time(),
            "asset": {
                "version": list(asset.version), "filename": asset.filename,
                "download_url": asset.download_url, "size": asset.size,
            },
        }
        try:
            self.cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass
