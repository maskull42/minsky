"""Refuse external citations and scopes unless their explicit opt-in permits them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import jsonschema
import pytest

from test_hardening import (
    PERSONA, SCRIPTS, finding_doc, load_script,
    record_call, register_round,
)
from test_lifecycle_w3b_forward import _git, _launch_args


converge = load_script("w10a_converge", "converge.py")
chain = load_script("w10a_chain", "chain.py")
scope_detect = load_script("w10a_scope_detect", "scope-detect.py")
launcher = load_script("w10a_launch_chain", "launch-chain.py")
REFUSAL = "evidence file_path must be repo-relative unless external evidence is explicitly allowed"
QUOTE = "Persisted fixture evidence with an exact matching quoted line."


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "inside.txt").write_text(QUOTE + "\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text(QUOTE + "\n", encoding="utf-8")
    return root


def _finding(path: str) -> dict:
    return {
        "severity": "medium", "category": "provenance", "claim": "Fixture citation",
        "evidence": {"file_path": path, "line_number": 1, "quoted_line": QUOTE},
        "suggestion": "Keep the cited evidence.",
    }


def _check_evidence(root: Path, path: str, allow_external: bool, accepted: bool) -> None:
    resolved = converge.evidence_path(path, root, allow_external)
    expected = (root / path).resolve() if accepted else None
    assert resolved == expected
    data = finding_doc(findings=[_finding(path)])
    assert converge.verify_findings(data, root, allow_external) is data
    finding = data["findings"][0]
    assert finding["_verified"] is accepted
    if accepted:
        assert "_verification_error" not in finding
    else:
        assert finding["_verification_error"] == REFUSAL


def test_repo_relative_evidence_verified(repo_root: Path) -> None:
    _check_evidence(repo_root, "inside.txt", False, True)


@pytest.mark.parametrize("allow_external", [False, True])
def test_relative_escape_refused(repo_root: Path, allow_external: bool) -> None:
    _check_evidence(repo_root, "../outside.txt", allow_external, False)


def test_absolute_inside_evidence_verified(repo_root: Path) -> None:
    _check_evidence(repo_root, str(repo_root / "inside.txt"), False, True)


@pytest.mark.parametrize("allow_external", [False, True])
def test_absolute_outside_evidence_requires_opt_in(repo_root: Path, allow_external: bool) -> None:
    _check_evidence(repo_root, str(repo_root.parent / "outside.txt"), allow_external, allow_external)


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("allow_external", [False, True])
def test_symlink_evidence_uses_resolved_boundary(repo_root: Path, absolute: bool,
                                                allow_external: bool) -> None:
    link = repo_root / "external-link.txt"
    link.symlink_to(repo_root.parent / "outside.txt")
    path = str(link) if absolute else link.name
    _check_evidence(repo_root, path, allow_external, absolute and allow_external)


def _valid_round(tmp_path: Path) -> Path:
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    pack = round_dir / "pack.xml"
    pack.write_text("<pack/>\n", encoding="utf-8")
    self_dir = round_dir / "claude-self"
    (self_dir / "findings").mkdir(parents=True)
    report = self_dir / "report.md"
    report.write_text("Section A\n" + "x" * 220 + "\nSection B\n", encoding="utf-8")
    self_json = self_dir / "findings" / f"{PERSONA}.json"
    self_json.write_text(json.dumps(finding_doc()), encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text(QUOTE + "\n", encoding="utf-8")
    codex_json = round_dir / "codex" / f"{PERSONA}.json"
    codex_json.parent.mkdir()
    codex_json.write_text(json.dumps(finding_doc(findings=[_finding(str(outside))])), encoding="utf-8")
    opencode_json = round_dir / "opencode" / f"{PERSONA}.json"
    opencode_json.parent.mkdir()
    opencode_json.write_text(json.dumps(finding_doc()), encoding="utf-8")
    record_call(
        round_dir, uid="w10ahost", step="claude_self", model="gpt-6-astra",
        effort="not_exposed", outputs=[report, self_json], invoked_at="2026-09-05T10:00:00Z",
        origin="host", raw_response=False, context_files=[pack],
    )
    record_call(
        round_dir, uid="w10acodex", step="codex", model="gpt-5.6-sol", effort="medium",
        persona=PERSONA, outputs=[codex_json], invoked_at="2026-09-05T10:01:00Z",
        usage={"status": "total-only", "total_tokens": 1},
        context_files=[pack, report, self_json],
    )
    record_call(
        round_dir, uid="w10aopencode", step="opencode", model="gemini-3.8-flash", effort="high",
        persona=PERSONA, outputs=[opencode_json], invoked_at="2026-09-05T10:02:00Z",
        usage={"status": "complete", "input_tokens": 1, "output_tokens": 1,
               "reasoning_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
               "total_tokens": 2, "cost_usd": 0},
        context_files=[pack, report, self_json, codex_json],
    )
    return round_dir


def test_converge_external_scope_metadata_and_incomplete_round(tmp_path: Path) -> None:
    round_dir = _valid_round(tmp_path)
    db = tmp_path / "unused-audit.db"
    argv = [sys.executable, str(SCRIPTS / "converge.py"), "--audit-id", "test-audit",
            "--round", "2", "--round-dir", str(round_dir), "--skip-db", "--db", str(db)]
    schema = json.loads((SCRIPTS.parent / "schemas" / "verdict.schema.json").read_text(encoding="utf-8"))
    for incomplete in (False, True):
        if incomplete:
            (round_dir / "opencode" / f"{PERSONA}.json").unlink()
        for allow_external in (False, True):
            result = subprocess.run(
                [*argv, *(["--allow-external-evidence"] if allow_external else [])],
                text=True, capture_output=True,
            )
            assert result.returncode == (2 if incomplete else 0), result.stderr
            verdict = json.loads((round_dir / "converge.json").read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(schema).validate(verdict)
            refused = int(not allow_external and not incomplete)
            assert verdict["evidence_scope"] == {
                "allow_external_evidence": allow_external,
                "repo_root": str(converge.REPO_ROOT), "refused_external": refused,
            }
            assert verdict["decision"] == ("incomplete" if incomplete else "agree")
            assert verdict["finding_index"][0]["verified"] is (allow_external and not incomplete)
            line = (f"Evidence scope: external evidence allowed: {'yes' if allow_external else 'no'}; "
                    f"refused external citations: {refused}")
            assert line in (round_dir / "consensus.md").read_text(encoding="utf-8").splitlines()[:8]
            legacy = {key: value for key, value in verdict.items() if key != "evidence_scope"}
            jsonschema.Draft202012Validator(schema).validate(legacy)
            assert not db.exists()
    scope_schema = schema["properties"]["evidence_scope"]
    valid = {"allow_external_evidence": False, "repo_root": "fixture", "refused_external": 0}
    invalid = [
        {key: value for key, value in valid.items() if key != missing}
        for missing in valid
    ] + [
        {**valid, "extra": True}, {**valid, "refused_external": -1},
        {**valid, "allow_external_evidence": "false"}, {**valid, "repo_root": 1},
        {**valid, "refused_external": 0.5},
    ]
    for value in invalid:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(scope_schema).validate(value)


@pytest.mark.parametrize("allow_external", [False, True])
def test_chain_converge_argv_forwards_only_explicit_opt_in(tmp_path: Path, allow_external: bool) -> None:
    args = Namespace(audit_id="test-audit", round=2, allow_external_evidence=allow_external)
    expected = [sys.executable, str(SCRIPTS / "converge.py"), "--audit-id", "test-audit",
                "--round", "2", "--round-dir", str(tmp_path)]
    if allow_external:
        expected.append("--allow-external-evidence")
    assert chain._converge_argv(args, tmp_path) == expected


def test_launch_external_evidence_reaches_converge_argv(tmp_path: Path) -> None:
    args = _launch_args(tmp_path, extra=["--", "--allow-external-evidence"])
    launch = launcher.build_launch(args)
    assert launch.argv[-1] == "--allow-external-evidence"
    assert "CHAIN_ARGV=" in launch.wrapper
    assert "--allow-external-evidence" in launch.wrapper
    forwarded = Namespace(audit_id=args.audit_id, round=args.round,
                          allow_external_evidence="--allow-external-evidence" in launch.argv)
    assert "--allow-external-evidence" in chain._converge_argv(forwarded, Path(args.round_dir))
    assert not list(Path(args.round_dir).iterdir())


@pytest.fixture
def scope_repo(repo_root: Path, monkeypatch) -> Path:
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(key, "2026-10-01T00:00:00Z")
    _git(repo_root, "init", "--quiet", "--initial-branch=main")
    _git(repo_root, "add", "--", "inside.txt")
    _git(repo_root, "commit", "--quiet", "-m", "W10a scope fixture")
    return repo_root


def _scope_subprocess(repo: Path, *rest: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "scope-detect.py"), "audit", *rest],
        cwd=repo, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, text=True, capture_output=True,
    )


def test_scope_outside_refused(scope_repo: Path) -> None:
    outside = scope_repo.parent / "outside.txt"
    result = _scope_subprocess(scope_repo, str(outside))
    assert result.returncode != 0
    assert result.stderr.strip() == (
        f"scope-detect: refusing absolute path outside repo without --allow-external: {outside.resolve()}"
    )


def test_scope_outside_allowed_explicitly(scope_repo: Path) -> None:
    outside = scope_repo.parent / "outside.txt"
    result = _scope_subprocess(scope_repo, "--allow-external", str(outside))
    assert result.returncode == 0, result.stderr
    scope = json.loads(result.stdout)
    assert scope["allow_external"] is True
    assert scope["files"] == [str(outside.resolve())]


def test_scope_inside_emitted_repo_relative(scope_repo: Path) -> None:
    result = _scope_subprocess(scope_repo, str(scope_repo / "inside.txt"))
    assert result.returncode == 0, result.stderr
    scope = json.loads(result.stdout)
    assert scope["allow_external"] is False
    assert scope["files"] == ["inside.txt"]


def test_scope_sibling_prefix_refused(scope_repo: Path) -> None:
    sibling = scope_repo.with_name(scope_repo.name + "2")
    sibling.mkdir()
    outside = sibling / "file.txt"
    outside.write_text(QUOTE + "\n", encoding="utf-8")
    result = _scope_subprocess(scope_repo, str(outside))
    assert result.returncode != 0
    assert result.stderr.strip() == (
        f"scope-detect: refusing absolute path outside repo without --allow-external: {outside.resolve()}"
    )


@pytest.mark.parametrize("mode,rest", [
    ("explicit", ["fixture-task"]), ("delta", []),
    ("paths", ["inside.txt"]), ("time", ["--since", "2 hours ago"]),
])
@pytest.mark.parametrize("allow_external", [False, True])
def test_scope_records_opt_in_in_every_mode(repo_root: Path, monkeypatch, capsys,
                                           mode: str, rest: list[str], allow_external: bool) -> None:
    monkeypatch.setattr(scope_detect, "find_repo_root", lambda: repo_root)
    monkeypatch.setattr(scope_detect, "current_branch", lambda repo: "main")
    monkeypatch.setattr(scope_detect, "head_commit", lambda repo: "a" * 40)
    monkeypatch.setattr(scope_detect, "last_audit_commit_for_branch", lambda repo, branch: None)
    monkeypatch.setattr(scope_detect, "changed_paths", lambda repo, base: ["inside.txt"])
    monkeypatch.setattr(scope_detect, "run_git", lambda args, repo: "inside.txt")
    argv = ["audit", *rest, *(["--allow-external"] if allow_external else [])]
    assert scope_detect.main(argv) == 0
    scope = json.loads(capsys.readouterr().out)
    assert scope["scope_kind"] == mode
    assert scope["allow_external"] is allow_external
