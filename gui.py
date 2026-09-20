from __future__ import annotations

import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

import storage
import netutil
import orphans
import tray
from downloader import (AUDIO_FORMATS, SUBTITLE_CHOICES, VIDEO_CONTAINERS, DownloadCancelledError, DownloaderError,
                        default_download_dir, download, fetch_thumbnail, get_info, get_playlist_info, has_ffmpeg,
                        parse_rate_limit, safe_filename, url_kind, THUMBNAIL_FORMATS, estimate_playlist_size,
                        estimate_seconds_size, estimate_video_size, format_size, free_space)
from updater import UpdateError, apply_update, can_self_update, download_update, fetch_latest_release, is_newer
from version import APP_NAME, GITHUB_REPO, __version__

try:
    from tkinterdnd2 import DND_ALL, TkinterDnD
    _Base = TkinterDnD.Tk
except ImportError:
    DND_ALL = None
    _Base = tk.Tk

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

BEST = "Mejor disponible"
THUMB_W, THUMB_H = 160, 90
URL_PATTERN = re.compile(r"https?://[^\s{}\"'<>]+")

SPEEDS = {"Sin límite": None, "500 KB/s": "500K", "1 MB/s": "1M", "2 MB/s": "2M", "5 MB/s": "5M", "10 MB/s": "10M"}
QUEUE_QUALITIES = [BEST, "2160p", "1440p", "1080p", "720p", "480p", "360p"]
FPS_AUTO = "Automático"
FPS_PRESETS = [60, 30, 24]
PARALLEL_CHOICES = ("1", "2", "3")
FINISHED_STATES = ("Listo", "Omitido")
SPACE_MARGIN = 1.15
MIN_FREE_BYTES = 500 * 1024 * 1024
DEFAULT_ITEM_SECONDS = 300

COLOR_KEYS = ("BG", "SURFACE", "FIELD", "BORDER", "HOVER", "FG", "MUTED", "ACCENT", "ACCENT_HOVER", "ACCENT_TEXT",
              "GREEN", "RED")
COLOR_OPTIONS = ("bg", "background", "fg", "foreground", "highlightbackground", "highlightcolor", "insertbackground",
                 "selectbackground", "selectforeground", "activebackground", "activeforeground", "troughcolor")

PALETTES = {
    "Oscuro": dict(BG="#14161b", SURFACE="#1e2128", FIELD="#252932", BORDER="#323744", HOVER="#2d323d",
                   FG="#e8eaf0", MUTED="#8b93a5", ACCENT="#5b8cff", ACCENT_HOVER="#7aa1ff", ACCENT_TEXT="#ffffff",
                   GREEN="#3ddc84", RED="#ff6b6b", DARK=True),
    "Claro": dict(BG="#f3f4f7", SURFACE="#e6e9ef", FIELD="#fdfdfe", BORDER="#a9b2c1", HOVER="#d9dde5",
                  FG="#1b1e27", MUTED="#5f6675", ACCENT="#2f63e0", ACCENT_HOVER="#4a7aee", ACCENT_TEXT="#ffffff",
                  GREEN="#188a4a", RED="#c93838", DARK=False),
    "Cyberpunk": dict(BG="#0a0512", SURFACE="#170a2b", FIELD="#1f1040", BORDER="#42217c", HOVER="#2b1660",
                      FG="#f0eaff", MUTED="#a38fd6", ACCENT="#e01fbd", ACCENT_HOVER="#ff4fd8", ACCENT_TEXT="#ffffff",
                      GREEN="#00f0a0", RED="#ff4d6d", DARK=True),
    "Minimalista": dict(BG="#111111", SURFACE="#191919", FIELD="#1f1f1f", BORDER="#2e2e2e", HOVER="#262626",
                        FG="#f0f0f0", MUTED="#8c8c8c", ACCENT="#e6e6e6", ACCENT_HOVER="#ffffff", ACCENT_TEXT="#0d0d0d",
                        GREEN="#7fd3a0", RED="#e07a7a", DARK=True),
}
DEFAULT_THEME = "Oscuro"
ACTIVE: dict = {}
ACTIVE_THEME = DEFAULT_THEME


def use_palette(name: str) -> None:
    global ACTIVE_THEME
    if name not in PALETTES:
        name = DEFAULT_THEME
    ACTIVE_THEME = name
    ACTIVE.clear()
    ACTIVE.update(PALETTES[name])
    globals().update(PALETTES[name])


use_palette(DEFAULT_THEME)
FONT = ("Segoe UI", 10)


