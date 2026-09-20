from __future__ import annotations

import errno
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import netutil
import orphans
import yt_dlp
from lyrics import LYRIC_FORMATS, embed_lyrics, find_lyrics
from orphans import PARTIAL_SUFFIX
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError, PostProcessingError, parse_bytes

ProgressCallback = Callable[[dict], None]

VIDEO_CONTAINERS = ("mp4", "mkv")
AUDIO_FORMATS = ("mp3", "m4a", "aac", "flac", "wav")
COVER_FORMATS = ("mp3", "m4a", "flac")
THUMBNAIL_FORMATS = ("jpg", "webp")
AUDIO_KBPS = {"mp3": 192, "m4a": 192, "aac": 192, "flac": 900, "wav": 1411}
VIDEO_KBPS = {2160: 16000, 1440: 8000, 1080: 4500, 720: 2500, 480: 1200, 360: 700, 240: 400, 144: 200}
RETRY_ATTEMPTS = 3
RETRY_DELAY = 4.0
SUBTITLE_CHOICES = {"es": "Español", "en": "Inglés", "auto": "Automático"}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
ALLOWED_PUNCTUATION = " .()[]&+,'-_!"


class DownloaderError(Exception):
    pass


class DownloadCancelledError(DownloaderError):
    pass


class NetworkDownloadError(DownloaderError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    title: str
    uploader: str
    duration: Optional[int]
    heights: list[int]
    thumbnail_url: Optional[str] = None
    fps_by_height: dict[int, tuple[int, ...]] = field(default_factory=dict)
    video_sizes: dict[int, int] = field(default_factory=dict)
    audio_size: Optional[int] = None

    @property
    def fps_options(self) -> list[int]:
        return sorted({rate for rates in self.fps_by_height.values() for rate in rates}, reverse=True)


@dataclass(frozen=True)
class PlaylistEntry:
    index: int
    video_id: str
    title: str
    duration: Optional[int]
    url: str
    channel: str = ""


@dataclass(frozen=True)
class PlaylistInfo:
    title: str
    uploader: str
    total: int
    skipped: int
    entries: list[PlaylistEntry]
    thumbnail_url: Optional[str] = None
    unavailable: list[PlaylistEntry] = field(default_factory=list)


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
    if "no space left" in msg:
        return DownloaderError("No hay espacio suficiente en el disco de destino.")
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
        return NetworkDownloadError("Error de red. Revisa tu conexión e inténtalo de nuevo.")
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
        with netutil.open_url(request, timeout=10) as response:
            return response.read(3_000_000)
    except Exception:
        return None


class _SafeTitlePP(PostProcessor):
    def __init__(self, output_dir: Optional[Path] = None, prefix: str = "") -> None:
        super().__init__()
        self.output_dir = output_dir
        self.prefix = prefix
        self.base = ""
        self.before: set[str] = set()
        self.token: Optional[str] = None

    def run(self, information):
        title = safe_filename(information.get("title") or "video")
        information["safe_title"] = title
        if self.output_dir is not None and not self.base:
            self.base = _clean_prefix(self.prefix) + title
            self.before = self._matching()
            self.token = orphans.register(self.output_dir, self.base, self.before)
        return [], information

    def finish(self) -> None:
        if self.token:
            orphans.unregister(self.token)
            self.token = None

    def _matching(self) -> set[str]:
        if not self.base or self.output_dir is None:
            return set()
        try:
            return {name for name in os.listdir(self.output_dir) if name.startswith(self.base + ".")}
        except OSError:
            return set()

    def cleanup(self) -> None:
        for name in self._matching():
            partial = bool(PARTIAL_SUFFIX.search(name[len(self.base):]))
            if partial or name not in self.before:
                try:
                    (self.output_dir / name).unlink()
                except OSError:
                    pass
        self.finish()


def _final_entry(result: dict) -> Optional[dict]:
    for candidate in [result, *(result.get("entries") or [])]:
        if (candidate or {}).get("requested_downloads"):
            return candidate
    return None


def _final_path(result: dict) -> Optional[Path]:
    entry = _final_entry(result)
    return Path(entry["requested_downloads"][0]["filepath"]) if entry else None


def _convert_saved_thumbnail(path: Path, target_format: str, ffmpeg_bin: str) -> None:
    source = path.with_suffix(".jpg")
    target = path.with_suffix("." + target_format)
    if not source.exists():
        return
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        done = subprocess.run([ffmpeg_bin, "-y", "-loglevel", "error", "-i", str(source), str(target)],
                              creationflags=flags, capture_output=True)
        if done.returncode == 0 and target.exists():
            source.unlink()
    except OSError:
        pass


def playlist_id(url: str) -> Optional[str]:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if "youtube.com" not in host and "youtu.be" not in host:
        return None
    values = parse_qs(parsed.query).get("list")
    if not values or values[0].startswith("RD"):
        return None
    return values[0]


def url_kind(url: str) -> str:
    if playlist_id(url) is None:
        return "video"
    parsed = urlparse(url.strip())
    is_short_link = parsed.netloc.lower().endswith("youtu.be") and len(parsed.path.strip("/")) > 0
    return "video_in_list" if "v" in parse_qs(parsed.query) or is_short_link else "playlist"


def get_playlist_info(url: str) -> PlaylistInfo:
    url = validate_url(url)
    list_id = playlist_id(url)
    if list_id is None:
        raise DownloaderError("El enlace no corresponde a una lista de reproducción.")
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "logger": _QuietLogger(),
            "proxy": netutil.get_proxy()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/playlist?list={list_id}", download=False)
    except (DownloadError, ExtractorError) as exc:
        raise _translate_error(exc) from exc

    raw = [entry for entry in (info.get("entries") or []) if entry]
    entries: list[PlaylistEntry] = []
    unavailable: list[PlaylistEntry] = []
    skipped = 0
    for position, entry in enumerate(raw, 1):
        video_id = entry.get("id")
        title = entry.get("title") or ""
        if not video_id or title in ("[Private video]", "[Deleted video]"):
            skipped += 1
            missing_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
            unavailable.append(PlaylistEntry(position, video_id or "", title or "Video no disponible", None,
                                             entry.get("url") or missing_url))
            continue
        video_url = entry.get("url") or ""
        if not video_url.startswith("http"):
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        channel = entry.get("channel") or entry.get("uploader") or ""
        entries.append(PlaylistEntry(position, video_id, title or video_id, entry.get("duration"), video_url,
                                     channel))
    if not entries:
        raise DownloaderError("La lista está vacía o es privada.")
    return PlaylistInfo(
        title=info.get("title") or "Lista de reproducción",
        uploader=info.get("uploader") or info.get("channel") or "desconocido",
        total=len(raw),
        skipped=skipped,
        entries=entries,
        thumbnail_url=_pick_thumbnail(info) or _pick_thumbnail(raw[0]),
        unavailable=unavailable,
    )


def _collect_fps(info: dict) -> dict[int, tuple[int, ...]]:
    rates: dict[int, set[int]] = {}
    for item in info.get("formats") or []:
        height, rate = item.get("height"), item.get("fps")
        if height and rate and item.get("vcodec") not in (None, "none"):
            rates.setdefault(int(height), set()).add(int(round(rate)))
    return {height: tuple(sorted(values, reverse=True)) for height, values in rates.items()}


def _item_size(item: dict, duration: Optional[float]) -> Optional[int]:
    size = item.get("filesize") or item.get("filesize_approx")
    if not size and item.get("tbr") and duration:
        size = item["tbr"] * 125 * duration
    return int(size) if size else None


def _collect_sizes(info: dict) -> tuple[dict[int, int], Optional[int]]:
    duration = info.get("duration")
    video: dict[int, int] = {}
    audio: Optional[int] = None
    for item in info.get("formats") or []:
        size = _item_size(item, duration)
        if not size:
            continue
        has_video = item.get("vcodec") not in (None, "none")
        has_audio = item.get("acodec") not in (None, "none")
        if has_video and item.get("height"):
            height = int(item["height"])
            video[height] = max(video.get(height, 0), size)
        elif has_audio and not has_video:
            audio = max(audio or 0, size)
    return video, audio


def estimate_video_size(info: VideoInfo, height: Optional[int], audio_only: bool = False,
                        audio_format: str = "mp3") -> Optional[int]:
    if audio_only:
        if info.duration:
            return int(info.duration * AUDIO_KBPS.get(audio_format, 192) * 125)
        return info.audio_size
    if not info.video_sizes:
        return None
    if height:
        options = [h for h in info.video_sizes if h <= height]
        key = max(options) if options else min(info.video_sizes)
    else:
        key = max(info.video_sizes)
    return info.video_sizes[key] + (info.audio_size or 0)


def estimate_playlist_size(entries: list[PlaylistEntry], height: Optional[int], audio_only: bool = False,
                           audio_format: str = "mp3") -> Optional[int]:
    return estimate_seconds_size(sum(entry.duration or 0 for entry in entries), height, audio_only, audio_format)


def estimate_seconds_size(seconds: float, height: Optional[int], audio_only: bool = False,
                          audio_format: str = "mp3") -> Optional[int]:
    if not seconds:
        return None
    if audio_only:
        kbps = AUDIO_KBPS.get(audio_format, 192)
    else:
        target = height or 1080
        kbps = VIDEO_KBPS[min(VIDEO_KBPS, key=lambda h: abs(h - target))] + 128
    return int(seconds * kbps * 125)


def free_space(path: str | Path) -> Optional[int]:
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return None


def format_size(size: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    text = f"{value:.0f}" if index < 2 else f"{value:.1f}".replace(".", ",")
    return f"{text} {units[index]}"


def get_info(url: str) -> VideoInfo:
    url = validate_url(url)
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": _QuietLogger(),
            "proxy": netutil.get_proxy()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except (DownloadError, ExtractorError) as exc:
        raise _translate_error(exc) from exc

    heights = sorted(
        {f["height"] for f in info.get("formats", []) if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    video_sizes, audio_size = _collect_sizes(info)
    return VideoInfo(
        title=info.get("title", "desconocido"),
        uploader=info.get("uploader", "desconocido"),
        duration=info.get("duration"),
        heights=heights,
        thumbnail_url=_pick_thumbnail(info),
        fps_by_height=_collect_fps(info),
        video_sizes=video_sizes,
        audio_size=audio_size,
    )


def _fps_filter(fps: Optional[int]) -> str:
    return f"[fps>={fps - 1}][fps<={fps + 1}]" if fps else ""


def build_format(height: Optional[int], audio_only: bool, merge: bool, fps: Optional[int] = None) -> str:
    if audio_only:
        return "ba/b"
    cap = f"[height<={height}]" if height else ""
    rate = _fps_filter(fps)
    if not merge:
        return "/".join([f"b{cap}{rate}"] * bool(rate) + [f"b{cap}", "b"])
    return "/".join([f"bv*{cap}{rate}+ba"] * bool(rate) + [f"bv*{cap}+ba", f"b{cap}", "b"])


def _pick_language(code: str, keys: list[str]) -> Optional[str]:
    if code in keys:
        return code
    return next((k for k in keys if k.startswith(code + "-") and not k.endswith("-orig")), None)


def choose_subtitle_langs(choice: str, info: dict) -> list[str]:
    manual = list(info.get("subtitles") or {})
    automatic = list(info.get("automatic_captions") or {})
    if choice in ("es", "en"):
        for keys in (manual, automatic):
            picked = _pick_language(choice, keys)
            if picked:
                return [picked]
        return []
    language = (info.get("language") or "").split("-")[0]
    if language:
        picked = _pick_language(language, manual)
        if picked:
            return [picked]
    original = next((k for k in automatic if k.endswith("-orig")), None)
    return [original] if original else []


def parse_rate_limit(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    value = parse_bytes(text.strip())
    if not value or value <= 0:
        raise DownloaderError(f"Límite de velocidad no válido: {text!r}. Ejemplos: 500K, 2M")
    return value


def _make_hook(on_progress: Optional[ProgressCallback], should_cancel: Optional[Callable[[], bool]]):
    def hook(status: dict) -> None:
        if should_cancel is not None and should_cancel():
            raise DownloadCancelled()
        if on_progress is not None:
            on_progress(status)

    return hook


def _sleep_unless_cancelled(seconds: float, should_cancel: Optional[Callable[[], bool]]) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_cancel is not None and should_cancel():
            return True
        time.sleep(0.2)
    return bool(should_cancel is not None and should_cancel())


def _make_pp_hook(should_cancel: Optional[Callable[[], bool]]):
    def hook(status: dict) -> None:
        if should_cancel is not None and status.get("status") == "started" and should_cancel():
            raise DownloadCancelled()

    return hook


def _clean_prefix(prefix: str) -> str:
    return re.sub(r"[^0-9A-Za-z _.-]", "", prefix)


def _fetch_subtitles(url: str, output_dir: Path, ffmpeg_bin: Optional[str], choice: str,
                     filename_prefix: str = "") -> None:
    opts: dict = {
        "skip_download": True,
        "outtmpl": str(output_dir / f"{_clean_prefix(filename_prefix)}%(safe_title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "windowsfilenames": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [],
        "subtitlesformat": "srt/best",
        "sleep_interval_subtitles": 1,
        "retries": 3,
        "proxy": netutil.get_proxy(),
    }
    if ffmpeg_bin:
        opts["ffmpeg_location"] = ffmpeg_bin
        opts["postprocessors"] = [{"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.add_post_processor(_SafeTitlePP(), when="pre_process")
            info = ydl.extract_info(url, download=False)
            langs = choose_subtitle_langs(choice, info)
            if not langs:
                return
            ydl.params["subtitleslangs"] = langs
            ydl.extract_info(url, download=True)
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
    subtitles: Optional[str] = None,
    rate_limit: Optional[int] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    filename_prefix: str = "",
    fps: Optional[int] = None,
    thumbnail: Optional[str] = None,
    lyrics: bool = False,
    on_retry: Optional[Callable[[int, int], None]] = None,
) -> Path:
    url = validate_url(url)
    if container not in VIDEO_CONTAINERS:
        raise DownloaderError(f"Contenedor no soportado: {container}")
    if audio_format not in AUDIO_FORMATS:
        raise DownloaderError(f"Formato de audio no soportado: {audio_format}")
    if subtitles is not None and subtitles not in SUBTITLE_CHOICES:
        raise DownloaderError(f"Idioma de subtítulos no soportado: {subtitles}")
    if thumbnail is not None and thumbnail not in THUMBNAIL_FORMATS:
        raise DownloaderError(f"Formato de miniatura no soportado: {thumbnail}")
    output_dir = Path(output_dir) if output_dir else default_download_dir()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloaderError(f"No se pudo crear la carpeta de destino: {exc}") from exc

    ffmpeg_bin = ffmpeg_path()
    ffmpeg = ffmpeg_bin is not None
    opts: dict = {
        "format": build_format(height, audio_only, ffmpeg, fps),
        "outtmpl": str(output_dir / f"{_clean_prefix(filename_prefix)}%(safe_title)s.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "windowsfilenames": True,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "socket_timeout": 30,
        "progress_hooks": [_make_hook(on_progress, should_cancel)],
        "postprocessor_hooks": [_make_pp_hook(should_cancel)],
        "ratelimit": rate_limit,
        "proxy": netutil.get_proxy(),
    }
    save_thumbnail = thumbnail in THUMBNAIL_FORMATS
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
            postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": save_thumbnail})
    elif ffmpeg:
        opts["merge_output_format"] = container
        if container == "mp4":
            opts["format_sort"] = ["res", "vcodec:h264", "acodec:m4a"]
        postprocessors.append({"key": "FFmpegVideoRemuxer", "preferedformat": container})

    if save_thumbnail and not opts.get("writethumbnail"):
        opts["writethumbnail"] = True
        if ffmpeg:
            postprocessors.insert(0, {"key": "FFmpegThumbnailsConvertor", "format": thumbnail, "when": "before_dl"})

    if postprocessors:
        opts["postprocessors"] = postprocessors

    path = entry = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        title_pp = _SafeTitlePP(output_dir, filename_prefix)
        try:
            with yt_dlp.YoutubeDL(dict(opts)) as ydl:
                ydl.add_post_processor(title_pp, when="pre_process")
                result = ydl.extract_info(url, download=True)
                path = _final_path(result)
                entry = _final_entry(result)
            title_pp.finish()
            break
        except DownloadCancelled as exc:
            title_pp.cleanup()
            raise DownloadCancelledError("Descarga cancelada.") from exc
        except (DownloadError, ExtractorError, PostProcessingError) as exc:
            error = _translate_error(exc)
            if isinstance(error, NetworkDownloadError) and attempt < RETRY_ATTEMPTS:
                title_pp.finish()
                if on_retry is not None:
                    on_retry(attempt + 1, RETRY_ATTEMPTS)
                if _sleep_unless_cancelled(RETRY_DELAY, should_cancel):
                    title_pp.cleanup()
                    raise DownloadCancelledError("Descarga cancelada.") from exc
                continue
            title_pp.cleanup()
            raise error from exc
        except OSError as exc:
            title_pp.cleanup()
            if exc.errno == errno.ENOSPC:
                raise DownloaderError("No hay espacio suficiente en el disco de destino.") from exc
            raise DownloaderError(f"Error de archivo/disco: {exc}") from exc

    if path is None:
        raise DownloaderError("No se pudo determinar el archivo descargado.")
    if save_thumbnail and thumbnail != "jpg" and audio_only and ffmpeg and audio_format in COVER_FORMATS:
        _convert_saved_thumbnail(path, thumbnail, ffmpeg_bin)
    if lyrics and audio_only and audio_format in LYRIC_FORMATS and entry is not None:
        try:
            found = find_lyrics(entry)
            if found:
                embed_lyrics(path, found)
        except Exception:
            pass
    if subtitles and not audio_only:
        _fetch_subtitles(url, output_dir, ffmpeg_bin, subtitles, filename_prefix)
    return path
