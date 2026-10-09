"""T12 (host-owned): routine expiry of a code-only raw pack is fail-closed (v0.2 §6.7; LC-F10, F1, F17; RT′ 7c).

Written line by line by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 2 Oct 2026. The judgements encoded:
- unknown never means uncited: with no sanctioned TITAN read (today), an unreadable source, a challenge or a methodology class,
  nothing is deleted;
- a dependency found by the fresh census promotes instead of deleting;
- when everything holds, ONLY the copy-A pack file goes; CAS blobs stay; the register states exactly what is no longer
  recoverable; a challenge cannot race the deletion (same per-audit lock).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import lifecycle_expire as expiry
import lifecycle_offload as lo
import lifecycle_register as lr
import test_lifecycle_t9_offload as t9
from test_lifecycle_t9_offload import commit, events, offload
from test_lifecycle_w6_pack import ACTOR, AUDIT_ID
import test_lifecycle_w6_pack as w6
from test_lifecycle_w8_register import _git

SKILL = Path(__file__).resolve().parents[1]
offload_ready = t9.offload_ready   # re-exported pytest fixture


@pytest.fixture
def expirable(offload_ready: dict, tmp_path: Path) -> dict:
    """offload_ready + offloaded + committed digest controls, a work log and a read-only audits DB."""
    audit = offload_ready
    lifecycle = audit["dir"] / "lifecycle"
    (lifecycle / "digest.json").write_text("{}\n", encoding="utf-8")
    (lifecycle / "DIGEST.md").write_text("# digest\n", encoding="utf-8")
    log = audit["repo"] / "documentation/phd_work_log.md"
    log.write_text("# log\n\n## Ongoing Log\n\n### 2026-10-02 — unrelated\n\n**Categories:** Coding.\n", encoding="utf-8")
    commit(audit, "digest controls and work log")
    offload(audit, "prepare")
    commit(audit, "offload intent")
    offload(audit, "execute")
    commit(audit, "offload completion records")
    db = tmp_path / "audits.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript((SKILL / "schemas/audit-db.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO audits (audit_id, started_at, branch, git_commit_at_start, mode, scope_kind,"
                     " files_audited, personas_active, models_used, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (AUDIT_ID, "2026-10-01T00:00:00Z", "b", "0" * 40, "audit", "explicit", "[]", "[]", "[]",
                      "2026-10-01T01:00:00Z"))
    return {**audit, "db": db, "pack": audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst"}


@pytest.fixture
def after_grace(monkeypatch):
    """Move expire's clock 100 days past now (the seal row was written today)."""
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz or UTC) + timedelta(days=100)
    monkeypatch.setattr(expiry, "datetime", Later)


def run(audit: dict, *, execute: bool, titan_reader=None) -> dict:
    return expiry.expire(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, execute=execute, audits_db=audit["db"],
                         titan_reader=titan_reader)


def no_titan_citations(audit_id: str) -> list[dict]:
    return []


def test_t12_no_sanctioned_titan_read_means_never_deleted(expirable: dict, after_grace) -> None:
    """Today's reality (no sanctioned TITAN read): every code-only audit stays ineligible. Mutant: 'treat unknown as uncited'."""
    report = run(expirable, execute=True, titan_reader=None)
    assert not report["eligible"] and "titan source unavailable (no sanctioned read)" in report["reasons"]
    assert expirable["pack"].is_file() and "expire" not in events(expirable)


def test_t12_grace_period_is_ninety_days(expirable: dict) -> None:
    report = run(expirable, execute=True, titan_reader=no_titan_citations)
    assert "90-day seal grace period has not elapsed" in report["reasons"]
    assert expirable["pack"].is_file()


def test_t12_eligible_expiry_deletes_only_the_pack_and_states_exactly_what_is_lost(expirable: dict, after_grace) -> None:
    audit = expirable
    blob_before = audit["blob"].read_bytes() if audit["blob"].exists() else None
    replica = w6.ls.store.cas_path(audit["copy_a"] / "cas", audit["blob_sha"])
    dry = run(audit, execute=False, titan_reader=no_titan_citations)
    assert dry["eligible"], dry["reasons"]
    assert audit["pack"].is_file(), "dry run is the default and deletes nothing"
    report = run(audit, execute=True, titan_reader=no_titan_citations)
    assert report["eligible"] and not audit["pack"].exists()
    assert replica.is_file() and (blob_before is None or audit["blob"].read_bytes() == blob_before)   # CAS untouched
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "expire" and row["state_after"] == "expired"
    assert row["note"].startswith("raw pack deleted; runtime CAS blobs retained and verifiable; this code-only audit's "
                                  "prompts, responses, outputs and other non-CAS raw members are no longer recoverable"
                                  "; retained_cas_refs:")
    assert (audit["dir"] / "lifecycle/EXPIRED.tsv").is_file()


def test_t12_unreadable_work_log_is_ineligible(expirable: dict, after_grace) -> None:
    log = expirable["repo"] / "documentation/phd_work_log.md"
    log.chmod(0o000)
    try:
        report = run(expirable, execute=True, titan_reader=no_titan_citations)
    finally:
        log.chmod(0o644)
    assert not report["eligible"] and expirable["pack"].is_file()


def test_t12_failing_titan_reader_is_ineligible(expirable: dict, after_grace) -> None:
    def broken(audit_id: str) -> list[dict]:
        raise ConnectionError("TITAN unreachable")
    report = run(expirable, execute=True, titan_reader=broken)
    assert not report["eligible"] and expirable["pack"].is_file()


def test_t12_methodology_citation_in_titan_promotes_instead_of_deleting(expirable: dict, after_grace) -> None:
    def cited(audit_id: str) -> list[dict]:
        return [{"category": "Methodology", "id": "daily-log-1"}]
    report = run(expirable, execute=True, titan_reader=cited)
    assert not report["eligible"] and report["dependencies"]
    assert expirable["pack"].is_file()
    assert "promotion-pending" in events(expirable)


def test_t12_challenge_blocks_and_methodology_is_never_touched(expirable: dict, after_grace) -> None:
    w6._append(expirable, "challenge", "offloaded", **{"class": "methodology-bearing"}, note="by:orchestrator; reason:x")
    report = run(expirable, execute=True, titan_reader=no_titan_citations)
    assert not report["eligible"]
    assert {"challenge without a later ruling", "class is not code-only"} <= set(report["reasons"])
    assert expirable["pack"].is_file()


def test_t12_challenge_cannot_race_expire(expirable: dict, after_grace) -> None:
    """Mutant: 'skip the lock'. A challenger holding the per-audit lock makes expire refuse outright."""
    with lr.AuditLock.acquire(expirable["repo"], AUDIT_ID, verb="challenge", session="challenger"):
        with pytest.raises(lr.LockHeld):
            run(expirable, execute=True, titan_reader=no_titan_citations)
    assert expirable["pack"].is_file()


def test_t12_not_offloaded_is_ineligible(offload_ready: dict, tmp_path: Path, after_grace) -> None:
    db = tmp_path / "audits.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript((SKILL / "schemas/audit-db.sql").read_text(encoding="utf-8"))
    report = expiry.expire(offload_ready["repo"], audit_id=AUDIT_ID, actor=ACTOR, execute=True, audits_db=db,
                           titan_reader=no_titan_citations)
    assert "state is not offloaded" in report["reasons"]
    assert (offload_ready["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst").is_file()
    _ = (_git, lo)
