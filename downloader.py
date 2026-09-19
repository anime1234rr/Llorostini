from __future__ import annotations

import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

ProgressCallback = Callable[[dict], None]


class DownloaderError(Exception):
    pass


@dataclass(frozen=True)
class VideoInfo:
    title: str
    uploader: str
    duration: Optional[int]
    heights: list[int]


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def ffmpeg_path() -> Optional[str]:
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg.exe"
        if bundled.is_file():
            return str(bundled)
    return shutil.which("ffmpeg")


def has_ffmpeg() -> bool:
    return ffmpeg_path() is not None


def validate_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DownloaderError("La URL no es válida. Debe empezar con http:// o https://")
    return url


def _translate_error(exc: Exception) -> DownloaderError:
    msg = str(exc).lower()
    if "private video" in msg or "sign in" in msg:
        return DownloaderError("El video es privado o requiere iniciar sesión.")
    if "unavailable" in msg or "removed" in msg or "deleted" in msg or "terminated" in msg:
        return DownloaderError("El video no está disponible (eliminado o borrado).")
    if "age" in msg and "restricted" in msg:
        return DownloaderError("El video tiene restricción de edad.")
    if "not available in your country" in msg or "geo" in msg:
        return DownloaderError("El video no está disponible en tu país.")
    if "unsupported url" in msg:
        return DownloaderError("URL no soportada: no parece un enlace de video válido.")
    if any(k in msg for k in ("urlopen error", "timed out", "connection", "network", "getaddrinfo")):
        return DownloaderError("Error de red. Revisa tu conexión a internet e inténtalo de nuevo.")
    return DownloaderError(f"No se pudo completar la operación: {exc}")


def safe_filename(name: str, max_length: int = 100) -> str:
    text = unicodedata.normalize("NFKD", name)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w .()\[\]&+,'-]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text[:max_length].rstrip(" .-_") or "video"


def clean_output_name(path: Path) -> Path:
    target = path.with_name(safe_filename(path.stem) + path.suffix.lower())
    if target == path:
        return path
    counter = 1
    while target.exists():
        target = path.with_name(f"{safe_filename(path.stem)} ({counter}){path.suffix.lower()}")
        counter += 1
    try:
        path.rename(target)
    except OSError:
        return path
    return target


def get_info(url: str) -> VideoInfo:
    url = validate_url(url)
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": _QuietLogger()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except (DownloadError, ExtractorError) as exc:
        raise _translate_error(exc) from exc

    heights = sorted(
        {f["height"] for f in info.get("formats", []) if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    return VideoInfo(
        title=info.get("title", "desconocido"),
        uploader=info.get("uploader", "desconocido"),
        duration=info.get("duration"),
        heights=heights,
    )


VIDEO_CONTAINERS = ("mp4", "mkv")
AUDIO_FORMATS = ("mp3", "m4a", "aac", "flac", "wav")


def build_format(height: Optional[int], audio_only: bool, merge: bool) -> str:
    if audio_only:
        return "ba/b"
    cap = f"[height<={height}]" if height else ""
    if not merge:
        return f"b{cap}/b"
    return f"bv*{cap}+ba/b{cap}/b"


def download(
    url: str,
    output_dir: str | Path = "descargas",
    height: Optional[int] = None,
    audio_only: bool = False,
    on_progress: Optional[ProgressCallback] = None,
    container: str = "mp4",
    audio_format: str = "mp3",
) -> Path:
    url = validate_url(url)
    if container not in VIDEO_CONTAINERS:
        raise DownloaderError(f"Contenedor no soportado: {container}")
    if audio_format not in AUDIO_FORMATS:
        raise DownloaderError(f"Formato de audio no soportado: {audio_format}")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = ffmpeg_path()
    ffmpeg = ffmpeg_bin is not None
    opts: dict = {
        "format": build_format(height, audio_only, ffmpeg),
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "logger": _QuietLogger(),
        "progress_hooks": [on_progress] if on_progress else [],
    }
    if ffmpeg_bin:
        opts["ffmpeg_location"] = ffmpeg_bin
    if audio_only and ffmpeg:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": audio_format, "preferredquality": "192"}
        ]
    elif ffmpeg:
        opts["merge_output_format"] = container
        if container == "mp4":
            opts["format_sort"] = ["res", "vcodec:h264", "acodec:m4a"]
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": container}]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloads = info.get("requested_downloads") or []
            path = Path(downloads[0]["filepath"]) if downloads else Path(ydl.prepare_filename(info))
    except (DownloadError, ExtractorError) as exc:
        raise _translate_error(exc) from exc
    except OSError as exc:
        raise DownloaderError(f"Error de archivo/disco: {exc}") from exc
    return clean_output_name(path)
