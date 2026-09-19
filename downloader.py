from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

ProgressCallback = Callable[[dict], None]

VIDEO_CONTAINERS = ("mp4", "mkv")
AUDIO_FORMATS = ("mp3", "m4a", "aac", "flac", "wav")
COVER_FORMATS = ("mp3", "m4a", "flac")
SUBTITLE_LANGS = ["es(-[0-9]+|-[A-Z]{2})?", "en(-[0-9]+|-[A-Z]{2})?"]
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
ALLOWED_PUNCTUATION = " .()[]&+,'-_!"


class DownloaderError(Exception):
    pass


@dataclass(frozen=True)
class VideoInfo:
    title: str
    uploader: str
    duration: Optional[int]
    heights: list[int]
    thumbnail_url: Optional[str] = None


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def default_download_dir() -> Path:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            folder_id = GUID(0x374DE290, 0x123F, 0x4565,
                             (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            buffer = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                                     ctypes.POINTER(ctypes.c_wchar_p)]
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buffer)) == 0:
                path = buffer.value
                ctypes.windll.ole32.CoTaskMemFree(buffer)
                if path:
                    return Path(path)
        except Exception:
            pass
    return Path.home() / "Downloads"




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
        return DownloaderError("Error de red. Revisa tu conexión e inténtalo de nuevo: la descarga se reanudará donde quedó.")
    return DownloaderError(f"No se pudo completar la operación: {exc}")


def safe_filename(name: str, max_length: int = 100) -> str:
    text = unicodedata.normalize("NFKC", name)
    kept = []
    for char in text:
        if 0xFE00 <= ord(char) <= 0xFE0F:
            continue
        if unicodedata.category(char)[0] in "LNM" or char in ALLOWED_PUNCTUATION:
            kept.append(char)
    text = re.sub(r"\s+", " ", "".join(kept)).strip(" .-_")
    text = text[:max_length].rstrip(" .-_") or "video"
    if text.split(".")[0].upper() in RESERVED_NAMES:
        text = f"_{text}"
    return text


def _pick_thumbnail(info: dict) -> Optional[str]:
    candidates = [
        t for t in info.get("thumbnails") or []
        if t.get("url", "").lower().split("?")[0].endswith((".jpg", ".jpeg", ".png")) and t.get("width") and t.get("height")
    ]
    wide = [t for t in candidates if t["width"] / t["height"] >= 1.7]
    pool = wide or candidates
    if pool:
        return min(pool, key=lambda t: abs(t["width"] - 400))["url"]
    return info.get("thumbnail")


def fetch_thumbnail(url: Optional[str]) -> Optional[bytes]:
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read(3_000_000)
    except Exception:
        return None


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
        thumbnail_url=_pick_thumbnail(info),
    )


def build_format(height: Optional[int], audio_only: bool, merge: bool) -> str:
    if audio_only:
        return "ba/b"
    cap = f"[height<={height}]" if height else ""
    if not merge:
        return f"b{cap}/b"
    return f"bv*{cap}+ba/b{cap}/b"


def _fetch_subtitles(url: str, output_dir: Path, safe_title: str, ffmpeg_bin: Optional[str]) -> None:
    opts: dict = {
        "skip_download": True,
        "outtmpl": str(output_dir / "%(safe_title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "windowsfilenames": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": SUBTITLE_LANGS,
        "subtitlesformat": "srt/best",
        "sleep_interval_subtitles": 1,
        "retries": 3,
    }
    if ffmpeg_bin:
        opts["ffmpeg_location"] = ffmpeg_bin
        opts["postprocessors"] = [{"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
            info["safe_title"] = safe_title
            ydl.process_ie_result(info, download=True)
    except Exception:
        pass


def download(
    url: str,
    output_dir: str | Path | None = None,
    height: Optional[int] = None,
    audio_only: bool = False,
    on_progress: Optional[ProgressCallback] = None,
    container: str = "mp4",
    audio_format: str = "mp3",
    subtitles: bool = False,
) -> Path:
    url = validate_url(url)
    if container not in VIDEO_CONTAINERS:
        raise DownloaderError(f"Contenedor no soportado: {container}")
    if audio_format not in AUDIO_FORMATS:
        raise DownloaderError(f"Formato de audio no soportado: {audio_format}")
    output_dir = Path(output_dir) if output_dir else default_download_dir()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloaderError(f"No se pudo crear la carpeta de destino: {exc}") from exc

    ffmpeg_bin = ffmpeg_path()
    ffmpeg = ffmpeg_bin is not None
    opts: dict = {
        "format": build_format(height, audio_only, ffmpeg),
        "outtmpl": str(output_dir / "%(safe_title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "windowsfilenames": True,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "socket_timeout": 30,
        "progress_hooks": [on_progress] if on_progress else [],
    }
    postprocessors: list[dict] = []
    if ffmpeg_bin:
        opts["ffmpeg_location"] = ffmpeg_bin

    if audio_only and ffmpeg:
        postprocessors += [
            {"key": "FFmpegExtractAudio", "preferredcodec": audio_format, "preferredquality": "192"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]
        if audio_format in COVER_FORMATS:
            opts["writethumbnail"] = True
            postprocessors.insert(0, {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
            postprocessors.append({"key": "EmbedThumbnail"})
    elif ffmpeg:
        opts["merge_output_format"] = container
        if container == "mp4":
            opts["format_sort"] = ["res", "vcodec:h264", "acodec:m4a"]
        postprocessors.append({"key": "FFmpegVideoRemuxer", "preferedformat": container})

    if postprocessors:
        opts["postprocessors"] = postprocessors

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
            safe_title = safe_filename(info.get("title") or "video")
            info["safe_title"] = safe_title
            result = ydl.process_ie_result(info, download=True)
            downloads = result.get("requested_downloads") or []
            path = Path(downloads[0]["filepath"]) if downloads else Path(ydl.prepare_filename(result))
    except (DownloadError, ExtractorError) as exc:
        raise _translate_error(exc) from exc
    except OSError as exc:
        raise DownloaderError(f"Error de archivo/disco: {exc}") from exc

    if subtitles and not audio_only:
        _fetch_subtitles(url, output_dir, safe_title, ffmpeg_bin)
    return path
