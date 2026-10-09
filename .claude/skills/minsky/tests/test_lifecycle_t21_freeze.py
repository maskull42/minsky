"""T21 (host-owned): the audits.db freeze is a preservation dependency (v0.2 §6.4; LC-F3; gate G1).

Written line by line by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 1 Oct 2026. The judgement encoded: after AD-B
untracks audits.db, a hash in git is worthless unless the bytes it names exist on a second device and demonstrably restore. So a
seal is complete only with a verified off-boot freeze copy, and the copy (not just the TSV row) must restore with a matching
sha256 and a clean PRAGMA integrity_check even when the boot freeze is gone.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from lifecycle_host_fixtures import Finding, build_audit, commit_all, lifecycle  # noqa: E402
import progress  # noqa: E402

ACTOR = "ebef6afd claude-opus-5-5@test"
FREEZES_HEADER = "utc\taudit_id\tevent\tlocation\tpath\tbytes\tsha256\tintegrity_check\tnote"


def ready_audit(tmp: Path):
    """A finished, classified, committed fixture audit whose progress ends in audit_close."""
    audit = build_audit(tmp, repo_files={"notes/plain.txt": "plain\n"}, target=["notes/plain.txt"],
                        findings=[Finding("codex", "notes/plain.txt", 1, "plain", verified=False, resolution="retracted")],
                        pack_files={})
    progress_root = tmp / "progress"
    progress.emit_to(progress_root, audit.audit_id, "audit_close", convergence_status="agree", num_rounds=1)
    env = {"MINSKY_PROGRESS_ROOT": str(progress_root)}
    done = lifecycle("classify", "--audit", audit.audit_id, "--audit-dir", str(audit.dir), "--repo", str(audit.repo),
                     "--audits-db", str(audit.db), "--actor", ACTOR, env=env)
    assert done.returncode == 0, done.stderr
    commit_all(audit)
    return audit, env


def seal(audit, tmp: Path, env: dict[str, str], freeze_copy: Path):
    return lifecycle("seal", "--audit", audit.audit_id, "--audit-dir", str(audit.dir), "--repo", str(audit.repo),
                     "--audits-db", str(audit.db), "--actor", ACTOR, "--freeze-dir", str(tmp / "frozen"),
                     "--freeze-copy", str(freeze_copy), env=env)


def freeze_rows(audit) -> list[dict[str, str]]:
    lines = (audit.repo / "documentation" / "minsky_audits_db_freezes.tsv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == FREEZES_HEADER
    keys = lines[0].split("\t")
    return [dict(zip(keys, line.split("\t"))) for line in lines[1:]]


def test_freeze_is_replicated_and_restores_without_the_boot_copy(tmp_path: Path) -> None:
    audit, env = ready_audit(tmp_path)
    copy_dir = tmp_path / "copy-a" / "freezes"
    done = seal(audit, tmp_path, env, copy_dir)
    assert done.returncode == 0, done.stderr
    rows = freeze_rows(audit)
    boot = next(r for r in rows if r["event"] == "freeze")
    copy = next(r for r in rows if r["event"] == "freeze-copy")
    assert boot["sha256"] == copy["sha256"] and copy["integrity_check"] == "ok"
    copied = Path(copy["path"])
    assert copied.parent == copy_dir.resolve()
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == copy["sha256"]
    with sqlite3.connect(f"file:{copied}?mode=ro", uri=True) as conn:              # the copy really is the database
        assert conn.execute("select audit_id from audits").fetchall() == [(audit.audit_id,)]

    Path(boot["path"]).unlink()                                                   # boot freeze lost
    line_no = 1 + rows.index(copy) + 1                                            # 1-based line in the TSV
    scratch = tmp_path / "restore-scratch"
    restored = lifecycle("freeze-restore-check", "--freeze-row", str(line_no), "--scratch", str(scratch),
                         "--repo", str(audit.repo), "--actor", ACTOR, env=env)
    assert restored.returncode == 0, restored.stderr
    demo = freeze_rows(audit)[-1]
    assert demo["event"] == "restore-demo" and demo["sha256"] == copy["sha256"] and demo["integrity_check"] == "ok"
    assert not any(scratch.rglob("*.db"))                                        # the scratch copy is removed


def test_a_copy_that_was_never_written_cannot_restore(tmp_path: Path) -> None:
    """Mutant killed: 'replicate only the TSV'. If the destination bytes are absent or altered, the restore check refuses."""
    audit, env = ready_audit(tmp_path)
    assert seal(audit, tmp_path, env, tmp_path / "copy-a" / "freezes").returncode == 0
    rows = freeze_rows(audit)
    copy = next(r for r in rows if r["event"] == "freeze-copy")
    Path(copy["path"]).write_bytes(b"not a database")
    restored = lifecycle("freeze-restore-check", "--freeze-row", str(1 + rows.index(copy) + 1),
                         "--scratch", str(tmp_path / "s"), "--repo", str(audit.repo), "--actor", ACTOR, env=env)
    assert restored.returncode != 0


def test_seal_refuses_without_freeze_coverage(tmp_path: Path) -> None:
    audit, env = ready_audit(tmp_path)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the copy directory should be\n", encoding="utf-8")
    done = seal(audit, tmp_path, env, blocked / "freezes")
    assert done.returncode != 0
    register = audit.repo / "documentation" / "minsky_audit_lifecycle_register.tsv"
    events = [line.split("\t")[3] for line in register.read_text(encoding="utf-8").splitlines()[1:]]
    assert "seal" not in events
