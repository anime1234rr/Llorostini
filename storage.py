from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

MAX_HISTORY = 100

DEFAULT_SETTINGS = {
    "speed_limit": "Sin límite",
    "minimize_to_tray": False,
    "subtitle_lang": "es",
    "save_thumbnail": False,
    "thumbnail_format": "jpg",
    "embed_lyrics": False,
    "by_channel": False,
    "proxy": "",
    "theme": "Oscuro",
    "parallel_downloads": 1,
}


def app_dir() -> Path:
    base = os.environ.get("APPDATA")
    folder = Path(base) / "LLorostini" if base else Path.home() / ".llorostini"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return folder


def _read(name: str, default):
    try:
        with open(app_dir() / name, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write(name: str, data) -> None:
    try:
        with open(app_dir() / name, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def read_json(name: str, default):
    return _read(name, default)


def write_json(name: str, data) -> None:
    _write(name, data)


def load_settings() -> dict:
    data = _read("settings.json", {})
    return {**DEFAULT_SETTINGS, **(data if isinstance(data, dict) else {})}


def save_settings(settings: dict) -> None:
    _write("settings.json", settings)


def _key(entry: dict) -> str:
    return entry.get("path") or f"{entry.get('url', '')}|{entry.get('date', '')}"


def load_history() -> list[dict]:
    data = _read("history.json", [])
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and (e.get("path") or e.get("url"))]


def add_history(path: str | Path, kind: str) -> list[dict]:
    path = str(path)
    entries = [e for e in load_history() if e.get("path") != path]
    entries.insert(0, {
        "title": Path(path).stem,
        "path": path,
        "kind": kind,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    entries = entries[:MAX_HISTORY]
    _write("history.json", entries)
    return entries


def add_failure(url: str, title: str, reason: str) -> list[dict]:
    entries = load_history()
    entries.insert(0, {
        "title": title or url,
        "url": url,
        "path": "",
        "kind": "error",
        "reason": reason,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    entries = entries[:MAX_HISTORY]
    _write("history.json", entries)
    return entries


def remove_history(target) -> list[dict]:
    key = target if isinstance(target, str) else _key(target)
    entries = [e for e in load_history() if _key(e) != key]
    _write("history.json", entries)
    return entries


def clear_history() -> None:
    _write("history.json", [])
