from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from version import APP_NAME, __version__
from downloader import AUDIO_FORMATS, VIDEO_CONTAINERS, DownloaderError, download, get_info, has_ffmpeg

BEST = "Mejor disponible"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("560x400")
        self.minsize(520, 380)

        self.events: queue.Queue = queue.Queue()
        self.heights: list[int] = []
        self.output_dir = tk.StringVar(value=str(Path.cwd() / "descargas"))
        self.url = tk.StringVar()
        self.audio_only = tk.BooleanVar()
        self.container = tk.StringVar(value=VIDEO_CONTAINERS[0])
        self.audio_format = tk.StringVar(value=AUDIO_FORMATS[0])
        self.busy = False

        self._build()
        self.after(100, self._poll)

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="URL:").grid(row=0, column=0, sticky="w", **pad)
        entry = ttk.Entry(frm, textvariable=self.url)
        entry.grid(row=0, column=1, sticky="ew", **pad)
        entry.bind("<Return>", lambda _e: self.on_search())
        entry.focus()
        self.search_btn = ttk.Button(frm, text="Buscar", command=self.on_search)
        self.search_btn.grid(row=0, column=2, **pad)

        self.info_lbl = ttk.Label(frm, text="Pega un enlace y pulsa Buscar.", wraplength=500, justify="left")
        self.info_lbl.grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(frm, text="Calidad:").grid(row=2, column=0, sticky="w", **pad)
        self.quality = ttk.Combobox(frm, state="disabled", values=[BEST])
        self.quality.set(BEST)
        self.quality.grid(row=2, column=1, sticky="ew", **pad)
        ttk.Checkbutton(frm, text="Solo audio", variable=self.audio_only,
                        command=self._toggle_audio).grid(row=2, column=2, **pad)

        ttk.Label(frm, text="Formato:").grid(row=3, column=0, sticky="w", **pad)
        self.fmt = ttk.Combobox(frm, state="readonly", textvariable=self.container, values=VIDEO_CONTAINERS)
        self.fmt.grid(row=3, column=1, sticky="ew", **pad)

        ttk.Label(frm, text="Carpeta:").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.output_dir).grid(row=4, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Elegir...", command=self.on_browse).grid(row=4, column=2, **pad)

        self.dl_btn = ttk.Button(frm, text="Descargar", command=self.on_download, state="disabled")
        self.dl_btn.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)

        self.bar = ttk.Progressbar(frm, maximum=100)
        self.bar.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
        self.status = ttk.Label(frm, text="", wraplength=500, justify="left")
        self.status.grid(row=7, column=0, columnspan=3, sticky="w", **pad)

        if not has_ffmpeg():
            self.status.config(text="Aviso: ffmpeg no encontrado; calidad limitada y sin mp3.")


    def _toggle_audio(self) -> None:
        state = "disabled" if self.audio_only.get() or not self.heights else "readonly"
        self.quality.config(state=state)
        if self.audio_only.get():
            self.fmt.config(values=AUDIO_FORMATS, textvariable=self.audio_format)
        else:
            self.fmt.config(values=VIDEO_CONTAINERS, textvariable=self.container)

    def on_browse(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_dir.get())
        if folder:
            self.output_dir.set(folder)

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
        self.info_lbl.config(text="Buscando...")
        self.bar.config(value=0)
        url = self.url.get()
        self._run(lambda: get_info(url), "info")

    def on_download(self) -> None:
        if self.busy:
            return
        sel = self.quality.get()
        height = None if sel == BEST else int(sel.rstrip("p"))
        url, out, audio = self.url.get(), self.output_dir.get(), self.audio_only.get()
        self._set_busy(True)
        self.bar.config(value=0)
        self.status.config(text="Iniciando descarga...")
        cont, afmt = self.container.get(), self.audio_format.get()
        self._run(lambda: download(url, out, height, audio, lambda d: self.events.put(("progress", d)), cont, afmt), "done")


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

    def _on_info(self, info) -> None:
        mins, secs = divmod(info.duration or 0, 60)
        self.info_lbl.config(text=f"{info.title}\n{info.uploader} · {mins}:{secs:02d}")
        self.heights = info.heights
        self.quality.config(values=[BEST] + [f"{h}p" for h in info.heights])
        self.quality.set(BEST)
        self._set_busy(False)
        self._toggle_audio()

    def _on_progress(self, d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                self.bar.config(value=d["downloaded_bytes"] / total * 100)
            self.status.config(text=f"Descargando... {d.get('_percent_str', '').strip()}  {d.get('_speed_str', '').strip()}")
        elif d["status"] == "finished":
            self.bar.config(value=100)
            self.status.config(text="Procesando archivo...")

    def _on_done(self, path) -> None:
        self.status.config(text=f"Listo: {path}")
        self._set_busy(False)
        self._toggle_audio()

    def _on_error(self, msg: str) -> None:
        self.info_lbl.config(text="") if not self.heights else None
        self.status.config(text="")
        self._set_busy(False)
        self._toggle_audio()
        messagebox.showerror("Error", msg)


if __name__ == "__main__":
    App().mainloop()
