from __future__ import annotations

from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

AVAILABLE = pystray is not None


def _icon_image():
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(91, 140, 255, 255))
    draw.polygon([(32, 46), (17, 30), (26, 30), (26, 14), (38, 14), (38, 30), (47, 30)], fill="white")
    draw.rectangle((16, 51, 48, 55), fill="white")
    return image


class TrayIcon:
    def __init__(self, on_show: Callable[[], None], on_quit: Callable[[], None]) -> None:
        self._on_show = on_show
        self._on_quit = on_quit
        self._icon = None

    @property
    def active(self) -> bool:
        return self._icon is not None

    def start(self, tooltip: str) -> None:
        if not AVAILABLE or self._icon is not None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Mostrar LLorostini", lambda *_: self._on_show(), default=True),
            pystray.MenuItem("Salir", lambda *_: self._on_quit()),
        )
        try:
            self._icon = pystray.Icon("LLorostini", _icon_image(), tooltip[:127], menu)
            self._icon.run_detached()
        except Exception:
            self._icon = None

    def set_tooltip(self, text: str) -> None:
        if self._icon is not None:
            try:
                self._icon.title = text[:127]
            except Exception:
                pass

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None:
            try:
                self._icon.notify(message[:255], title[:63])
            except Exception:
                pass

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
