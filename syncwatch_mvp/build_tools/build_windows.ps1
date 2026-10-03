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

$env:VLC_HOME = $VlcDir
& $Python -c "from src.infrastructure.bundled_vlc import configure_bundled_vlc; configure_bundled_vlc(); import vlc; instance = vlc.Instance('--no-video-title-show'); assert instance is not None, 'VLC initialization failed: check matching DLLs and plugins'; instance.release()"
Assert-NativeSuccess "Initializing source VLC runtime"

& $Python -m ruff check --no-cache src tests
Assert-NativeSuccess "Checking source code"
& $Python -m pytest -q -p no:cacheprovider
Assert-NativeSuccess "Running regression and integration tests"

$env:SYNCWATCH_VLC_DIR = $VlcDir
# Keep incomplete output out of dist. An EXE can exist before COLLECT finishes
# copying Qt, VLC and its plugins; only publish a verified, complete package.
$StagingRoot = Join-Path $ProjectRoot "build\windows-package"
& $Python -m PyInstaller --noconfirm --clean --distpath $StagingRoot packaging\syncwatch.spec
Assert-NativeSuccess "Building SyncWatch"

$StagedApp = Join-Path $StagingRoot "SyncWatch"
$Executable = Join-Path $StagedApp "SyncWatch.exe"
if (-not (Test-Path $Executable)) {
    throw "Build did not produce SyncWatch.exe."
}

$Process = Start-Process $Executable -ArgumentList "--smoke-test" -PassThru
if (-not $Process.WaitForExit(60000)) {
    Stop-Process -Id $Process.Id -Force
    throw "Packaged application did not exit within 60 seconds. Check the startup log."
}
if ($Process.ExitCode -ne 0) {
    throw "Packaged startup failed. Check $env:USERPROFILE\.syncwatch\logs\syncwatch.log"
}

$StagedZip = Join-Path $StagingRoot "SyncWatch-Windows-x64.zip"
Compress-Archive -Path (Join-Path $StagedApp "*") -DestinationPath $StagedZip -Force

$DistRoot = Join-Path $ProjectRoot "dist"
New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
$FinalApp = Join-Path $DistRoot "SyncWatch"
if (Test-Path $FinalApp) {
    Remove-Item -LiteralPath $FinalApp -Recurse -Force
}
Move-Item -LiteralPath $StagedApp -Destination $FinalApp
$Zip = Join-Path $DistRoot "SyncWatch-Windows-x64.zip"
Move-Item -LiteralPath $StagedZip -Destination $Zip -Force
Write-Host "Created: $Zip"
