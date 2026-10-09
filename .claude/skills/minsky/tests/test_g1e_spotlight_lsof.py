"""Synthetic G1E checks; refuse unverified holders and unbounded Spotlight retries."""

from __future__ import annotations

import ast
import importlib.util
import logging
import os
import subprocess
from pathlib import Path

import pytest

import lifecycle_seal as seal
from lifecycle_register import RegisterError

TESTS = Path(__file__).parent
SUPPORT = ("/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/"
           "Metadata.framework/Versions/A/Support/")
WORKER = SUPPORT + "mdworker_shared"
TARGET = ["+D", "/synthetic/audit"]
PID = 987654321


def _mock_lsof(monkeypatch, outputs: list[str], *, executable: str | None = WORKER,
               status: int = 0, stderr: str = "", legacy: bool = False) -> list[list[str]]:
    calls = []
    scans = 0

    def run(argv, **kwargs):
        nonlocal scans
        calls.append(argv)
        assert kwargs == {"capture_output": True, "text": True}
        if argv[:2] == ["lsof", "-F"]:
            assert argv == ["lsof", "-F", "pan", *TARGET] or (
                legacy and argv == ["lsof", "-F", "p", *TARGET])
            output = outputs[min(scans, len(outputs) - 1)]
            scans += 1
            assert scans <= 4, "unbounded holder re-checks"
            return subprocess.CompletedProcess(argv, status, output, stderr)
        assert argv[:3] == ["lsof", "-a", "-p"], "executable must come from lsof txt, never ps"
        assert argv[4:] == ["-d", "txt", "-F", "n"]
        path = executable if argv[3] == str(PID) else "/usr/bin/other"
        return subprocess.CompletedProcess(argv, 0, "" if path is None else f"n{path}\nn/ignored/library\n", "")

    monkeypatch.setattr(seal.subprocess, "run", run)
    return calls


def _holder(mode: str = "r", *, pid: int = PID) -> str:
    return f"p{pid}\nf3\nar\nn/synthetic/audit/one\nf4\na{mode}\nn/synthetic/audit/two\n"


def _scans(calls: list[list[str]]) -> int:
    return sum(argv[:2] == ["lsof", "-F"] for argv in calls)


def _warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if record.name == "lifecycle_seal" and record.levelno == logging.WARNING]


@pytest.mark.parametrize("executable", [WORKER, SUPPORT + "mds"])
@pytest.mark.parametrize("status", [0, 1])
def test_spotlight_readonly_passes_and_logs(monkeypatch, caplog, executable, status) -> None:
    calls = _mock_lsof(monkeypatch, [_holder()], executable=executable, status=status)
    seal._lsof(TARGET)
    assert _scans(calls) == 1
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert all(value in warnings[0] for value in (
        str(PID), executable, "fds=", "'f': '3'", "'f': '4'", TARGET[-1],
    ))


@pytest.mark.parametrize("mode", ["u", "w", "", " ", None])
def test_unconfirmed_refuses_after_three_rechecks(monkeypatch, caplog, mode) -> None:
    output = _holder(mode or "")
    if mode is None:
        output = output.replace("f4\na\n", "f4\n")
    calls = _mock_lsof(monkeypatch, [output])
    with pytest.raises(RegisterError, match="Spotlight holder not confirmed read-only after 3 re-checks"):
        seal._lsof(TARGET)
    assert _scans(calls) == 4
    warnings = _warnings(caplog)
    assert len(warnings) == 3
    assert all("re-check" in line and TARGET[-1] in line for line in warnings)


def test_second_recheck_becomes_readonly(monkeypatch, caplog) -> None:
    calls = _mock_lsof(monkeypatch, [_holder("u"), _holder("w"), _holder()])
    seal._lsof(TARGET)
    assert _scans(calls) == 3
    assert len(_warnings(caplog)) == 3


