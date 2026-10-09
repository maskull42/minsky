#!/usr/bin/env python3
"""
converge.py — convergence engine for one round of /minsky.

Runs after Step 3 (OpenCode adversarial), BEFORE Step 4 (Claude synthesis).
Produces:
  round-N/converge.json     (machine-readable verdict, conforms to verdict.schema.json)
  round-N/consensus.md      (human-readable summary for Claude Step 4 + user review)
  + inserts findings into audits.db (one row per (audit_id, round, step, persona, finding))
  + computes provisional decision: agree | disagree | incomplete | paused-rate-limit
                                    | user_decision_required

Implements the GodModeSkill self-consistency check verbatim: every finding with
structured evidence (file_path, line_number, quoted_line) is verified by grepping
the cited file for the verbatim quoted-line (whitespace-tolerant). Findings whose
quote can't be located are marked verified=false (likely hallucinations).

This script does NOT make the final decision. Step 4 (Claude synthesis) reads
the converge.json and consensus.md and decides actual remediation. converge.py
just surfaces the structure for Claude to reason over.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
SCHEMAS_DIR = SKILL_DIR / "schemas"
EXTERNAL_EVIDENCE_ERROR = "evidence file_path must be repo-relative unless external evidence is explicitly allowed"

from provenance import load_round_scope, reconcile_artifact, stable_finding_uid

# Try to use jsonschema if available; fall back to a structural check
try:
    import jsonschema
    _have_jsonschema = True
except Exception:
    _have_jsonschema = False


def fail(msg: str, code: int = 1) -> "None":
    print(f"converge: {msg}", file=sys.stderr)
    sys.exit(code)


def is_relative_to(path: Path, root: Path) -> bool:
    """Check resolved containment; refuse sibling-prefix matches."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_findings(path: Path) -> dict | None:
    """Load and structurally validate one findings JSON file. Returns None if missing."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"{path}: invalid JSON: {e}")
    return data


def validate_against_schema(data: dict, schema: dict, where: str) -> list[str]:
    """Return list of validation error messages. Empty list = valid."""
    errs: list[str] = []
    if _have_jsonschema:
        v = jsonschema.Draft202012Validator(schema)
        for err in sorted(v.iter_errors(data), key=lambda e: e.path):
            path = "/".join(str(p) for p in err.absolute_path) or "(root)"
            errs.append(f"{where}: {path}: {err.message}")
        return errs
    # Structural fallback
    if not isinstance(data, dict):
        return [f"{where}: top-level must be an object"]
    for k in ("persona", "findings", "verdict"):
        if k not in data:
            errs.append(f"{where}: missing required field '{k}'")
    if isinstance(data.get("findings"), list):
        for i, f in enumerate(data["findings"]):
            if not isinstance(f, dict):
                errs.append(f"{where}: findings[{i}] not an object")
                continue
            for k in ("severity", "category", "claim", "evidence", "suggestion"):
                if k not in f:
                    errs.append(f"{where}: findings[{i}].{k} missing")
            ev = f.get("evidence")
            if isinstance(ev, dict):
                for k in ("file_path", "line_number", "quoted_line"):
                    if k not in ev:
                        errs.append(f"{where}: findings[{i}].evidence.{k} missing")
    if isinstance(data.get("verdict"), dict):
        for k in ("agree", "reasoning"):
            if k not in data["verdict"]:
                errs.append(f"{where}: verdict.{k} missing")
    return errs


def whitespace_tolerant_check(file_path: Path, line_number: int, quoted: str) -> bool:
    """Return True if the cited file contains the quoted line at (or near) the cited position.

    Lifted from GodModeSkill's self-consistency check, with two refinements for
    humanities-corpus realities:

    1. Whitespace-normalized exact match (the original check).
    2. Substring containment — the agent's quote must appear inside the file's line
       (or vice versa) AND must be at least 30 characters AND at least 70% of the
       longer side's length. This catches real citations that differ only in
       trailing punctuation, JSON-quote escaping, or partial-line excerpts, while
       still requiring meaningful overlap (no tiny-token gaming).
    """
    if not file_path.is_file():
        return False
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return quoted_line_matches(text, line_number, quoted)


def quoted_line_matches(text: str, line_number: int, quoted: str) -> bool:
    """Match persisted text with the convergence rules; refuse empty quotes."""
    lines = text.splitlines()

    def normalize(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    target = normalize(quoted)
    if not target:
        return False

    def looks_like_match(file_line: str) -> bool:
        norm_file = normalize(file_line)
        if norm_file == target:
            return True
        # Substring containment with size + overlap constraints
        if len(target) < 30:
            return False
        if target in norm_file:
            return len(target) / max(1, len(norm_file)) >= 0.70
        if norm_file in target:
            return len(norm_file) / max(1, len(target)) >= 0.70
        return False

    # Direct check at cited line (1-indexed) and small off-by-one window
    if 1 <= line_number <= len(lines):
        if looks_like_match(lines[line_number - 1]):
            return True
        for offset in (-2, -1, 1, 2):
            idx = line_number - 1 + offset
            if 0 <= idx < len(lines) and looks_like_match(lines[idx]):
                return True

    # Whole-file fallback
    return any(looks_like_match(line) for line in lines)


def evidence_path(fp: str, repo_root: Path, allow_external: bool) -> Path | None:
    """Resolve evidence; refuse escaping relative paths and unapproved external paths."""
    path = Path(fp)
    if path.is_absolute():
        resolved = path.resolve()
        if is_relative_to(resolved, repo_root) or allow_external:
            return resolved
        return None
    resolved = (repo_root / path).resolve()
    if not is_relative_to(resolved, repo_root):
        return None
    return resolved


def verify_findings(data: dict, repo_root: Path, allow_external: bool) -> dict:
    """Mark evidence verified=true|false in place; refuse paths outside the allowed scope."""
    findings = data.get("findings") or []
    for f in findings:
        ev = (f or {}).get("evidence") or {}
        fp = ev.get("file_path", "")
        ln = ev.get("line_number")
        q = ev.get("quoted_line", "")
        if not (fp and ln and q):
            f["_verified"] = False
            continue
        path = evidence_path(fp, repo_root, allow_external)
        if path is None:
            f["_verified"] = False
            f["_verification_error"] = EXTERNAL_EVIDENCE_ERROR
            continue
        f["_verified"] = whitespace_tolerant_check(path, int(ln), q)
    return data


def collect_step_outputs(round_dir: Path, expected_personas: list[str]) -> tuple[dict, list[str]]:
    """Read all per-(persona) outputs from each step subdir.

    Returns: { step_name: { persona: data_dict_or_None } }
    """
    out: dict = {}
    errors: list[str] = []
    # Step 1 — Claude self: round-N/claude-self/findings/<persona>.json
    s1 = round_dir / "claude-self" / "findings"
    out["claude_self"] = {}
    found = {f.stem for f in s1.glob("*.json")} if s1.is_dir() else set()
    extras = sorted(found - set(expected_personas))
    if extras:
        errors.append(f"claude_self has unregistered persona JSON: {', '.join(extras)}")
    for persona in expected_personas:
        out["claude_self"][persona] = load_findings(s1 / f"{persona}.json")
    # Steps 2/3
    for step_name, sub in (("codex", "codex"), ("opencode", "opencode")):
        sd = round_dir / sub
        out[step_name] = {}
        found = {f.stem for f in sd.glob("*.json")} if sd.is_dir() else set()
        extras = sorted(found - set(expected_personas))
        if extras:
            errors.append(f"{step_name} has unregistered persona JSON: {', '.join(extras)}")
        for persona in expected_personas:
            out[step_name][persona] = load_findings(sd / f"{persona}.json")
    return out, errors


def reconcile_round_outputs(round_dir: Path, scope: dict) -> tuple[list[str], list[dict]]:
    """Reconcile every consumed artifact to its latest terminal call manifest."""
    errors: list[str] = []
    evidence: list[dict] = []
    pack = round_dir / "pack.xml"
    report = round_dir / "claude-self" / "report.md"
    codex_outputs = [round_dir / "codex" / f"{p}.json" for p in scope["personas"]]
    checks: list[tuple[str, str | None, Path, list[Path]]] = [
        ("claude_self", None, report, [pack]),
    ]
    for persona in scope["personas"]:
        self_output = round_dir / "claude-self" / "findings" / f"{persona}.json"
        checks.extend([
            ("claude_self", persona, self_output, [pack]),
            ("codex", persona, round_dir / "codex" / f"{persona}.json",
             [pack, report, self_output]),
            ("opencode", persona, round_dir / "opencode" / f"{persona}.json",
             [pack, report, self_output, *codex_outputs]),
        ])
    for step, persona, path, required_context in checks:
        ok, reason, manifest = reconcile_artifact(
            round_dir, step=step, persona=persona, output_path=path,
            registered_models=scope["models"],
            required_context_paths=required_context,
        )
        evidence.append({
            "step": step, "persona": persona, "output_path": str(path.resolve()),
            "ok": ok, "reason": reason,
            "call_uid": (manifest or {}).get("call_uid"),
            "manifest_path": (manifest or {}).get("_path"),
        })
        if not ok:
            label = f"{step}/{persona}" if persona else step
            errors.append(f"{label}: {reason}")
    return errors, evidence


def build_finding_index(audit_id: str, round_n: int, step_outputs: dict) -> list[dict]:
    """Build one stable entry per exact source finding, collapsing only exact duplicates."""
    indexed: dict[str, dict] = {}
    for step_name, by_persona in step_outputs.items():
        for persona, data in by_persona.items():
            for finding in (data or {}).get("findings") or []:
                uid = stable_finding_uid(audit_id, round_n, step_name, persona, finding)
                finding["_finding_uid"] = uid
                if uid in indexed:
                    indexed[uid]["source_occurrences"] += 1
                    continue
                indexed[uid] = {
                    "finding_uid": uid,
                    "round": round_n,
                    "step": step_name,
                    "persona": persona,
                    "severity": finding.get("severity"),
                    "category": finding.get("category"),
                    "claim": finding.get("claim"),
                    "verified": bool(finding.get("_verified")),
                    "source_occurrences": 1,
                }
    return sorted(indexed.values(), key=lambda item: item["finding_uid"])


def write_findings_to_db(audit_id: str, round_n: int, step_outputs: dict,
                         db_path: str | None = None) -> int:
    """Insert all findings into the audit DB. Returns count inserted."""
    helper = SKILL_DIR / "scripts" / "audit-db.py"
    count = 0
    seen_uids: set[str] = set()
    for step_name, by_persona in step_outputs.items():
        for persona, data in by_persona.items():
            if data is None:
                continue
            for f in data.get("findings") or []:
                uid = f.get("_finding_uid")
                if uid in seen_uids:
                    continue
                seen_uids.add(uid)
                ev = f.get("evidence") or {}
                # Use --opt=value form for ALL value-bearing args: a value that starts with
                # '-' / '--' (e.g. a YAML/markdown/diff '---' delimiter cited as evidence, or a
                # claim/suggestion/path beginning with a dash) is parsed correctly by argparse
                # only in the '=' form; the separated form ("--opt", "---") makes argparse treat
                # the value as an option flag and exit 2. (Apparatus hardening, 2026-05-29.)
                args = [sys.executable, str(helper)]
                if db_path:
                    args += ["--db", db_path]
                args += [
                    "insert-finding",
                    f"--audit-id={audit_id}",
                    f"--round={round_n}",
                    f"--step={step_name}",
                    f"--persona={persona}",
                    f"--severity={f.get('severity', 'low')}",
                    f"--category={f.get('category', 'other')}",
                    f"--claim={f.get('claim', '')}",
                    f"--suggestion={f.get('suggestion', '') or ''}",
                    f"--verified={'1' if f.get('_verified') else '0'}",
                ]
                if ev.get("file_path"):
                    args.append(f"--evidence-file={ev['file_path']}")
                if ev.get("line_number") is not None:
                    args.append(f"--evidence-line={ev['line_number']}")
                if ev.get("quoted_line"):
                    args.append(f"--evidence-quoted-line={ev['quoted_line']}")
                proc = subprocess.run(args, check=True, capture_output=True, text=True)
                if not proc.stdout.startswith("duplicate:"):
                    count += 1
    return count


def compute_decision(step_outputs: dict) -> tuple[str, str]:
    """Surface a provisional decision; Claude Step 4 makes the final call.

    Returns (decision, rationale).
    Decisions:
      incomplete             — schema-invalid output, missing step output, or no findings JSON
      user_decision_required — Codex AND OpenCode unanimously challenged a Claude Step 1 finding
      disagree               — at least one persona's verdict from Codex or OpenCode is "false"
                              with critical/high findings carried forward
      agree                  — all per-persona verdicts agree (or partial-with-no-criticals)
    """
    # Sanity: every step must have at least one persona JSON
    for step in ("claude_self", "codex", "opencode"):
        if not step_outputs.get(step):
            return ("incomplete", f"Step '{step}' produced no per-persona output JSON.")

    # Look for unverified critical/high findings — that's a strong signal
    has_unresolved_critical = False
    for step in ("codex", "opencode"):
        for persona, data in step_outputs[step].items():
            if data is None:
                return ("incomplete", f"{step}/{persona}.json missing or unreadable.")
            for f in data.get("findings") or []:
                if f.get("severity") in ("critical", "high"):
                    has_unresolved_critical = True

    # Tally per-persona verdicts across Codex and OpenCode
    disagrees = []
    partials_with_critical = []
    for step in ("codex", "opencode"):
        for persona, data in step_outputs[step].items():
            v = ((data or {}).get("verdict") or {}).get("agree")
            if v == "false":
                disagrees.append((step, persona))
            elif v == "partial" and has_unresolved_critical:
                partials_with_critical.append((step, persona))

    if disagrees or partials_with_critical:
        rationale = (
            f"Disagrees: {disagrees}; "
            f"Partials with unresolved critical/high: {partials_with_critical}"
        )
        return ("disagree", rationale)

    # Check user_decision_required: did Codex AND OpenCode unanimously challenge a Claude finding?
    # Heuristic: for each Claude Step 1 finding, see if Codex and OpenCode both produced a
    # finding with the same evidence file but disagreeing severity/claim or absence.
    # (Approximate; final adjudication is Step 4's job.)
    # For v1, defer: only flag user_decision_required if both Codex and OpenCode lack any
    # finding for a critical/high Claude Step 1 finding (i.e., they implicitly retract it).
    claude_findings = []
    for persona, data in step_outputs["claude_self"].items():
        if data is None:
            continue
        for f in data.get("findings") or []:
            if f.get("severity") in ("critical", "high"):
                claude_findings.append((persona, f))
    user_decision = []
    for persona, cf in claude_findings:
        ev_file = ((cf or {}).get("evidence") or {}).get("file_path")
        if not ev_file:
            continue
        codex_has = any(
            ((ff or {}).get("evidence") or {}).get("file_path") == ev_file
            for d in step_outputs["codex"].values() if d
            for ff in (d.get("findings") or [])
        )
        opencode_has = any(
            ((ff or {}).get("evidence") or {}).get("file_path") == ev_file
            for d in step_outputs["opencode"].values() if d
            for ff in (d.get("findings") or [])
        )
        if not codex_has and not opencode_has:
            user_decision.append((persona, cf.get("claim", "<no claim>")))
    if user_decision:
        return (
            "user_decision_required",
            f"Codex and OpenCode both implicitly retracted these Claude Step 1 critical/high "
            f"findings: {user_decision}. User must adjudicate.",
        )

    return ("agree", "All persona verdicts agree, or partials only with no critical/high carried forward.")


def render_consensus_md(audit_id: str, round_n: int, step_outputs: dict, decision: str, rationale: str) -> str:
    lines = [f"# Minsky audit consensus — {audit_id} round {round_n}", "", f"**Provisional decision**: `{decision}`", ""]
    lines.append(f"**Rationale**: {rationale}")
    lines.append("")
    for step_name in ("claude_self", "codex", "opencode"):
        lines.append(f"## Step: {step_name}")
        lines.append("")
        if not step_outputs.get(step_name):
            lines.append("_(no output)_")
            lines.append("")
            continue
        for persona in sorted(step_outputs[step_name].keys()):
            data = step_outputs[step_name][persona]
            lines.append(f"### {persona}")
            if data is None:
                lines.append("_(missing or unreadable)_")
                lines.append("")
                continue
            verdict = data.get("verdict") or {}
            lines.append(f"- Verdict: `{verdict.get('agree', '?')}`")
            lines.append(f"- Reasoning: {verdict.get('reasoning', '')}")
            findings = data.get("findings") or []
            lines.append(f"- Findings: {len(findings)}")
            for i, f in enumerate(findings, 1):
                ev = f.get("evidence") or {}
                tick = "✓" if f.get("_verified") else "✗"
                lines.append(
                    f"  {i}. `{f.get('_finding_uid', '?')}` [{tick} verified={f.get('_verified')}] "
                    f"**{f.get('severity','?')}** ({f.get('category','?')}): "
                    f"{f.get('claim','')}"
                )
                if ev:
                    lines.append(
                        f"     - evidence: `{ev.get('file_path','?')}`:{ev.get('line_number','?')} "
                        f"— `{(ev.get('quoted_line') or '')[:80]}`"
                    )
                if f.get("suggestion"):
                    lines.append(f"     - suggestion: {f['suggestion']}")
            lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convergence engine for one round of /minsky")
    parser.add_argument("--audit-id", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--round-dir", required=True, help="Path to round-N/")
    parser.add_argument("--skip-db", action="store_true", help="Don't write findings to audits.db (useful for V3 testing)")
    parser.add_argument("--db", help="Override audit DB path (primarily for isolated tests)")
    parser.add_argument("--allow-external-evidence", action="store_true",
        help="Permit absolute evidence paths outside the repo root. Use only for intentional external audits.")
    args = parser.parse_args(argv)

    round_dir = Path(args.round_dir).resolve()
    if not round_dir.is_dir():
        fail(f"round-dir does not exist: {round_dir}")

    # Load schema for structural validation
    schema_path = SCHEMAS_DIR / "findings.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # Scope is a per-round immutable sidecar. It deliberately permits a narrow
    # round to differ from a broader historical audit-row persona list.
    scope = load_round_scope(round_dir)
    if scope.get("audit_id") != args.audit_id or scope.get("round_number") != args.round:
        fail("round-scope audit_id/round_number does not match convergence arguments")

    # Collect only registered personas and reject stale/unregistered JSON files.
    step_outputs, collection_errs = collect_step_outputs(round_dir, scope["personas"])

    # Before reading reviewer judgments into convergence, require the latest
    # terminal record for every consumed artifact to be ok and hash-identical.
    provenance_errs, reconciliation = reconcile_round_outputs(round_dir, scope)

    # Validate every loaded JSON against schema
    all_errs: list[str] = [*collection_errs, *provenance_errs]
    for step_name, by_persona in step_outputs.items():
        for persona, data in by_persona.items():
            if data is None:
                continue
            errs = validate_against_schema(data, schema, f"{step_name}/{persona}")
            all_errs.extend(errs)
    if all_errs:
        for e in all_errs:
            print(f"converge: SCHEMA ERROR — {e}", file=sys.stderr)
        # Loud failure on schema problems — but still write a converge.json so user sees it
        decision = "incomplete"
        rationale = f"Schema validation failed for {len(all_errs)} fields. See stderr."
    else:
        # Run self-consistency check
        for step_name, by_persona in step_outputs.items():
            for persona, data in by_persona.items():
                if data is not None:
                    verify_findings(data, REPO_ROOT, args.allow_external_evidence)

        finding_index = build_finding_index(args.audit_id, args.round, step_outputs)

        # Optionally write to DB. Exact duplicates are idempotent; semantic
        # consolidation remains a synthesis/adjudication responsibility.
        if not args.skip_db:
            inserted = write_findings_to_db(
                args.audit_id, args.round, step_outputs, db_path=args.db
            )
            print(f"converge: wrote {inserted} findings to audits.db", file=sys.stderr)

        # Compute decision
        decision, rationale = compute_decision(step_outputs)

    # Stable identities are useful even on incomplete rounds, provided the
    # source JSON itself parsed. Verification flags remain false when checks did
    # not run because a provenance/schema gate failed.
    finding_index = build_finding_index(args.audit_id, args.round, step_outputs)

    evidence_scope = {
        "allow_external_evidence": args.allow_external_evidence,
        "repo_root": str(REPO_ROOT),
        "refused_external": sum(
            not f.get("_verified") and f.get("_verification_error") == EXTERNAL_EVIDENCE_ERROR
            for by_persona in step_outputs.values()
            for data in by_persona.values()
            for f in (data or {}).get("findings") or []
        ),
    }

    # Build verdict JSON
    agreement_matrix: dict = {}
    for step_name, by_persona in step_outputs.items():
        for persona, data in by_persona.items():
            v = ((data or {}).get("verdict") or {}).get("agree")
            agreement_matrix.setdefault(persona, {})[step_name] = v
    unresolved = []
    for step_name, by_persona in step_outputs.items():
        if step_name == "claude_self":
            continue
        for persona, data in by_persona.items():
            for f in (data or {}).get("findings") or []:
                if f.get("severity") in ("critical", "high"):
                    unresolved.append({
                        "finding_uid": f.get("_finding_uid"),
                        "step": step_name,
                        "persona": persona,
                        "severity": f.get("severity"),
                        "claim": f.get("claim"),
                        "verified": bool(f.get("_verified")),
                    })

    verdict = {
        "audit_id": args.audit_id,
        "round_number": args.round,
        "evidence_scope": evidence_scope,
        "step": "claude_synth",   # this verdict is what Step 4 will read
        "agreement_matrix": agreement_matrix,
        "round_scope": {
            "personas": scope["personas"],
            "models": scope["models"],
            "step_models": scope["step_models"],
        },
        "provenance_reconciliation": {
            "status": "ok" if not provenance_errs else "incomplete",
            "checks": reconciliation,
        },
        "finding_index": finding_index,
        "unresolved_findings": unresolved,
        "decision": decision,
        "rationale": rationale,
    }
    (round_dir / "converge.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")

    # Build consensus.md
    consensus = render_consensus_md(args.audit_id, args.round, step_outputs, decision, rationale)
    scope_line = (
        f"Evidence scope: external evidence allowed: {'yes' if args.allow_external_evidence else 'no'}; "
        f"refused external citations: {evidence_scope['refused_external']}"
    )
    consensus = consensus.replace("\n\n", f"\n\n{scope_line}\n\n", 1)
    (round_dir / "consensus.md").write_text(consensus, encoding="utf-8")

    print(f"converge: decision={decision}", file=sys.stderr)
    print(f"converge: wrote {round_dir/'converge.json'} and {round_dir/'consensus.md'}", file=sys.stderr)

    # Exit code reflects decision for orchestrator
    if decision == "agree":
        return 0
    if decision == "disagree":
        return 1
    if decision == "user_decision_required":
        return 3
    return 2  # incomplete or other


if __name__ == "__main__":
    sys.exit(main())
