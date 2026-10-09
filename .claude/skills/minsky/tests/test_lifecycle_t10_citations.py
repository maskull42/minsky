"""T10 (host-owned): citation resolvability (v0.2 §6.3; LC-F6, F7, F11, F25).

Written line by line by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 1 Oct 2026. Each case states the provenance
judgement it encodes. Only audit-time bytes may resolve a citation: (a) a tracked or raw-manifest file in the audit dir, (b) the
pack.xml-embedded copy (pack.xml itself bound to a recorded context hash), (c) a CAS-held referenced-context snapshot, (d) the
git blob at the round's recorded git_head when the file was not dirty. The audit's finish commit alone never counts.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from lifecycle_host_fixtures import Finding, build_audit, commit_all, git, sha, sha_bytes, write  # noqa: E402
import converge  # noqa: E402  (scripts/ is on sys.path via test_hardening)
import lifecycle_citations  # noqa: E402

PLAN_V1 = "alpha line\nthe audited sentence that the leg quoted verbatim here\nomega line\n"
PLAN_V2 = "alpha line\nthe sentence was REWRITTEN after the audit\nomega line\n"
QUOTE = "the audited sentence that the leg quoted verbatim here"


def check(audit, store=None) -> dict:
    return lifecycle_citations.citation_resolvability(audit.dir, audit_id=audit.audit_id, repo=audit.repo,
                                                      store_root=store)


def only(result: dict) -> dict:
    assert len(result["citations"]) == 1, result
    return result["citations"][0]


def test_matcher_is_converges_own() -> None:
    assert lifecycle_citations.quoted_line_matches is converge.quoted_line_matches


def test_b_pack_embedded_copy_resolves_a_file_changed_after_the_audit(tmp_path: Path) -> None:
    """LC-F6 acceptance: a citation of a file that later changed passes via pack.xml, and fails once that copy is gone."""
    audit = build_audit(tmp_path, repo_files={"docs/plan.md": PLAN_V2}, target=["docs/plan.md"],
                        findings=[Finding("codex", "docs/plan.md", 2, QUOTE)],
                        pack_files={"docs/plan.md": PLAN_V1}, dirty_paths=[[" M", "docs/plan.md"]])
    cite = only(check(audit))
    assert (cite["status"], cite["source"]) == ("resolved", "b")
    assert cite["source_sha256"] == sha_bytes(PLAN_V1.encode("utf-8"))      # the extracted embedded text

    stripped = tmp_path / "second"
    stripped.mkdir()
    audit2 = build_audit(stripped, repo_files={"docs/plan.md": PLAN_V2}, target=["docs/plan.md"],
                         findings=[Finding("codex", "docs/plan.md", 2, QUOTE)],
                         pack_files={"docs/other.md": "unrelated\n"}, dirty_paths=[[" M", "docs/plan.md"]])
    result = check(audit2)
    assert result["result"] == "fail" and only(result)["status"] == "unresolved"


def test_b_requires_the_pack_to_be_audit_time_bytes(tmp_path: Path) -> None:
    """A pack.xml edited after the audit (its hash no longer equals any recorded context) is not a source."""
    audit = build_audit(tmp_path, repo_files={"docs/plan.md": PLAN_V2}, target=["docs/plan.md"],
                        findings=[Finding("codex", "docs/plan.md", 2, QUOTE)],
                        pack_files={"docs/plan.md": PLAN_V1}, dirty_paths=[[" M", "docs/plan.md"]])
    pack = audit.round_dir / "pack.xml"
    pack.write_text(pack.read_text(encoding="utf-8") + "<!-- edited later -->\n", encoding="utf-8")
    cite = only(check(audit))
    assert cite["status"] == "unresolved" and "pack-unanchored" in cite["detail"]


def test_d_git_blob_at_recorded_head_when_clean(tmp_path: Path) -> None:
    audit = build_audit(tmp_path, repo_files={"docs/plan.md": PLAN_V1}, target=["docs/plan.md"],
                        findings=[Finding("opencode", "docs/plan.md", 2, QUOTE)], pack_files={})
    write(audit.repo / "docs/plan.md", PLAN_V2)               # the working copy changed after the audit
    git(audit.repo, "commit", "-q", "-am", "later edit")       # and the change was even committed
    cite = only(check(audit))
    assert (cite["status"], cite["source"]) == ("resolved", "d")


def test_d_refused_when_the_file_was_dirty_at_audit_time(tmp_path: Path) -> None:
    """The recorded HEAD is not what the leg read if the file was dirty then: (d) must not be used."""
    audit = build_audit(tmp_path, repo_files={"docs/plan.md": PLAN_V1}, target=["docs/plan.md"],
                        findings=[Finding("codex", "docs/plan.md", 2, QUOTE)], pack_files={},
                        dirty_paths=[[" M", "docs/plan.md"]])
    result = check(audit)
    assert result["result"] == "fail" and only(result)["source"] is None


def test_finish_commit_alone_is_never_a_source(tmp_path: Path) -> None:
    """Mutant killed: 'accept the finish commit'. The quote exists only in a later commit that the DB names as the finish."""
    audit = build_audit(tmp_path, repo_files={"docs/plan.md": PLAN_V2}, target=["docs/plan.md"],
                        findings=[Finding("codex", "docs/plan.md", 2, QUOTE)], pack_files={},
                        dirty_paths=[[" M", "docs/plan.md"]])
    write(audit.repo / "docs/plan.md", PLAN_V1)
    finish = commit_all(audit, "finish")
    with closing(sqlite3.connect(audit.db)) as conn, conn:
        conn.execute("UPDATE audits SET git_commit_at_finish=? WHERE audit_id=?", (finish, audit.audit_id))
    result = check(audit)
    assert result["result"] == "fail" and only(result)["status"] == "unresolved"


def test_a_tracked_file_inside_the_audit_dir(tmp_path: Path) -> None:
    note_rel = "codex-audits/fixture-audit/round-1/notes.md"
    audit = build_audit(tmp_path, repo_files={}, target=["x.md"],
                        findings=[Finding("claude_self", note_rel, 2, QUOTE)], pack_files={})
    write(audit.repo / note_rel, PLAN_V1)
    commit_all(audit)
    cite = only(check(audit))
    assert (cite["status"], cite["source"]) == ("resolved", "a")
    write(audit.repo / note_rel, PLAN_V2)                      # uncommitted change: the tracked bytes no longer match
    assert check(audit)["result"] == "fail"


def test_c_cas_snapshot_of_a_referenced_context(tmp_path: Path) -> None:
    audit = build_audit(tmp_path, repo_files={}, target=["ctx/context.md"],
                        findings=[Finding("codex", "ctx/context.md", 2, QUOTE)], pack_files={},
                        extra_context={"ctx/context.md": PLAN_V1})
    context = audit.repo / "ctx/context.md"
    store = tmp_path / "cas"
    store.mkdir()
    (store / "STORE_ID").write_text("mars-minsky-cas-v1\n", encoding="utf-8")
    digest = sha(context)
    blob = store / "sha256" / digest[:2] / digest[2:4] / digest
    blob.parent.mkdir(parents=True)
    shutil.copyfile(context, blob)
    context.write_text(PLAN_V2, encoding="utf-8")              # the live context drifted after the audit
    cite = only(check(audit, store))
    assert (cite["status"], cite["source"]) == ("resolved", "c")
    assert only(check(audit, None))["status"] == "unresolved"  # without the CAS snapshot nothing audit-time remains


def test_matcher_tolerance_is_converges(tmp_path: Path) -> None:
    """Mutant killed: 'exact-line matcher'. A quote cited two lines off, with collapsed whitespace, still resolves."""
    text = "a\nb\nc\n" + "the   audited sentence that the leg   quoted verbatim here" + "\nz\n"
    audit = build_audit(tmp_path, repo_files={"docs/plan.md": text}, target=["docs/plan.md"],
                        findings=[Finding("codex", "docs/plan.md", 2, QUOTE)], pack_files={})
    assert only(check(audit))["status"] == "resolved"


def test_unverified_at_audit_does_not_block_unless_adopted(tmp_path: Path) -> None:
    """LC-F11: a leg hallucination flagged at convergence is listed, not blocking; an ADOPTED one blocks."""
    bogus = Finding("codex", "docs/missing.md", 1, "a sentence that never existed anywhere in this repo", verified=False,
                    resolution="retracted")
    audit = build_audit(tmp_path, repo_files={}, target=["x.md"], findings=[bogus], pack_files={})
    result = check(audit)
    assert result["result"] == "pass"
    assert [c["status"] for c in result["citations"]] == ["unverified-at-audit"]
    assert len(result["unverified_at_audit"]) == 1

    adopted_dir = tmp_path / "adopted"
    adopted_dir.mkdir()
    for resolution in ("addressed", "carried_forward", "user_overruled"):
        case = adopted_dir / resolution
        case.mkdir()
        f = Finding("codex", "docs/missing.md", 1, "a sentence that never existed anywhere in this repo",
                    verified=False, resolution=resolution)
        audit2 = build_audit(case, repo_files={}, target=["x.md"], findings=[f], pack_files={})
        assert check(audit2)["result"] == "fail", resolution


def test_verified_citation_must_resolve(tmp_path: Path) -> None:
    f = Finding("codex", "docs/missing.md", 1, "a sentence that never existed anywhere in this repo", verified=True,
                resolution="retracted")
    audit = build_audit(tmp_path, repo_files={}, target=["x.md"], findings=[f], pack_files={})
    assert check(audit)["result"] == "fail"


def test_decision_join_must_be_complete_and_known(tmp_path: Path) -> None:
    """LC-F7: decisions join through finding_uid; a missing or unknown join fails loudly."""
    audit = build_audit(tmp_path, repo_files={"docs/plan.md": PLAN_V1}, target=["docs/plan.md"],
                        findings=[Finding("codex", "docs/plan.md", 2, QUOTE)], pack_files={})
    resolutions = audit.round_dir / "claude-synth" / "resolutions.json"
    entries = json.loads(resolutions.read_text(encoding="utf-8"))
    entries.append({**entries[0], "finding_uid": "mf_000000000000000000000000"})
    resolutions.write_text(json.dumps(entries), encoding="utf-8")
    result = check(audit)
    assert result["result"] == "fail" and any("mf_000000000000000000000000" in e for e in result["errors"])


def test_finding_missing_from_the_index_fails(tmp_path: Path) -> None:
    audit = build_audit(tmp_path, repo_files={"docs/plan.md": PLAN_V1}, target=["docs/plan.md"],
                        findings=[Finding("codex", "docs/plan.md", 2, QUOTE)], pack_files={})
    converge_json = audit.round_dir / "converge.json"
    data = json.loads(converge_json.read_text(encoding="utf-8"))
    data["finding_index"] = []
    converge_json.write_text(json.dumps(data), encoding="utf-8")
    result = check(audit)
    assert result["result"] == "fail" and result["errors"]


def test_synthesis_evidence_must_resolve(tmp_path: Path) -> None:
    audit = build_audit(tmp_path, repo_files={"docs/plan.md": PLAN_V1}, target=["docs/plan.md"],
                        findings=[Finding("codex", "docs/plan.md", 2, QUOTE)], pack_files={})
    resolutions = audit.round_dir / "claude-synth" / "resolutions.json"
    entries = json.loads(resolutions.read_text(encoding="utf-8"))
    entries[0]["evidence"] = {"file_path": "docs/plan.md", "line_number": 1,
                              "quoted_line": "synthesis cites a line that is not in the file at all, anywhere"}
    resolutions.write_text(json.dumps(entries), encoding="utf-8")
    result = check(audit)
    assert result["result"] == "fail"
    assert any(c["origin"] == "decision-evidence" and c["status"] == "unresolved" for c in result["citations"])
