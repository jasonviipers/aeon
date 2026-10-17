"""`python -m aeon` with a Python that lacks AEON's packages: switch to the project's .venv, or explain."""
import importlib
import sys
from pathlib import Path

import pytest

import aeon.__main__ as entry


def fake_venv(root: Path, rel: str = "Scripts/python.exe") -> Path:
    p = root / ".venv" / rel
    p.parent.mkdir(parents=True)
    p.write_text("")
    return p


def test_all_dependencies_are_present_in_the_test_environment():
    assert entry._missing_dependency() is None


def test_a_missing_package_is_named(monkeypatch):
    real = importlib.import_module

    def fake(name, *a, **k):
        if name == "textual":
            raise ModuleNotFoundError("No module named 'textual'", name="textual")
        return real(name, *a, **k)
    monkeypatch.setattr(entry.importlib, "import_module", fake)
    assert entry._missing_dependency() == "textual"


def test_venv_python_is_found_on_windows_and_unix_layouts(tmp_path):
    assert entry._venv_python(tmp_path) is None
    win = fake_venv(tmp_path / "w", "Scripts/python.exe")
    assert entry._venv_python(tmp_path / "w") == win
    nix = fake_venv(tmp_path / "u", "bin/python")
    assert entry._venv_python(tmp_path / "u") == nix


def test_relaunch_only_into_a_different_interpreter_and_only_once(tmp_path):
    venv = fake_venv(tmp_path)
    other = str(tmp_path / "system" / "python.exe")
    assert entry._relaunch_target(tmp_path, other, already=False) == venv        # system python -> use the venv
    assert entry._relaunch_target(tmp_path, str(venv), already=False) is None    # already the venv: nothing to switch to
    assert entry._relaunch_target(tmp_path, other, already=True) is None         # never loop
    assert entry._relaunch_target(tmp_path / "nowhere", other, already=False) is None   # no venv at all


def test_it_reruns_itself_with_the_venv_and_passes_the_arguments_through(tmp_path, monkeypatch, capsys):
    venv = fake_venv(tmp_path)
    calls = {}
    monkeypatch.setattr(entry, "_missing_dependency", lambda: "httpx")
    monkeypatch.setattr(entry, "_relaunch_target", lambda root, current, already: venv)
    monkeypatch.setattr(entry.subprocess, "call", lambda args, **kw: calls.update(args=args, kw=kw) or 0)
    monkeypatch.setattr(sys, "argv", ["__main__.py", "--check", "--mode", "paper"])
    monkeypatch.delenv("AEON_RELAUNCHED", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        entry._ensure_dependencies()
    assert exit_info.value.code == 0
    assert calls["args"] == [str(venv), "-m", "aeon", "--check", "--mode", "paper"]
    assert calls["kw"]["env"]["AEON_RELAUNCHED"] == "1"
    assert "no 'httpx'" in capsys.readouterr().err


def test_the_child_exit_code_is_passed_back(monkeypatch):
    monkeypatch.setattr(entry, "_missing_dependency", lambda: "httpx")
    monkeypatch.setattr(entry, "_relaunch_target", lambda *a: Path("python"))
    monkeypatch.setattr(entry.subprocess, "call", lambda *a, **k: 3)
    monkeypatch.setattr(sys, "argv", ["__main__.py"])
    with pytest.raises(SystemExit) as exit_info:
        entry._ensure_dependencies()
    assert exit_info.value.code == 3


def test_without_a_venv_the_fix_is_explained_not_a_traceback(monkeypatch):
    monkeypatch.setattr(entry, "_missing_dependency", lambda: "httpx")
    monkeypatch.setattr(entry, "_relaunch_target", lambda *a: None)
    with pytest.raises(SystemExit) as exit_info:
        entry._ensure_dependencies()
    message = str(exit_info.value.code)
    assert "'httpx' is not installed" in message and "aeon.bat" in message and "pip install -e ." in message


def test_nothing_happens_when_everything_is_installed(monkeypatch):
    monkeypatch.setattr(entry.subprocess, "call", lambda *a, **k: pytest.fail("must not relaunch"))
    entry._ensure_dependencies()
