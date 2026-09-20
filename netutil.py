from __future__ import annotations

import urllib.request
from typing import Optional
from urllib.parse import urlparse

PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")
PROXY_ERROR = "El proxy no es válido. Usa por ejemplo http://127.0.0.1:8080 o socks5://127.0.0.1:1080."

_proxy: Optional[str] = None
_opener = urllib.request.build_opener()


def normalize_proxy(text: Optional[str]) -> Optional[str]:
    value = (text or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    if parsed.scheme.lower() not in PROXY_SCHEMES or not parsed.hostname:
        raise ValueError(PROXY_ERROR)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(PROXY_ERROR) from exc
    return value


def set_proxy(text: Optional[str]) -> Optional[str]:
    global _proxy, _opener
    value = normalize_proxy(text)
    _proxy = value
    if value and urlparse(value).scheme.lower() in ("http", "https"):
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": value, "https": value}))
    else:
        _opener = urllib.request.build_opener()
    return value


def get_proxy() -> Optional[str]:
    return _proxy


def open_url(request, timeout: float = 20):
    return _opener.open(request, timeout=timeout)
