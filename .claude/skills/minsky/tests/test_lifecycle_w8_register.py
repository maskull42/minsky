"""W8 register, lock, census and hook mechanics using KB-sized isolated fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import lifecycle_register as lr
from test_hardening import SCRIPTS, load_script, register_round

lifecycle = load_script("w8_lifecycle", "minsky-lifecycle.py")
HOOK = SCRIPTS.parents[3] / "scripts/git-hooks/pre-commit"
TSVS = (lr.REGISTER_PATH, Path("documentation/minsky_store_index.tsv"),
        Path("documentation/minsky_audits_db_freezes.tsv"))


def _git(repo: Path, *args: str, env: dict | None = None, check: bool = True):
    # T16 explicitly requires Git writes, confined to fixture repositories under --basetemp.
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": os.devnull, **(env or {})}
    for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        environment.pop(key, None)
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), "-c", "commit.gpgsign=false",
         "-c", "user.name=Minsky fixture", "-c", "user.email=fixture@invalid", *args],
        env=environment, text=True, capture_output=True,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    for name in ("MARS_ALLOW_RAW_AUDIT", "MARS_ALLOW_TSV_CORRECTION", "MARS_ALLOW_TSV_CORRECTION_REASON"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", "fixture")
    return root


def _row(audit_id: str = "fixture", **values) -> dict:
    return {**dict.fromkeys(lr.COLUMNS, ""), "audit_id": audit_id, "event": "census",
            "state_after": "modern", **values}


def _register_path(repo: Path) -> Path:
    path = repo / lr.REGISTER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_register_header_mismatch_refused(repo: Path) -> None:
    _register_path(repo).write_text("wrong\n", encoding="utf-8")
    with pytest.raises(lr.RegisterError, match="line 1.*header mismatch"):
        lr.read_register(repo)
    with pytest.raises(lr.RegisterError, match="line 1"):
        lr.append_event(repo, _row(), lock=None)


def test_register_outside_repo_symlink_refused(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "external-register.tsv"
    outside.write_text(lr.HEADER)
    _register_path(repo).symlink_to(outside)
    with pytest.raises(lr.RegisterError, match="register.*outside repo"):
        lr.read_register(repo)
    with pytest.raises(lr.RegisterError, match="register.*outside repo"):
        lr.append_event(repo, _row(), lock=None)
    with pytest.raises(lr.RegisterError, match="register.*outside repo"):
        lr.append_correction(repo, malformed_line=None, reason="reviewed", by="actor")
    assert outside.read_text() == lr.HEADER


def test_register_partial_line_correction_preserves_bytes(repo: Path) -> None:
    path = _register_path(repo)
    original = lr.HEADER.encode() + b"partial\tlast\tline"
    path.write_bytes(original)
    with pytest.raises(lr.RegisterError, match="line 2"):
        lr.append_event(repo, _row(), lock=None)
    assert path.read_bytes() == original
    assert lr.append_correction(repo, malformed_line=2, reason="interrupted write", by="fixture actor") == 3
    assert path.read_bytes().startswith(original + b"\n")
    assert lr.read_register(repo).acknowledged_lines == (2,)
    assert lr.append_event(repo, _row(), lock=None) == 4
    assert lr.read_register(repo).rows[-1]["event"] == "census"


@pytest.mark.parametrize("field,value", [("note", "bad\tvalue"), ("actor", "bad\nvalue"),
                                        ("note", "bad\rvalue"), ("utc", "bad\tvalue")])
def test_register_delimiters_refused(repo: Path, field: str, value: str) -> None:
    with pytest.raises(lr.RegisterError, match=field):
        lr.append_event(repo, _row(**{field: value}), lock=None)
    assert not (repo / lr.REGISTER_PATH).exists()


@pytest.mark.parametrize("defect", ["fields", "utc", "event", "utf8"])
def test_register_defects_require_later_valid_correction(repo: Path, defect: str) -> None:
    path = _register_path(repo)
    lr.append_event(repo, _row(), lock=None)
    fields = path.read_bytes().splitlines()[1].split(b"\t")
    if defect == "fields":
        fields.pop()
    elif defect == "utc":
        fields[0] = b"2026-13-01T00:00:00Z"
    elif defect == "event":
        fields[3] = b"unknown"
    else:
        fields[12] = b"\xff"
    path.write_bytes(lr.HEADER.encode() + b"\t".join(fields) + b"\n")
    with pytest.raises(lr.RegisterError, match="line 2"):
        lr.read_register(repo)
    lr.append_correction(repo, malformed_line=3, reason="wrong line", by="actor")
    with pytest.raises(lr.RegisterError, match="line 2"):
        lr.read_register(repo)
    lr.append_correction(repo, malformed_line=2, reason="acknowledged", by="actor")
    assert lr.read_register(repo).rows[-1]["note"] == "malformed-line:2; acknowledged"


def test_register_single_write_and_current_state(repo: Path, monkeypatch) -> None:
    writes = []
    real_write = os.write

    def capture(fd: int, data: bytes) -> int:
        writes.append(data)
        return real_write(fd, data)

    monkeypatch.setattr(lr.os, "write", capture)
    assert lr.append_event(repo, _row(), lock=None) == 2
    register_writes = [value for value in writes if value.startswith(lr.HEADER.encode())]
    assert len(register_writes) == 1 and register_writes[0].count(b"\n") == 2
    with lr.AuditLock.acquire(repo, "fixture", verb="seal", session="fixture session") as lock:
        lr.append_event(repo, _row(event="seal", state_after="sealed", manifest_sha256="a" * 64), lock=lock)
    state = lr.current_state(lr.read_register(repo), "fixture")
    assert state["state_after"] == "sealed" and state["manifest_sha256"] == "a" * 64
    assert state["lock_id"] == lock.lock_id
    assert lr.current_state(lr.read_register(repo), "missing") is None


def test_register_two_concurrent_processes_append_complete_lines(repo: Path) -> None:
    script = (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from lifecycle_register import COLUMNS, append_event\n"
        "sys.stdin.read(1)\n"
        "row = dict.fromkeys(COLUMNS, '')\n"
        "row.update(audit_id=sys.argv[2], event='census', state_after='modern')\n"
        "append_event(Path(sys.argv[1]), row, lock=None)\n"
    )
    processes = [subprocess.Popen([sys.executable, "-c", script, str(repo), audit_id],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for audit_id in ("first", "second")]
    try:
        for process in processes:
            process.stdin.write(b"x")
            process.stdin.flush()
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr.decode()
        register = lr.read_register(repo)
        assert register.line_numbers == [2, 3]
        assert {row["audit_id"] for row in register.rows} == {"first", "second"}
        assert (repo / lr.REGISTER_PATH).read_bytes().count(b"\n") == 3
        assert not list(lr.lock_directory(repo).glob("*.lock"))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_audit_lock_acquire_release_and_contention(repo: Path) -> None:
    with lr.AuditLock.acquire(repo, "fixture", verb="seal", session="test-session") as lock:
        content = json.loads(lock.path.read_bytes())
        assert set(content) == {"lock_id", "audit_id", "verb", "pid", "host", "session", "utc"}
        assert len(lock.lock_id) == 32 and content["session"] == "test-session"
        with pytest.raises(lr.LockHeld, match="test-session"):
            lr.AuditLock.acquire(repo, "fixture", verb="expire", session="second")
    assert not lock.path.exists()


def test_audit_lock_replaced_release_refused(repo: Path) -> None:
    lock = lr.AuditLock.acquire(repo, "fixture", verb="seal", session="test-session")
    lock.path.unlink()
    lock.path.write_bytes(b"replacement evidence\n")
    with pytest.raises(lr.RegisterError, match="replaced lock"):
        lock.release()
    assert lock.path.read_bytes() == b"replacement evidence\n"


def test_append_requires_correct_held_audit_lock(repo: Path) -> None:
    with pytest.raises(lr.RegisterError, match="per-audit lock required"):
        lr.append_event(repo, _row(event="seal"), lock=None)
    with lr.AuditLock.acquire(repo, "first", verb="seal", session="fixture") as lock:
        with pytest.raises(lr.RegisterError, match="another audit/repo"):
            lr.append_event(repo, _row("second", event="seal"), lock=lock)
        with pytest.raises(lr.RegisterError, match="does not match"):
            lr.append_event(repo, _row("first", event="seal", lock_id="wrong"), lock=lock)


def test_census_row_keeps_empty_lock_id_under_held_lock(repo: Path) -> None:
    with lr.AuditLock.acquire(repo, "fixture", verb="census", session="fixture") as lock:
        lr.append_event(repo, _row(), lock=lock)
        assert lr.read_register(repo).rows[-1]["lock_id"] == ""
        with pytest.raises(lr.RegisterError, match="census lock_id.*empty"):
            lr.append_event(repo, _row(lock_id=lock.lock_id), lock=lock)


def test_lock_status_clear_flags_and_correction(repo: Path, capsys) -> None:
    lock = lr.AuditLock.acquire(repo, "fixture", verb="seal", session="test-session")
    assert lifecycle.main(["lock-status", "--repo", str(repo), "--audit", "fixture"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["content"] == lock.content.decode()
    for extra in ([], ["--by", "actor"], ["--reason", "stale"]):
        with pytest.raises(SystemExit) as exc:
            lifecycle.main(["lock-clear", "--repo", str(repo), "--audit", "fixture", *extra])
        assert exc.value.code == 2 and lock.path.exists()
    assert lifecycle.main(["lock-clear", "--repo", str(repo), "--audit", "fixture",
                           "--by", "actor", "--reason", "stale and manually reviewed"]) == 0
    assert not lock.path.exists()
    row = lr.read_register(repo).rows[-1]
    assert row["event"] == "correction" and row["actor"] == "actor" and row["lock_id"] == ""
    assert row["note"] == f"lock-cleared:{hashlib.sha256(lock.content).hexdigest()}; stale and manually reviewed"


def test_real_gate_refused_and_both_fixture_paths_required(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "fixture-root"
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))
    lr.require_gate("G3", audit_dir=root / "audit", db_path=root / "fixture.sqlite")
    with pytest.raises(lr.RegisterError, match="gate G3"):
        lr.require_gate("G3", audit_dir=tmp_path / "real-looking-audit", db_path=root / "fixture.sqlite")
    with pytest.raises(lr.RegisterError, match="gate G1"):
        lr.require_gate("G1", audit_dir=root / "audit", db_path=tmp_path / "real-looking.sqlite")


@pytest.mark.parametrize("met,evidence,allowed", [(False, "record", False), (True, None, False),
                                                  (True, "", False), (True, "host evidence", True)])
def test_gate_requires_true_and_nonempty_evidence(tmp_path: Path, monkeypatch, met, evidence,
                                                allowed: bool) -> None:
    config = tmp_path / "skill/config"
    config.mkdir(parents=True)
    (config / "real_data_gates.json").write_text(json.dumps({
        "schema": "minsky-real-data-gates/1", "G1": {"met": met, "evidence": evidence},
    }))
    monkeypatch.setattr(lr, "SKILL_DIR", config.parent)
    monkeypatch.setenv("MINSKY_TEST_TMP", str(tmp_path / "fixtures"))
    if allowed:
        lr.require_gate("G1", audit_dir=tmp_path / "real-audit", db_path=tmp_path / "real.sqlite")
    else:
        with pytest.raises(lr.RegisterError, match="gate G1"):
            lr.require_gate("G1", audit_dir=tmp_path / "real-audit", db_path=tmp_path / "real.sqlite")


@pytest.mark.parametrize("relative", [str(path) for path in TSVS])
@pytest.mark.parametrize("change", ["append", "middle", "truncate", "partial", "delete", "rename", "typechange"])
def test_t16_hook_prefix_guard(repo: Path, relative: str, change: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b"header\nfirst\nsecond\n"
    path.write_bytes(original)
    _git(repo, "add", "--", relative)
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "baseline TSV")
    _git(repo, "config", "core.hooksPath", str(HOOK.parent))
    if change == "delete":
        path.unlink()
    elif change == "rename":
        path.rename(path.with_suffix(".renamed"))
    elif change == "typechange":                      # host addition (Astra W8): file -> symlink is a T change
        path.unlink()
        path.symlink_to("elsewhere.tsv")
    else:
        data = {"append": original + b"third\n", "middle": original.replace(b"first", b"edited"),
                "truncate": b"header\nfirst\n", "partial": original + b"third"}[change]
        path.write_bytes(data)
    _git(repo, "add", "--all", "--", "documentation")
    result = _git(repo, "commit", "--quiet", "-m", "test staged TSV", check=False)
    assert (result.returncode == 0) == (change == "append"), result.stderr
    if change != "append":
        assert "GUARD 4" in result.stderr
    assert not list((repo / ".git").glob("mars-hook-tmp.*"))


def test_t16_override_requires_reason_and_is_logged(repo: Path) -> None:
    path = _register_path(repo)
    path.write_text("header\nrow\n")
    _git(repo, "add", "--", str(lr.REGISTER_PATH))
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "baseline")
    _git(repo, "config", "core.hooksPath", str(HOOK.parent))
    path.write_text("changed\n")
    _git(repo, "add", "--", str(lr.REGISTER_PATH))
    env = {"MARS_ALLOW_TSV_CORRECTION": "1", "MARS_ALLOW_TSV_CORRECTION_REASON": ""}
    refused = _git(repo, "commit", "--quiet", "-m", "refused", env=env, check=False)
    assert refused.returncode != 0 and "needs MARS_ALLOW_TSV_CORRECTION_REASON" in refused.stderr
    env["MARS_ALLOW_TSV_CORRECTION_REASON"] = "reviewed fixture correction"
    _git(repo, "commit", "--quiet", "-m", "allowed", env=env)
    log = (repo / ".git/mars-tsv-correction-overrides.log").read_text()
    assert "reviewed fixture correction\t1 path(s)\n" in log
    assert str(lr.REGISTER_PATH) in log


def test_t16_new_tsv_and_unchanged_blob_allowed(repo: Path) -> None:
    _git(repo, "config", "core.hooksPath", str(HOOK.parent))
    path = _register_path(repo)
    path.write_text("header\nrow\n")
    _git(repo, "add", "--", str(lr.REGISTER_PATH))
    _git(repo, "commit", "--quiet", "-m", "new TSV")
    path.chmod(0o755)
    _git(repo, "add", "--", str(lr.REGISTER_PATH))
    _git(repo, "commit", "--quiet", "-m", "same TSV bytes, executable bit only")
    result = subprocess.run(["bash", str(HOOK)], cwd=repo, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_t16_accept_any_staged_change_mutant_is_killed(repo: Path, tmp_path: Path) -> None:
    path = _register_path(repo)
    original = b"header\nfirst\nsecond\n"
    path.write_bytes(original)
    _git(repo, "add", "--", str(lr.REGISTER_PATH))
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "baseline")
    path.write_bytes(b"header\nEDITED\nsecond\n")
    _git(repo, "add", "--", str(lr.REGISTER_PATH))
    source = HOOK.read_text()
    start = source.index("# GUARD 4 — append-only")
    end = source.index("# END GUARD 4", start) + len("# END GUARD 4")
    mutant = tmp_path / "accept_any_staged_change"
    mutant.write_text(source[:start] + "# mutant: accept every TSV change\n" + source[end:])
    assert subprocess.run(["bash", str(HOOK)], cwd=repo, capture_output=True).returncode == 1
    with pytest.raises(AssertionError):
        assert subprocess.run(["bash", str(mutant)], cwd=repo, capture_output=True).returncode == 1


@pytest.mark.parametrize("command", ["diff", "ls-tree", "cat-file", "rev-parse"])
def test_t16_git_read_failures_block_even_with_override(repo: Path, tmp_path: Path,
                                                       command: str) -> None:
    path = _register_path(repo)
    path.write_bytes(b"header\nrow\n")
    _git(repo, "add", "--", str(lr.REGISTER_PATH))
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "baseline")
    path.write_bytes(b"header\nrow\nappend\n")
    _git(repo, "add", "--", str(lr.REGISTER_PATH))
    shim = tmp_path / "shim"
    shim.mkdir()
    real_git = shutil.which("git")
    assert real_git
    wrapper = shim / "git"
    wrapper.write_text(
        '#!/bin/bash\n'
        'for argument in "$@"; do\n'
        '  if [ "$argument" = "--no-optional-locks" ]; then continue; fi\n'
        '  command="$argument"; break\n'
        'done\n'
        'if [ "$command" = "$MINSKY_FAULT_GIT_COMMAND" ]; then\n'
        '  if [ "$command" != "cat-file" ] || [[ " $* " = *" blob "* ]]; then\n'
        '    printf "fixture git read failure\\n" >&2; exit 88\n'
        '  fi\n'
        'fi\n'
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    wrapper.chmod(0o755)
    result = subprocess.run(["bash", str(HOOK)], cwd=repo, text=True, capture_output=True, env={
        **os.environ, "PATH": str(shim) + os.pathsep + os.environ["PATH"],
        "MINSKY_FAULT_GIT_COMMAND": command, "MARS_ALLOW_TSV_CORRECTION": "1",
        "MARS_ALLOW_TSV_CORRECTION_REASON": "cannot bypass unreadable Git evidence",
    })
    assert result.returncode == 1 and "GUARD 4 cannot read" in result.stderr
    assert not list((repo / ".git").glob("mars-hook-tmp.*"))


def _census_inputs(repo: Path, tmp_path: Path, rows=(), rd_rows=()):
    db = tmp_path / "audits.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE audits(audit_id TEXT, finished_at TEXT)")
        conn.executemany("INSERT INTO audits VALUES (?, ?)", rows)
    rd = tmp_path / "rd-view.tsv"
    rd.write_text(lifecycle.RD_HEADER + "\n" + "".join("\t".join(row) + "\n" for row in rd_rows))
    return {"audits_db": db, "rd_register_view": rd, "report_out": tmp_path / "report.json"}


def test_census_bespoke_markers_record_and_readonly_database(repo: Path, tmp_path: Path) -> None:
    round_dir = repo / "fixtures/test-audit/round-2"
    register_round(round_dir)
    inputs = _census_inputs(repo, tmp_path, [("test-audit", None)])
    db_bytes = inputs["audits_db"].read_bytes()
    assert lifecycle.census(repo, **inputs) == 0
    report = json.loads(inputs["report_out"].read_text())
    assert report["entries"][0]["status"] == "bespoke"
    assert report["entries"][0]["rounds"] == [str(round_dir)]
    assert not (repo / lr.REGISTER_PATH).exists()
    assert inputs["audits_db"].read_bytes() == db_bytes
    assert lifecycle.census(repo, **inputs, record=True) == 0
    row = lr.read_register(repo).rows[0]
    assert row["event"] == "census" and row["state_after"] == "bespoke" and row["lock_id"] == ""


def test_census_provenance_marker_and_exclusions(repo: Path, tmp_path: Path) -> None:
    for prefix in ("fixtures", ".worktrees", "node_modules", ".git"):
        path = repo / prefix / "audit/round-1/provenance"
        path.mkdir(parents=True)
        (path / "fixture.call.json").write_text("{}\n")
    inputs = _census_inputs(repo, tmp_path)
    assert lifecycle.census(repo, **inputs) == 0
    report = json.loads(inputs["report_out"].read_text())
    assert report["union_size"] == 1 and report["entries"][0]["audit_id"] == "audit"


def test_census_status_rules_metadata_only_without_standard_tree_writes(repo: Path, tmp_path: Path) -> None:
    # Mock lstat for standard placement; never create a codex-audits fixture tree.
    standard = repo / "codex-audits/standard"
    round_dir = standard / "round-1"
    directory_stat = repo.lstat()
    original_lstat = Path.lstat
    from unittest.mock import patch

    def metadata(path: Path):
        if path == standard:
            return directory_stat
        return original_lstat(path)

    with patch.object(Path, "lstat", metadata):
        assert lifecycle._status(repo, standard, [round_dir], True, False, None)[0] == "modern"
        assert lifecycle._status(repo, standard, [round_dir], False, False, None)[0] == "bespoke"
    assert lifecycle._status(repo, standard, [], True, False, None)[0] == "unclassifiable"
    assert not (repo / "codex-audits").exists()


def test_census_legacy_inventory_lstat_only(repo: Path, tmp_path: Path, monkeypatch) -> None:
    legacy = repo / "fixtures/legacy"
    legacy.mkdir(parents=True)
    tracked = legacy / "tracked.txt"
    tracked.write_bytes(b"tracked\n")
    _git(repo, "add", "--", "fixtures/legacy/tracked.txt")
    untracked = legacy / "untracked.txt"
    untracked.write_bytes(b"1234")
    cache = legacy / "node_modules/pkg"
    cache.mkdir(parents=True)
    (cache / "cache.txt").write_bytes(b"abc")
    link = legacy / "external-link"
    link.symlink_to(tmp_path / "never-open")
    inputs = _census_inputs(repo, tmp_path, rd_rows=[("legacy", "fixtures/legacy", "boot", "")])
    real_read = Path.read_bytes

    def forbid_legacy_reads(path: Path) -> bytes:
        if path.is_relative_to(legacy):
            pytest.fail(f"census read legacy contents: {path}")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_legacy_reads)
    assert lifecycle.census(repo, **inputs) == 0
    entry = json.loads(inputs["report_out"].read_text())["entries"][0]
    assert entry["status"] == "legacy-preserved"
    assert entry["inventory"] == {"untracked_files": 3, "untracked_bytes": 7 + link.lstat().st_size}


@pytest.mark.parametrize("target", ["/Volumes/W8-never-read/audit", "outside"])
def test_census_external_symlink_is_deferred_without_target_reads(repo: Path, tmp_path: Path,
                                                                 monkeypatch, target: str) -> None:
    link = repo / "fixtures/deferred"
    link.parent.mkdir()
    destination = target if target.startswith("/") else str(tmp_path / target)
    link.symlink_to(destination, target_is_directory=True)
    inputs = _census_inputs(repo, tmp_path, rd_rows=[("deferred", "fixtures/deferred", "boot", "")])
    original = Path.lstat

    def metadata_only(path: Path):
        if str(path).startswith(destination):
            pytest.fail(f"census inspected external symlink target: {path}")
        return original(path)

    monkeypatch.setattr(Path, "lstat", metadata_only)
    assert lifecycle.census(repo, **inputs) == 0
    assert json.loads(inputs["report_out"].read_text())["entries"][0]["status"] == "deferred"


def test_census_rd_extended_prunes_marker_tree(repo: Path, tmp_path: Path) -> None:
    round_dir = repo / "fixtures/extended/round-1"
    round_dir.mkdir(parents=True)
    (round_dir / "round-scope.json").write_text("invalid; must not be read")
    inputs = _census_inputs(repo, tmp_path, rd_rows=[("extended", "fixtures/extended", "extended", "manifest.tsv")])
    assert lifecycle.census(repo, **inputs) == 0
    entry = json.loads(inputs["report_out"].read_text())["entries"][0]
    assert entry["status"] == "deferred" and entry["rounds"] == []


def test_census_unclassifiable_writes_report_and_fails(repo: Path, tmp_path: Path, capsys) -> None:
    inputs = _census_inputs(repo, tmp_path, [("missing", None)])
    assert lifecycle.census(repo, **inputs) == 1
    report = json.loads(inputs["report_out"].read_text())
    assert report["totals"]["unclassifiable"] == report["union_size"] == 1
    assert "missing" in capsys.readouterr().err


def test_census_internal_report_missing_view_and_totals_refuse(repo: Path, tmp_path: Path,
                                                             monkeypatch, capsys) -> None:
    inputs = _census_inputs(repo, tmp_path, [("missing", None)])
    with pytest.raises(lr.RegisterError, match="--report-out.*inside repo"):
        lifecycle.census(repo, **{**inputs, "report_out": repo / "report.json"})
    with pytest.raises(FileNotFoundError):
        lifecycle.census(repo, **{**inputs, "rd_register_view": tmp_path / "missing-view.tsv"})
    monkeypatch.setattr(lifecycle, "STATUSES", ("modern",))
    assert lifecycle.census(repo, **inputs) == 1
    assert "totals" in capsys.readouterr().err
    assert json.loads(inputs["report_out"].read_text())["union_size"] == 1


def test_closeout_flags_without_copies_and_unresolved_copy_notes(repo: Path) -> None:
    with lr.AuditLock.acquire(repo, "fixture", verb="seal", session="fixture") as lock:
        lr.append_event(repo, _row(event="seal", **{"class": "methodology-bearing"}), lock=lock)
        register = lr.read_register(repo)
        assert lifecycle.closeout_flags("fixture", "2026-09-01T00:00:00Z", register, date(2026, 10, 1)) == ["overdue-first-copy"]
        assert lifecycle.closeout_flags("fixture", "2026-08-01T00:00:00Z", register, date(2026, 10, 1)) == ["overdue-first-copy", "overdue-second-copy"]
        assert lifecycle.closeout_flags("fixture", "2026-09-24T00:00:00Z", register, date(2026, 10, 1)) == []
        lr.append_event(repo, _row(event="copy", copy_location="off-boot", note="format not yet specified"), lock=lock)
    with pytest.raises(lr.RegisterError, match="W6.*note format.*unresolved"):
        lifecycle.closeout_flags("fixture", "2026-08-01T00:00:00Z", lr.read_register(repo), date(2026, 10, 1))


@pytest.mark.parametrize("verb", ["classify", "challenge", "promote", "seal", "pack", "replicate",
                                  "verify", "offload", "rehydrate", "expire", "archive"])
def test_unimplemented_verbs_exit_two(repo: Path, capsys, verb: str) -> None:
    assert lifecycle.main([verb, "--repo", str(repo), "--future-option", "value"]) == 2
    assert capsys.readouterr().err == f"{verb}: not implemented in W8\n"
