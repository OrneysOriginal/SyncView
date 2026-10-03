"""Check packaging compatibility and visible errors without requiring Qt/VLC."""
from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

PROJECT = Path(__file__).parents[1]


def test_spec_uses_current_pyinstaller_api_and_bundles_full_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "vlc"
    runtime.mkdir()
    for name in ("libvlc.dll", "libvlccore.dll"):
        (runtime / name).touch()
    (runtime / "plugins").mkdir()
    monkeypatch.setenv("SYNCWATCH_VLC_DIR", str(runtime))
    monkeypatch.setattr(sys, "platform", "win32")
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = lambda package: [package]
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    # Current Analysis has no zipped_data/zipfiles attributes.
    analysis = SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[("runtime", "vlc")])
    constructors = {"Analysis": Mock(return_value=analysis), "PYZ": Mock(),
                    "EXE": Mock(), "COLLECT": Mock()}
    spec = PROJECT / "packaging/syncwatch.spec"
    runpy.run_path(str(spec), init_globals={"SPEC": str(spec), **constructors})
    assert constructors["EXE"].call_args.kwargs["console"] is False
    assert constructors["Analysis"].call_args.kwargs["datas"] == [(str(runtime), "vlc")]
    assert constructors["COLLECT"].call_args.args[-1] is analysis.datas


@pytest.mark.parametrize("smoke_test", [False, True])
def test_entrypoint_preserves_exit_code_and_waits_for_startup_worker(monkeypatch, smoke_test):
    widgets = ModuleType("PySide6.QtWidgets")
    widgets.QApplication = Mock()
    widgets.QApplication.return_value.exec.return_value = 1
    startup = ModuleType("src.ui.startup")
    startup.StartupController = Mock()
    monkeypatch.setitem(sys.modules, "PySide6", ModuleType("PySide6"))
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", widgets)
    monkeypatch.setitem(sys.modules, "src.ui.startup", startup)
    spec = importlib.util.spec_from_file_location("packaging_entrypoint", PROJECT / "src/main.py")
    entrypoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entrypoint)
    log_path = Path("startup.log")
    monkeypatch.setattr(entrypoint, "configure_logging", Mock(return_value=log_path))
    monkeypatch.setattr(sys, "argv", ["SyncWatch.exe"] + (["--smoke-test"] if smoke_test else []))
    assert entrypoint.main() == 1
    startup.StartupController.assert_called_once_with(
        widgets.QApplication.return_value, log_path, smoke_test=smoke_test,
    )
    startup.StartupController.return_value.worker.wait.assert_called_once()
