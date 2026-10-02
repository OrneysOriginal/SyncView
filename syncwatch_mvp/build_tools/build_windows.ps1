param(
    [string]$VlcDir = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $VlcDir) {
    $VlcDir = Join-Path $env:ProgramFiles "VideoLAN\VLC"
}
if (-not (Test-Path $VlcDir)) {
    throw "Install VLC x64 first, or pass -VlcDir pointing to a full VLC runtime."
}
$VlcDir = (Resolve-Path $VlcDir).Path

if (-not (Test-Path (Join-Path $VlcDir "libvlc.dll"))) {
    throw "Missing libvlc.dll: $VlcDir"
}
if (-not (Test-Path (Join-Path $VlcDir "plugins"))) {
    throw "Missing VLC plugins directory: $VlcDir"
}
if (-not (Test-Path (Join-Path $VlcDir "libvlccore.dll"))) {
    throw "Missing libvlccore.dll: $VlcDir"
}

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code $LASTEXITCODE)."
    }
}

Set-Location $ProjectRoot
if ($Clean) {
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

if (-not (Test-Path ".venv-build\Scripts\python.exe")) {
    py -3.12 -m venv .venv-build
    Assert-NativeSuccess "Creating Python 3.12 environment"
}
$Python = Join-Path $ProjectRoot ".venv-build\Scripts\python.exe"
& $Python -c "import struct, sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 1)"
Assert-NativeSuccess "Checking Python 3.12 x64"
& $Python -m pip install --upgrade pip
Assert-NativeSuccess "Upgrading pip"
& $Python -m pip install -r requirements-build.txt
Assert-NativeSuccess "Installing build dependencies"

$env:SYNCWATCH_VLC_DIR = $VlcDir
& $Python -m PyInstaller --noconfirm --clean packaging\syncwatch.spec
Assert-NativeSuccess "Building SyncWatch"

$Executable = Join-Path $ProjectRoot "dist\SyncWatch\SyncWatch.exe"
if (-not (Test-Path $Executable)) {
    throw "Build did not produce SyncWatch.exe."
}

$Zip = Join-Path $ProjectRoot "dist\SyncWatch-Windows-x64.zip"
Remove-Item $Zip -ErrorAction SilentlyContinue
Compress-Archive -Path "dist\SyncWatch\*" -DestinationPath $Zip
Write-Host "Created: $Zip"
