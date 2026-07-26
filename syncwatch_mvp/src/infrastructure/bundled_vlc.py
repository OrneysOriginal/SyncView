from __future__ import annotations

import os
import sys
from pathlib import Path


def application_root() -> Path:
    """Return the writable/readable root used by source and PyInstaller builds."""
    if getattr(sys, "frozen", False):
        # In one-folder mode resources live beside the executable. In one-file
        # mode PyInstaller exposes the extraction directory as _MEIPASS.
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def configure_bundled_vlc() -> Path | None:
    """Configure dynamic-library paths for a VLC runtime bundled with SyncWatch.

    Expected packaged layouts:
      Windows: <app>/vlc/libvlc.dll and <app>/vlc/plugins/
      macOS:   <app>/vlc/lib/libvlc.dylib and <app>/vlc/plugins/

    Returns the detected VLC directory, or ``None`` when no bundled runtime is
    present. A system VLC can still be used for source-development runs.
    """
    root = application_root()
    candidates = [
        root / "vlc",
        root / "Resources" / "vlc",
        Path(sys.executable).resolve().parent / "vlc",
        Path(sys.executable).resolve().parent.parent / "Resources" / "vlc",
    ]
    vlc_dir = next((path for path in candidates if path.exists()), None)
    if vlc_dir is None:
        return None

    plugin_candidates = [vlc_dir / "plugins", vlc_dir / "lib" / "vlc" / "plugins"]
    plugin_dir = next((path for path in plugin_candidates if path.exists()), None)
    if plugin_dir:
        os.environ["VLC_PLUGIN_PATH"] = str(plugin_dir)

    if sys.platform == "win32":
        os.environ["PATH"] = str(vlc_dir) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            # Keep the handle alive for the lifetime of the process.
            global _DLL_DIRECTORY_HANDLE
            _DLL_DIRECTORY_HANDLE = os.add_dll_directory(str(vlc_dir))
    elif sys.platform == "darwin":
        library_dirs = [vlc_dir, vlc_dir / "lib"]
        existing = os.environ.get("DYLD_LIBRARY_PATH", "")
        os.environ["DYLD_LIBRARY_PATH"] = os.pathsep.join(
            [*(str(path) for path in library_dirs if path.exists()), existing]
        ).strip(os.pathsep)

    return vlc_dir


_DLL_DIRECTORY_HANDLE = None
