#!/usr/bin/env python3
"""
claude-self-audit.py — prep + validate for Step 1 (host self-report-and-audit).

The actual writing in Step 1 is done by the explicitly registered host
coordinator. This script does:

  prep      — create the directory structure for Step 1's outputs and emit the
              checklist of files Claude must produce; also write a per-persona
              prompt-template hint Claude can reference
  validate  — verify all expected files exist and conform to findings.schema.json;
              loud-fail otherwise (no silent fallback)

Per-persona JSON files are validated against findings.schema.json. The factual
report.md is checked for existence and minimum length.

Usage:
  claude-self-audit.py prep \\
      --audit-id <id> --round <n> --personas marcion-heresiologist,ml-finetuning-phd \\
      --round-dir <path>
  claude-self-audit.py validate \\
      --audit-id <id> --round <n> --personas marcion-heresiologist,ml-finetuning-phd \\
      --round-dir <path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = SKILL_DIR / "schemas"

from provenance import load_round_scope, parse_string_list, reconcile_artifact

try:
    import jsonschema
    _have_jsonschema = True
except Exception:
    _have_jsonschema = False


def fail(msg: str, code: int = 1) -> "None":
    print(f"claude-self-audit: {msg}", file=sys.stderr)
    sys.exit(code)


def cmd_prep(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir).resolve()
    self_dir = round_dir / "claude-self"
    findings_dir = self_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    personas = [p.strip() for p in args.personas.split(",") if p.strip()]
    parse_string_list(json.dumps(personas), "--personas", persona=True)
    scope = load_round_scope(round_dir)
    if personas != scope["personas"]:
        fail("--personas must exactly match round-scope.json order and membership")

    # Write a checklist that SKILL.md / the host Claude can consult
    checklist_path = self_dir / "_step1_checklist.md"
    checklist_path.write_text(
        "# Step 1 — host self-report-and-audit checklist\n\n"
        f"Audit: `{args.audit_id}`, round {args.round}\n\n"
        "Files the registered host coordinator must produce, in order:\n\n"
        f"1. `{(self_dir / 'report.md').relative_to(round_dir.parent.parent)}`\n"
        "   - **Section A: What was done.** Factual report. List of files touched, design\n"
        "     decisions made, tradeoffs declared, attempted-and-abandoned approaches.\n"
        "     NO self-evaluation. Adversaries will read this as ground-truth context.\n"
        "   - **Section B: Per-persona candidate findings.** Headed sub-sections, one per\n"
        "     active persona below. Each sub-section is your first-pass guess at what an\n"
        "     adversary in that lens would flag — *as candidate hypotheses*, not assertions.\n\n"
        "2. For each active persona, a JSON file conforming to "
        "`findings.schema.json`:\n"
        + "".join(
            f"   - `{(findings_dir / (p + '.json')).relative_to(round_dir.parent.parent)}`\n"
            for p in personas
        )
        + "\n"
        "Each JSON must include:\n"
        "  - `persona`: the persona name (matches the file name without .json)\n"
        "  - `findings`: array of zero or more findings with severity, category, claim,\n"
        "    evidence (file_path + line_number + verbatim quoted_line), suggestion\n"
        "  - `verdict`: { agree: 'true'|'false'|'partial', reasoning: '...' }\n\n"
        "When you finish, run `claude-self-audit.py validate` to confirm.\n",
        encoding="utf-8",
    )

    print(f"prep: created {self_dir} with checklist for {len(personas)} personas", file=sys.stderr)
    print(str(self_dir.resolve()))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir).resolve()
    self_dir = round_dir / "claude-self"
    report = self_dir / "report.md"
    findings_dir = self_dir / "findings"
    personas = [p.strip() for p in args.personas.split(",") if p.strip()]
    parse_string_list(json.dumps(personas), "--personas", persona=True)
    scope = load_round_scope(round_dir)
    if personas != scope["personas"]:
        fail("--personas must exactly match round-scope.json order and membership")

    errors: list[str] = []

    # report.md exists and has both Section A and Section B headers
    if not report.is_file():
        errors.append(f"missing {report}")
    else:
        text = report.read_text(encoding="utf-8")
        if len(text.strip()) < 200:
            errors.append(f"{report} appears too short (<200 chars)")
        if "Section A" not in text or "Section B" not in text:
            errors.append(f"{report} must contain explicit 'Section A' and 'Section B' headings")

    # One findings file per persona, schema-valid
    schema_path = SCHEMAS_DIR / "findings.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    extras = sorted(
        f.stem for f in findings_dir.glob("*.json") if f.stem not in set(personas)
    ) if findings_dir.is_dir() else []
    if extras:
        errors.append(f"unregistered persona findings JSON: {', '.join(extras)}")
    for p in personas:
        f = findings_dir / f"{p}.json"
        if not f.is_file():
            errors.append(f"missing {f}")
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"{f}: invalid JSON: {e}")
            continue
        if data.get("persona") != p:
            errors.append(f"{f}: persona must equal {p!r}")
        if _have_jsonschema:
            v = jsonschema.Draft202012Validator(schema)
            for err in v.iter_errors(data):
                path = "/".join(str(x) for x in err.absolute_path) or "(root)"
                errors.append(f"{f}: {path}: {err.message}")
        else:
            # Structural minimum
            for k in ("persona", "findings", "verdict"):
                if k not in data:
                    errors.append(f"{f}: missing field {k!r}")

    # Require the observable host call record before any paid adversarial leg.
    provenance_outputs = [(None, report)] + [
        (p, findings_dir / f"{p}.json") for p in personas
    ]
    for persona, output in provenance_outputs:
        ok, reason, _manifest = reconcile_artifact(
            round_dir, step="claude_self", persona=persona, output_path=output,
            registered_models=scope["models"],
            required_context_paths=[round_dir / "pack.xml"],
        )
        if not ok:
            errors.append(f"claude_self provenance for {output.name}: {reason}")

    if errors:
        for e in errors:
            print(f"validate: ERROR — {e}", file=sys.stderr)
        fail(f"{len(errors)} validation error(s) — Step 1 outputs not ready", code=2)

    print(f"validate: OK — Step 1 outputs complete and valid for {len(personas)} personas", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prep/validate Step 1 outputs (Claude self-audit)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("prep", "validate"):
        s = sub.add_parser(name)
        s.add_argument("--audit-id", required=True)
        s.add_argument("--round", type=int, required=True)
        s.add_argument("--personas", required=True, help="Comma-separated list")
        s.add_argument("--round-dir", required=True)
    args = parser.parse_args(argv)
    return {"prep": cmd_prep, "validate": cmd_validate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
