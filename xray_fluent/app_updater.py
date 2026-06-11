"""Self-update: check GitHub releases, download, extract, restart."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile  # kept for legacy .zip support
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request

from .http_utils import build_opener, urlopen
from .zip_utils import safe_extract_zip

from PyQt6.QtCore import QThread, pyqtSignal

from .constants import APP_VERSION, BASE_DIR

GITHUB_REPO = "krambovic/Lumen-KVN"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT = f"BebraVPN/{APP_VERSION}"

LUMEN_APP_NAME = "Lumen KVN"


def _find_self_uninstaller() -> Path | None:
    """Locate the Inno Setup uninstaller for the current bebra-kvn install."""
    try:
        candidates = sorted(BASE_DIR.glob("unins*.exe"))
    except Exception:
        candidates = []
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except Exception:
            continue
    return None


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _write_utf8_bom_text(path: Path, text: str) -> None:
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))


def _resolve_extracted_app_dir(root: Path, exe_name: str) -> Path:
    if (root / exe_name).is_file():
        return root
    child_dirs = [path for path in root.iterdir() if path.is_dir()]
    if len(child_dirs) == 1 and (child_dirs[0] / exe_name).is_file():
        return child_dirs[0]
    for path in child_dirs:
        if (path / exe_name).is_file():
            return path
    return root


@dataclass(slots=True)
class AppUpdate:
    version: str
    tag: str
    download_url: str
    size: int
    notes: str
    digest_sha256: str = ""


_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


def _parse_semver(version: str) -> tuple[int, int, int, list[str]] | None:
    match = _SEMVER_RE.search(version.strip().lstrip("v"))
    if not match:
        return None
    major, minor, patch, suffix = match.groups()
    prerelease = suffix.split(".") if suffix else []
    return int(major), int(minor), int(patch), prerelease


def _compare_prerelease(left: list[str], right: list[str]) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1

    for left_part, right_part in zip(left, right):
        if left_part == right_part:
            continue
        left_is_num = left_part.isdigit()
        right_is_num = right_part.isdigit()
        if left_is_num and right_is_num:
            left_num = int(left_part)
            right_num = int(right_part)
            if left_num != right_num:
                return 1 if left_num > right_num else -1
            continue
        if left_is_num != right_is_num:
            return -1 if left_is_num else 1
        return 1 if left_part > right_part else -1

    if len(left) == len(right):
        return 0
    return 1 if len(left) > len(right) else -1


def _is_newer_version(latest: str, current: str) -> bool:
    latest_parts = _parse_semver(latest)
    current_parts = _parse_semver(current)
    if latest_parts is None or current_parts is None:
        return latest.strip().lstrip("v") != current.strip().lstrip("v")

    latest_core = latest_parts[:3]
    current_core = current_parts[:3]
    if latest_core != current_core:
        return latest_core > current_core
    return _compare_prerelease(latest_parts[3], current_parts[3]) > 0


def _extract_digest(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1].strip()
    parts = "".join(ch for ch in text if ch in "0123456789abcdef")
    return parts if len(parts) == 64 else ""


def _sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8", errors="replace")


def _is_setup_asset(asset: dict) -> bool:
    name = str(asset.get("name") or "").lower()
    if not name.endswith(".exe"):
        return False
    return ("setup" in name) or ("install" in name)


def _asset_score(asset: dict) -> tuple[int, str]:
    name = str(asset.get("name") or "").lower()
    if not _is_setup_asset(asset):
        return (0, name)
    score = 1
    if "lumen" in name:
        score += 3
    if "windows" in name or "win" in name:
        score += 2
    if "x64" in name or "win64" in name or "amd64" in name:
        score += 2
    if "setup" in name:
        score += 1
    return (score, name)


class UpdateChecker(QThread):
    """Check GitHub for a newer release."""

    result = pyqtSignal(object)  # AppUpdate | None
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self) -> None:
        try:
            req = Request(GITHUB_API, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            tag = data.get("tag_name", "")

            setup_assets = [
                a for a in data.get("assets", [])
                if _is_setup_asset(a)
            ]
            asset = max(
                setup_assets,
                key=_asset_score,
                default=None,
            )

            if not asset:
                self.error.emit(
                    f"Релиз Lumen KVN {tag} найден, но в нём нет setup-установщика"
                )
                return

            digest = _extract_digest(str(asset.get("digest") or ""))
            if not digest:
                asset_name = str(asset.get("name") or "")
                sidecar = None
                for suffix in (".sha256", ".dgst"):
                    expected = f"{asset_name}{suffix}".lower()
                    sidecar = next(
                        (
                            candidate for candidate in data.get("assets", [])
                            if str(candidate.get("name") or "").lower() == expected
                        ),
                        None,
                    )
                    if sidecar:
                        break
                if sidecar:
                    digest = _extract_digest(
                        _fetch_text(str(sidecar.get("browser_download_url") or ""))
                    )

            self.result.emit(AppUpdate(
                version=tag.lstrip("v"),
                tag=tag,
                download_url=asset["browser_download_url"],
                size=asset.get("size", 0),
                notes=data.get("body", ""),
                digest_sha256=digest,
            ))
        except Exception as exc:
            self.error.emit(str(exc))
            return


_log = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 30  # seconds — per socket operation (connect + each read)
_NUM_SEGMENTS = 4       # parallel download segments
_CHUNK_SIZE = 1024 * 1024  # 1 MB


class UpdateDownloader(QThread):
    """Download and extract update, then launch restart script."""

    progress = pyqtSignal(int)       # percent 0-100
    status = pyqtSignal(str)         # human-readable status message
    finished_ok = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(
        self,
        update: AppUpdate,
        proxy_url: str | None = None,
        restart_in_tray: bool = False,
        parent=None,
        migration_state: dict | None = None,
    ):
        super().__init__(parent)
        self._update = update
        self._proxy_url = proxy_url
        self._restart_in_tray = restart_in_tray
        # Full local AppState (servers + settings) to migrate into Lumen KVN.
        self._migration_state = migration_state

    # ── download helpers ────────────────────────────────────────

    def _build_opener(self, proxy_url: str | None) -> urllib.request.OpenerDirector:
        if proxy_url:
            handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            return build_opener(handler)
        return build_opener()

    def _supports_range(self, url: str, opener: urllib.request.OpenerDirector) -> tuple[bool, int]:
        """HEAD request to check Range support and get Content-Length."""
        req = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with opener.open(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
            accepts = resp.headers.get("Accept-Ranges", "").lower()
            length = int(resp.headers.get("Content-Length", 0))
            return accepts == "bytes" and length > 0, length

    def _download_segment(
        self,
        url: str,
        proxy_url: str | None,
        start: int,
        end: int,
        seg_path: Path,
        seg_index: int,
        lock: threading.Lock,
        progress_arr: list[int],
        total: int,
    ) -> None:
        """Download one segment with Range header."""
        opener = self._build_opener(proxy_url)
        expected_length = end - start + 1
        req = Request(url, headers={
            "User-Agent": USER_AGENT,
            "Range": f"bytes={start}-{end}",
        })
        with opener.open(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
            status_code = getattr(resp, "status", None)
            content_range = resp.headers.get("Content-Range", "")
            if status_code != 206 or not content_range.startswith(f"bytes {start}-{end}/"):
                raise RuntimeError("Сервер некорректно ответил на Range-запрос")

            downloaded = 0
            with open(seg_path, "wb") as f:
                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    with lock:
                        progress_arr[seg_index] += len(chunk)
                        done = sum(progress_arr)
                        self.progress.emit(int(done * 100 / total))
            if downloaded != expected_length:
                raise RuntimeError("Сервер вернул неполный фрагмент архива")

    def _download_single(self, url: str, opener: urllib.request.OpenerDirector, zip_path: Path) -> None:
        """Single-connection fallback download."""
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with opener.open(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(zip_path, "wb") as f:
                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        if downloaded == 0:
                            raise TimeoutError("Сервер не отдаёт данные")
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        self.progress.emit(int(downloaded * 100 / total))

    def _download(self, zip_path: Path, proxy_url: str | None) -> None:
        """Download update zip with multi-segment acceleration.

        Tries parallel Range-based download first; falls back to single
        connection if the server doesn't support Range requests.
        """
        url = self._update.download_url
        opener = self._build_opener(proxy_url)

        # Check if server supports Range requests
        try:
            supports_range, total = self._supports_range(url, opener)
        except Exception:
            supports_range, total = False, 0

        if not supports_range or total == 0 or total < _NUM_SEGMENTS * _CHUNK_SIZE:
            _log.info("Server does not support Range or file too small — single download")
            self._download_single(url, opener, zip_path)
            return

        # Split into segments
        seg_size = total // _NUM_SEGMENTS
        segments: list[tuple[int, int]] = []
        for i in range(_NUM_SEGMENTS):
            start = i * seg_size
            end = total - 1 if i == _NUM_SEGMENTS - 1 else (i + 1) * seg_size - 1
            segments.append((start, end))

        # Prepare temp segment files
        seg_dir = zip_path.parent / "_segments"
        seg_dir.mkdir(exist_ok=True)
        seg_paths = [seg_dir / f"seg_{i}" for i in range(_NUM_SEGMENTS)]

        lock = threading.Lock()
        progress_arr = [0] * _NUM_SEGMENTS

        # Download segments in parallel
        try:
            with ThreadPoolExecutor(max_workers=_NUM_SEGMENTS) as pool:
                futures = []
                for i, (start, end) in enumerate(segments):
                    fut = pool.submit(
                        self._download_segment,
                        url, proxy_url, start, end,
                        seg_paths[i], i, lock, progress_arr, total,
                    )
                    futures.append(fut)

                # Re-raise any segment exception
                for fut in futures:
                    fut.result()

            # Concatenate segments into final file
            with open(zip_path, "wb") as out:
                for sp in seg_paths:
                    with open(sp, "rb") as seg_f:
                        shutil.copyfileobj(seg_f, out)
        except Exception as exc:
            _log.warning("Segmented download failed, falling back to single download: %s", exc)
            if zip_path.exists():
                zip_path.unlink()
            self.progress.emit(0)
            self._download_single(url, opener, zip_path)
        finally:
            # Clean up segment temp files
            shutil.rmtree(seg_dir, ignore_errors=True)

    # ── main thread entry ───────────────────────────────────────

    def run(self) -> None:
        tmp_dir: Path | None = None
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="lumen_migrate_"))
            setup_name = self._update.download_url.rsplit("/", 1)[-1] or "LumenKVN-Setup.exe"
            if not setup_name.lower().endswith(".exe"):
                setup_name += ".exe"
            zip_path = tmp_dir / setup_name

            downloaded_ok = False

            # Attempt 1: through proxy (if available)
            if self._proxy_url:
                self.status.emit("Загрузка через прокси...")
                try:
                    self._download(zip_path, self._proxy_url)
                    downloaded_ok = True
                except Exception as exc:
                    _log.warning("Proxy download failed: %s", exc)
                    self.status.emit(
                        "Прокси-сервер недоступен, пробую напрямую..."
                    )
                    self.progress.emit(0)
                    # clean partial file
                    if zip_path.exists():
                        zip_path.unlink()

            # Attempt 2: direct (no proxy)
            if not downloaded_ok:
                self.status.emit("Загрузка напрямую...")
                try:
                    self._download(zip_path, None)
                    downloaded_ok = True
                except Exception as exc:
                    _log.warning("Direct download failed: %s", exc)

            if not downloaded_ok:
                msg = (
                    "Не удалось скачать обновление.\n"
                    "Переключитесь на рабочий сервер и попробуйте снова."
                )
                if self._proxy_url:
                    msg = (
                        "Не удалось скачать обновление ни через прокси, ни напрямую.\n"
                        "Переключитесь на рабочий сервер и попробуйте снова."
                    )
                self.error.emit(msg)
                # cleanup
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return

            # Integrity check is optional for the Lumen KVN installer: verify
            # only when the release exposes a digest (asset digest or sidecar).
            expected_hash = _extract_digest(self._update.digest_sha256)
            if expected_hash:
                self.status.emit("Проверка установщика...")
                real_hash = _sha256_file(zip_path)
                if real_hash.lower() != expected_hash.lower():
                    self.error.emit("Контрольная сумма установщика не совпадает")
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return

            self.progress.emit(100)

            self.status.emit("Подготовка переноса серверов...")
            config_file = ""
            try:
                # ensure_ascii + utf-8 (no BOM) so the file starts with '{'.
                state_path = tmp_dir / "lumen-servers.json"
                state_path.write_text(
                    json.dumps(self._migration_state or {}, ensure_ascii=True, indent=2),
                    encoding="utf-8",
                )
                config_file = str(state_path)
            except Exception as exc:
                _log.warning("Server config export failed: %s", exc)
                config_file = ""

            self.status.emit("Установка Lumen KVN...")
            current_pid = os.getpid()
            uninstaller = _find_self_uninstaller()
            script = tmp_dir / "_migrate.ps1"
            script_text = "\r\n".join([
                "$ErrorActionPreference = 'SilentlyContinue'",
                "$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()",
                "if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {",
                "    try { Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',$PSCommandPath } catch {}",
                "    exit",
                "}",
                f"$pidToWait = {current_pid}",
                f"$setupPath = {_powershell_literal(str(zip_path))}",
                f"$tempDir = {_powershell_literal(str(tmp_dir))}",
                f"$configFile = {_powershell_literal(config_file)}",
                f"$uninstaller = {_powershell_literal(str(uninstaller) if uninstaller else '')}",
                f"$lumenName = {_powershell_literal(LUMEN_APP_NAME)}",
                "$lumenDir = Join-Path $env:ProgramFiles $lumenName",
                "for ($i = 0; $i -lt 120; $i++) {",
                "    if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }",
                "    Start-Sleep -Milliseconds 500",
                "}",
                "if (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) { Stop-Process -Id $pidToWait -Force -ErrorAction SilentlyContinue }",
                "Start-Sleep -Milliseconds 800",
                "# (1) Install Lumen KVN first, into a known folder.",
                "if (Test-Path -LiteralPath $setupPath) {",
                "    try { Start-Process -FilePath $setupPath -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/DIR=' + $lumenDir) -Wait } catch { try { Start-Process -FilePath $setupPath -Wait } catch {} }",
                "}",
                "Start-Sleep -Milliseconds 1500",
                "# (2) Copy the server config straight into Lumen KVN's data folder.",
                "if ((Test-Path -LiteralPath $lumenDir) -and $configFile.Length -gt 0 -and (Test-Path -LiteralPath $configFile)) {",
                "    $dataDir = Join-Path $lumenDir 'data'",
                "    try { New-Item -ItemType Directory -Force -Path $dataDir | Out-Null } catch {}",
                "    try { Copy-Item -LiteralPath $configFile -Destination (Join-Path $dataDir 'state.enc') -Force } catch {}",
                "}",
                "Start-Sleep -Milliseconds 400",
                "# (3) Uninstall bebra-kvn.",
                "if ($uninstaller.Length -gt 0 -and (Test-Path -LiteralPath $uninstaller)) {",
                "    try { Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait } catch {}",
                "}",
                "Start-Sleep -Milliseconds 800",
                "# (4) Launch Lumen KVN.",
                "if (Test-Path -LiteralPath $lumenDir) {",
                "    $exe = Get-ChildItem -Path $lumenDir -Filter *.exe -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike 'unins*' } | Select-Object -First 1",
                "    if ($exe) { try { Start-Process -FilePath $exe.FullName } catch {} }",
                "}",
                "Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue",
                "",
            ])
            _write_utf8_bom_text(script, script_text)

            # Launch the detached handoff script and exit so the uninstaller can
            # remove bebra-kvn while the Lumen KVN setup takes over.
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-WindowStyle",
                    "Hidden",
                    "-File",
                    str(script),
                ],
                creationflags=0x08000000,
                close_fds=True,
            )

            self.finished_ok.emit()
            return

        except Exception as exc:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self.error.emit(str(exc))
