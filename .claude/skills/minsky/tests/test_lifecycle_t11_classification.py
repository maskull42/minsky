"""T11 (host-owned): classification (v0.2 §6.2; v0.1 §6.2; RT′ 7c; LC-F24).

Written line by line by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 1 Oct 2026. The judgement encoded: a fired
trigger can never be downgraded by the host; only the researcher's verbatim ruling can. Doubt resolves to methodology-bearing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from lifecycle_host_fixtures import SKILL, Finding, build_audit, lifecycle, write  # noqa: E402

TRIGGERS = SKILL / "config" / "methodology_triggers.txt"
GLOBS = [line.strip() for line in TRIGGERS.read_text(encoding="utf-8").splitlines()
         if line.strip() and not line.lstrip().startswith("#")]
# One path per glob that matches THAT glob and, where possible, no other (the minsky glob gets a scripts path, not personas).
PROBE = {
    "phd_project_context/**": "phd_project_context/condensed.md",
    "documentation/*pipeline_plan*": "documentation/hermeneut_pipeline_plan.md",
    "documentation/plans/**": "documentation/plans/some_plan.md",
    ".claude/agents/**": ".claude/agents/reviewer.md",
    ".claude/skills/*quality*/**": ".claude/skills/dialogic-quality-check/SKILL.md",
    ".claude/skills/minsky/**": ".claude/skills/minsky/scripts/x.py",
    "src/agents/*quality*": "src/agents/d9_quality_checks.py",
    "scripts/r3_*": "scripts/r3_roles.py",
    "data/**/profiles/**": "data/dialogic/profiles/x.txt",
    "**/*profile*.json": "notes/a_profile_b.json",
    "**/export*/**": "notes/exports/rows.txt",
    ".claude/skills/minsky/personas/source-corpus/**": ".claude/skills/minsky/personas/source-corpus/x.md",
    "**/rubric*": "notes/rubric_v2.md",
}
NEUTRAL = "notes/plain.txt"


def test_the_trigger_file_is_the_ratified_list() -> None:
    assert GLOBS == list(PROBE), "methodology_triggers.txt must hold exactly v0.2 §6.2's thirteen globs, in order"


def classify(audit, *extra: str):
    return lifecycle("classify", "--audit", audit.audit_id, "--audit-dir", str(audit.dir), "--repo", str(audit.repo),
                     "--audits-db", str(audit.db), "--actor", "ebef6afd claude-opus-5-5@test", *extra)


def statement(tmp: Path) -> Path:
    return write(tmp / "statement.md", "No finding or adopted decision changed method, theory, a rubric, a profile, "
                                       "training data, evaluation or the audit method itself. Evidence: none cited.\n")


def neutral_audit(tmp: Path, *, target: str = NEUTRAL, evidence: str = NEUTRAL, pack_path: str = NEUTRAL):
    return build_audit(tmp, repo_files={NEUTRAL: "plain\n"}, target=[target],
                       findings=[Finding("codex", evidence, 1, "plain")], pack_files={pack_path: "plain\n"})


def test_neutral_audit_can_be_code_only_with_a_statement(tmp_path: Path) -> None:
    audit = neutral_audit(tmp_path)
    assert classify(audit, "--class", "code-only").returncode != 0           # a statement is required
    done = classify(audit, "--class", "code-only", "--statement-file", str(statement(tmp_path)))
    assert done.returncode == 0, done.stderr
    text = (audit.dir / "lifecycle" / "classification.md").read_text(encoding="utf-8")
    assert "code-only" in text and "titan: unknown-no-sanctioned-read" in text


@pytest.mark.parametrize("glob", list(PROBE))
@pytest.mark.parametrize("where", ["target", "evidence", "pack"])
def test_each_glob_forces_methodology_bearing(tmp_path: Path, glob: str, where: str) -> None:
    """Mutant killed (among others): 'ignore the .claude/skills/minsky/** glob'."""
    probe = PROBE[glob]
    audit = neutral_audit(tmp_path, **{where if where != "pack" else "pack_path": probe})
    refused = classify(audit, "--class", "code-only", "--statement-file", str(statement(tmp_path)))
    assert refused.returncode != 0
    assert glob in refused.stderr
    assert not (audit.dir / "lifecycle" / "classification.md").exists()
    accepted = classify(audit, "--class", "methodology-bearing")
    assert accepted.returncode == 0, accepted.stderr


def test_default_is_methodology_bearing(tmp_path: Path) -> None:
    audit = neutral_audit(tmp_path)
    done = classify(audit)
    assert done.returncode == 0, done.stderr
    text = (audit.dir / "lifecycle" / "classification.md").read_text(encoding="utf-8")
    assert "methodology-bearing" in text and "default" in text


def test_work_log_methodology_citation_forces_methodology_bearing(tmp_path: Path) -> None:
    audit = neutral_audit(tmp_path)
    write(audit.repo / "documentation" / "phd_work_log.md",
          "# log\n\n## Ongoing Log\n\n### 2026-10-01 — an entry\n\n**Categories:** Coding, Methodology. "
          f"**Phase:** phase3.\n\nThe audit {audit.audit_id} changed the evaluation rubric.\n")
    refused = classify(audit, "--class", "code-only", "--statement-file", str(statement(tmp_path)))
    assert refused.returncode != 0 and "work-log" in refused.stderr


def test_work_log_entry_in_coding_only_does_not_fire(tmp_path: Path) -> None:
    audit = neutral_audit(tmp_path)
    write(audit.repo / "documentation" / "phd_work_log.md",
          "# log\n\n### 2026-10-01 — an entry\n\n**Categories:** Coding.\n\nMentions " + audit.audit_id + ".\n")
    done = classify(audit, "--class", "code-only", "--statement-file", str(statement(tmp_path)))
    assert done.returncode == 0, done.stderr


def test_unreadable_work_log_is_unknown_and_forces_methodology(tmp_path: Path) -> None:
    audit = neutral_audit(tmp_path)
    log = write(audit.repo / "documentation" / "phd_work_log.md", "x\n")
    log.chmod(0o000)
    try:
        refused = classify(audit, "--class", "code-only", "--statement-file", str(statement(tmp_path)))
    finally:
        log.chmod(0o644)
    assert refused.returncode != 0 and "unknown" in refused.stderr


def test_downgrade_only_by_a_recorded_ruling(tmp_path: Path) -> None:
    audit = neutral_audit(tmp_path, target=PROBE["documentation/plans/**"])
    assert classify(audit, "--class", "code-only", "--statement-file", str(statement(tmp_path))).returncode != 0
    assert classify(audit).returncode == 0                     # registered (methodology-bearing by default)
    ruling = lifecycle("ruling", "--audit", audit.audit_id, "--kind", "downgrade", "--verbatim",
                       "Classify it code-only (researcher, fixture)", "--researcher-session", "fixture-session",
                       "--repo", str(audit.repo), "--actor", "ebef6afd claude-opus-5-5@test")
    assert ruling.returncode == 0, ruling.stderr
    done = classify(audit, "--class", "code-only", "--statement-file", str(statement(tmp_path)))
    assert done.returncode == 0, done.stderr
    latest = audit.dir / "lifecycle" / "classification.2.md"           # never overwrites the first record
    assert "Classify it code-only (researcher, fixture)" in latest.read_text(encoding="utf-8")
    assert "class: methodology-bearing" in (audit.dir / "lifecycle" / "classification.md").read_text(encoding="utf-8")


def test_challenge_forces_methodology_until_ruled(tmp_path: Path) -> None:
    audit = neutral_audit(tmp_path)
    challenged = lifecycle("challenge", "--audit", audit.audit_id, "--by", "orchestrator", "--reason", "cited later",
                           "--repo", str(audit.repo), "--actor", "ebef6afd claude-opus-5-5@test")
    assert challenged.returncode == 0, challenged.stderr
    refused = classify(audit, "--class", "code-only", "--statement-file", str(statement(tmp_path)))
    assert refused.returncode != 0 and "challenge" in refused.stderr


def test_the_trigger_file_hash_is_recorded(tmp_path: Path) -> None:
    import hashlib
    audit = neutral_audit(tmp_path)
    assert classify(audit).returncode == 0
    text = (audit.dir / "lifecycle" / "classification.md").read_text(encoding="utf-8")
    assert hashlib.sha256(TRIGGERS.read_bytes()).hexdigest() in text
