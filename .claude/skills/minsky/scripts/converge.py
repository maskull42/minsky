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

# Try to use jsonschema if available; fall back to a structural check
try:
    import jsonschema
    _have_jsonschema = True
except Exception:
    _have_jsonschema = False


def fail(msg: str, code: int = 1) -> "None":
    print(f"converge: {msg}", file=sys.stderr)
    sys.exit(code)


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


def verify_findings(data: dict, repo_root: Path) -> dict:
    """Mark each finding's evidence as verified=true|false; return same dict (mutated)."""
    findings = data.get("findings") or []
    for f in findings:
        ev = (f or {}).get("evidence") or {}
        fp = ev.get("file_path", "")
        ln = ev.get("line_number")
        q = ev.get("quoted_line", "")
        if not (fp and ln and q):
            f["_verified"] = False
            continue
        path = repo_root / fp if not Path(fp).is_absolute() else Path(fp)
        f["_verified"] = whitespace_tolerant_check(path, int(ln), q)
    return data


def collect_step_outputs(round_dir: Path) -> dict:
    """Read all per-(persona) outputs from each step subdir.

    Returns: { step_name: { persona: data_dict_or_None } }
    """
    out: dict = {}
    # Step 1 — Claude self: round-N/claude-self/findings/<persona>.json
    s1 = round_dir / "claude-self" / "findings"
    out["claude_self"] = {}
    if s1.is_dir():
        for f in s1.glob("*.json"):
            persona = f.stem
            out["claude_self"][persona] = load_findings(f)
    # Steps 2/3
    for step_name, sub in (("codex", "codex"), ("opencode", "opencode")):
        sd = round_dir / sub
        out[step_name] = {}
        if sd.is_dir():
            for f in sd.glob("*.json"):
                out[step_name][f.stem] = load_findings(f)
    return out


def write_findings_to_db(audit_id: str, round_n: int, step_outputs: dict) -> int:
    """Insert all findings into the audit DB. Returns count inserted."""
    helper = SKILL_DIR / "scripts" / "audit-db.py"
    count = 0
    for step_name, by_persona in step_outputs.items():
        for persona, data in by_persona.items():
            if data is None:
                continue
            for f in data.get("findings") or []:
                ev = f.get("evidence") or {}
                args = [
                    "python3", str(helper), "insert-finding",
                    "--audit-id", audit_id,
                    "--round", str(round_n),
                    "--step", step_name,
                    "--persona", persona,
                    "--severity", f.get("severity", "low"),
                    "--category", f.get("category", "other"),
                    "--claim", f.get("claim", ""),
                    "--suggestion", f.get("suggestion", "") or "",
                    "--verified", "1" if f.get("_verified") else "0",
                ]
                if ev.get("file_path"):
                    args += ["--evidence-file", ev["file_path"]]
                if ev.get("line_number") is not None:
                    args += ["--evidence-line", str(ev["line_number"])]
                if ev.get("quoted_line"):
                    args += ["--evidence-quoted-line", ev["quoted_line"]]
                subprocess.run(args, check=True, capture_output=True)
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
                    f"  {i}. [{tick} verified={f.get('_verified')}] "
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
    args = parser.parse_args(argv)

    round_dir = Path(args.round_dir).resolve()
    if not round_dir.is_dir():
        fail(f"round-dir does not exist: {round_dir}")

    # Load schema for structural validation
    schema_path = SCHEMAS_DIR / "findings.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # Collect outputs
    step_outputs = collect_step_outputs(round_dir)

    # Validate every loaded JSON against schema
    all_errs: list[str] = []
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
                    verify_findings(data, REPO_ROOT)

        # Optionally write to DB
        if not args.skip_db:
            inserted = write_findings_to_db(args.audit_id, args.round, step_outputs)
            print(f"converge: wrote {inserted} findings to audits.db", file=sys.stderr)

        # Compute decision
        decision, rationale = compute_decision(step_outputs)

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
                        "finding_id": -1,  # populated by DB if needed
                        "persona": persona,
                        "severity": f.get("severity"),
                        "claim": f.get("claim"),
                        "verified": bool(f.get("_verified")),
                    })

    verdict = {
        "audit_id": args.audit_id,
        "round_number": args.round,
        "step": "claude_synth",   # this verdict is what Step 4 will read
        "agreement_matrix": agreement_matrix,
        "unresolved_findings": unresolved,
        "decision": decision,
        "rationale": rationale,
    }
    (round_dir / "converge.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")

    # Build consensus.md
    consensus = render_consensus_md(args.audit_id, args.round, step_outputs, decision, rationale)
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
