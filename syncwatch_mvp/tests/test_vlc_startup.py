"""Regression checks for packaged VLC selection and null-instance startup."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from src.infrastructure import bundled_vlc


def create_runtime(root):
    runtime = root / "vlc"
    (runtime / "plugins/access").mkdir(parents=True)
    (runtime / "libvlc.dll").touch()
    (runtime / "libvlccore.dll").touch()
    (runtime / "plugins/access/libfilesystem_plugin.dll").touch()
    return runtime


def test_windows_bundled_runtime_overrides_external_vlc(tmp_path, monkeypatch):
    runtime = create_runtime(tmp_path)
    monkeypatch.setattr(bundled_vlc, "application_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "SyncWatch.exe"))
    for name in ("VLC_PLUGIN_PATH", "PYTHON_VLC_LIB_PATH", "PYTHON_VLC_MODULE_PATH", "PATH"):
        monkeypatch.setenv(name, "external-vlc")
    add_directory = Mock()
    monkeypatch.setattr(os, "add_dll_directory", add_directory, raising=False)
    monkeypatch.setattr(bundled_vlc, "_DLL_DIRECTORY_HANDLE", None)
    assert bundled_vlc.configure_bundled_vlc() == runtime
    assert os.environ["PYTHON_VLC_LIB_PATH"] == str(runtime / "libvlc.dll")
    assert os.environ["PYTHON_VLC_MODULE_PATH"] == str(runtime / "plugins")
    assert os.environ["VLC_PLUGIN_PATH"] == str(runtime / "plugins")
    add_directory.assert_called_once_with(str(runtime))


@pytest.mark.parametrize("missing", ["libvlc.dll", "libvlccore.dll", "plugins/access/libfilesystem_plugin.dll"])
def test_windows_rejects_incomplete_runtime(tmp_path, monkeypatch, missing):
    runtime = create_runtime(tmp_path)
    (runtime / missing).unlink()
    monkeypatch.setattr(bundled_vlc, "application_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "SyncWatch.exe"))
    monkeypatch.setenv("VLC_PLUGIN_PATH", "previous")
    with pytest.raises(FileNotFoundError):
        bundled_vlc.configure_bundled_vlc()


def test_frozen_app_without_runtime_does_not_fall_back_to_installed_vlc(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled_vlc, "application_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "SyncWatch.exe"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("VLC_HOME", str(create_runtime(tmp_path / "installed")))
    with pytest.raises(FileNotFoundError, match="_internal/vlc"):
        bundled_vlc.configure_bundled_vlc()


@pytest.mark.parametrize("stage", ["instance", "player"])
def test_null_vlc_object_raises_actionable_error(monkeypatch, stage):
    vlc = ModuleType("vlc")
    instance = Mock()
    instance.media_player_new.return_value = None
    vlc.Instance = Mock(return_value=None if stage == "instance" else instance)
    vlc.libvlc_errmsg = Mock(return_value=b"no plugins found")
    widgets = ModuleType("PySide6.QtWidgets")
    widgets.QWidget = object
    monkeypatch.setitem(sys.modules, "vlc", vlc)
    monkeypatch.setitem(sys.modules, "PySide6", ModuleType("PySide6"))
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", widgets)
    path = Path(__file__).parents[1] / "src/player/vlc_player.py"
    spec = importlib.util.spec_from_file_location("vlc_startup_regression", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = "no plugins found" if stage == "instance" else "не смог создать медиаплеер"
    with pytest.raises(RuntimeError, match=expected):
        module.VlcPlayer(Mock())
    if stage == "player":
        instance.release.assert_called_once()
