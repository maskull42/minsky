"""Host-owned fixture builder for the lifecycle judgement tests (T9–T12, T20, T21).

Written by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 1 Oct 2026. It builds a small but complete audit inside a temp
git repository: a schema-1.1 round scope bound to the fixture repo's HEAD, findings from three legs, a hand-built converge.json
finding index (verified flags chosen per case), resolutions.json, a pack.xml that embeds artifact text, and real call manifests
written by provenance prepare/record (the pack is recorded as referenced context, so it is audit-time bytes).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

TESTS = Path(__file__).resolve().parent
SKILL = TESTS.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(TESTS))
from test_hardening import MODELS, PERSONA, STEP_MODELS, provenance  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
LIFECYCLE = SCRIPTS / "minsky-lifecycle.py"
PY = sys.executable


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True,
                          env={**os.environ, **GIT_ENV}).stdout


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def lifecycle(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([PY, str(LIFECYCLE), *args], text=True, capture_output=True,
                          env={**os.environ, **GIT_ENV, **(env or {})})


@dataclass
class Finding:
    """One finding of one leg; `verified` is what converge.json's index will say; `resolution` its synthesis decision."""
    step: str                        # claude_self | codex | opencode
    file_path: str
    line_number: int
    quoted_line: str
    verified: bool = True
    resolution: str = "addressed"
    claim: str = "claim"
    uid: str = ""


