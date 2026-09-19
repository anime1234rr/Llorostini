from __future__ import annotations

import io
import os
import queue
import re
import subprocess
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import storage
import tray
from downloader import (AUDIO_FORMATS, SUBTITLE_CHOICES, VIDEO_CONTAINERS, DownloadCancelledError, DownloaderError,
                        default_download_dir, download, fetch_thumbnail, get_info, has_ffmpeg, parse_rate_limit)
from updater import UpdateError, apply_update, can_self_update, check_for_update, download_update
from version import APP_NAME, __version__

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

BG = "#14161b"
SURFACE = "#1e2128"
FIELD = "#252932"
BORDER = "#323744"
HOVER = "#2d323d"
FG = "#e8eaf0"
MUTED = "#8b93a5"
ACCENT = "#5b8cff"
ACCENT_HOVER = "#7aa1ff"
GREEN = "#3ddc84"
RED = "#ff6b6b"
FONT = ("Segoe UI", 10)


def find_urls(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for url in URL_PATTERN.findall(str(text)):
        seen.setdefault(url.rstrip(".,;)"), None)
    return list(seen)


class App(_Base):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("700x700")
        self.minsize(660, 660)
        self.configure(bg=BG)

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

        self._apply_theme()
        self._build()
        self._build_menu()
        self._enable_drop()
        self._dark_titlebar(self)
        for var in (self.speed, self.to_tray, self.sub_lang):
            var.trace_add("write", lambda *_: self._save_settings())
        self.after(100, self._poll)
        self.after(1500, lambda: self.check_updates(silent=True))

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
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT, padding=(14, 9))
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", SURFACE)],
                  foreground=[("disabled", MUTED)], bordercolor=[("disabled", BORDER)])
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
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=SURFACE, foreground=MUTED, relief="flat", padding=7)
        style.map("Treeview.Heading", background=[("active", HOVER)])
        style.configure("Vertical.TScrollbar", background=SURFACE, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED)
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", FONT)

    def _dark_titlebar(self, window) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass

    def _build_menu(self) -> None:
        menubar = tk.Menu(self, bg=SURFACE, fg=FG, activebackground=ACCENT, activeforeground="#ffffff", borderwidth=0)
        help_menu = tk.Menu(menubar, tearoff=False, bg=SURFACE, fg=FG, activebackground=ACCENT,
                            activeforeground="#ffffff", borderwidth=0)
        help_menu.add_command(label="Buscar actualizaciones...", command=lambda: self.check_updates(silent=False))
        help_menu.add_separator()
        help_menu.add_command(label=f"Versión {__version__}", state="disabled")
        menubar.add_cascade(label="Ayuda", menu=help_menu)
        self.config(menu=menubar)

    def _build(self) -> None:
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=12, pady=10)
        self.root_frame = root

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        self.tab_download = ttk.Frame(self.notebook)
        self.tab_queue = ttk.Frame(self.notebook)
        self.tab_history = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_download, text="Descargar")
        self.notebook.add(self.tab_queue, text="Cola")
        self.notebook.add(self.tab_history, text="Historial")
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab_changed())

        self._build_download_tab(self.tab_download)
        self._build_queue_tab(self.tab_queue)
        self._build_history_tab(self.tab_history)
        self._build_bottom(root)

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
        self.info_lbl = ttk.Label(info_row, text="Pega o arrastra un enlace y pulsa Buscar.", style="Muted.TLabel",
                                  wraplength=380, justify="left")
        self.info_lbl.grid(row=0, column=1, sticky="w")

        ttk.Label(frm, text="Calidad").grid(row=2, column=0, sticky="w", **pad)
        self.quality = ttk.Combobox(frm, state="disabled", values=[BEST])
        self.quality.set(BEST)
        self.quality.grid(row=2, column=1, sticky="ew", **pad)
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
        ttk.Entry(frm, textvariable=self.output_dir).grid(row=5, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Elegir...", command=self.on_browse).grid(row=5, column=2, sticky="ew", **pad)

        self.dl_btn = ttk.Button(frm, text="Descargar", style="Accent.TButton", command=self.on_download,
                                 state="disabled")
        self.dl_btn.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)

    def _build_queue_tab(self, parent) -> None:
        frm = ttk.Frame(parent)
        frm.pack(fill="both", expand=True, padx=12, pady=10)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)

        ttk.Label(frm, text="Se usan el formato, los subtítulos, la velocidad y la carpeta de la pestaña Descargar.",
                  style="Muted.TLabel", wraplength=600, justify="left").grid(row=0, column=0, columnspan=2,
                                                                              sticky="w", pady=(0, 8))
        quality_row = ttk.Frame(frm)
        quality_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(quality_row, text="Calidad máxima").pack(side="left")
        ttk.Combobox(quality_row, state="readonly", textvariable=self.queue_quality, values=QUEUE_QUALITIES,
                     width=18).pack(side="left", padx=10)

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
        frm.rowconfigure(0, weight=1)

        self.history_tree = ttk.Treeview(frm, columns=("name", "kind", "date"), show="headings",
                                         selectmode="browse")
        self.history_tree.heading("name", text="Nombre", anchor="w")
        self.history_tree.heading("kind", text="Tipo", anchor="w")
        self.history_tree.heading("date", text="Fecha", anchor="w")
        self.history_tree.column("name", width=360, anchor="w")
        self.history_tree.column("kind", width=70, anchor="w")
        self.history_tree.column("date", width=130, anchor="w")
        scroll = ttk.Scrollbar(frm, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scroll.set)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.history_tree.bind("<Double-1>", lambda _e: self.on_history_play())
        self.history_tree.bind("<<TreeviewSelect>>", lambda _e: self._update_history_buttons())

        buttons = ttk.Frame(frm)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
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
        bottom.pack(fill="x", pady=(8, 0))
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
            self._say(f"{added} enlaces añadidos a la cola.", MUTED)
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
            self.info_lbl.grid_configure(padx=(14, 0))
        else:
            self.thumb_lbl.config(image="")
            self.thumb_box.grid_remove()
            self.info_lbl.grid_configure(padx=0)

    def _refresh_subs_widget(self) -> None:
        if self.subtitles.get() and not self.audio_only.get():
            self.sub_lang_box.pack(side="left", padx=(8, 0))
        else:
            self.sub_lang_box.pack_forget()

    def _toggle_audio(self) -> None:
        audio = self.audio_only.get()
        self.quality.config(state="disabled" if audio or not self.heights else "readonly")
        self.subs_chk.config(state="disabled" if audio else "normal")
        self._refresh_subs_widget()
        if audio:
            self.fmt.config(values=AUDIO_FORMATS, textvariable=self.audio_format)
        else:
            self.fmt.config(values=VIDEO_CONTAINERS, textvariable=self.container)

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
        self.dl_btn.config(state="disabled" if busy or not self.heights else "normal")
        self._update_queue_buttons()

    def on_search(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.heights = []
        self.quality.config(state="disabled")
        self.open_btn.grid_remove()
        self._bar_hide()
        self.info_lbl.config(text="Buscando...")
        self._set_thumbnail(None)
        self._say("")
        url = self.url.get()

        def search():
            info = get_info(url)
            return info, fetch_thumbnail(info.thumbnail_url)

        self._run(search, "info")

    def _collect_options(self) -> dict:
        subs = None
        if self.subtitles.get() and not self.audio_only.get():
            subs = next((c for c, label in SUBTITLE_CHOICES.items() if label == self.sub_lang.get()), "es")
        return {
            "out": self.output_dir.get(),
            "audio": self.audio_only.get(),
            "container": self.container.get(),
            "audio_format": self.audio_format.get(),
            "subs": subs,
            "rate": parse_rate_limit(SPEEDS.get(self.speed.get())),
        }

    def on_download(self) -> None:
        if self.busy:
            return
        sel = self.quality.get()
        height = None if sel == BEST else int(sel.rstrip("p"))
        self._start_jobs([{"url": self.url.get(), "height": height, "iid": None}], "single")

    def _start_jobs(self, jobs: list[dict], mode: str) -> None:
        try:
            opts = self._collect_options()
        except DownloaderError as exc:
            messagebox.showerror("Error", str(exc))
            return
        self.job_mode = mode
        self.job_total = len(jobs)
        self.job_prefix = ""
        self.results = {"ok": 0, "fail": 0}
        self.last_error = ""
        self._last_tray_percent = -1
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

        def hook(status: dict) -> None:
            self.events.put(("progress", status))

        def worker() -> None:
            for index, job in enumerate(jobs):
                if self.cancel_event.is_set():
                    self.events.put(("job_cancelled", job))
                    continue
                self.events.put(("job_start", (index, job)))
                try:
                    path = download(job["url"], opts["out"], job["height"], opts["audio"], hook,
                                    opts["container"], opts["audio_format"], opts["subs"], opts["rate"],
                                    self.cancel_event.is_set)
                    self.events.put(("job_done", (job, path, opts["audio"])))
                except DownloadCancelledError:
                    self.events.put(("job_cancelled", job))
                except DownloaderError as exc:
                    self.events.put(("job_error", (job, str(exc))))
                except Exception as exc:
                    self.events.put(("job_error", (job, f"Error inesperado: {exc}")))
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

    def _on_job_start(self, data) -> None:
        index, job = data
        self.job_prefix = f"({index + 1}/{self.job_total}) " if self.job_total > 1 else ""
        self._last_tray_percent = -1
        self._set_item(job, "Descargando", "run")
        self._say(f"{self.job_prefix}Iniciando descarga...", MUTED)

    def _on_progress(self, d: dict) -> None:
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

    def _on_job_done(self, data) -> None:
        job, path, audio = data
        self.last_path = Path(path)
        self.results["ok"] += 1
        self._set_item(job, "Listo", "ok", self.last_path.stem)
        storage.add_history(self.last_path, "audio" if audio else "video")
        self._refresh_history()

    def _on_job_error(self, data) -> None:
        job, message = data
        self.results["fail"] += 1
        self.last_error = message
        self._set_item(job, f"Error: {message}"[:80], "err")

    def _on_job_cancelled(self, job: dict) -> None:
        self._set_item(job, "Cancelado")

    def _on_jobs_finished(self, _data) -> None:
        self._bar_hide()
        self.cancel_btn.grid_remove()
        self._set_busy(False)
        self._toggle_audio()
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
        pending = any(self.item_state.get(iid) != "Listo" for iid in children)
        self.run_q_btn.config(state="normal" if pending and not self.busy else "disabled")
        state = "disabled" if self.busy else "normal"
        self.remove_q_btn.config(state=state)
        self.clear_q_btn.config(state=state)

    def _add_urls(self, urls: list[str]) -> int:
        known = set(self.item_url.values())
        added = 0
        for url in urls:
            if url in known:
                continue
            iid = self.queue_tree.insert("", "end", values=(url, "Pendiente"))
            self.item_url[iid] = url
            self.item_state[iid] = "Pendiente"
            known.add(url)
            added += 1
        self._update_queue_buttons()
        return added

    def on_add_links(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Añadir enlaces")
        dialog.configure(bg=BG)
        dialog.geometry("580x360")
        dialog.minsize(420, 260)
        dialog.transient(self)
        ttk.Label(dialog, text="Pega uno o varios enlaces, uno por línea.").pack(anchor="w", padx=16, pady=(16, 8))
        text = tk.Text(dialog, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", font=FONT, wrap="none",
                       highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT, padx=8, pady=8,
                       selectbackground=ACCENT, selectforeground="#ffffff")
        text.pack(fill="both", expand=True, padx=16)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=16, pady=14)

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
            self._say(f"{added} enlaces añadidos a la cola." if added else "Esos enlaces ya estaban en la cola.",
                      MUTED)
            dialog.destroy()

        ttk.Button(buttons, text="Cargar .txt...", command=load_file).pack(side="left")
        ttk.Button(buttons, text="Añadir", style="Accent.TButton", command=accept).pack(side="right")
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(side="right", padx=8)
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
        self._update_queue_buttons()

    def on_queue_clear_done(self) -> None:
        if self.busy:
            return
        for iid in list(self.queue_tree.get_children()):
            if self.item_state.get(iid) == "Listo":
                self.queue_tree.delete(iid)
                self.item_state.pop(iid, None)
                self.item_url.pop(iid, None)
        self._update_queue_buttons()

    def on_run_queue(self) -> None:
        if self.busy:
            return
        selection = self.queue_quality.get()
        height = None if selection == BEST else int(selection.rstrip("p"))
        jobs = [{"url": self.item_url[iid], "height": height, "iid": iid}
                for iid in self.queue_tree.get_children() if self.item_state.get(iid) != "Listo"]
        if not jobs:
            return
        for job in jobs:
            self._set_item(job, "En espera")
        self._start_jobs(jobs, "queue")

    def _on_tab_changed(self) -> None:
        if self.notebook.select() == str(self.tab_history):
            self._refresh_history()

    def _refresh_history(self) -> None:
        self.history_entries = storage.load_history()
        self.history_tree.delete(*self.history_tree.get_children())
        for index, entry in enumerate(self.history_entries):
            exists = Path(entry["path"]).exists()
            name = entry.get("title", "") + ("" if exists else "  (archivo no encontrado)")
            kind = "Audio" if entry.get("kind") == "audio" else "Video"
            self.history_tree.insert("", "end", iid=str(index), values=(name, kind, entry.get("date", "")))
        self._update_history_buttons()

    def _selected_history(self) -> dict | None:
        selection = self.history_tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        return self.history_entries[index] if index < len(self.history_entries) else None

    def _update_history_buttons(self) -> None:
        entry = self._selected_history()
        exists = bool(entry) and Path(entry["path"]).exists()
        self.play_btn.config(state="normal" if exists else "disabled")
        self.reveal_btn.config(state="normal" if exists else "disabled")
        self.forget_btn.config(state="normal" if entry else "disabled")

    def on_history_play(self) -> None:
        entry = self._selected_history()
        if not entry or not Path(entry["path"]).exists():
            return
        try:
            os.startfile(entry["path"])
        except OSError as exc:
            messagebox.showerror("Error", f"No se pudo reproducir el archivo: {exc}")

    def on_history_reveal(self) -> None:
        entry = self._selected_history()
        if entry:
            self._reveal(Path(entry["path"]))

    def on_history_remove(self) -> None:
        entry = self._selected_history()
        if entry:
            storage.remove_history(entry["path"])
            self._refresh_history()

    def on_history_clear(self) -> None:
        if self.history_entries and messagebox.askyesno("Historial", "¿Borrar todo el historial?\n\n"
                                                        "Los archivos descargados no se eliminan."):
            storage.clear_history()
            self._refresh_history()

    def check_updates(self, silent: bool) -> None:
        def worker() -> None:
            try:
                self.events.put(("update_found", (check_for_update(), silent)))
            except UpdateError as exc:
                if not silent:
                    self.events.put(("update_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_found(self, data) -> None:
        release, silent = data
        if release is None:
            if not silent:
                messagebox.showinfo("Actualizaciones", f"Ya tienes la última versión ({__version__}).")
            return
        if self.busy:
            if not silent:
                messagebox.showinfo("Actualizaciones", "Termina la operación en curso y vuelve a intentarlo.")
            return
        notes = release.notes[:600] + ("..." if len(release.notes) > 600 else "")
        text = f"Hay una versión nueva: {release.version} (tienes {__version__}).\n\n{notes}\n\n"
        if can_self_update() and release.asset_url:
            if messagebox.askyesno("Actualización disponible", text + "¿Descargar e instalar ahora?"):
                self._start_update(release)
        elif messagebox.askyesno("Actualización disponible", text + "¿Abrir la página de descarga?"):
            webbrowser.open(release.page_url)

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

    def _on_update_error(self, msg: str) -> None:
        messagebox.showerror("Actualizaciones", msg)

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
        info, raw = data
        self._set_thumbnail(raw)
        mins, secs = divmod(info.duration or 0, 60)
        self.info_lbl.config(text=f"{info.title}\n{info.uploader} · {mins}:{secs:02d}", foreground=FG)
        self.heights = info.heights
        self.quality.config(values=[BEST] + [f"{h}p" for h in info.heights])
        self.quality.set(BEST)
        self._set_busy(False)
        self._toggle_audio()

    def _on_error(self, msg: str) -> None:
        if not self.heights:
            self.info_lbl.config(text="Pega o arrastra un enlace y pulsa Buscar.")
        self._bar_hide()
        self._say("")
        self._set_busy(False)
        self._toggle_audio()
        messagebox.showerror("Error", msg)


if __name__ == "__main__":
    App().mainloop()
