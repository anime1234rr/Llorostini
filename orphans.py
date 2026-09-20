from __future__ import annotations

import os
import re
import threading
import uuid
from pathlib import Path

import storage

PARTIAL_SUFFIX = re.compile(r"\.(part(-Frag\d+)?|ytdl|temp)(\.|$)|\.f\d+(-\w+)?\.")
REGISTRY = "pending.json"
STILL_ACTIVE = 259

_lock = threading.Lock()


def _load() -> list[dict]:
    data = storage.read_json(REGISTRY, [])
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def register(output_dir: Path, base: str, before: set[str]) -> str:
    token = uuid.uuid4().hex
    with _lock:
        entries = _load()
        entries.append({"token": token, "pid": os.getpid(), "dir": str(output_dir), "base": base,
                        "before": sorted(before)})
        storage.write_json(REGISTRY, entries)
    return token


def unregister(token: str) -> None:
    with _lock:
        entries = [entry for entry in _load() if entry.get("token") != token]
        storage.write_json(REGISTRY, entries)


def pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def _remove_leftovers(entry: dict) -> int:
    folder = Path(str(entry.get("dir", "")))
    base = str(entry.get("base", ""))
    before = set(entry.get("before", []))
    if not base or not folder.is_dir():
        return 0
    removed = 0
    try:
        names = [name for name in os.listdir(folder) if name.startswith(base + ".")]
    except OSError:
        return 0
    for name in names:
        partial = bool(PARTIAL_SUFFIX.search(name[len(base):]))
        if partial or name not in before:
            try:
                (folder / name).unlink()
                removed += 1
            except OSError:
                pass
    return removed


def sweep() -> int:
    removed = 0
    with _lock:
        entries = _load()
        keep = []
        for entry in entries:
            pid = entry.get("pid")
            if isinstance(pid, int) and pid_alive(pid):
                keep.append(entry)
                continue
            removed += _remove_leftovers(entry)
        if len(keep) != len(entries):
            storage.write_json(REGISTRY, keep)
    return removed
