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


def load_settings() -> dict:
    data = _read("settings.json", {})
    return {**DEFAULT_SETTINGS, **(data if isinstance(data, dict) else {})}


def save_settings(settings: dict) -> None:
    _write("settings.json", settings)


def load_history() -> list[dict]:
    data = _read("history.json", [])
    return [e for e in data if isinstance(e, dict) and e.get("path")] if isinstance(data, list) else []


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


def remove_history(path: str) -> list[dict]:
    entries = [e for e in load_history() if e.get("path") != path]
    _write("history.json", entries)
    return entries


def clear_history() -> None:
    _write("history.json", [])
