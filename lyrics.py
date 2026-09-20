from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import netutil

LRCLIB_SEARCH = "https://lrclib.net/api/search"
LYRIC_FORMATS = ("mp3", "m4a", "flac")
USER_AGENT = "LLorostini (https://github.com/anime1234rr/Llorostini)"
MAX_DURATION_GAP = 5.0

NOISE = re.compile(
    r"[\(\[\{][^\)\]\}]*(official|video|audio|lyrics?|letra|remaster|hd|4k|hq|mv|clip|visualizer|live)[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)
FEATURING = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
LRC_LINE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s?(.*)$")
TOPIC_SUFFIX = re.compile(r"\s*(-\s*Topic|VEVO)$", re.IGNORECASE)


@dataclass(frozen=True)
class Lyrics:
    plain: str
    synced: tuple[tuple[int, str], ...]

    @property
    def lrc(self) -> str:
        if not self.synced:
            return self.plain
        lines = []
        for millis, text in self.synced:
            minutes, rest = divmod(millis, 60_000)
            lines.append(f"[{minutes:02d}:{rest / 1000:05.2f}] {text}")
        return "\n".join(lines)


def clean(text: str) -> str:
    text = NOISE.sub(" ", text or "")
    text = FEATURING.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -_|")


def guess_track(info: dict) -> tuple[str, str]:
    track = (info.get("track") or "").strip()
    artist = (info.get("artist") or info.get("creator") or "").strip()
    if artist and "," in artist:
        artist = artist.split(",")[0].strip()
    if track and artist:
        return artist, clean(track)
    title = clean(info.get("title") or "")
    if " - " in title:
        left, right = title.split(" - ", 1)
        return clean(left), clean(right)
    uploader = TOPIC_SUFFIX.sub("", (info.get("uploader") or info.get("channel") or "")).strip()
    return uploader, title


def parse_synced(text: str) -> tuple[tuple[int, str], ...]:
    lines = []
    for raw in (text or "").splitlines():
        match = LRC_LINE.match(raw.strip())
        if match:
            millis = int((int(match.group(1)) * 60 + float(match.group(2))) * 1000)
            lines.append((millis, match.group(3)))
    return tuple(lines)


def _similar(wanted: str, found: str) -> bool:
    wanted_words = set(re.findall(r"\w+", wanted.lower()))
    found_words = set(re.findall(r"\w+", found.lower()))
    return bool(wanted_words) and len(wanted_words & found_words) / len(wanted_words) >= 0.5


def find_lyrics(info: dict) -> Optional[Lyrics]:
    artist, track = guess_track(info)
    if not track:
        return None
    duration = info.get("duration")
    query = urllib.parse.urlencode({"q": f"{artist} {track}".strip()})
    request = urllib.request.Request(f"{LRCLIB_SEARCH}?{query}", headers={"User-Agent": USER_AGENT})
    try:
        with netutil.open_url(request, timeout=12) as response:
            results = json.load(response)
    except Exception:
        return None
    if not isinstance(results, list):
        return None

    candidates = []
    for item in results:
        if not (item.get("syncedLyrics") or item.get("plainLyrics")):
            continue
        length = item.get("duration")
        gap = abs(float(length) - float(duration)) if length and duration else 0.0
        if gap > MAX_DURATION_GAP or not _similar(track, item.get("trackName") or ""):
            continue
        candidates.append((bool(item.get("syncedLyrics")), -gap, item))
    if not candidates:
        return None

    best = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]
    synced = parse_synced(best.get("syncedLyrics") or "")
    plain = best.get("plainLyrics") or "\n".join(text for _, text in synced)
    return Lyrics(plain=plain, synced=synced)


def _embed_mp3(path: Path, lyrics: Lyrics) -> None:
    from mutagen.id3 import ID3, SYLT, USLT, Encoding, ID3NoHeaderError

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("USLT")
    tags.delall("SYLT")
    tags.add(USLT(encoding=Encoding.UTF8, lang="XXX", desc="", text=lyrics.plain))
    if lyrics.synced:
        tags.add(SYLT(encoding=Encoding.UTF8, lang="XXX", format=2, type=1, desc="",
                      text=[(line, millis) for millis, line in lyrics.synced]))
    tags.save(str(path))


def _embed_m4a(path: Path, lyrics: Lyrics) -> None:
    from mutagen.mp4 import MP4

    audio = MP4(str(path))
    audio["\xa9lyr"] = [lyrics.plain]
    audio.save()


def _embed_flac(path: Path, lyrics: Lyrics) -> None:
    from mutagen.flac import FLAC

    audio = FLAC(str(path))
    audio["LYRICS"] = [lyrics.lrc]
    audio.save()


def embed_lyrics(path: Path, lyrics: Lyrics) -> bool:
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            _embed_mp3(path, lyrics)
        elif suffix in (".m4a", ".mp4"):
            _embed_m4a(path, lyrics)
        elif suffix == ".flac":
            _embed_flac(path, lyrics)
        else:
            return False
    except Exception:
        return False
    return True
