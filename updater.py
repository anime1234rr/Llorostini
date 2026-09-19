from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from version import ASSET_NAME, GITHUB_REPO, __version__

ProgressCallback = Callable[[int, int], None]

TRUSTED_PREFIX = "https://github.com/"


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    page_url: str
    asset_url: Optional[str]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def can_self_update() -> bool:
    return is_frozen() and os.name == "nt"


def parse_version(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", text.strip().lstrip("vV").split("-")[0])
    if not numbers:
        raise UpdateError(f"Versión no reconocida: {text!r}")
    parts = [int(n) for n in numbers]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def _open(url: str, timeout: int = 20):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "LLorostini-updater", "Accept": "application/vnd.github+json"},
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No se encontró ninguna release publicada en GitHub.") from exc
        if exc.code in (403, 429):
            raise UpdateError("GitHub limitó las consultas. Inténtalo de nuevo en unos minutos.") from exc
        raise UpdateError(f"GitHub respondió con el error {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError("No se pudo conectar con GitHub. Revisa tu conexión a internet.") from exc


def fetch_latest_release() -> Release:
    with _open(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest") as response:
        try:
            data = json.load(response)
        except json.JSONDecodeError as exc:
            raise UpdateError("Respuesta inválida de GitHub.") from exc

    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets", [])}
    return Release(
        version=str(data.get("tag_name", "")),
        notes=str(data.get("body") or "").strip(),
        page_url=str(data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"),
        asset_url=assets.get(ASSET_NAME),
    )


def check_for_update() -> Optional[Release]:
    release = fetch_latest_release()
    return release if is_newer(release.version) else None


def download_update(release: Release, on_progress: Optional[ProgressCallback] = None) -> Path:
    if not can_self_update():
        raise UpdateError("La actualización automática solo funciona en el ejecutable .exe.")
    if not release.asset_url or not release.asset_url.startswith(TRUSTED_PREFIX):
        raise UpdateError("La release no incluye un ejecutable descargable.")

    target = Path(sys.executable)
    destination = target.with_name(f"{target.stem}.update{target.suffix}")
    try:
        with _open(release.asset_url, timeout=60) as response, open(destination, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(1024 * 256):
                out.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
        if total and done != total:
            raise UpdateError("La descarga quedó incompleta. Inténtalo de nuevo.")
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise UpdateError(f"No se pudo guardar la actualización: {exc}") from exc
    except UpdateError:
        destination.unlink(missing_ok=True)
        raise
    return destination


def apply_update(new_file: Path) -> None:
    current = Path(sys.executable)
    script = Path(tempfile.gettempdir()) / "llorostini_update.bat"
    script.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "set tries=0\r\n"
        ":retry\r\n"
        "set /a tries+=1\r\n"
        f'move /y "{new_file}" "{current}" >nul 2>&1\r\n'
        "if not errorlevel 1 goto done\r\n"
        "if %tries% geq 60 goto fail\r\n"
        "ping 127.0.0.1 -n 2 >nul\r\n"
        "goto retry\r\n"
        ":done\r\n"
        f'start "" "{current}"\r\n'
        ":fail\r\n"
        'del "%~f0"\r\n',
        encoding="utf-8",
    )
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen(
        ["cmd", "/c", str(script)],
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