def shorten(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plain_notes(text: str) -> str:
    lines = []
    for line in str(text).replace("\r", "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("# ").strip()
        elif stripped.startswith(("- ", "* ")):
            stripped = "• " + stripped[2:]
        lines.append(stripped.replace("**", "").replace("`", ""))
    return "\n".join(lines).strip()


def find_urls(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for url in URL_PATTERN.findall(str(text)):
        seen.setdefault(url.rstrip(".,;)"), None)
    return list(seen)


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def youtube_url(text: str) -> str | None:
    for url in find_urls(text):
        host = urlparse(url).netloc.lower()
        if host == "youtu.be" or host.endswith(".youtu.be") or "youtube.com" in host:
            return url
    return None


def read_queue_file(path: Path) -> tuple[list[dict], str | None, str | None]:
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    if path.suffix.lower() == ".json" or text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, (dict, list)):
            raw = data.get("items", []) if isinstance(data, dict) else data
            items = []
            for entry in raw if isinstance(raw, list) else []:
                record = entry if isinstance(entry, dict) else {"url": entry}
                found = find_urls(str(record.get("url") or ""))
                if not found:
                    continue
                title = record.get("title")
                item = {"url": found[0], "title": title if isinstance(title, str) else None}
                for key in ("out_dir", "prefix"):
                    if isinstance(record.get(key), str) and record[key]:
                        item[key] = record[key]
                duration = record.get("duration")
                if isinstance(duration, (int, float)) and duration > 0:
                    item["duration"] = int(duration)
                items.append(item)
            quality = data.get("quality") if isinstance(data, dict) else None
            fps = data.get("fps") if isinstance(data, dict) else None
            return items, quality, fps
    return [{"url": url, "title": None} for url in find_urls(text)], None, None


class App(_Base):
    def __init__(self) -> None:
        super().__init__()
        use_palette(str(storage.load_settings().get("theme", DEFAULT_THEME)))
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("700x730")
        self.minsize(660, 690)
        self.configure(bg=BG)
        self._titlebar_state: dict[str, bool] = {}
        self._dark_titlebar(self)

        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.tray = tray.TrayIcon(lambda: self.events.put(("tray_show", None)),
                                  lambda: self.events.put(("tray_quit", None)))
        self.settings = storage.load_settings()
        self.heights: list[int] = []
        self.last_path: Path | None = None
        self.busy = False
        self._thumb_img = None
        self.job_mode = "single"
        self.job_total = 1
        self.job_prefix = ""
        self.results = {"ok": 0, "fail": 0}
        self.last_error = ""
        self._last_tray_percent = -1
        self.item_state: dict[str, str] = {}
        self.item_url: dict[str, str] = {}
        self.history_entries: list[dict] = []
        self._clip_url = ""
        self._clip_dismissed = ""
        self.ready = False
        self.video_info = None
        self.playlist = None
        self.thumb_video = None
        self.thumb_list = None
        self.item_extra: dict[str, dict] = {}
        self.skipped_playlists = 0
        self.latest_release = None
        self.update_available = False
        self.jobs_done = 0
        self.job_frac: dict[int, float] = {}
        self.job_speed: dict[int, float] = {}
        self.parallel_active = False
        self.failures: list[tuple[str, str]] = []

        self.output_dir = tk.StringVar(value=str(default_download_dir()))
        self.url = tk.StringVar()
        self.audio_only = tk.BooleanVar()
        self.subtitles = tk.BooleanVar()
        self.sub_lang = tk.StringVar(value=SUBTITLE_CHOICES.get(self.settings["subtitle_lang"], "Español"))
        self.container = tk.StringVar(value=VIDEO_CONTAINERS[0])
        self.audio_format = tk.StringVar(value=AUDIO_FORMATS[0])
        speed = self.settings["speed_limit"] if self.settings["speed_limit"] in SPEEDS else "Sin límite"
        self.speed = tk.StringVar(value=speed)
        self.to_tray = tk.BooleanVar(value=bool(self.settings["minimize_to_tray"]) and tray.AVAILABLE)
        self.queue_quality = tk.StringVar(value=BEST)
        self.pl_from = tk.StringVar(value="1")
        self.history_filter = tk.StringVar()
        self.pl_to = tk.StringVar(value="1")
        self.only_video = tk.BooleanVar(value=True)
        self.fps_choice = tk.StringVar(value=FPS_AUTO)
        self.queue_fps = tk.StringVar(value=FPS_AUTO)
        self._fps_locked = False
        self.save_thumb = tk.BooleanVar(value=bool(self.settings["save_thumbnail"]))
        thumb_format = self.settings["thumbnail_format"]
        self.thumb_format = tk.StringVar(value=thumb_format if thumb_format in THUMBNAIL_FORMATS
                                         else THUMBNAIL_FORMATS[0])
        self.embed_lyrics = tk.BooleanVar(value=bool(self.settings["embed_lyrics"]))
        self.by_channel = tk.BooleanVar(value=bool(self.settings["by_channel"]))
        self.proxy = tk.StringVar(value=str(self.settings["proxy"]))
        self.theme = tk.StringVar(value=ACTIVE_THEME)
        parallel = str(self.settings.get("parallel_downloads", 1))
        self.parallel = tk.StringVar(value=parallel if parallel in PARALLEL_CHOICES else PARALLEL_CHOICES[0])
        try:
            netutil.set_proxy(self.proxy.get())
        except ValueError:
            pass

        self._apply_theme()
        self._build()
        self._build_menu()
        self._enable_drop()
        for var in (self.speed, self.to_tray, self.sub_lang, self.save_thumb, self.thumb_format, self.embed_lyrics,
                    self.by_channel, self.proxy, self.parallel):
            var.trace_add("write", lambda *_: self._save_settings())
        for var in (self.output_dir, self.pl_from, self.pl_to, self.audio_format):
            var.trace_add("write", lambda *_: self._refresh_space())
        self._bind_shortcuts()
        self._refresh_space()
        self.history_filter.trace_add("write", lambda *_: self._refresh_history())
        self.url.trace_add("write", lambda *_: self._on_url_typed())
        self.bind("<FocusIn>", self._on_focus_in)
        self.after(700, self._check_clipboard)
        self.after(900, self._sweep_orphans)
        self.after(100, self._poll)
        self.after(1500, lambda: self.check_updates(silent=True))

    def on_theme_change(self) -> None:
        name = self.theme.get()
        if name not in PALETTES or name == ACTIVE_THEME:
            return
        old = dict(ACTIVE)
        use_palette(name)
        mapping = {str(old[key]).lower(): ACTIVE[key] for key in COLOR_KEYS
                   if str(old[key]).lower() != str(ACTIVE[key]).lower()}
        self._apply_theme()
        self._recolor(self, mapping)
        self._configure_tree_tags()
        self._update_popdowns()
        self._save_settings()
        self._offer_restart_for_titlebar()

    def _recolor(self, widget, mapping: dict) -> None:
        for option in COLOR_OPTIONS:
            try:
                current = str(widget.cget(option)).lower()
            except tk.TclError:
                continue
            new = mapping.get(current)
            if new:
                try:
                    widget.configure(**{option: new})
                except tk.TclError:
                    pass
        for child in widget.winfo_children():
            self._recolor(child, mapping)

    def _configure_tree_tags(self) -> None:
        self.queue_tree.tag_configure("ok", foreground=GREEN)
        self.queue_tree.tag_configure("err", foreground=RED)
        self.queue_tree.tag_configure("run", foreground=ACCENT_HOVER)
        self.history_tree.tag_configure("err", foreground=RED)

    def _update_popdowns(self) -> None:
        def walk(widget) -> None:
            for child in widget.winfo_children():
                if isinstance(child, ttk.Combobox):
                    try:
                        popdown = str(self.tk.call("ttk::combobox::PopdownWindow", str(child)))
                        self.tk.call(popdown + ".f.l", "configure", "-background", FIELD, "-foreground", FG,
                                     "-selectbackground", ACCENT, "-selectforeground", ACCENT_TEXT)
                    except tk.TclError:
                        pass
                walk(child)

        walk(self)

    def _apply_theme(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, font=FONT, focuscolor=BG)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("TButton", background=SURFACE, foreground=FG, padding=(14, 7), borderwidth=1)
        style.map("TButton", background=[("active", HOVER), ("disabled", BG)],
                  foreground=[("disabled", MUTED)], bordercolor=[("focus", ACCENT)])
        style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_TEXT, bordercolor=ACCENT, padding=(14, 9))
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", SURFACE)],
                  foreground=[("disabled", MUTED)], bordercolor=[("disabled", BORDER)])
        style.configure("Small.TButton", background=SURFACE, foreground=FG, padding=(9, 3), borderwidth=1)
        style.map("Small.TButton", background=[("active", HOVER), ("disabled", BG)],
                  foreground=[("disabled", MUTED)], bordercolor=[("focus", ACCENT)])
        style.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, padding=6)
        style.map("TEntry", bordercolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=FIELD, background=SURFACE, foreground=FG,
                        arrowcolor=FG, padding=5)
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD), ("disabled", BG)],
                  foreground=[("disabled", MUTED)],
                  selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", FG)],
                  bordercolor=[("focus", ACCENT)])
        style.configure("TCheckbutton", background=BG, foreground=FG, indicatorbackground=FIELD,
                        indicatorforeground=FG)
        style.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", ACCENT)],
                  foreground=[("disabled", MUTED)])
        style.configure("Horizontal.TProgressbar", background=GREEN, troughcolor=FIELD, bordercolor=BG,
                        lightcolor=GREEN, darkcolor=GREEN, thickness=10)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED, padding=(20, 9), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", FIELD)], foreground=[("selected", FG)])
        style.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG, bordercolor=BORDER,
                        rowheight=28, borderwidth=0)
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", ACCENT_TEXT)])
        style.configure("Treeview.Heading", background=SURFACE, foreground=MUTED, relief="flat", padding=7)
        style.map("Treeview.Heading", background=[("active", HOVER)])
        for scrollbar in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(scrollbar, background=SURFACE, troughcolor=BG, bordercolor=BORDER, arrowcolor=MUTED,
                            lightcolor=SURFACE, darkcolor=SURFACE)
            style.map(scrollbar, background=[("disabled", BG), ("pressed", HOVER), ("active", HOVER)],
                      arrowcolor=[("disabled", BORDER)])
        style.configure("TSpinbox", fieldbackground=FIELD, background=SURFACE, foreground=FG, arrowcolor=FG,
                        insertcolor=FG, bordercolor=BORDER, padding=4)
        style.map("TSpinbox", bordercolor=[("focus", ACCENT)])
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", ACCENT_TEXT)
        self.option_add("*TCombobox*Listbox.font", FONT)

    def _dark_titlebar(self, window) -> None:
        if os.name != "nt" or not DARK:
            return
        key = str(window)
        if self._titlebar_state.get(key):
            return
        try:
            import ctypes
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
            self._titlebar_state[key] = True
        except Exception:
            pass

    def _offer_restart_for_titlebar(self) -> None:
        if os.name != "nt" or bool(DARK) == self._titlebar_state.get(str(self), False):
            return
        if messagebox.askyesno("Cambio de tema",
                               "El tema ya está aplicado, pero la barra de título de la ventana solo cambia al "
                               "reiniciar LLorostini.\n\n¿Reiniciar ahora?"):
            self._restart()

    def _restart(self) -> None:
        frozen = bool(getattr(sys, "frozen", False))
        command = [sys.executable] if frozen else [sys.executable, os.path.abspath(sys.argv[0])]
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            subprocess.Popen(command, env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}, cwd=os.getcwd(),
                             creationflags=flags, close_fds=True)
        except OSError as exc:
            messagebox.showerror("Error", f"No se pudo reiniciar la aplicación: {exc}")
            return
        self.tray.stop()
        self.destroy()

    def _build_menu(self) -> None:
        menubar = tk.Menu(self, bg=SURFACE, fg=FG, activebackground=ACCENT, activeforeground=ACCENT_TEXT, borderwidth=0)
        help_menu = tk.Menu(menubar, tearoff=False, bg=SURFACE, fg=FG, activebackground=ACCENT,
                            activeforeground=ACCENT_TEXT, borderwidth=0)
        help_menu.add_command(label="Buscar actualizaciones...", command=self._open_update_tab)
        help_menu.add_separator()
        help_menu.add_command(label=f"Versión {__version__}", state="disabled")
        menubar.add_cascade(label="Ayuda", menu=help_menu)
        self.config(menu=menubar)

    def _build(self) -> None:
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=12, pady=10)
        self.root_frame = root
        self._build_bottom(root)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        self.tab_download = ttk.Frame(self.notebook)
        self.tab_queue = ttk.Frame(self.notebook)
        self.tab_history = ttk.Frame(self.notebook)
        self.tab_update = ttk.Frame(self.notebook)
        self.tab_settings = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_download, text="Descargar")
        self.notebook.add(self.tab_queue, text="Cola")
        self.notebook.add(self.tab_history, text="Historial")
        self.notebook.add(self.tab_update, text="Actualizaciones")
        self.notebook.add(self.tab_settings, text="Ajustes")
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab_changed())

        self._build_download_tab(self.tab_download)
        self._build_queue_tab(self.tab_queue)
        self._build_history_tab(self.tab_history)
        self._build_update_tab(self.tab_update)
        self._build_settings_tab(self.tab_settings)

        if not has_ffmpeg():
            self._say("Aviso: ffmpeg no encontrado; calidad limitada y sin conversión de audio.", RED)

    def _build_download_tab(self, parent) -> None:
        pad = {"padx": 12, "pady": 7}
        frm = ttk.Frame(parent)
        frm.pack(fill="both", expand=True, padx=6, pady=6)
        frm.columnconfigure(1, weight=1)
        self.frm = frm

        ttk.Label(frm, text="URL").grid(row=0, column=0, sticky="w", **pad)
        self.entry = ttk.Entry(frm, textvariable=self.url)
        self.entry.grid(row=0, column=1, sticky="ew", **pad)
        self.entry.bind("<Return>", lambda _e: self.on_search())
        self.entry.focus()
        self.search_btn = ttk.Button(frm, text="Buscar", command=self.on_search)
        self.search_btn.grid(row=0, column=2, sticky="ew", **pad)

        info_row = ttk.Frame(frm)
        info_row.grid(row=1, column=0, columnspan=3, sticky="ew", **pad)
        info_row.columnconfigure(1, weight=1)
        info_row.rowconfigure(0, minsize=THUMB_H)
        self.thumb_box = tk.Frame(info_row, width=THUMB_W, height=THUMB_H, bg=FIELD)
        self.thumb_box.grid(row=0, column=0, sticky="w")
        self.thumb_lbl = tk.Label(self.thumb_box, bg=FIELD)
        self.thumb_lbl.place(relx=0.5, rely=0.5, anchor="center")
        self.thumb_box.grid_remove()
        self.info_col = ttk.Frame(info_row)
        self.info_col.grid(row=0, column=1, sticky="w")
        self.info_lbl = ttk.Label(self.info_col, text="Pega o arrastra un enlace y pulsa Buscar.",
                                  style="Muted.TLabel", wraplength=380, justify="left")
        self.info_lbl.pack(anchor="w")
        self.clip_bar = ttk.Frame(self.info_col)
        self.clip_label = ttk.Label(self.clip_bar, text="", style="Muted.TLabel")
        self.clip_label.pack(side="left")
        ttk.Button(self.clip_bar, text="Usar", style="Small.TButton", command=self._use_clipboard_url).pack(
            side="left", padx=(10, 4))
        ttk.Button(self.clip_bar, text="✕", style="Small.TButton", width=3, command=self._dismiss_clipboard).pack(
            side="left")
        self.pl_box = ttk.Frame(self.info_col)
        self.range_row = ttk.Frame(self.pl_box)
        ttk.Label(self.range_row, text="Videos del").pack(side="left")
        ttk.Spinbox(self.range_row, from_=1, to=9999, width=5, textvariable=self.pl_from).pack(side="left", padx=6)
        ttk.Label(self.range_row, text="al").pack(side="left")
        ttk.Spinbox(self.range_row, from_=1, to=9999, width=5, textvariable=self.pl_to).pack(side="left", padx=6)
        self.pl_total_lbl = ttk.Label(self.range_row, text="", style="Muted.TLabel")
        self.pl_total_lbl.pack(side="left", padx=4)
        self.only_video_chk = ttk.Checkbutton(self.pl_box, text="Solo este video (ignorar la lista)",
                                              variable=self.only_video, command=self._apply_mode)

        ttk.Label(frm, text="Calidad").grid(row=2, column=0, sticky="w", **pad)
        quality_box = ttk.Frame(frm)
        quality_box.grid(row=2, column=1, sticky="ew", **pad)
        quality_box.columnconfigure(0, weight=1)
        self.quality = ttk.Combobox(quality_box, state="disabled", values=[BEST])
        self.quality.set(BEST)
        self.quality.grid(row=0, column=0, sticky="ew")
        self.quality.bind("<<ComboboxSelected>>", lambda _e: (self._refresh_fps_options(), self._refresh_space()))
        ttk.Label(quality_box, text="FPS").grid(row=0, column=1, padx=(12, 6))
        self.fps_box = ttk.Combobox(quality_box, state="disabled", width=12, textvariable=self.fps_choice,
                                    values=[FPS_AUTO])
        self.fps_box.grid(row=0, column=2)
        ttk.Checkbutton(frm, text="Solo audio", variable=self.audio_only,
                        command=self._toggle_audio).grid(row=2, column=2, sticky="w", **pad)

        ttk.Label(frm, text="Formato").grid(row=3, column=0, sticky="w", **pad)
        self.fmt = ttk.Combobox(frm, state="readonly", textvariable=self.container, values=VIDEO_CONTAINERS)
        self.fmt.grid(row=3, column=1, sticky="ew", **pad)
        subs_box = ttk.Frame(frm)
        subs_box.grid(row=3, column=2, sticky="w", **pad)
        self.subs_chk = ttk.Checkbutton(subs_box, text="Subtítulos", variable=self.subtitles,
                                        command=self._refresh_subs_widget)
        self.subs_chk.pack(side="left")
        self.sub_lang_box = ttk.Combobox(subs_box, width=10, state="readonly", textvariable=self.sub_lang,
                                         values=list(SUBTITLE_CHOICES.values()))

        ttk.Label(frm, text="Velocidad").grid(row=4, column=0, sticky="w", **pad)
        ttk.Combobox(frm, state="readonly", textvariable=self.speed, values=list(SPEEDS)).grid(
            row=4, column=1, sticky="ew", **pad)
        if tray.AVAILABLE:
            ttk.Checkbutton(frm, text="Minimizar a la bandeja", variable=self.to_tray).grid(
                row=4, column=2, sticky="w", **pad)

        ttk.Label(frm, text="Carpeta").grid(row=5, column=0, sticky="w", **pad)
        self.folder_entry = ttk.Entry(frm, textvariable=self.output_dir)
        self.folder_entry.grid(row=5, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Elegir...", command=self.on_browse).grid(row=5, column=2, sticky="ew", **pad)

        self.dl_btn = ttk.Button(frm, text="Descargar", style="Accent.TButton", command=self.on_download,
                                 state="disabled")
        self.space_lbl = ttk.Label(frm, text="", style="Muted.TLabel")
        self.space_lbl.grid(row=6, column=1, columnspan=2, sticky="w", padx=12)
        self.dl_btn.grid(row=7, column=0, columnspan=3, sticky="ew", **pad)

    def _build_queue_tab(self, parent) -> None:
        frm = ttk.Frame(parent)
        frm.pack(fill="both", expand=True, padx=12, pady=10)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)

        ttk.Label(frm, text="Se usan el formato, los subtítulos, la velocidad y la carpeta de la pestaña Descargar.",
                  style="Muted.TLabel", wraplength=600, justify="left").grid(row=0, column=0, columnspan=2,
                                                                              sticky="w", pady=(0, 8))
        quality_row = ttk.Frame(frm)
        quality_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(quality_row, text="Calidad máxima").pack(side="left")
        ttk.Combobox(quality_row, state="readonly", textvariable=self.queue_quality, values=QUEUE_QUALITIES,
                     width=18).pack(side="left", padx=10)
        ttk.Label(quality_row, text="FPS").pack(side="left", padx=(12, 0))
        ttk.Combobox(quality_row, state="readonly", textvariable=self.queue_fps,
                     values=[FPS_AUTO] + [f"{value} fps" for value in FPS_PRESETS], width=12).pack(side="left",
                                                                                                  padx=10)
        self.load_q_btn = ttk.Button(quality_row, text="Cargar cola...", style="Small.TButton",
                                     command=self.on_queue_load)
        self.load_q_btn.pack(side="right")
        self.save_q_btn = ttk.Button(quality_row, text="Guardar cola...", style="Small.TButton",
                                     command=self.on_queue_save)
        self.save_q_btn.pack(side="right", padx=(0, 6))

        self.queue_tree = ttk.Treeview(frm, columns=("video", "state"), show="headings", selectmode="extended")
        self.queue_tree.heading("video", text="Video o enlace", anchor="w")
        self.queue_tree.heading("state", text="Estado", anchor="w")
        self.queue_tree.column("video", width=380, anchor="w")
        self.queue_tree.column("state", width=170, anchor="w")
        self.queue_tree.tag_configure("ok", foreground=GREEN)
        self.queue_tree.tag_configure("err", foreground=RED)
        self.queue_tree.tag_configure("run", foreground=ACCENT_HOVER)
        scroll = ttk.Scrollbar(frm, orient="vertical", command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=scroll.set)
        self.queue_tree.grid(row=2, column=0, sticky="nsew")
        scroll.grid(row=2, column=1, sticky="ns")

        buttons = ttk.Frame(frm)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.add_q_btn = ttk.Button(buttons, text="Añadir enlaces...", command=self.on_add_links)
        self.add_q_btn.pack(side="left")
        self.remove_q_btn = ttk.Button(buttons, text="Quitar", command=self.on_queue_remove)
        self.remove_q_btn.pack(side="left", padx=6)
        self.clear_q_btn = ttk.Button(buttons, text="Limpiar terminados", command=self.on_queue_clear_done)
        self.clear_q_btn.pack(side="left")
        self.run_q_btn = ttk.Button(buttons, text="Descargar cola", style="Accent.TButton",
                                    command=self.on_run_queue, state="disabled")
        self.run_q_btn.pack(side="right")

    def _build_history_tab(self, parent) -> None:
        frm = ttk.Frame(parent)
        frm.pack(fill="both", expand=True, padx=12, pady=10)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)

        search_row = ttk.Frame(frm)
        search_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="Buscar").grid(row=0, column=0, padx=(0, 10))
        self.history_search = ttk.Entry(search_row, textvariable=self.history_filter)
        self.history_search.grid(row=0, column=1, sticky="ew")
        self.history_search.bind("<Escape>", lambda _e: self.history_filter.set(""))
        ttk.Button(search_row, text="✕", style="Small.TButton", width=3,
                   command=lambda: self.history_filter.set("")).grid(row=0, column=2, padx=(6, 0))
        self.history_count = ttk.Label(search_row, text="", style="Muted.TLabel")
        self.history_count.grid(row=0, column=3, padx=(12, 0))

        self.history_tree = ttk.Treeview(frm, columns=("name", "kind", "date"), show="headings",
                                         selectmode="browse")
        self.history_tree.heading("name", text="Nombre", anchor="w")
        self.history_tree.heading("kind", text="Tipo", anchor="w")
        self.history_tree.heading("date", text="Fecha", anchor="w")
        self.history_tree.column("name", width=360, anchor="w")
        self.history_tree.column("kind", width=70, anchor="w")
        self.history_tree.column("date", width=130, anchor="w")
        self.history_tree.tag_configure("err", foreground=RED)
        scroll = ttk.Scrollbar(frm, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scroll.set)
        self.history_tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.history_tree.bind("<Double-1>", lambda _e: self.on_history_play())
        self.history_tree.bind("<<TreeviewSelect>>", lambda _e: self._update_history_buttons())

        buttons = ttk.Frame(frm)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.play_btn = ttk.Button(buttons, text="Reproducir", style="Accent.TButton", command=self.on_history_play)
        self.play_btn.pack(side="left")
        self.reveal_btn = ttk.Button(buttons, text="Abrir carpeta", command=self.on_history_reveal)
        self.reveal_btn.pack(side="left", padx=6)
        self.forget_btn = ttk.Button(buttons, text="Quitar", command=self.on_history_remove)
        self.forget_btn.pack(side="left")
        ttk.Button(buttons, text="Limpiar historial", command=self.on_history_clear).pack(side="right")
        self._refresh_history()

    def _build_bottom(self, parent) -> None:
        bottom = ttk.Frame(parent)
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(bottom, maximum=100)
        self.bar.grid(row=0, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 6))
        self.bar.grid_remove()
        self.status = ttk.Label(bottom, text="", wraplength=470, justify="left")
        self.status.grid(row=1, column=0, sticky="w", padx=6)
        self.cancel_btn = ttk.Button(bottom, text="Cancelar", command=self.on_cancel)
        self.cancel_btn.grid(row=1, column=1, padx=(6, 0))
        self.cancel_btn.grid_remove()
        self.open_btn = ttk.Button(bottom, text="Abrir carpeta", command=self.on_open_folder)
        self.open_btn.grid(row=1, column=2, padx=(6, 0))
        self.open_btn.grid_remove()
        self.notice = ttk.Label(bottom, text="", wraplength=640, justify="left")
        self.notice.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 0))
        self.notice.grid_remove()

    def _enable_drop(self) -> None:
        if DND_ALL is None:
            return
        for widget in (self, self.root_frame, self.notebook, self.frm, self.entry, self.info_lbl, self.queue_tree):
            try:
                widget.drop_target_register(DND_ALL)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _on_drop(self, event):
        urls = find_urls(event.data)
        if len(urls) > 1:
            added = self._add_urls(urls)
            self.notebook.select(self.tab_queue)
            self._say(self._added_message(added), MUTED)
        elif urls:
            self.url.set(urls[0])
            self.notebook.select(self.tab_download)
            self._say("Enlace recibido. Pulsa Buscar.", MUTED)
        return event.action

    def _save_settings(self) -> None:
        lang = next((code for code, label in SUBTITLE_CHOICES.items() if label == self.sub_lang.get()), "es")
        storage.save_settings({
            "speed_limit": self.speed.get(),
            "minimize_to_tray": bool(self.to_tray.get()),
            "subtitle_lang": lang,
            "save_thumbnail": bool(self.save_thumb.get()),
            "thumbnail_format": self.thumb_format.get(),
            "embed_lyrics": bool(self.embed_lyrics.get()),
            "by_channel": bool(self.by_channel.get()),
            "proxy": self.proxy.get().strip(),
            "theme": ACTIVE_THEME,
            "parallel_downloads": int(self.parallel.get() or 1),
        })

    def _say(self, text: str, color: str = FG) -> None:
        self.status.config(text=text, foreground=color)

    def _bar_show(self) -> None:
        self.bar.stop()
        self.bar.config(mode="determinate", value=0)
        self.bar.grid()

    def _bar_hide(self) -> None:
        self.bar.stop()
        self.bar.config(mode="determinate", value=0)
        self.bar.grid_remove()

    def _bar_working(self) -> None:
        if str(self.bar.cget("mode")) != "indeterminate":
            self.bar.config(mode="indeterminate")
            self.bar.start(14)

    def _bar_set(self, percent: float) -> None:
        if str(self.bar.cget("mode")) != "determinate":
            self.bar.stop()
            self.bar.config(mode="determinate")
        self.bar.config(value=percent)

    def _set_thumbnail(self, raw: bytes | None) -> None:
        self._thumb_img = None
        if raw and Image is not None:
            try:
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                img.thumbnail((THUMB_W, THUMB_H))
                self._thumb_img = ImageTk.PhotoImage(img)
            except Exception:
                self._thumb_img = None
        if self._thumb_img is not None:
            self.thumb_lbl.config(image=self._thumb_img)
            self.thumb_box.grid()
            self.info_col.grid_configure(padx=(14, 0))
        else:
            self.thumb_lbl.config(image="")
            self.thumb_box.grid_remove()
            self.info_col.grid_configure(padx=0)

    def _refresh_subs_widget(self) -> None:
        if self.subtitles.get() and not self.audio_only.get():
            self.sub_lang_box.pack(side="left", padx=(8, 0))
        else:
            self.sub_lang_box.pack_forget()

    def _fps_values(self) -> list[int]:
        if self._list_mode():
            return list(FPS_PRESETS)
        info = self.video_info
        if info is None:
            return []
        selection = self.quality.get()
        if selection.endswith("p") and selection[:-1].isdigit():
            return list(info.fps_by_height.get(int(selection[:-1]), ()))
        return info.fps_options

    def _refresh_fps_options(self) -> None:
        values = self._fps_values() if self.ready else []
        if self.audio_only.get() or not values:
            self._fps_locked = False
            self.fps_box.config(values=[FPS_AUTO], state="disabled")
            self.fps_choice.set(FPS_AUTO)
            return
        if len(values) == 1 and not self._list_mode():
            label = f"{values[0]} fps"
            self._fps_locked = True
            self.fps_box.config(values=[label], state="disabled")
            self.fps_choice.set(label)
            return
        labels = [FPS_AUTO] + [f"{value} fps" for value in values]
        self.fps_box.config(values=labels, state="readonly")
        if self._fps_locked or self.fps_choice.get() not in labels:
            self.fps_choice.set(FPS_AUTO)
        self._fps_locked = False

    def _selected_fps(self) -> int | None:
        parts = self.fps_choice.get().split()
        return int(parts[0]) if parts and parts[0].isdigit() else None

    def _toggle_audio(self) -> None:
        audio = self.audio_only.get()
        self.quality.config(state="disabled" if audio or not self.ready else "readonly")
        self.subs_chk.config(state="disabled" if audio else "normal")
        self._refresh_subs_widget()
        if audio:
            self.fmt.config(values=AUDIO_FORMATS, textvariable=self.audio_format)
        else:
            self.fmt.config(values=VIDEO_CONTAINERS, textvariable=self.container)
        self._refresh_fps_options()
        self._refresh_space()

    def _range_values(self) -> tuple[int, int]:
        total = self.playlist.total if self.playlist else 1
        try:
            start = int(self.pl_from.get() or 1)
        except ValueError:
            start = 1
        try:
            end = int(self.pl_to.get() or total)
        except ValueError:
            end = total
        start = max(1, start)
        return start, max(start, end)

    def _estimated_bytes(self) -> int | None:
        if not self.ready:
            return None
        selection = self.quality.get()
        height = int(selection[:-1]) if selection.endswith("p") and selection[:-1].isdigit() else None
        audio, audio_format = self.audio_only.get(), self.audio_format.get()
        if self._list_mode():
            start, end = self._range_values()
            entries = [entry for entry in self.playlist.entries if start <= entry.index <= end]
            return estimate_playlist_size(entries, height, audio, audio_format)
        if self.video_info is None:
            return None
        return estimate_video_size(self.video_info, height, audio, audio_format)

    def _refresh_space(self) -> None:
        free = free_space(self.output_dir.get())
        if free is None:
            self.space_lbl.config(text="")
            return
        text = f"Espacio libre: {format_size(free)}"
        color = MUTED
        estimate = self._estimated_bytes()
        if estimate:
            text += f"  ·  Descarga estimada: {format_size(estimate)}"
            if estimate * SPACE_MARGIN > free:
                text += "  ·  espacio insuficiente"
                color = RED
        self.space_lbl.config(text=text, foreground=color)

    def _confirm_space(self, folder: str, estimate: int | None) -> bool:
        free = free_space(folder)
        if free is None:
            return True
        if estimate and estimate * SPACE_MARGIN > free:
            text = (f"Se necesitan unos {format_size(estimate)} y solo hay {format_size(free)} libres en:\n{folder}\n\n"
                    "La descarga podría fallar a mitad. ¿Continuar de todos modos?")
        elif free < MIN_FREE_BYTES:
            text = f"Quedan solo {format_size(free)} libres en:\n{folder}\n\n¿Continuar de todos modos?"
        else:
            return True
        return messagebox.askyesno("Espacio en disco", text)

    def _main_focus(self):
        try:
            focus = self.focus_get()
        except KeyError:
            return None
        if focus is None or focus.winfo_toplevel() is not self:
            return None
        return focus

    def _bind_shortcuts(self) -> None:
        for sequence, handler in (("<Control-v>", self._on_global_paste), ("<Control-V>", self._on_global_paste),
                                  ("<Control-l>", self._focus_url), ("<Control-L>", self._focus_url),
                                  ("<Control-Return>", self._on_ctrl_enter), ("<Control-f>", self._focus_history_search),
                                  ("<Control-F>", self._focus_history_search)):
            self.bind_all(sequence, handler)
        for entry in (self.entry, self.folder_entry, self.proxy_entry):
            self._attach_context_menu(entry)

    def _on_global_paste(self, _event):
        focus = self._main_focus()
        if focus is None or isinstance(focus, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox, ttk.Spinbox)):
            return None
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return "break"
        urls = find_urls(text)
        if not urls:
            self._say("El portapapeles no contiene ningún enlace.", MUTED)
            return "break"
        if len(urls) > 1:
            added = self._add_urls(urls)
            self.notebook.select(self.tab_queue)
            self._say(self._added_message(added), MUTED)
        else:
            self.url.set(urls[0])
            self.notebook.select(self.tab_download)
            self.entry.focus_set()
            self.entry.icursor("end")
            self._say("Enlace pegado. Pulsa Enter para buscar.", MUTED)
        return "break"

    def _focus_url(self, _event=None):
        if self._main_focus() is None:
            return None
        self.notebook.select(self.tab_download)
        self.entry.focus_set()
        self.entry.selection_range(0, "end")
        return "break"

    def _on_ctrl_enter(self, _event):
        if self._main_focus() is None:
            return None
        if str(self.dl_btn.cget("state")) == "normal":
            self.on_download()
        return "break"

    def _focus_history_search(self, _event=None):
        if self._main_focus() is None:
            return None
        self.notebook.select(self.tab_history)
        self.history_search.focus_set()
        self.history_search.selection_range(0, "end")
        return "break"

    def _on_url_typed(self) -> None:
        if self.url.get().strip():
            self._hide_clip_bar()

    def _on_focus_in(self, event) -> None:
        if event.widget is self:
            self.after(150, self._check_clipboard)

    def _hide_clip_bar(self) -> None:
        self.clip_bar.pack_forget()

    def _check_clipboard(self) -> None:
        if self.busy or self.ready or self.url.get().strip():
            return
        try:
            text = self.clipboard_get()
        except tk.TclError:
            self._hide_clip_bar()
            return
        url = youtube_url(text)
        if not url or url == self._clip_dismissed:
            self._hide_clip_bar()
            return
        self._clip_url = url
        self.clip_label.config(text=f"Enlace copiado: {shorten(url, 46)}")
        self.clip_bar.pack(anchor="w", pady=(8, 0))

    def _use_clipboard_url(self) -> None:
        url = self._clip_url
        self._clip_dismissed = url
        self._hide_clip_bar()
        if not url:
            return
        self.url.set(url)
        self.notebook.select(self.tab_download)
        self.on_search()

    def _dismiss_clipboard(self) -> None:
        self._clip_dismissed = self._clip_url
        self._hide_clip_bar()

    def _sweep_orphans(self) -> None:
        try:
            removed = orphans.sweep()
        except Exception:
            return
        if removed:
            self._say(f"Se limpiaron {removed} restos de descargas interrumpidas.", MUTED)

    def _on_job_retry(self, data) -> None:
        job, attempt, total = data
        self._set_item(job, f"Reintentando ({attempt}/{total})", "run")
        if not self.parallel_active:
            self._say(f"Error de red. Reintentando ({attempt}/{total})...", RED)

    def on_queue_save(self) -> None:
        items = []
        for iid in self.queue_tree.get_children():
            url = self.item_url.get(iid)
            if not url or self.item_state.get(iid) in FINISHED_STATES:
                continue
            items.append({"url": url, "title": self.queue_tree.set(iid, "video"), **self.item_extra.get(iid, {})})
        if not items:
            messagebox.showinfo("Guardar cola", "No hay enlaces pendientes que guardar.")
            return
        path = filedialog.asksaveasfilename(
            title="Guardar cola", defaultextension=".json", initialfile="cola_llorostini",
            filetypes=[("Cola de LLorostini (JSON)", "*.json"), ("Lista de enlaces (TXT)", "*.txt")])
        if not path:
            return
        try:
            if path.lower().endswith(".txt"):
                Path(path).write_text("\n".join(item["url"] for item in items) + "\n", encoding="utf-8")
            else:
                data = {"app": APP_NAME, "version": 1, "quality": self.queue_quality.get(),
                        "fps": self.queue_fps.get(), "items": items}
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Error", f"No se pudo guardar la cola: {exc}")
            return
        self._say(f"Cola guardada: {len(items)} enlaces en {Path(path).name}.", MUTED)

    def on_queue_load(self) -> None:
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Cargar cola", filetypes=[("Cola o lista de enlaces", "*.json *.txt"), ("Todos los archivos", "*.*")])
        if not path:
            return
        try:
            items, quality, fps = read_queue_file(Path(path))
        except OSError as exc:
            messagebox.showerror("Error", f"No se pudo leer el archivo: {exc}")
            return
        if not items:
            messagebox.showinfo("Cargar cola", "No se encontró ningún enlace válido en el archivo.")
            return
        known = set(self.item_url.values())
        added = repeated = playlists = 0
        for item in items:
            url = item["url"]
            if url in known:
                repeated += 1
                continue
            if url_kind(url) == "playlist":
                playlists += 1
                continue
            iid = self._queue_insert(url, item.get("title"))
            extra = {key: item[key] for key in ("out_dir", "prefix", "duration") if item.get(key)}
            if extra:
                self.item_extra[iid] = extra
            known.add(url)
            added += 1
        if quality in QUEUE_QUALITIES:
            self.queue_quality.set(quality)
        if fps in [FPS_AUTO] + [f"{value} fps" for value in FPS_PRESETS]:
            self.queue_fps.set(fps)
        self._update_queue_buttons()
        message = f"Cola cargada: {added} enlaces"
        if repeated:
            message += f" ({repeated} ya estaban)"
        if playlists:
            message += f" · {playlists} listas omitidas (pégalas en Descargar)"
        self._say(message + ".", MUTED)

    def _attach_context_menu(self, entry) -> None:
        menu = tk.Menu(entry, tearoff=False, bg=SURFACE, fg=FG, activebackground=ACCENT,
                       activeforeground=ACCENT_TEXT, borderwidth=0)
        for label, sequence in (("Cortar", "<<Cut>>"), ("Copiar", "<<Copy>>"), ("Pegar", "<<Paste>>")):
            menu.add_command(label=label, command=lambda seq=sequence: entry.event_generate(seq))
        menu.add_separator()
        menu.add_command(label="Seleccionar todo", command=lambda: (entry.focus_set(), entry.selection_range(0, "end")))
        entry.bind("<Button-3>", lambda event: (entry.focus_set(), menu.tk_popup(event.x_root, event.y_root)))

    def on_browse(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_dir.get())
        if folder:
            self.output_dir.set(folder)

    def _reveal(self, path: Path | None) -> None:
        try:
            if path and path.exists():
                subprocess.Popen(f'explorer /select,"{path}"')
            else:
                os.startfile(self.output_dir.get())
        except OSError as exc:
            messagebox.showerror("Error", f"No se pudo abrir la carpeta: {exc}")

    def on_open_folder(self) -> None:
        self._reveal(self.last_path)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.search_btn.config(state="disabled" if busy else "normal")
        self.dl_btn.config(state="disabled" if busy or not self.ready else "normal")
        self._update_queue_buttons()
        self._refresh_update_buttons()

    def on_search(self) -> None:
        if self.busy:
            return
        self._hide_clip_bar()
        try:
            netutil.set_proxy(self.proxy.get())
        except ValueError as exc:
            messagebox.showerror("Proxy", str(exc))
            return
        self._set_busy(True)
        self.ready = False
        self.heights = []
        self.video_info = None
        self.playlist = None
        self.quality.config(state="disabled")
        self._refresh_fps_options()
        self.open_btn.grid_remove()
        self._bar_hide()
        self.pl_box.pack_forget()
        self.dl_btn.config(text="Descargar")
        self.info_lbl.config(text="Buscando...", foreground=MUTED)
        self._set_thumbnail(None)
        self._say("")
        url = self.url.get()

        def search():
            kind = url_kind(url)
            result = {"kind": kind, "video": None, "playlist": None, "thumb_video": None, "thumb_list": None}
            if kind != "playlist":
                result["video"] = get_info(url)
                result["thumb_video"] = fetch_thumbnail(result["video"].thumbnail_url)
            if kind != "video":
                try:
                    result["playlist"] = get_playlist_info(url)
                    result["thumb_list"] = fetch_thumbnail(result["playlist"].thumbnail_url)
                except DownloaderError:
                    if kind == "playlist":
                        raise
            return result

        self._run(search, "info")

    def _list_mode(self) -> bool:
        return self.playlist is not None and (self.video_info is None or not self.only_video.get())

    def _apply_mode(self) -> None:
        self.pl_box.pack_forget()
        self.range_row.pack_forget()
        self.only_video_chk.pack_forget()
        if self.playlist is None and self.video_info is None:
            return
        if self._list_mode():
            playlist = self.playlist
            note = f" · {playlist.skipped} no disponibles" if playlist.skipped else ""
            self.info_lbl.config(text=f"Lista: {shorten(playlist.title, 70)}\n"
                                      f"{shorten(playlist.uploader, 35)} · {playlist.total} videos{note}",
                                 foreground=FG)
            self._set_thumbnail(self.thumb_list)
            self.heights = []
            self.quality.config(values=QUEUE_QUALITIES)
            self.quality.set(BEST)
            self.dl_btn.config(text="Descargar lista")
            self.pl_total_lbl.config(text=f"de {playlist.total}")
            self.range_row.pack(anchor="w", pady=(8, 0))
        else:
            info = self.video_info
            mins, secs = divmod(info.duration or 0, 60)
            self.info_lbl.config(text=f"{shorten(info.title, 90)}\n{info.uploader} · {mins}:{secs:02d}",
                                 foreground=FG)
            self._set_thumbnail(self.thumb_video)
            self.heights = info.heights
            self.quality.config(values=[BEST] + [f"{h}p" for h in info.heights])
            self.quality.set(BEST)
            self.dl_btn.config(text="Descargar")
        if self.playlist is not None and self.video_info is not None:
            self.only_video_chk.pack(anchor="w", pady=(6, 0))
        if self.range_row.winfo_manager() or self.only_video_chk.winfo_manager():
            self.pl_box.pack(anchor="w")
        self.ready = True
        self._set_busy(self.busy)
        self._toggle_audio()

    def _collect_options(self) -> dict:
        subs = None
        if self.subtitles.get() and not self.audio_only.get():
            subs = next((c for c, label in SUBTITLE_CHOICES.items() if label == self.sub_lang.get()), "es")
        try:
            netutil.set_proxy(self.proxy.get())
        except ValueError as exc:
            raise DownloaderError(str(exc)) from exc
        thumbnail = self.thumb_format.get() if self.save_thumb.get() else None
        return {
            "out": self.output_dir.get(),
            "audio": self.audio_only.get(),
            "container": self.container.get(),
            "audio_format": self.audio_format.get(),
            "subs": subs,
            "rate": parse_rate_limit(SPEEDS.get(self.speed.get())),
            "thumbnail": thumbnail,
            "lyrics": bool(self.embed_lyrics.get()) and self.audio_only.get(),
            "parallel": int(self.parallel.get() or 1),
        }

    def on_download(self) -> None:
        if self.busy:
            return
        sel = self.quality.get()
        height = None if sel == BEST else int(sel.rstrip("p"))
        if self._list_mode():
            self._download_playlist(height)
            return
        self._start_jobs([{"url": self.url.get(), "height": height, "iid": None, "fps": self._selected_fps()}],
                         "single", self._estimated_bytes())

    def _download_playlist(self, height: int | None) -> None:
        playlist = self.playlist
        try:
            start = int(self.pl_from.get() or 1)
            end = int(self.pl_to.get() or playlist.total)
        except ValueError:
            messagebox.showerror("Rango no válido", "Escribe números enteros en «Videos del … al …».")
            return
        if start < 1 or end < start:
            messagebox.showerror("Rango no válido", "El primer número debe ser 1 o más y el segundo no puede ser "
                                                    "menor que el primero.")
            return
        selected = [entry for entry in playlist.entries if start <= entry.index <= end]
        if not selected:
            messagebox.showinfo("Lista de reproducción", "No hay videos disponibles en ese rango.")
            return
        folder = str(Path(self.output_dir.get()) / safe_filename(playlist.title))
        if len(selected) > 20 and not messagebox.askyesno(
                "Lista de reproducción",
                f"Se descargarán {len(selected)} videos en:\n{folder}\n\n¿Continuar?"):
            return
        fps = self._selected_fps()
        width = max(2, len(str(playlist.total)))
        estimate = estimate_playlist_size(selected, height, self.audio_only.get(), self.audio_format.get())
        jobs = []
        for entry in selected:
            prefix = f"{entry.index:0{width}d} - "
            iid = self._queue_insert(entry.url, f"{prefix}{entry.title}")
            target = (str(Path(folder) / safe_filename(entry.channel or "Sin canal"))
                      if self.by_channel.get() else folder)
            self.item_extra[iid] = {"out_dir": target, "prefix": prefix, "duration": entry.duration}
            jobs.append({"url": entry.url, "height": height, "iid": iid, "out_dir": target, "prefix": prefix,
                         "fps": fps})
        self.notebook.select(self.tab_queue)
        for job in jobs:
            self._set_item(job, "En espera")
        self._start_jobs(jobs, "queue", estimate)
        if self.busy:
            for entry in (e for e in playlist.unavailable if start <= e.index <= end):
                reason = ("El video es privado." if "Private" in entry.title
                          else "El video fue eliminado." if "Deleted" in entry.title
                          else "El video no está disponible.")
                label = f"{entry.index:0{width}d} - {entry.title}"
                row = self.queue_tree.insert("", "end", values=(label, f"Omitido: {reason}"), tags=("err",))
                self.item_state[row] = "Omitido"
                storage.add_failure(entry.url, label, reason)
                self._notify_failure(label, reason)
            self._refresh_history()

    def _start_jobs(self, jobs: list[dict], mode: str, estimate: int | None = None) -> None:
        try:
            opts = self._collect_options()
        except DownloaderError as exc:
            messagebox.showerror("Error", str(exc))
            return
        if not self._confirm_space(jobs[0].get("out_dir") or opts["out"], estimate):
            return
        for position, job in enumerate(jobs):
            job["index"] = position
        self.job_mode = mode
        self.job_total = len(jobs)
        self.job_prefix = ""
        self.results = {"ok": 0, "fail": 0}
        self.last_error = ""
        self._last_tray_percent = -1
        self.jobs_done = 0
        self.job_frac = {}
        self.job_speed = {}
        self.failures = []
        self.notice.grid_remove()
        self.cancel_event.clear()
        self._set_busy(True)
        self.open_btn.grid_remove()
        self._bar_show()
        self.cancel_btn.config(state="normal")
        self.cancel_btn.grid()
        self._say("Iniciando descarga...", MUTED)
        if self.to_tray.get() and tray.AVAILABLE:
            self.tray.start(f"{APP_NAME} - descargando")
            if self.tray.active:
                self.withdraw()

        workers = max(1, min(int(opts["parallel"]), len(jobs))) if mode == "queue" else 1
        self.parallel_active = workers > 1
        rate = opts["rate"] // workers if opts["rate"] and workers > 1 else opts["rate"]

        def run(job: dict) -> None:
            if self.cancel_event.is_set():
                self.events.put(("job_cancelled", job))
                return
            self.events.put(("job_start", (job["index"], job)))

            def hook(status: dict) -> None:
                self.events.put(("progress", (job["index"], status)))

            try:
                path = download(job["url"], job.get("out_dir") or opts["out"], job["height"], opts["audio"], hook,
                                opts["container"], opts["audio_format"], opts["subs"], rate,
                                self.cancel_event.is_set, job.get("prefix", ""), job.get("fps"),
                                opts["thumbnail"], opts["lyrics"],
                                on_retry=lambda attempt, total: self.events.put(("job_retry", (job, attempt, total))))
                self.events.put(("job_done", (job, path, opts["audio"])))
            except DownloadCancelledError:
                self.events.put(("job_cancelled", job))
            except DownloaderError as exc:
                self.events.put(("job_error", (job, str(exc))))
            except Exception as exc:
                self.events.put(("job_error", (job, f"Error inesperado: {exc}")))

        def worker() -> None:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for job in jobs:
                    pool.submit(run, job)
            self.events.put(("jobs_finished", None))

        threading.Thread(target=worker, daemon=True).start()

    def on_cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_btn.config(state="disabled")
        self._say("Cancelando...", MUTED)

    def _set_item(self, job: dict, state: str, tag: str = "", name: str | None = None) -> None:
        iid = job.get("iid")
        if not iid or not self.queue_tree.exists(iid):
            return
        self.item_state[iid] = state
        shown = name if name is not None else self.queue_tree.set(iid, "video")
        self.queue_tree.item(iid, values=(shown, state), tags=(tag,) if tag else ())

    def _job_title(self, job: dict) -> str:
        iid = job.get("iid")
        if iid and self.queue_tree.exists(iid):
            return self.queue_tree.set(iid, "video")
        if self.video_info is not None and not self._list_mode():
            return self.video_info.title
        return job["url"]

    def _forget_job(self, job: dict) -> None:
        index = job.get("index")
        self.job_frac.pop(index, None)
        self.job_speed.pop(index, None)
        self.jobs_done += 1

    def _notify_failure(self, title: str, message: str) -> None:
        self.failures.append((title, message))
        text = f"⚠ Omitido: {shorten(title, 55)} — {shorten(message, 70)}"
        if len(self.failures) > 1:
            text += f"   ({len(self.failures)} en total, ver Historial)"
        self.notice.config(text=text, foreground=RED)
        self.notice.grid()

    def _on_job_start(self, data) -> None:
        index, job = data
        self._last_tray_percent = -1
        self._set_item(job, "Descargando", "run")
        if self.parallel_active:
            return
        self.job_prefix = f"({index + 1}/{self.job_total}) " if self.job_total > 1 else ""
        self._say(f"{self.job_prefix}Iniciando descarga...", MUTED)

    def _on_progress(self, data) -> None:
        index, d = data
        if self.parallel_active:
            self._on_parallel_progress(index, d)
            return
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                percent = d["downloaded_bytes"] / total * 100
                self._bar_set(percent)
                if int(percent) != self._last_tray_percent:
                    self._last_tray_percent = int(percent)
                    self.tray.set_tooltip(f"{APP_NAME} - {self.job_prefix}{int(percent)}%")
            self._say(f"{self.job_prefix}Descargando... {d.get('_percent_str', '').strip()}  "
                      f"{d.get('_speed_str', '').strip()}", MUTED)
        elif d["status"] == "finished":
            self._bar_working()
            self._say(f"{self.job_prefix}Procesando archivo...", MUTED)

    def _on_parallel_progress(self, index: int, d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                fraction = min(1.0, d["downloaded_bytes"] / total)
                self.job_frac[index] = max(self.job_frac.get(index, 0.0), fraction)
            self.job_speed[index] = d.get("speed") or 0
        percent = min(100.0, (self.jobs_done + sum(self.job_frac.values())) / max(1, self.job_total) * 100)
        self._bar_set(percent)
        if int(percent) != self._last_tray_percent:
            self._last_tray_percent = int(percent)
            self.tray.set_tooltip(f"{APP_NAME} - {int(percent)}%")
        self._say(f"Descargando {len(self.job_frac)} a la vez  ·  {self.jobs_done}/{self.job_total} listos  ·  "
                  f"{format_size(sum(self.job_speed.values()))}/s", MUTED)

    def _on_job_done(self, data) -> None:
        job, path, audio = data
        self.last_path = Path(path)
        self.results["ok"] += 1
        self._set_item(job, "Listo", "ok", self.last_path.stem)
        self._forget_job(job)
        storage.add_history(self.last_path, "audio" if audio else "video")
        self._refresh_history()

    def _on_job_error(self, data) -> None:
        job, message = data
        self.results["fail"] += 1
        self.last_error = message
        self._set_item(job, f"Error: {message}"[:80], "err")
        self._forget_job(job)
        title = self._job_title(job)
        storage.add_failure(job["url"], title, message)
        self._refresh_history()
        if self.job_mode == "queue":
            self._notify_failure(title, message)

    def _on_job_cancelled(self, job: dict) -> None:
        self._set_item(job, "Cancelado")
        self._forget_job(job)

    def _on_jobs_finished(self, _data) -> None:
        self._bar_hide()
        self.cancel_btn.grid_remove()
        self._set_busy(False)
        self._toggle_audio()
        self._refresh_space()
        ok, fail = self.results["ok"], self.results["fail"]
        cancelled = self.cancel_event.is_set()

        if self.job_mode == "single":
            if ok and self.last_path:
                self._say(f"Listo: {self.last_path.name}", GREEN)
                self.open_btn.grid()
                self._finish_tray("Descarga completada", self.last_path.name)
            elif cancelled:
                self._say("Descarga cancelada.", MUTED)
                self._finish_tray("Descarga cancelada", "Puedes reanudarla más tarde.")
            else:
                self._say("")
                self._finish_tray("Error en la descarga", self.last_error)
                messagebox.showerror("Error", self.last_error or "No se pudo completar la descarga.")
            return

        summary = f"Cola terminada: {ok} descargados"
        if fail:
            summary += f", {fail} con error"
        if cancelled:
            summary += ", cancelada"
        self._say(summary + ".", GREEN if not fail else RED)
        if ok:
            self.open_btn.grid()
        self._finish_tray("Cola terminada", summary + ".")

    def _finish_tray(self, title: str, message: str) -> None:
        if not self.tray.active:
            return
        self.tray.notify(title, message)
        self.deiconify()
        self.lift()
        self.after(7000, self.tray.stop)

    def _on_tray_show(self, _data) -> None:
        self.deiconify()
        self.lift()

    def _on_tray_quit(self, _data) -> None:
        self.cancel_event.set()
        self.tray.stop()
        self.destroy()

    def _update_queue_buttons(self) -> None:
        children = self.queue_tree.get_children()
        pending = any(self.item_state.get(iid) not in FINISHED_STATES for iid in children)
        self.run_q_btn.config(state="normal" if pending and not self.busy else "disabled")
        state = "disabled" if self.busy else "normal"
        self.remove_q_btn.config(state=state)
        self.clear_q_btn.config(state=state)
        self.load_q_btn.config(state=state)

    def _queue_insert(self, url: str, name: str | None = None) -> str:
        iid = self.queue_tree.insert("", "end", values=(name or url, "Pendiente"))
        self.item_url[iid] = url
        self.item_state[iid] = "Pendiente"
        return iid

    def _add_urls(self, urls: list[str]) -> int:
        known = set(self.item_url.values())
        added = 0
        self.skipped_playlists = 0
        for url in urls:
            if url in known:
                continue
            if url_kind(url) == "playlist":
                self.skipped_playlists += 1
                continue
            self._queue_insert(url)
            known.add(url)
            added += 1
        self._update_queue_buttons()
        return added

    def _added_message(self, added: int) -> str:
        if not added:
            text = "Esos enlaces ya estaban en la cola."
        elif added == 1:
            text = "1 enlace añadido a la cola."
        else:
            text = f"{added} enlaces añadidos a la cola."
        if self.skipped_playlists == 1:
            text += " 1 es una lista de reproducción: pégala en la pestaña Descargar."
        elif self.skipped_playlists:
            text += f" {self.skipped_playlists} son listas de reproducción: pégalas en la pestaña Descargar."
        return text

    def on_add_links(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Añadir enlaces")
        dialog.configure(bg=BG)
        dialog.geometry("580x360")
        dialog.minsize(420, 260)
        dialog.transient(self)
        ttk.Label(dialog, text="Pega uno o varios enlaces, uno por línea.").pack(anchor="w", padx=16, pady=(16, 8))
        buttons = ttk.Frame(dialog)
        buttons.pack(side="bottom", fill="x", padx=16, pady=14)
        text = tk.Text(dialog, width=40, height=8, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", font=FONT,
                       wrap="none", highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                       padx=8, pady=8, selectbackground=ACCENT, selectforeground=ACCENT_TEXT)
        text.pack(fill="both", expand=True, padx=16)

        def load_file() -> None:
            path = filedialog.askopenfilename(parent=dialog, title="Elegir lista de enlaces",
                                              filetypes=[("Texto", "*.txt"), ("Todos", "*.*")])
            if path:
                try:
                    text.insert("end", Path(path).read_text(encoding="utf-8", errors="ignore") + "\n")
                except OSError as exc:
                    messagebox.showerror("Error", f"No se pudo leer el archivo: {exc}", parent=dialog)

        def accept() -> None:
            urls = find_urls(text.get("1.0", "end"))
            if not urls:
                messagebox.showinfo("Añadir enlaces", "No se encontró ningún enlace válido.", parent=dialog)
                return
            added = self._add_urls(urls)
            self._say(self._added_message(added), MUTED)
            dialog.destroy()

        ttk.Button(buttons, text="Cargar .txt...", command=load_file).pack(side="left")
        ttk.Button(buttons, text="Añadir", style="Accent.TButton", command=accept).pack(side="right")
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Control-Return>", lambda _e: accept())
        self._dark_titlebar(dialog)
        text.focus_set()
        dialog.grab_set()

    def on_queue_remove(self) -> None:
        if self.busy:
            return
        for iid in self.queue_tree.selection():
            self.queue_tree.delete(iid)
            self.item_state.pop(iid, None)
            self.item_url.pop(iid, None)
            self.item_extra.pop(iid, None)
        self._update_queue_buttons()

    def on_queue_clear_done(self) -> None:
        if self.busy:
            return
        for iid in list(self.queue_tree.get_children()):
            if self.item_state.get(iid) in FINISHED_STATES:
                self.queue_tree.delete(iid)
                self.item_state.pop(iid, None)
                self.item_url.pop(iid, None)
                self.item_extra.pop(iid, None)
        self._update_queue_buttons()

    def on_run_queue(self) -> None:
        if self.busy:
            return
        selection = self.queue_quality.get()
        height = None if selection == BEST else int(selection.rstrip("p"))
        fps_parts = self.queue_fps.get().split()
        queue_fps = int(fps_parts[0]) if fps_parts and fps_parts[0].isdigit() else None
        jobs = [{"url": self.item_url[iid], "height": height, "iid": iid, "fps": queue_fps,
                 **self.item_extra.get(iid, {})}
                for iid in self.queue_tree.get_children() if self.item_state.get(iid) not in FINISHED_STATES]
        if not jobs:
            return
        for job in jobs:
            self._set_item(job, "En espera")
        seconds = sum(job.get("duration") or DEFAULT_ITEM_SECONDS for job in jobs)
        estimate = estimate_seconds_size(seconds, height, self.audio_only.get(), self.audio_format.get())
        self._start_jobs(jobs, "queue", estimate)

    def _on_tab_changed(self) -> None:
        if self.notebook.select() == str(self.tab_history):
            self._refresh_history()

    def _history_text(self, entry: dict) -> str:
        kind = "error" if entry.get("kind") == "error" else "audio" if entry.get("kind") == "audio" else "video"
        parts = [entry.get(key, "") for key in ("title", "reason", "path", "url", "date")]
        return " ".join(str(part) for part in parts) + " " + kind

    def _refresh_history(self) -> None:
        self.history_entries = storage.load_history()
        self.history_tree.delete(*self.history_tree.get_children())
        needle = fold(self.history_filter.get().strip())
        shown = 0
        for index, entry in enumerate(self.history_entries):
            if needle and needle not in fold(self._history_text(entry)):
                continue
            shown += 1
            if entry.get("kind") == "error":
                name = f"{entry.get('reason', '')} — {entry.get('title', '')}"
                self.history_tree.insert("", "end", iid=str(index), values=(name, "Error", entry.get("date", "")),
                                         tags=("err",))
                continue
            exists = bool(entry.get("path")) and Path(entry["path"]).exists()
            name = entry.get("title", "") + ("" if exists else "  (archivo no encontrado)")
            kind = "Audio" if entry.get("kind") == "audio" else "Video"
            self.history_tree.insert("", "end", iid=str(index), values=(name, kind, entry.get("date", "")))
        total = len(self.history_entries)
        if needle:
            self.history_count.config(text=f"{shown} de {total}" if shown else "Sin resultados")
        else:
            self.history_count.config(text=f"{total} en total" if total else "")
        self._update_history_buttons()

    def _selected_history(self) -> dict | None:
        selection = self.history_tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        return self.history_entries[index] if index < len(self.history_entries) else None

    def _update_history_buttons(self) -> None:
        entry = self._selected_history()
        exists = bool(entry) and bool(entry.get("path")) and Path(entry["path"]).exists()
        self.play_btn.config(state="normal" if exists else "disabled")
        self.reveal_btn.config(state="normal" if exists else "disabled")
        self.forget_btn.config(state="normal" if entry else "disabled")

    def on_history_play(self) -> None:
        entry = self._selected_history()
        if not entry or not entry.get("path") or not Path(entry["path"]).exists():
            return
        try:
            os.startfile(entry["path"])
        except OSError as exc:
            messagebox.showerror("Error", f"No se pudo reproducir el archivo: {exc}")

    def on_history_reveal(self) -> None:
        entry = self._selected_history()
        if entry and entry.get("path"):
            self._reveal(Path(entry["path"]))

    def on_history_remove(self) -> None:
        entry = self._selected_history()
        if entry:
            storage.remove_history(entry)
            self._refresh_history()

    def on_history_clear(self) -> None:
        if self.history_entries and messagebox.askyesno("Historial", "¿Borrar todo el historial?\n\n"
                                                        "Los archivos descargados no se eliminan."):
            storage.clear_history()
            self._refresh_history()

    def _build_settings_tab(self, parent) -> None:
        frm = ttk.Frame(parent)
        frm.pack(fill="both", expand=True, padx=16, pady=12)
        frm.columnconfigure(1, weight=1)

        def heading(row: int, text: str) -> None:
            ttk.Label(frm, text=text, font=("Segoe UI Semibold", 11)).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(0 if row == 0 else 16, 6))

        heading(0, "Contenido")
        ttk.Checkbutton(frm, text="Guardar la miniatura junto al archivo", variable=self.save_thumb).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Combobox(frm, state="readonly", width=8, textvariable=self.thumb_format,
                     values=list(THUMBNAIL_FORMATS)).grid(row=1, column=2, sticky="e", pady=3)
        ttk.Checkbutton(frm, text="Incrustar letras en el audio (MP3, M4A y FLAC)", variable=self.embed_lyrics).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Checkbutton(frm, text="Organizar las listas en carpetas por canal", variable=self.by_channel).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=3)

        heading(4, "Red")
        ttk.Label(frm, text="Proxy").grid(row=5, column=0, sticky="w", pady=3)
        self.proxy_entry = ttk.Entry(frm, textvariable=self.proxy)
        self.proxy_entry.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(14, 0), pady=3)
        ttk.Label(frm, text="Ejemplos: http://127.0.0.1:8080 o socks5://127.0.0.1:1080. Vacío: sin proxy.",
                  style="Muted.TLabel").grid(row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))

        heading(7, "Apariencia")
        ttk.Label(frm, text="Tema").grid(row=8, column=0, sticky="w", pady=3)
        theme_box = ttk.Combobox(frm, state="readonly", textvariable=self.theme, values=list(PALETTES))
        theme_box.grid(row=8, column=1, columnspan=2, sticky="ew", padx=(14, 0), pady=3)
        theme_box.bind("<<ComboboxSelected>>", lambda _e: self.on_theme_change())

        heading(9, "Rendimiento")
        ttk.Label(frm, text="Descargas simultáneas").grid(row=10, column=0, sticky="w", pady=3)
        ttk.Combobox(frm, state="readonly", width=8, textvariable=self.parallel, values=list(PARALLEL_CHOICES)).grid(
            row=10, column=1, sticky="w", padx=(14, 0), pady=3)
        ttk.Label(frm, text="Con 1 se descarga un video a la vez (recomendado). Con 2 o 3, las colas y listas "
                            "terminan antes si tu conexión lo permite; el límite de velocidad se reparte entre ellas.",
                  style="Muted.TLabel", wraplength=580, justify="left").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(2, 0))

    def _build_update_tab(self, parent) -> None:
        frm = ttk.Frame(parent)
        frm.pack(fill="both", expand=True, padx=14, pady=12)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(4, weight=1)

        ttk.Label(frm, text=f"{APP_NAME} {__version__}", font=("Segoe UI Semibold", 16)).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frm, text="Versión instalada", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self.upd_status = ttk.Label(frm, text="Pulsa «Buscar actualizaciones» para ver si hay una versión nueva.",
                                    style="Muted.TLabel", wraplength=600, justify="left")
        self.upd_status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.notes_title = ttk.Label(frm, text="Novedades de la última versión", style="Muted.TLabel")
        self.notes_title.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self.notes_text = tk.Text(frm, height=8, wrap="word", bg=FIELD, fg=FG, relief="flat", font=FONT,
                                  padx=12, pady=10, highlightthickness=1, highlightbackground=BORDER,
                                  highlightcolor=BORDER, selectbackground=ACCENT, selectforeground=ACCENT_TEXT,
                                  insertbackground=FG, state="disabled")
        scroll = ttk.Scrollbar(frm, orient="vertical", command=self.notes_text.yview)
        self.notes_text.configure(yscrollcommand=scroll.set)
        self.notes_text.grid(row=4, column=0, columnspan=2, sticky="nsew")
        scroll.grid(row=4, column=2, sticky="ns")

        buttons = ttk.Frame(frm)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.check_btn = ttk.Button(buttons, text="Buscar actualizaciones",
                                    command=lambda: self.check_updates(silent=False))
        self.check_btn.pack(side="left")
        self.update_btn = ttk.Button(buttons, text="Actualizar ahora", style="Accent.TButton",
                                     command=self.on_update_now, state="disabled")
        self.update_btn.pack(side="left", padx=8)
        self.github_btn = ttk.Button(buttons, text="Ver en GitHub", command=self.on_open_release_page)
        self.github_btn.pack(side="right")

    def _open_update_tab(self) -> None:
        self.notebook.select(self.tab_update)
        self.check_updates(silent=False)

    def _refresh_update_buttons(self) -> None:
        if not hasattr(self, "update_btn"):
            return
        release = self.latest_release
        can_update = (bool(release) and self.update_available and can_self_update()
                      and bool(release.asset_url) and not self.busy)
        self.update_btn.config(state="normal" if can_update else "disabled")

    def _show_notes(self, release) -> None:
        self.notes_title.config(text=f"Novedades de {release.version}")
        self.notes_text.config(state="normal")
        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", plain_notes(release.notes) or "Esta versión no incluye notas.")
        self.notes_text.config(state="disabled")

    def check_updates(self, silent: bool) -> None:
        if not silent:
            self.upd_status.config(text="Buscando actualizaciones...", foreground=MUTED)
            self.check_btn.config(state="disabled")

        def worker() -> None:
            try:
                self.events.put(("update_checked", (fetch_latest_release(), silent)))
            except UpdateError as exc:
                self.events.put(("update_error", (str(exc), silent)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_checked(self, data) -> None:
        release, silent = data
        self.latest_release = release
        self.update_available = is_newer(release.version)
        self.check_btn.config(state="normal")
        self.notebook.tab(self.tab_update, text="Actualizaciones ●" if self.update_available else "Actualizaciones")
        self._show_notes(release)
        if self.update_available:
            hint = "" if can_self_update() else " La actualización automática solo funciona con el .exe."
            self.upd_status.config(text=f"Hay una versión nueva: {release.version} (tienes {__version__}).{hint}",
                                   foreground=GREEN)
            if silent and not self.busy:
                self._say(f"Hay una versión nueva ({release.version}). Míralo en la pestaña Actualizaciones.", MUTED)
        else:
            self.upd_status.config(text=f"Estás al día. Última versión publicada: {release.version}.", foreground=FG)
        self._refresh_update_buttons()

    def _on_update_error(self, data) -> None:
        message, silent = data
        self.check_btn.config(state="normal")
        if not silent:
            self.upd_status.config(text=message, foreground=RED)

    def on_update_now(self) -> None:
        release = self.latest_release
        if not release or self.busy:
            return
        if messagebox.askyesno("Actualización disponible",
                               f"Se descargará la versión {release.version} y la aplicación se reiniciará.\n\n"
                               "¿Deseas actualizarlo ahora?"):
            self._start_update(release)

    def on_open_release_page(self) -> None:
        release = self.latest_release
        webbrowser.open(release.page_url if release else f"https://github.com/{GITHUB_REPO}/releases")

    def _start_update(self, release) -> None:
        self._set_busy(True)
        self._bar_show()
        self._say("Descargando actualización...", MUTED)

        def on_progress(done: int, total: int) -> None:
            self.events.put(("update_progress", (done, total)))

        def worker() -> None:
            try:
                self.events.put(("update_ready", download_update(release, on_progress)))
            except UpdateError as exc:
                self.events.put(("update_failed", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_progress(self, data) -> None:
        done, total = data
        if total:
            self._bar_set(done / total * 100)
        self._say(f"Descargando actualización... {done / 1_048_576:.1f} MB", MUTED)

    def _on_update_ready(self, path) -> None:
        self._bar_working()
        self._say("Reiniciando para aplicar la actualización...", MUTED)
        try:
            apply_update(path)
        except OSError as exc:
            self._on_update_failed(f"No se pudo aplicar la actualización: {exc}")
            return
        self.destroy()

    def _on_update_failed(self, msg: str) -> None:
        self._bar_hide()
        self._say("")
        self._set_busy(False)
        self._toggle_audio()
        messagebox.showerror("Actualización", msg)

    def _run(self, fn, kind: str) -> None:
        def worker() -> None:
            try:
                self.events.put((kind, fn()))
            except DownloaderError as exc:
                self.events.put(("error", str(exc)))
            except Exception as exc:
                self.events.put(("error", f"Error inesperado: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                getattr(self, f"_on_{kind}")(data)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(100, self._poll)
        except tk.TclError:
            pass

    def _on_info(self, data) -> None:
        self.video_info = data["video"]
        self.playlist = data["playlist"]
        self.thumb_video = data["thumb_video"]
        self.thumb_list = data["thumb_list"]
        self.only_video.set(data["kind"] == "video_in_list")
        if self.playlist is not None:
            self.pl_from.set("1")
            self.pl_to.set(str(self.playlist.total))
        self._apply_mode()
        self._set_busy(False)

    def _on_error(self, msg: str) -> None:
        if not self.ready:
            self.info_lbl.config(text="Pega o arrastra un enlace y pulsa Buscar.", foreground=MUTED)
        self._bar_hide()
        self._say("")
        self._set_busy(False)
        self._toggle_audio()
        messagebox.showerror("Error", msg)


if __name__ == "__main__":
    App().mainloop()