def test_similar_executable_refuses_immediately(monkeypatch, caplog) -> None:
    calls = _mock_lsof(monkeypatch, [_holder()], executable=SUPPORT + "mdworker")
    with pytest.raises(RegisterError, match="refused live writer"):
        seal._lsof(TARGET)
    assert _scans(calls) == 1
    assert not _warnings(caplog), f"unexpected warnings among {len(caplog.records)} log records"


def test_missing_txt_is_unconfirmed(monkeypatch, caplog) -> None:
    calls = _mock_lsof(monkeypatch, [_holder()], executable=None)
    with pytest.raises(RegisterError, match="Spotlight holder not confirmed read-only after 3 re-checks"):
        seal._lsof(TARGET)
    assert _scans(calls) == 4
    assert len(_warnings(caplog)) == 3


def test_other_alongside_spotlight_refuses_without_recheck(monkeypatch, caplog) -> None:
    calls = _mock_lsof(monkeypatch, [_holder() + _holder(pid=PID + 1)])
    with pytest.raises(RegisterError, match="refused live writer"):
        seal._lsof(TARGET)
    assert _scans(calls) == 1
    assert not any("re-check" in line for line in _warnings(caplog)), calls


@pytest.mark.parametrize("status,stderr", [(2, ""), (0, "problem"), (1, "problem")])
def test_status_and_stderr_regression(monkeypatch, status, stderr) -> None:
    calls = _mock_lsof(monkeypatch, [""], status=status, stderr=stderr, legacy=True)
    with pytest.raises(RegisterError, match="refused lsof"):
        seal._lsof(TARGET)
    assert _scans(calls) == 1


def test_no_holder_regression(monkeypatch) -> None:
    calls = _mock_lsof(monkeypatch, [""], status=1, legacy=True)
    seal._lsof(TARGET)
    assert _scans(calls) == 1


@pytest.mark.parametrize("allow_self", [True, False])
def test_self_regression(monkeypatch, allow_self) -> None:
    calls = _mock_lsof(monkeypatch, [_holder(pid=os.getpid())], legacy=True)
    if allow_self:
        seal._lsof(TARGET, allow_self=True)
    else:
        with pytest.raises(RegisterError, match="refused live writer"):
            seal._lsof(TARGET)
    assert _scans(calls) == 1


@pytest.mark.parametrize("output", ["", "n/orphan\n", f"p{PID}\nf3\nar\nzbad\n",
                                    f"p{PID}\nf3\nar\nau\n", "pnot-a-pid\n"])
def test_unparseable_record_refuses(monkeypatch, output) -> None:
    calls = _mock_lsof(monkeypatch, [output])
    with pytest.raises(RegisterError, match="refused.*lsof"):
        seal._lsof(TARGET)
    assert _scans(calls) == 1


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_root_noindex(monkeypatch) -> None:
    conftest = _load(TESTS / "conftest.py", "g1e_conftest")
    monkeypatch.delenv("MINSKY_TEST_TMP", raising=False)
    assert conftest._resolve_test_tmp() == (Path.home() / "Library/Application Support/MARS/"
                                           "minsky-test-tmp/default.noindex")


def test_explicit_root_unchanged(monkeypatch, tmp_path: Path) -> None:
    conftest = _load(TESTS / "conftest.py", "g1e_conftest_explicit")
    root = tmp_path / "explicit"
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))
    assert conftest._resolve_test_tmp() == root


def test_matrix_temp_creation_noindex(tmp_path: Path) -> None:
    # Execute the creation expression itself, without launching a matrix or mocking its IO.
    path = TESTS / "tools/kill_matrix.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "tempfile"
             and node.func.attr in ("TemporaryDirectory", "mkdtemp")]
    assert len(sites) == 1, "review every matrix temporary-directory creation site"
    import tempfile
    expression = ast.Expression(sites[0])
    with eval(compile(expression, str(path), "eval"),
              {"tempfile": tempfile, "temp_root": tmp_path}) as directory:
        assert Path(directory).is_dir()
        assert Path(directory).name.endswith(".noindex")
