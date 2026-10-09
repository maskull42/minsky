"""F1 fixture checks; refuse preservation loss, unsafe freezes and register pollution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import lifecycle_register as lr
import lifecycle_seal as ls
from test_lifecycle_w5_seal import ACTOR, AUDIT_ID, _seal, audit as w5_audit, cli

audit = w5_audit


def _argv(audit: dict, *, reason: str = "ad-b") -> list[str]:
    return ["freeze", "--repo", str(audit["repo"]), "--reason", reason,
            "--audits-db", str(audit["db"]), "--freeze-dir", str(audit["freeze_dir"]),
            "--freeze-copy", str(audit["freeze_copy"]), "--actor", ACTOR]


def _ledger(audit: dict) -> bytes:
    return (audit["repo"] / ls.FREEZES).read_bytes()


def _seed_ledger(audit: dict) -> None:
    # A real preexisting header makes byte-equality claims non-vacuous.
    (audit["repo"] / ls.FREEZES).write_bytes(ls._tsv(ls.FREEZE_HEADER, []))


def _unchanged(audit: dict, before: bytes) -> None:
    assert _ledger(audit) == before
    for key in ("freeze_dir", "freeze_copy"):
        assert not audit[key].exists(), key


def _database_content(path: Path) -> tuple[str, dict[str, int]]:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        dump = "\n".join(conn.iterdump()).encode("utf-8")
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        assert tables
        counts = {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for (name,) in tables}
    assert counts["audits"] > 0
    return hashlib.sha256(dump).hexdigest(), counts


def test_ad_b_preserves_database_and_reports_ledger_rows(audit: dict, capsys) -> None:
    _seed_ledger(audit)
    source = _database_content(audit["db"])
    assert cli.main([*_argv(audit, reason="campaign-close"), "--label", "prior-campaign"]) == 0
    capsys.readouterr()
    before = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
    assert len(before) == 2
    assert cli.main(_argv(audit)) == 0
    output = capsys.readouterr()
    assert not output.err and len(output.out.splitlines()) == 1
    result = json.loads(output.out)
    rows = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
    assert len(rows) == len(before) + 2
    assert rows[:len(before)] == before
    assert [row["event"] for row in rows[-2:]] == ["freeze", "freeze-copy"]
    assert result == {"audit_id": "freeze.AD-B", "sha256": rows[-2]["sha256"],
                      "bytes": int(rows[-2]["bytes"]), "freeze_row": len(before) + 2,
                      "freeze_copy_row": len(before) + 3}
    for key, event in (("freeze_row", "freeze"), ("freeze_copy_row", "freeze-copy")):
        row = rows[result[key] - 2]
        assert row["audit_id"] == "freeze.AD-B" and row["event"] == event
        path = Path(row["path"])
        assert path.name.endswith("_freeze.db")
        assert row["integrity_check"] == "ok"
        assert ls.sha256_file(path) == result["sha256"]
        assert path.stat().st_size == result["bytes"] > 0
        assert _database_content(path) == source


def test_freeze_bypasses_g1_but_seal_still_consults_it(audit: dict, monkeypatch) -> None:
    calls = []

    def reject(gate: str, **kwargs) -> None:
        calls.append(gate)
        raise lr.RegisterError("refused planted G1 gate")

    monkeypatch.setattr(ls, "require_gate", reject)
    assert cli.main(_argv(audit)) == 0
    assert calls == []
    with pytest.raises(lr.RegisterError, match="planted G1 gate"):
        _seal(audit)
    assert calls == ["G1"]


@pytest.mark.parametrize("collision", ["audits", "register"])
def test_collision_refuses_without_writes(audit: dict, capsys, collision: str) -> None:
    _seed_ledger(audit)
    if collision == "audits":
        with closing(sqlite3.connect(audit["db"])) as conn, conn:
            conn.execute("UPDATE audits SET audit_id=? WHERE audit_id=?", ("freeze.AD-B", AUDIT_ID))
    else:
        ls.challenge(audit["repo"], audit_id="freeze.AD-B", by="fixture", reason="collision", actor=ACTOR)
    before = _ledger(audit)
    register = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    assert cli.main(_argv(audit)) == 1
    error = capsys.readouterr().err
    assert "freeze.AD-B" in error and "collision" in error
    _unchanged(audit, before)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == register


@pytest.mark.parametrize("suffix,payload,refused", [("-journal", b"", True),
                                                    ("-wal", b"hot", True), ("-wal", b"", False)])
def test_hot_journal_refuses_before_writes(audit: dict, capsys, suffix: str,
                                         payload: bytes, refused: bool) -> None:
    _seed_ledger(audit)
    sidecar = Path(str(audit["db"]) + suffix)
    sidecar.write_bytes(payload)
    before = _ledger(audit)
    assert cli.main(_argv(audit)) == (1 if refused else 0)
    error = capsys.readouterr().err
    if refused:
        assert "refused hot journal" in error and str(sidecar) in error
        _unchanged(audit, before)
    else:
        assert not error
        assert len(ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)) == 2


def test_lsof_holder_refuses_without_writes(audit: dict, capsys) -> None:
    _seed_ledger(audit)
    before = _ledger(audit)
    process = subprocess.Popen([sys.executable, "-c",
                                "import sys; f=open(sys.argv[1]); print('ready',flush=True); sys.stdin.read()",
                                str(audit["db"])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        assert cli.main(_argv(audit)) == 1
        error = capsys.readouterr().err
        assert "lsof" in error and str(audit["db"]) in error
        _unchanged(audit, before)
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()


def test_usage_and_campaign_identity(audit: dict, capsys) -> None:
    _seed_ledger(audit)
    before = _ledger(audit)
    cases = [("campaign-close", None), ("ad-b", "not-allowed")]
    cases += [("campaign-close", label) for label in ("", ".", "..", "a/b", "a b", "a" * 81)]
    for reason, label in cases:
        argv = _argv(audit, reason=reason)
        if label is not None:
            argv += ["--label", label]
        assert cli.main(argv) == 1
        error = capsys.readouterr().err
        assert "refused" in error and repr(label) in error
        _unchanged(audit, before)
    argv = _argv(audit)
    index = argv.index("--freeze-copy")
    del argv[index:index + 2]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "the following arguments are required: --freeze-copy" in capsys.readouterr().err
    _unchanged(audit, before)
    assert cli.main([*_argv(audit, reason="campaign-close"), "--label", "minsky-lifecycle"]) == 0
    assert json.loads(capsys.readouterr().out)["audit_id"] == "freeze.campaign-close.minsky-lifecycle"


def test_reserved_restore_writes_demo_without_register_event(audit: dict, tmp_path: Path, capsys) -> None:
    assert cli.main(_argv(audit)) == 0
    result = json.loads(capsys.readouterr().out)
    register = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    before = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
    scratch = tmp_path / "restore"
    assert cli.main(["freeze-restore-check", "--repo", str(audit["repo"]), "--freeze-row",
                     str(result["freeze_copy_row"]), "--scratch", str(scratch), "--actor", ACTOR]) == 0
    rows = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
    assert rows[:-1] == before and len(rows) == len(before) + 1
    assert rows[-1]["event"] == "restore-demo" and rows[-1]["audit_id"] == "freeze.AD-B"
    assert rows[-1]["sha256"] == result["sha256"] and rows[-1]["integrity_check"] == "ok"
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == register
    assert scratch.is_dir() and list(scratch.iterdir()) == []


def test_real_audit_restore_still_appends_one_verify(audit: dict, tmp_path: Path) -> None:
    _seal(audit)
    before = lr.read_register(audit["repo"]).rows
    assert cli.main(["freeze-restore-check", "--repo", str(audit["repo"]), "--freeze-row", "3",
                     "--scratch", str(tmp_path / "restore"), "--actor", ACTOR]) == 0
    rows = lr.read_register(audit["repo"]).rows
    assert rows[:-1] == before and len(rows) == len(before) + 1
    assert rows[-1]["event"] == "verify" and rows[-1]["audit_id"] == AUDIT_ID
    assert "freeze-restore-check:3" in rows[-1]["note"]


def test_seal_keeps_seal_filename(audit: dict) -> None:
    _seal(audit)
    rows = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
    assert len(rows) == 2
    for row in rows:
        assert row["audit_id"] == AUDIT_ID
        assert Path(row["path"]).name.endswith("_seal.db")
        assert ls.sha256_file(Path(row["path"])) == row["sha256"]


def test_volume_copy_refused_without_writes(audit: dict, monkeypatch, capsys) -> None:
    _seed_ledger(audit)
    before = _ledger(audit)
    destination = Path("/Volumes/F1-never-touched")
    resolve = Path.resolve
    # The inherited guard resolves its argument; keep that operation lexical for this forbidden volume.
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **kw:
                        path if path == destination else resolve(path, *a, **kw))
    argv = _argv({**audit, "freeze_copy": destination})
    assert cli.main(argv) == 1
    error = capsys.readouterr().err
    assert str(destination) in error and "--window-confirmed required" in error
    _unchanged(audit, before)


@pytest.mark.parametrize("values,match", [({"reason": "unknown"}, "unknown"),
                                          ({"actor": ""}, "--actor"),
                                          ({"actor": "bad\tactor"}, "--actor")])
def test_freeze_api_validation_refuses_before_writes(audit: dict, values: dict, match: str) -> None:
    _seed_ledger(audit)
    before = _ledger(audit)
    kwargs = {"reason": "ad-b", "label": None, "audits_db": audit["db"],
              "freeze_dir": audit["freeze_dir"], "freeze_copy": audit["freeze_copy"],
              "actor": ACTOR, "window_confirmed": None, **values}
    with pytest.raises(lr.RegisterError, match=match):
        ls.freeze(audit["repo"], **kwargs)
    _unchanged(audit, before)


def test_freeze_database_refuses_unknown_kind(audit: dict) -> None:
    _seed_ledger(audit)
    before = _ledger(audit)
    with lr.AuditLock.acquire(audit["repo"], "freeze.AD-B", verb="freeze", session=ACTOR) as lock:
        with pytest.raises(lr.RegisterError, match="refused freeze kind 'unknown'"):
            ls.freeze_database(audit["repo"], audit["db"], audit_id="freeze.AD-B",
                               freeze_dir=audit["freeze_dir"], freeze_copy=audit["freeze_copy"],
                               utc="2026-10-08T00:00:00Z", window_confirmed=None, lock=lock, kind="unknown")
    _unchanged(audit, before)


@pytest.mark.parametrize("event,count", [("freeze", 0), ("freeze", 2), ("freeze-copy", 0), ("freeze-copy", 2)])
def test_freeze_refuses_ambiguous_ledger_rows(audit: dict, monkeypatch, event: str, count: int) -> None:
    sha, utc = "a" * 64, "2026-10-08T00:00:00Z"
    # Seed a missing or duplicated identity, as if the writer returned an invalid durable ledger.
    rows = [{"audit_id": "freeze.AD-B", "event": name, "sha256": sha, "utc": utc}
            for name in ("freeze", "freeze-copy") for _ in range(count if name == event else 1)]
    monkeypatch.setattr(ls, "utc_now", lambda: utc)
    monkeypatch.setattr(ls, "freeze_database", lambda *a, **kw: {"sha256": sha, "bytes": 1})
    monkeypatch.setattr(ls, "_read_table", lambda *a: rows)
    with pytest.raises(lr.RegisterError, match=f"freeze.AD-B.*{event}.*expected one row, found {count}"):
        ls.freeze(audit["repo"], reason="ad-b", label=None, audits_db=audit["db"],
                  freeze_dir=audit["freeze_dir"], freeze_copy=audit["freeze_copy"], actor=ACTOR,
                  window_confirmed=None)


def test_repeated_freeze_of_unchanged_database_reports_its_own_rows(audit: dict, monkeypatch, capsys) -> None:
    # Host fix: an unchanged database backs up to identical bytes, so the second freeze shares the first one's sha256.
    stamps = iter(("2026-10-08T00:00:01Z", "2026-10-08T00:00:02Z"))
    monkeypatch.setattr(ls, "utc_now", lambda: next(stamps))
    results = []
    for _ in range(2):
        assert cli.main(_argv(audit)) == 0
        output = capsys.readouterr()
        assert not output.err
        results.append(json.loads(output.out))
    rows = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
    assert len(rows) == 4 and results[0]["sha256"] == results[1]["sha256"]
    assert [(r["freeze_row"], r["freeze_copy_row"]) for r in results] == [(2, 3), (4, 5)]
    for result, utc in zip(results, ("2026-10-08T00:00:01Z", "2026-10-08T00:00:02Z")):
        for key, event in (("freeze_row", "freeze"), ("freeze_copy_row", "freeze-copy")):
            row = rows[result[key] - 2]
            assert (row["event"], row["utc"], row["audit_id"]) == (event, utc, "freeze.AD-B")


def test_hot_journal_beside_resolved_database_refuses(audit: dict, tmp_path: Path, capsys) -> None:
    # Host fix: SQLite keeps the journal beside the resolved database, not beside a symlink to it.
    _seed_ledger(audit)
    link = tmp_path / "linked-audits.db"
    link.symlink_to(audit["db"])
    journal = Path(str(audit["db"].resolve()) + "-journal")
    journal.write_bytes(b"")
    assert not Path(str(link) + "-journal").exists()
    before = _ledger(audit)
    assert cli.main([*_argv({**audit, "db": link})]) == 1
    error = capsys.readouterr().err
    assert "refused hot journal" in error and str(journal) in error
    _unchanged(audit, before)
