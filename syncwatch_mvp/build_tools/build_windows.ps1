param(
    [Parameter(Mandatory=$true)]
    [string]$VlcDir,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VlcDir = (Resolve-Path $VlcDir).Path

if (-not (Test-Path (Join-Path $VlcDir "libvlc.dll"))) {
    throw "В каталоге VLC не найден libvlc.dll: $VlcDir"
}
if (-not (Test-Path (Join-Path $VlcDir "plugins"))) {
    throw "В каталоге VLC не найден каталог plugins: $VlcDir"
}

Set-Location $ProjectRoot
if ($Clean) {
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

if (-not (Test-Path ".venv")) {
    py -3.12 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt

$env:SYNCWATCH_VLC_DIR = $VlcDir
& .\.venv\Scripts\pyinstaller.exe --noconfirm --clean packaging\syncwatch.spec

$Zip = Join-Path $ProjectRoot "dist\SyncWatch-Windows-x64.zip"
Remove-Item $Zip -ErrorAction SilentlyContinue
Compress-Archive -Path "dist\SyncWatch\*" -DestinationPath $Zip
Write-Host "Готово: $Zip"
