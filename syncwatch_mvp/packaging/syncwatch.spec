# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

project = Path(SPEC).resolve().parents[1]
vlc_source_raw = os.environ.get("SYNCWATCH_VLC_DIR", "").strip()
if not vlc_source_raw:
    raise SystemExit(
        "SYNCWATCH_VLC_DIR is not set. Point it to a prepared VLC runtime directory."
    )
vlc_source = Path(vlc_source_raw).resolve()
if not vlc_source.exists():
    raise SystemExit(f"VLC runtime directory does not exist: {vlc_source}")
if sys.platform == "win32":
    for name in ("libvlc.dll", "libvlccore.dll", "plugins"):
        if not (vlc_source / name).exists():
            raise SystemExit(f"Incomplete VLC runtime: missing {name}")

# Bundle the complete runtime. VLC discovers codecs and outputs through plugins,
# so copying only libvlc is insufficient.
datas = [(str(vlc_source), "vlc")]
hiddenimports = collect_submodules("websockets")

a = Analysis(
    [str(project / "src" / "main.py")],
    pathex=[str(project)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SyncWatch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SyncWatch",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="SyncWatch.app",
        icon=None,
        bundle_identifier="app.syncwatch.desktop",
        info_plist={
            "NSHighResolutionCapable": True,
            "NSLocalNetworkUsageDescription": (
                "SyncWatch использует локальную сеть для синхронизации просмотра видео."
            ),
        },
    )
