"""T20 (host-owned): the census is a UNION, and silence is never a classification (v0.2 §7.2; LC-F5).

Written line by line by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 1 Oct 2026. The judgement encoded: every audit
known to ANY of the three sources (audits.db, the RD register view, the directories) gets exactly one named status; the totals
reconcile to the union; an audit that cannot be classified fails the census loudly; a legacy-preserved audit is never sealed
(hence never packed, offloaded or expired) without a separate ruling.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from lifecycle_host_fixtures import SKILL, git, init_db, lifecycle, new_repo, write  # noqa: E402
from test_hardening import register_round  # noqa: E402

RD_HEADER = "audit_id\taudit_dir\tlocation\tmanifest_tsv\n"


def add_registered(db: Path, audit_id: str) -> None:
    schema = (SKILL / "schemas" / "audit-db.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO audits (audit_id, started_at, branch, git_commit_at_start, mode, scope_kind,"
                     " files_audited, personas_active, models_used, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (audit_id, "2026-10-01T00:00:00Z", "b", "0" * 40, "audit", "explicit", "[]", "[]", "[]",
                      "2026-10-01T01:00:00Z"))


def build_world(tmp: Path, *, orphan_registration: bool) -> tuple[Path, Path, Path]:
    """modern / legacy-preserved / bespoke (two ways) / deferred (two ways) [+ an unclassifiable registration]."""
    repo = new_repo(tmp, {"README.md": "fixture\n"})
    db = tmp / "audits.db"
    init_db(db, "modern-audit", ["x.md"])
    register_round(repo / "codex-audits" / "modern-audit" / "round-2", audit_id="modern-audit")
    add_registered(db, "legacy-audit")
    write(repo / "codex-audits" / "legacy-audit" / "round-1" / "codex" / "p.json", "{}\n")   # findings, no markers
    register_round(repo / "documentation" / "plans" / "x" / "bespoke-plan-audit" / "round-2",
                   audit_id="bespoke-plan-audit")                                          # unregistered, not under codex-audits
    add_registered(db, "bespoke-native-audit")
    native = repo / "codex-audits" / "bespoke-native-audit" / "round-2"
    register_round(native, audit_id="bespoke-native-audit")
    (native / "native-calls").mkdir()
    add_registered(db, "deferred-link-audit")
    outside = tmp / "elsewhere" / "deferred-link-audit"
    outside.mkdir(parents=True)
    (repo / "codex-audits" / "deferred-link-audit").symlink_to(outside)                     # resolves outside the repo
    rd = write(tmp / "rd_view.tsv", RD_HEADER + "rd-only-audit\tcodex-audits/rd-only-audit\textended\t\n")
    if orphan_registration:
        add_registered(db, "orphan-audit")                                                  # registered, no directory at all
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "world")
    return repo, db, rd


def census(repo: Path, db: Path, rd: Path, out: Path, *extra: str):
    return lifecycle("census", "--repo", str(repo), "--audits-db", str(db), "--rd-register-view", str(rd),
                     "--report-out", str(out), *extra)


def test_union_reconciles_with_one_status_each(tmp_path: Path) -> None:
    repo, db, rd = build_world(tmp_path, orphan_registration=False)
    write(repo / "codex-audits" / "legacy-audit" / "round-1" / "codex" / "raw.log", "12345\n")   # untracked raw bytes
    out = tmp_path / "report.json"
    done = census(repo, db, rd, out)
    assert done.returncode == 0, done.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    status = {e["audit_id"]: e["status"] for e in report["entries"]}
    assert status == {
        "modern-audit": "modern",
        "legacy-audit": "legacy-preserved",          # mutant killed: 'discover by markers only' (it would vanish)
        "bespoke-plan-audit": "bespoke",
        "bespoke-native-audit": "bespoke",
        "deferred-link-audit": "deferred",
        "rd-only-audit": "deferred",
    }
    assert report["union_size"] == 6
    assert sum(report["totals"].values()) == report["union_size"]
    legacy = next(e for e in report["entries"] if e["audit_id"] == "legacy-audit")
    assert legacy["inventory"] == {"untracked_files": 1, "untracked_bytes": 6}   # lstat-only; tracked files excluded


def test_an_unclassifiable_registration_fails_loudly(tmp_path: Path) -> None:
    repo, db, rd = build_world(tmp_path, orphan_registration=True)
    out = tmp_path / "report.json"
    done = census(repo, db, rd, out)
    assert done.returncode == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert {e["audit_id"]: e["status"] for e in report["entries"]}["orphan-audit"] == "unclassifiable"
    assert sum(report["totals"].values()) == report["union_size"] == 7


def test_census_never_follows_the_deferred_link(tmp_path: Path) -> None:
    repo, db, rd = build_world(tmp_path, orphan_registration=False)
    inside_link = tmp_path / "elsewhere" / "deferred-link-audit" / "round-9"
    register_round(inside_link, audit_id="deferred-link-audit")                 # markers exist only behind the link
    out = tmp_path / "report.json"
    assert census(repo, db, rd, out).returncode == 0
    entry = next(e for e in json.loads(out.read_text(encoding="utf-8"))["entries"]
                 if e["audit_id"] == "deferred-link-audit")
    assert entry["status"] == "deferred" and entry["rounds"] == []


def test_report_inside_the_repo_is_refused(tmp_path: Path) -> None:
    repo, db, rd = build_world(tmp_path, orphan_registration=False)
    assert census(repo, db, rd, repo / "report.json").returncode != 0
    assert not (repo / "report.json").exists()


def test_legacy_preserved_is_never_sealed_without_a_ruling(tmp_path: Path) -> None:
    repo, db, rd = build_world(tmp_path, orphan_registration=False)
    out = tmp_path / "report.json"
    recorded = census(repo, db, rd, out, "--record")
    assert recorded.returncode == 0, recorded.stderr
    legacy_dir = repo / "codex-audits" / "legacy-audit"
    classified = lifecycle("classify", "--audit", "legacy-audit", "--audit-dir", str(legacy_dir), "--repo", str(repo),
                           "--audits-db", str(db), "--actor", "ebef6afd claude-opus-5-5@test")
    assert classified.returncode == 0, classified.stderr
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "classification")
    sealed = lifecycle("seal", "--audit", "legacy-audit", "--audit-dir", str(legacy_dir), "--repo", str(repo),
                       "--audits-db", str(db), "--actor", "ebef6afd claude-opus-5-5@test",
                       "--freeze-dir", str(tmp_path / "fz"), "--freeze-copy", str(tmp_path / "fzc"),
                       "--legacy-registration", "fixture", "--audit-close-override", "fixture")
    assert sealed.returncode != 0 and "legacy-preserved" in sealed.stderr
    assert not (legacy_dir / "lifecycle" / "raw_manifest.tsv").exists()
