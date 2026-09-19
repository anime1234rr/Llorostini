$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --quiet --upgrade -r requirements.txt pyinstaller

$ffmpegDir = Join-Path $PSScriptRoot "build\ffmpeg"
$ffmpeg = Get-ChildItem $ffmpegDir -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ffmpeg) {
    Write-Host "Descargando ffmpeg..."
    New-Item -ItemType Directory -Force $ffmpegDir | Out-Null
    $zip = Join-Path $ffmpegDir "ffmpeg.zip"
    Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath $ffmpegDir -Force
    Remove-Item $zip
    $ffmpeg = Get-ChildItem $ffmpegDir -Recurse -Filter ffmpeg.exe | Select-Object -First 1
}

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name LLorostini `
    --collect-submodules yt_dlp `
    --collect-all tkinterdnd2 `
    --add-binary "$($ffmpeg.FullName);." `
    gui.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallo" }

$exe = Join-Path $PSScriptRoot "dist\LLorostini.exe"
Write-Host "Listo: $exe"
