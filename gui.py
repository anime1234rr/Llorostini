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

from downloader import (AUDIO_FORMATS, VIDEO_CONTAINERS, DownloaderError, default_download_dir, download,
                        fetch_thumbnail, get_info, has_ffmpeg)
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


class App(_Base):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("620x490")
        self.minsize(580, 470)
        self.configure(bg=BG)

        self.events: queue.Queue = queue.Queue()
        self.heights: list[int] = []
        self.last_path: Path | None = None
        self.output_dir = tk.StringVar(value=str(default_download_dir()))
        self.url = tk.StringVar()
        self.audio_only = tk.BooleanVar()
        self.subtitles = tk.BooleanVar()
        self.container = tk.StringVar(value=VIDEO_CONTAINERS[0])
        self.audio_format = tk.StringVar(value=AUDIO_FORMATS[0])
        self.busy = False
        self._thumb_img = None

        self._apply_theme()
        self._build()
        self._enable_drop()
        self._build_menu()
        self._dark_titlebar()
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
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 11))
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
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", FONT)

    def _dark_titlebar(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
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
        pad = {"padx": 12, "pady": 7}
        self.frm = frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=12, pady=10)
        frm.columnconfigure(1, weight=1)

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
                                  wraplength=340, justify="left")
        self.info_lbl.grid(row=0, column=1, sticky="w", padx=(14, 0))

        ttk.Label(frm, text="Calidad").grid(row=2, column=0, sticky="w", **pad)
        self.quality = ttk.Combobox(frm, state="disabled", values=[BEST])
        self.quality.set(BEST)
        self.quality.grid(row=2, column=1, sticky="ew", **pad)
        ttk.Checkbutton(frm, text="Solo audio", variable=self.audio_only,
                        command=self._toggle_audio).grid(row=2, column=2, sticky="w", **pad)

        ttk.Label(frm, text="Formato").grid(row=3, column=0, sticky="w", **pad)
        self.fmt = ttk.Combobox(frm, state="readonly", textvariable=self.container, values=VIDEO_CONTAINERS)
        self.fmt.grid(row=3, column=1, sticky="ew", **pad)
        self.subs_chk = ttk.Checkbutton(frm, text="Subtítulos", variable=self.subtitles)
        self.subs_chk.grid(row=3, column=2, sticky="w", **pad)

        ttk.Label(frm, text="Carpeta").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.output_dir).grid(row=4, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Elegir...", command=self.on_browse).grid(row=4, column=2, sticky="ew", **pad)

        self.dl_btn = ttk.Button(frm, text="Descargar", style="Accent.TButton", command=self.on_download,
                                 state="disabled")
        self.dl_btn.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)

        self.bar = ttk.Progressbar(frm, maximum=100)
        self.bar.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
        self.bar.grid_remove()

        self.status = ttk.Label(frm, text="", wraplength=430, justify="left")
        self.status.grid(row=7, column=0, columnspan=2, sticky="w", **pad)
        self.open_btn = ttk.Button(frm, text="Abrir carpeta", command=self.on_open_folder)
        self.open_btn.grid(row=7, column=2, sticky="ew", **pad)
        self.open_btn.grid_remove()

        if not has_ffmpeg():
            self._say("Aviso: ffmpeg no encontrado; calidad limitada y sin conversión de audio.", RED)

    def _enable_drop(self) -> None:
        if DND_ALL is None:
            return
        for widget in (self, self.frm, self.entry, self.info_lbl):
            try:
                widget.drop_target_register(DND_ALL)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _on_drop(self, event):
        match = re.search(r"https?://[^\s{}\"<>]+", str(event.data))
        if match:
            self.url.set(match.group(0))
            self._say("Enlace recibido. Pulsa Buscar.", MUTED)
        return event.action

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

    def _toggle_audio(self) -> None:
        audio = self.audio_only.get()
        self.quality.config(state="disabled" if audio or not self.heights else "readonly")
        self.subs_chk.config(state="disabled" if audio else "normal")
        if audio:
            self.fmt.config(values=AUDIO_FORMATS, textvariable=self.audio_format)
        else:
            self.fmt.config(values=VIDEO_CONTAINERS, textvariable=self.container)

    def on_browse(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_dir.get())
        if folder:
            self.output_dir.set(folder)

    def on_open_folder(self) -> None:
        try:
            if self.last_path and self.last_path.exists():
                subprocess.Popen(f'explorer /select,"{self.last_path}"')
            else:
                os.startfile(self.output_dir.get())
        except OSError as exc:
            messagebox.showerror("Error", f"No se pudo abrir la carpeta: {exc}")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.search_btn.config(state="disabled" if busy else "normal")
        self.dl_btn.config(state="disabled" if busy or not self.heights else "normal")

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

    def on_download(self) -> None:
        if self.busy:
            return
        sel = self.quality.get()
        height = None if sel == BEST else int(sel.rstrip("p"))
        url, out, audio = self.url.get(), self.output_dir.get(), self.audio_only.get()
        cont, afmt, subs = self.container.get(), self.audio_format.get(), self.subtitles.get()
        self._set_busy(True)
        self.open_btn.grid_remove()
        self._bar_show()
        self._say("Iniciando descarga...", MUTED)
        self._run(lambda: download(url, out, height, audio, lambda d: self.events.put(("progress", d)),
                                   cont, afmt, subs), "done")

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
        self.after(100, self._poll)

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

    def _on_progress(self, d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                self._bar_set(d["downloaded_bytes"] / total * 100)
            self._say(f"Descargando... {d.get('_percent_str', '').strip()}  {d.get('_speed_str', '').strip()}", MUTED)
        elif d["status"] == "finished":
            self._bar_working()
            self._say("Procesando archivo...", MUTED)

    def _on_done(self, path) -> None:
        self.last_path = Path(path)
        self._bar_hide()
        self._say(f"Listo: {self.last_path.name}", GREEN)
        self.open_btn.grid()
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
