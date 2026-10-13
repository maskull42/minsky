"""FCM fixture checks; refuse freeze copies whose owner-only mode is not preserved."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import lifecycle_register as lr
import lifecycle_seal as ls
from test_lifecycle_w5_seal import ACTOR, audit as w5_audit, cli

audit = w5_audit


def _freeze(audit: dict) -> dict:
    return ls.freeze(audit["repo"], reason="ad-b", label=None, audits_db=audit["db"],
                     freeze_dir=audit["freeze_dir"], freeze_copy=audit["freeze_copy"],
                     actor=ACTOR, window_confirmed=None)


@pytest.mark.parametrize("mask", [0o022, 0o000], ids=["umask022", "umask000"])
def test_freeze_copy_is_owner_only(audit: dict, monkeypatch, mask: int) -> None:
    open_fd = ls.os.open
    created = []

    def observe(path, flags, mode=0o777, *, dir_fd=None):
        fd = open_fd(path, flags, mode, dir_fd=dir_fd)
        if Path(path).parent == audit["freeze_copy"]:
            created.append((flags, mode, stat.S_IMODE(os.fstat(fd).st_mode)))
        return fd

    monkeypatch.setattr(ls.os, "open", observe)
    previous = os.umask(mask)
    try:
        result = _freeze(audit)
    finally:
        os.umask(previous)
    rows = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
    assert len(rows) == 2
    row = rows[result["freeze_copy_row"] - 2]
    assert row["event"] == "freeze-copy"
    destination = Path(row["path"])
    info = destination.lstat()
    assert stat.S_ISREG(info.st_mode) and not destination.is_symlink()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert created == [(os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, 0o600)]
    assert ls.sha256_file(destination) == result["sha256"]


def test_restore_scratch_copy_is_owner_only(audit: dict, tmp_path: Path, monkeypatch) -> None:
    result = _freeze(audit)
    scratch = tmp_path / "restore"
    integrity = ls._integrity
    inspected = []

    def inspect(path: Path) -> None:
        assert path.parent == scratch
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and not path.is_symlink()
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert ls.sha256_file(path) == result["sha256"]
        inspected.append(path)
        integrity(path)

    monkeypatch.setattr(ls, "_integrity", inspect)
    previous = os.umask(0o022)
    try:
        ls.freeze_restore_check(audit["repo"], freeze_row=result["freeze_copy_row"],
                                scratch=scratch, actor=ACTOR)
    finally:
        os.umask(previous)
    assert len(inspected) == 1
    assert not inspected[0].exists()
    rows = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
    assert len(rows) == 3 and rows[-1]["event"] == "restore-demo"


def test_copy_sets_mode_after_restrictive_umask(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "copy"
    source.write_bytes(b"fixture freeze bytes")
    sha = ls.sha256_file(source)
    previous = os.umask(0o777)
    try:
        ls._copy_checked(source, destination, sha)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert ls.sha256_file(destination) == sha


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_copy_refuses_nonregular_destination(tmp_path: Path, monkeypatch, kind: str) -> None:
    source, destination = tmp_path / "source", tmp_path / "copy"
    source.write_bytes(b"fixture freeze bytes")
    source.chmod(0o600)
    sha = ls.sha256_file(source)
    fsync = ls.os.fsync
    replaced = []

    def replace_destination(fd: int) -> None:
        fsync(fd)
        destination.unlink()
        if kind == "symlink":
            destination.symlink_to(source)
        else:
            os.mkfifo(destination, 0o600)
            destination.chmod(0o600)
        replaced.append(destination)

    # A special file must be refused before hashing, which could block on a FIFO.
    monkeypatch.setattr(ls.os, "fsync", replace_destination)
    monkeypatch.setattr(ls, "sha256_file", lambda path: pytest.fail("nonregular destination reached hashing"))
    with pytest.raises(lr.RegisterError, match="0600 required"):
        ls._copy_checked(source, destination, sha)
    assert replaced == [destination]
    assert not destination.exists() and not destination.is_symlink()
    assert source.read_bytes() == b"fixture freeze bytes"


@pytest.mark.parametrize("entrypoint", ["direct", "freeze"])
def test_copy_refuses_mode_not_kept(audit: dict, tmp_path: Path, monkeypatch, capsys,
                                   entrypoint: str) -> None:
    """Refuse a copy that keeps 0644; leave neither the copy nor a new freeze ledger row."""
    fchmod = ls.os.fchmod
    modes = []

    def lose_mode(fd: int, mode: int) -> None:
        fchmod(fd, 0o644)
        modes.append((mode, stat.S_IMODE(os.fstat(fd).st_mode)))

    if entrypoint == "direct":
        source, destination = tmp_path / "source", tmp_path / "bad-mode-copy"
        source.write_bytes(b"fixture freeze bytes")
        sha = ls.sha256_file(source)
        monkeypatch.setattr(ls.os, "fchmod", lose_mode)
        with pytest.raises(lr.RegisterError, match="0600 required"):
            ls._copy_checked(source, destination, sha)
        # Check here, before any freeze_database outer cleanup can hide a leftover.
        assert not destination.exists() and not destination.is_symlink()
    else:
        monkeypatch.setattr(ls, "utc_now", lambda: "2026-10-13T00:00:00Z")
        _freeze(audit)
        before = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
        assert len(before) == 2
        ledger = audit["repo"] / ls.FREEZES
        ledger_before = ledger.read_bytes()
        destination = audit["freeze_copy"] / "20261013T000001Z_freeze.AD-B_freeze.db"
        monkeypatch.setattr(ls, "utc_now", lambda: "2026-10-13T00:00:01Z")
        monkeypatch.setattr(ls.os, "fchmod", lose_mode)
        capsys.readouterr()
        assert cli.main([
            "freeze", "--repo", str(audit["repo"]), "--reason", "ad-b",
            "--audits-db", str(audit["db"]), "--freeze-dir", str(audit["freeze_dir"]),
            "--freeze-copy", str(audit["freeze_copy"]), "--actor", ACTOR,
        ]) == 1
        output = capsys.readouterr()
        assert "0600 required" in output.err and str(destination) in output.err
        assert not output.out
        assert not destination.exists() and not destination.is_symlink()
        rows = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
        assert len(rows) == len(before)
        assert ledger.read_bytes() == ledger_before
    assert modes == [(0o600, 0o644)]
