#!/usr/bin/env python3
"""
claude-synth.py — prep + validate for Step 4 (host-coordinator synthesis).

Like claude-self-audit.py, the actual reasoning happens in the explicitly named
host coordinator. This script does directory prep and post-validation.

Step 4 outputs (in round-N/claude-synth/):
  decisions.md         — markdown summary of what to actually change vs. defer/retract.
                         Per-finding resolution: addressed | carried_forward | retracted
                         | user_overruled. Surfaces user-decision-required cases at top.
  resolutions.json     — machine-readable structured resolution table; one entry per
                         finding seen this round, with finding identifier and resolution.
                         Used to update the audit DB findings.resolution column.

Usage:
  claude-synth.py prep --audit-id <id> --round <n> --round-dir <path>
  claude-synth.py validate --audit-id <id> --round <n> --round-dir <path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

from provenance import load_round_scope, reconcile_artifact


def fail(msg: str, code: int = 1) -> "None":
    print(f"claude-synth: {msg}", file=sys.stderr)
    sys.exit(code)


def cmd_prep(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir).resolve()
    synth_dir = round_dir / "claude-synth"
    synth_dir.mkdir(parents=True, exist_ok=True)

    converge_path = round_dir / "converge.json"
    consensus_path = round_dir / "consensus.md"

    if not converge_path.is_file():
        fail(f"missing {converge_path} — run converge.py before prep'ing Step 4")
    if not consensus_path.is_file():
        fail(f"missing {consensus_path} — run converge.py before prep'ing Step 4")

    converge = json.loads(converge_path.read_text(encoding="utf-8"))
    decision = converge.get("decision", "?")

    checklist = (
        "# Step 4 — host-coordinator synthesis checklist\n\n"
        f"Audit: `{args.audit_id}`, round {args.round}\n\n"
        f"Provisional decision from `converge.py`: **`{decision}`**\n\n"
        "Read first:\n"
        f"  - `{converge_path.relative_to(round_dir.parent.parent)}` — structured verdict + agreement matrix\n"
        f"  - `{consensus_path.relative_to(round_dir.parent.parent)}` — human-readable per-step output\n"
        f"  - All `claude-self/findings/*.json`, `codex/*.json`, `opencode/*.json` (raw outputs)\n\n"
        "Files the host coordinator must produce using the registered model/effort:\n\n"
        f"1. `{(synth_dir / 'decisions.md').relative_to(round_dir.parent.parent)}`\n"
        "   - For each finding from this round, decide and justify:\n"
        "     - **addressed** — proposer should change X (specify exactly what)\n"
        "     - **carried_forward** — finding stands; defer to next round or user override\n"
        "     - **retracted** — finding doesn't actually hold under closer reading\n"
        "     - **user_overruled** — user must adjudicate (Codex+OpenCode unanimously\n"
        "       challenged a Claude Step 1 finding, OR a critical/high finding has\n"
        "       conflicting evidence that this synthesis cannot resolve alone)\n"
        "   - Surface `user_overruled` cases at the TOP of the document so the user\n"
        "     reads them first.\n"
        "   - For each `addressed` resolution, propose the concrete change (file path,\n"
        "     before/after, or natural-language description if it's a methodology fix).\n\n"
        f"2. `{(synth_dir / 'resolutions.json').relative_to(round_dir.parent.parent)}`\n"
        "   - JSON array, one entry per finding from this round:\n"
        "     ```\n"
        "     [\n"
        "       {\n"
        '         "round": <n>,\n'
        '         "finding_uid": "<mf_... from converge.finding_index>",\n'
        '         "step": "claude_self|codex|opencode",\n'
        '         "persona": "<name>",\n'
        '         "claim": "<verbatim from the finding>",\n'
        '         "resolution": "addressed|carried_forward|retracted|user_overruled",\n'
        '         "rationale": "<one sentence>",\n'
        '         "concrete_change": "<optional, only if addressed>"\n'
        "       },\n"
        "       ...\n"
        "     ]\n"
        "     ```\n\n"
        "When you finish, run `claude-synth.py validate` to confirm.\n"
    )
    (synth_dir / "_step4_checklist.md").write_text(checklist, encoding="utf-8")

    print(f"prep: created {synth_dir} with synthesis checklist", file=sys.stderr)
    print(str(synth_dir.resolve()))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir).resolve()
    synth_dir = round_dir / "claude-synth"
    decisions = synth_dir / "decisions.md"
    resolutions = synth_dir / "resolutions.json"

    errors: list[str] = []
    if not decisions.is_file():
        errors.append(f"missing {decisions}")
    else:
        text = decisions.read_text(encoding="utf-8")
        if len(text.strip()) < 100:
            errors.append(f"{decisions}: too short (<100 chars)")

    converge_path = round_dir / "converge.json"
    finding_index: list[dict] | None = None
    if not converge_path.is_file():
        errors.append(f"missing {converge_path}")
    else:
        try:
            converge = json.loads(converge_path.read_text(encoding="utf-8"))
            raw_index = converge.get("finding_index")
            if not isinstance(raw_index, list):
                errors.append(f"{converge_path}: missing stable finding_index")
            else:
                finding_index = raw_index
        except json.JSONDecodeError as e:
            errors.append(f"{converge_path}: invalid JSON: {e}")

    scope = load_round_scope(round_dir)
    if scope.get("audit_id") != args.audit_id or scope.get("round_number") != args.round:
        errors.append("round-scope audit_id/round_number does not match validation arguments")

    if not resolutions.is_file():
        errors.append(f"missing {resolutions}")
    else:
        try:
            data = json.loads(resolutions.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"{resolutions}: invalid JSON: {e}")
        else:
            if not isinstance(data, list):
                errors.append(f"{resolutions}: must be a JSON array")
            else:
                allowed = {"addressed", "carried_forward", "retracted", "user_overruled"}
                for i, entry in enumerate(data):
                    if not isinstance(entry, dict):
                        errors.append(f"{resolutions}[{i}]: not an object")
                        continue
                    for k in ("finding_uid", "round", "step", "persona", "claim", "resolution", "rationale"):
                        if k not in entry:
                            errors.append(f"{resolutions}[{i}]: missing field {k!r}")
                    if entry.get("resolution") not in allowed:
                        errors.append(
                            f"{resolutions}[{i}]: resolution must be one of {sorted(allowed)}"
                        )
                if finding_index is not None:
                    expected = {item.get("finding_uid"): item for item in finding_index}
                    actual: dict[str, dict] = {}
                    for i, entry in enumerate(data):
                        if not isinstance(entry, dict):
                            continue
                        uid = entry.get("finding_uid")
                        if uid in actual:
                            errors.append(f"{resolutions}[{i}]: duplicate finding_uid {uid!r}")
                        elif isinstance(uid, str):
                            actual[uid] = entry
                    missing = sorted(set(expected) - set(actual))
                    extra = sorted(set(actual) - set(expected))
                    if missing:
                        errors.append(f"{resolutions}: missing {len(missing)} finding_uid(s): {missing}")
                    if extra:
                        errors.append(f"{resolutions}: unknown {len(extra)} finding_uid(s): {extra}")
                    for uid in sorted(set(expected) & set(actual)):
                        source, resolution = expected[uid], actual[uid]
                        for field in ("round", "step", "persona", "claim"):
                            if resolution.get(field) != source.get(field):
                                errors.append(
                                    f"{resolutions}: {uid} field {field!r} does not match finding_index"
                                )

    # Final adjudication is not provenance-exempt. Require a terminal-ok host
    # call manifest covering both synthesis artifacts and matching their hashes.
    synth_context = [
        converge_path,
        round_dir / "consensus.md",
        round_dir / "claude-self" / "report.md",
        *[round_dir / "claude-self" / "findings" / f"{p}.json" for p in scope["personas"]],
        *[round_dir / "codex" / f"{p}.json" for p in scope["personas"]],
        *[round_dir / "opencode" / f"{p}.json" for p in scope["personas"]],
    ]
    for output in (decisions, resolutions):
        ok, reason, _manifest = reconcile_artifact(
            round_dir, step="claude_synth", persona=None, output_path=output,
            registered_models=scope["models"],
            required_context_paths=synth_context,
        )
        if not ok:
            errors.append(f"claude_synth provenance for {output.name}: {reason}")

    if errors:
        for e in errors:
            print(f"validate: ERROR — {e}", file=sys.stderr)
        fail(f"{len(errors)} validation error(s) — Step 4 outputs not ready", code=2)

    print(f"validate: OK — Step 4 outputs complete", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prep/validate Step 4 outputs (Claude synthesis)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("prep", "validate"):
        s = sub.add_parser(name)
        s.add_argument("--audit-id", required=True)
        s.add_argument("--round", type=int, required=True)
        s.add_argument("--round-dir", required=True)
    args = parser.parse_args(argv)
    return {"prep": cmd_prep, "validate": cmd_validate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