@dataclass
class Audit:
    repo: Path
    audit_id: str
    dir: Path
    round_dir: Path
    db: Path
    findings: list[Finding] = field(default_factory=list)
    pack_files: dict[str, str] = field(default_factory=dict)

    @property
    def rel(self) -> str:
        return str(self.dir.relative_to(self.repo))


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def new_repo(tmp: Path, files: dict[str, str]) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    files = {"documentation/phd_work_log.md": "# PhD Work Log (fixture)\n\n## Ongoing Log\n", **files}
    for rel, text in files.items():
        write(repo / rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def pack_xml(files: dict[str, str]) -> str:
    parts = ["<pack>\n  <artifact>\n"]
    for path, text in sorted(files.items()):
        safe = text.replace("]]>", "]]]]><![CDATA[>")
        parts.append(f"    <file path={quoteattr(path)}>\n      <![CDATA[{safe}]]>\n    </file>\n")
    parts.append("  </artifact>\n</pack>\n")
    return "".join(parts)


def init_db(db: Path, audit_id: str, files: list[str], *, finished: bool = True) -> None:
    schema = (SKILL / "schemas" / "audit-db.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executescript(schema)
        conn.execute(
            "INSERT INTO audits (audit_id, started_at, branch, git_commit_at_start, mode, scope_kind, scope_description,"
            " files_audited, personas_active, models_used) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (audit_id, "2026-10-01T00:00:00Z", "b", "0" * 40, "audit", "explicit", "fixture",
             json.dumps(files), json.dumps([PERSONA]), json.dumps(MODELS)))
        if finished:
            conn.execute("UPDATE audits SET finished_at=?, git_commit_at_finish=? WHERE audit_id=?",
                         ("2026-10-01T01:00:00Z", "f" * 40, audit_id))


def record_host_call(round_dir: Path, *, audit_id: str, uid: str, outputs: list[Path], contexts: list[Path]) -> Path:
    """A host Step-1 call through the real prepare/record (round 1; no provider; response unavailable by host limitation)."""
    model, effort = MODELS[0].split("@")
    prompt = write(round_dir / "test-evidence" / f"{uid}.prompt.txt", f"prompt {uid}\n")
    shared = ["--call-uid", uid, "--audit-id", audit_id, "--round", "1", "--round-dir", str(round_dir),
              "--step", "claude_self", "--origin", "host", "--provider", "anthropic", "--model", model,
              "--effort", effort, "--client-name", "test-client", "--client-version", "1.0",
              "--prompt-path", str(prompt), "--runtime-json", "{}"]
    argv = ["prepare", *shared]
    for out in outputs:
        argv += ["--expected-output-path", str(out)]
    for ctx in contexts:
        argv += ["--context-file", str(ctx)]
    assert provenance.main(argv) == 0
    preflight = next((round_dir / "provenance").glob(f"*.{uid}.preflight.json"))
    argv = ["record", *shared, "--preflight-path", str(preflight), "--exit-status", "ok",
            "--usage-json", json.dumps({"status": "unavailable", "source": "not exposed"}),
            "--invoked-at-not-exposed", "--duration-not-exposed", "--exit-code-not-exposed",
            "--response-unavailable-reason", "host transcript is not exposed"]
    for out in outputs:
        argv += ["--output-path", str(out)]
    for ctx in contexts:
        argv += ["--context-file", str(ctx)]
    assert provenance.main(argv) == 0
    return next((round_dir / "provenance").glob(f"*.{uid}.call.json"))


def build_audit(tmp: Path, *, repo_files: dict[str, str], target: list[str], findings: list[Finding],
                pack_files: dict[str, str], dirty_paths: list[list[str]] | None = None,
                extra_context: dict[str, str] | None = None, audit_id: str = "fixture-audit") -> Audit:
    """Commit `repo_files`, then write a one-round audit whose scope records that commit as git_head."""
    repo = new_repo(tmp, repo_files)
    head = git(repo, "rev-parse", "HEAD").strip()
    audit_dir = repo / "codex-audits" / audit_id
    round_dir = audit_dir / "round-1"
    round_dir.mkdir(parents=True)
    scope = {"schema_version": "1.1", "audit_id": audit_id, "round_number": 1, "personas": [PERSONA],
             "models": MODELS, "step_models": STEP_MODELS, "registered_at": "2026-10-01T00:00:00.000000Z",
             "git_status": "ok", "git_head": head, "dirty_paths": sorted(dirty_paths or []), "pack_sha256": None}
    write(round_dir / "round-scope.json", json.dumps(scope, indent=2) + "\n")
    pack = write(round_dir / "pack.xml", pack_xml(pack_files))
    contexts = [pack] + [write(repo / rel, text) for rel, text in (extra_context or {}).items()]
    self_report = write(round_dir / "claude-self" / "report.md", "self report\n")
    record_host_call(round_dir, audit_id=audit_id, uid="selfcall0001", outputs=[self_report], contexts=contexts)
    index = []
    for step in ("claude_self", "codex", "opencode"):
        mine = [f for f in findings if f.step == step]
        items = [{"severity": "high", "category": "provenance", "claim": f.claim,
                  "evidence": {"file_path": f.file_path, "line_number": f.line_number, "quoted_line": f.quoted_line},
                  "suggestion": "s"} for f in mine]
        folder = round_dir / ("claude-self/findings" if step == "claude_self" else step)
        doc = {"persona": PERSONA, "findings": items, "verdict": {"agree": "true", "reasoning": "r"}}
        write(folder / f"{PERSONA}.json", json.dumps(doc, indent=2) + "\n")
        for f, item in zip(mine, items):
            f.uid = provenance.stable_finding_uid(audit_id, 1, step, PERSONA, item)
            index.append({"finding_uid": f.uid, "round": 1, "step": step, "persona": PERSONA,
                          "severity": "high", "category": "provenance", "claim": f.claim,
                          "verified": f.verified, "source_occurrences": 1})
    write(round_dir / "converge.json", json.dumps({"audit_id": audit_id, "round_number": 1,
                                                   "finding_index": sorted(index, key=lambda e: e["finding_uid"])},
                                                  indent=2) + "\n")
    resolutions = [{"round": 1, "finding_uid": f.uid, "step": f.step, "persona": PERSONA, "claim": f.claim,
                    "resolution": f.resolution, "rationale": "r"} for f in findings]
    write(round_dir / "claude-synth" / "resolutions.json", json.dumps(resolutions, indent=2) + "\n")
    db = tmp / "audits.db"
    init_db(db, audit_id, target)
    return Audit(repo=repo, audit_id=audit_id, dir=audit_dir, round_dir=round_dir, db=db,
                 findings=findings, pack_files=pack_files)


def commit_all(audit: Audit, message: str = "audit records") -> str:
    git(audit.repo, "add", "-A")
    git(audit.repo, "commit", "-q", "-m", message)
    return git(audit.repo, "rev-parse", "HEAD").strip()
