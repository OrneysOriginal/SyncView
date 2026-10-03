from __future__ import annotations

import logging
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

    Returns the detected VLC directory. Source runs without a local runtime
    return ``None`` and may use system VLC; VLC_HOME is also supported.
    Frozen applications must contain their runtime. Windows sets explicit
    python-vlc library/module paths so system VLC cannot override the bundle.
    """
    root = application_root()
    candidates = [
        root / "vlc",
        root / "Resources" / "vlc",
        Path(sys.executable).resolve().parent / "vlc",
        Path(sys.executable).resolve().parent / "_internal" / "vlc",
        Path(sys.executable).resolve().parent.parent / "Resources" / "vlc",
    ]
    if not getattr(sys, "frozen", False) and os.environ.get("VLC_HOME"):
        candidates.append(Path(os.environ["VLC_HOME"]))
    vlc_dir = next((path for path in candidates if path.is_dir()), None)
    if vlc_dir is None:
        if getattr(sys, "frozen", False):
            raise FileNotFoundError(
                "Не найден VLC внутри приложения. Распакуйте весь архив, включая "
                "папку _internal/vlc, и запускайте SyncWatch.exe из этой папки."
            )
        return None
    vlc_dir = vlc_dir.resolve()

    plugin_candidates = [vlc_dir / "plugins", vlc_dir / "lib" / "vlc" / "plugins"]
    plugin_dir = next((path for path in plugin_candidates if path.is_dir()), None)
    if plugin_dir:
        os.environ["VLC_PLUGIN_PATH"] = str(plugin_dir)

    if sys.platform == "win32":
        for name in ("libvlc.dll", "libvlccore.dll"):
            if not (vlc_dir / name).is_file():
                raise FileNotFoundError(f"Неполный комплект VLC: отсутствует {vlc_dir / name}")
        if plugin_dir is None or not any(path.is_file() for path in plugin_dir.rglob("*.dll")):
            raise FileNotFoundError(
                f"В {vlc_dir} не найдены DLL плагинов VLC. Нужна полная папка plugins."
            )
        # PATH alone doesn't guarantee that python-vlc loads this DLL. Its
        # automatic lookup can select a different installed VLC via registry.
        os.environ["PYTHON_VLC_LIB_PATH"] = str(vlc_dir / "libvlc.dll")
        os.environ["PYTHON_VLC_MODULE_PATH"] = str(plugin_dir)
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

    logging.info("VLC runtime: %s; plugins: %s", vlc_dir, plugin_dir)
    return vlc_dir


_DLL_DIRECTORY_HANDLE = None
