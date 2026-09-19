from __future__ import annotations

import argparse
import sys

from downloader import AUDIO_FORMATS, VIDEO_CONTAINERS, DownloaderError, VideoInfo, download, get_info, has_ffmpeg


def show_progress(d: dict) -> None:
    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        pct = f"{d['downloaded_bytes'] / total * 100:5.1f}%" if total else "  ...  "
        speed = d.get("_speed_str", "").strip()
        print(f"\r  Descargando {pct}  {speed}   ", end="", flush=True)
    elif d["status"] == "finished":
        print("\r  Descarga completa, procesando...           ")


def fmt_duration(seconds) -> str:
    if not seconds:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ask_quality(info: VideoInfo) -> tuple[int | None, bool]:
    print("\nCalidad disponible:")
    print("  0) Mejor disponible (video + audio)")
    for i, h in enumerate(info.heights, 1):
        print(f"  {i}) {h}p")
    audio_opt = len(info.heights) + 1
    print(f"  {audio_opt}) Solo audio")
    while True:
        choice = input("Elige una opción [0]: ").strip() or "0"
        if choice.isdigit() and 0 <= int(choice) <= audio_opt:
            n = int(choice)
            if n == 0:
                return None, False
            if n == audio_opt:
                return None, True
            return info.heights[n - 1], False
        print("Opción no válida.")


def ask_choice(prompt: str, options: tuple[str, ...]) -> str:
    print(f"\n{prompt}: " + ", ".join(f"{i}) {o}" for i, o in enumerate(options, 1)))
    while True:
        choice = input("Elige una opción [1]: ").strip() or "1"
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        print("Opción no válida.")


def main() -> int:
    parser = argparse.ArgumentParser(prog="LLorostini", description="LLorostini - descargador de videos de YouTube (yt-dlp)")
    parser.add_argument("url", nargs="?", help="URL del video")
    parser.add_argument("-q", "--quality", type=int, help="altura máxima, ej. 720")
    parser.add_argument("-a", "--audio", action="store_true", help="solo audio")
    parser.add_argument("-c", "--container", choices=VIDEO_CONTAINERS, help="contenedor de video (mp4 por defecto)")
    parser.add_argument("-f", "--audio-format", choices=AUDIO_FORMATS, help="formato de audio (mp3 por defecto)")
    parser.add_argument("-o", "--output", default="descargas", help="carpeta de salida")
    args = parser.parse_args()

    if not has_ffmpeg():
        print("Aviso: ffmpeg no está instalado. Se descargará un solo archivo (calidad limitada, sin mp3).\n")

    try:
        interactive = args.url is None
        url = args.url or input("URL del video: ").strip()
        info = get_info(url)
        print(f"\nTítulo: {info.title}\nCanal:  {info.uploader}\nDuración: {fmt_duration(info.duration)}")

        if interactive and args.quality is None and not args.audio:
            height, audio_only = ask_quality(info)
        else:
            height, audio_only = args.quality, args.audio

        container, audio_format = args.container, args.audio_format
        if interactive and args.quality is None and not args.audio:
            if audio_only:
                audio_format = audio_format or ask_choice("Formato de audio", AUDIO_FORMATS)
            else:
                container = container or ask_choice("Contenedor", VIDEO_CONTAINERS)

        path = download(url, args.output, height, audio_only, show_progress,
                        container or "mp4", audio_format or "mp3")
        print(f"\nListo: {path}")
        return 0
    except DownloaderError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
