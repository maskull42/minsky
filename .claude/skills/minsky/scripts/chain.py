#!/usr/bin/env python3
"""
chain.py — orchestrate the deterministic middle of one round of /minsky.

The minsky deliberation chain has 4 steps:

  Step 1 — Claude self-report-and-audit       (host Claude in SKILL.md flow)
  Step 2 — Codex per-persona walks            (this script)
  Step 3 — OpenCode per-persona walks         (this script)
  Convergence (this script)
  Step 4 — Claude synthesis                   (host Claude in SKILL.md flow)

Steps 1 and 4 are NOT in this script — they're host-Claude reasoning, prepped and
validated by claude-self-audit.py and claude-synth.py respectively.

This script:
  1. For each active persona: builds the per-persona prompt for Step 2,
     invokes invoke-codex.sh (sequential per persona).
  2. For each active persona: builds the per-persona prompt for Step 3,
     invokes invoke-opencode.sh.
  3. Runs converge.py.

Loud failure on any non-zero exit (no silent fallbacks). Special exit-42 from
invoke wrappers signals rate-limit; chain.py surfaces this distinctly so SKILL.md
can present user-decision options.

Usage:
  chain.py adversaries \\
    --audit-id <id> \\
    --round <n> \\
    --round-dir <path> \\
    --pack <path-to-pack.xml> \\
    --personas marcion-heresiologist,ml-finetuning-phd

Recovery examples:
  chain.py adversaries ... --only-step opencode --only-persona ml-finetuning-phd
  chain.py adversaries ... --resume-existing
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
PERSONAS_DIR = SKILL_DIR / "personas"
SCHEMAS_DIR = SKILL_DIR / "schemas"


# Best-effort import of the progress emitter. Wrapped so a malformed/missing
# progress.py never breaks chain.py; chain integrity always wins over progress
# observability.
SCRIPTS_DIR_FOR_PROGRESS = Path(__file__).resolve().parent
if str(SCRIPTS_DIR_FOR_PROGRESS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR_FOR_PROGRESS))
try:
    import progress as _progress  # type: ignore[import-not-found]
except Exception:
    _progress = None


def _emit(audit_id: str, event: str, **fields) -> None:
    """Emit a progress event, swallowing any failure."""
    if _progress is None:
        return
    try:
        _progress.emit(audit_id, event, **fields)
    except Exception as exc:
        print(f"chain: progress emit failed (non-fatal): {exc}", file=sys.stderr)


def fail(msg: str, code: int = 1) -> "None":
    print(f"chain: {msg}", file=sys.stderr)
    sys.exit(code)


def read_persona_file(persona: str) -> str:
    """Return the persona file's full text (YAML frontmatter + Markdown body).

    The frontmatter carries the substantive evaluator rubric (adversarial-stance,
    red-flags-must-catch, preferred-questions, relevance-rubric, training-summary,
    voice-tier system) for every v1 persona. Stripping it before passing to
    Codex/OpenCode silently degraded every audit prior to 2026-05-02 — see
    audit personas-performance-studies-roach-revised-2026-05-02 finding N1
    (CRITICAL) for the full diagnosis. The fix: deliver the whole file.
    """
    p = PERSONAS_DIR / f"{persona}.md"
    if not p.is_file():
        fail(f"missing persona file: {p}")
    return p.read_text(encoding="utf-8")


def build_codex_prompt(persona: str, round_dir: Path, pack_path: Path, output_path: Path) -> str:
    persona_body = read_persona_file(persona)
    return f"""You are auditing a MARS PhD work product as the **{persona}** persona. Single-lens. Stay in your lens.

# Persona instructions

{persona_body}

# The audit pack

The full audit pack is at: `{pack_path}`

Read it in full before forming any findings. It contains, in order:
- `<audit-meta>` (audit_id, round, mode)
- `<ask>` (mode-specific ask scaffolding)
- `<findings-schema>` (the JSON Schema your output must conform to — embedded in CDATA)
- `<artifact>` (the work product under review, file by file)
- `<phd-frame>` (PhD project framing — calibrate severity using this)
- `<doc-drift-warnings>` (parts of MARS docs known stale; discount findings anchored on them)

# Prior-step outputs (read these BEFORE forming your own findings)

You are running Step 2 of the deliberation chain. Step 1 (Claude self-audit) outputs are at:

- `{round_dir / 'claude-self' / 'report.md'}` — Section A (factual: what was done)
   and Section B (per-persona candidate findings — hypotheses for you to test)
- `{round_dir / 'claude-self' / 'findings' / (persona + '.json')}` — Claude's
   first-pass findings under your specific persona lens

**Treat Section B as hypotheses to TEST against the original artifact, not assertions to accept.**
Confirm, refute, or extend. If Claude flagged something that doesn't actually hold under your
lens, retract it. If Claude missed something, add it.

# Your task

Investigate the artifact rigorously. Use your tools — read related files in the MARS
repository (you have read access), grep for cross-references, follow citation chains.

Produce a JSON file at `{output_path}` conforming to the schema embedded in the pack's
`<findings-schema>` block. Self-validate (e.g. with `jq`) before declaring done. If your
JSON is invalid, fix and re-write.

If you find nothing worth flagging under the {persona} lens, that is a valid output:
emit `verdict.agree = "true"` with empty `findings` array and a brief `reasoning` sentence.

Do not modify any file outside your current working directory. You are an auditor, not an editor.
"""


def build_opencode_prompt(persona: str, round_dir: Path, pack_path: Path, output_path: Path,
                           active_personas: list[str]) -> str:
    persona_body = read_persona_file(persona)
    codex_paths = "\n".join(
        f"  - `{round_dir / 'codex' / (p + '.json')}`" for p in active_personas
    )
    return f"""You are auditing a MARS PhD work product as the **{persona}** persona. Single-lens. Stay in your lens.

# Persona instructions

{persona_body}

# The audit pack

The full audit pack is at: `{pack_path}`

Read it in full before forming any findings. (Same structure as documented for Step 2.)

# Prior-step outputs (read these BEFORE forming your own findings)

You are running Step 3 of the deliberation chain (the meta-adversarial step). Read:

- Claude's Step 1 self-audit:
  - `{round_dir / 'claude-self' / 'report.md'}` (Section A factual + Section B candidate findings)
  - `{round_dir / 'claude-self' / 'findings' / (persona + '.json')}`

- Codex's Step 2 findings (one file per active persona, raw — no compilation):
{codex_paths}

You may CHALLENGE Codex's framing, evidence, or severity calibration if your reading
disagrees. Codex is not authoritative; treat its critiques as hypotheses just like Claude's.
You may also confirm Codex's findings if independent investigation supports them — but use
your own evidence, not Codex's.

# Your task

Audit the artifact under the {persona} lens. Produce a JSON file at `{output_path}`
conforming to the schema embedded in the pack's `<findings-schema>` block. Self-validate
(e.g. with `jq`) before declaring done.

If you find nothing worth flagging, emit `verdict.agree = "true"` with empty findings.

Do not modify any file outside your cwd. You are an auditor, not an editor.
"""


def run_invoke(script: Path, prompt: str, cwd: Path, audit_id: str, round_n: int, persona: str) -> int:
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [
        "bash", str(script),
        "--cwd", str(cwd),
        "--audit-id", audit_id,
        "--round", str(round_n),
        "--persona", persona,
    ]
    proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def parse_persona_filter(values: list[str] | None) -> set[str]:
    """Parse repeatable/comma-separated --only-persona values."""
    selected: set[str] = set()
    for value in values or []:
        for part in value.split(","):
            persona = part.strip()
            if persona:
                selected.add(persona)
    return selected


def findings_output_is_valid(path: Path, persona: str) -> bool:
    """Return True when an existing output parses and validates for this persona."""
    if not path.is_file():
        return False
    rc = subprocess.run(
        [
            "python3",
            str(SCRIPTS_DIR / "validate-findings.py"),
            str(path),
            "--persona",
            persona,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    return rc == 0


def cmd_adversaries(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir).resolve()
    pack = Path(args.pack).resolve()
    if not pack.is_file():
        fail(f"pack not found: {pack}")
    if not round_dir.is_dir():
        fail(f"round-dir not found: {round_dir}")
    all_personas = [p.strip() for p in args.personas.split(",") if p.strip()]
    if not all_personas:
        fail("no personas specified")
    selected = parse_persona_filter(args.only_persona)
    unknown = sorted(selected - set(all_personas))
    if unknown:
        fail(f"--only-persona includes persona(s) not in --personas: {', '.join(unknown)}")
    run_personas = [p for p in all_personas if not selected or p in selected]
    if not run_personas:
        fail("no personas selected to run")

    # Step 2 — Codex
    if args.only_step in ("all", "codex"):
        print("chain: Step 2 (Codex per-persona walks)", file=sys.stderr)
        _emit(args.audit_id, "step_start", round=args.round, step="codex")
        codex_dir = round_dir / "codex"
        for p in run_personas:
            out = codex_dir / f"{p}.json"
            if args.resume_existing and findings_output_is_valid(out, p):
                print(f"chain:   codex × {p}: existing valid output, skipping", file=sys.stderr)
                continue
            print(f"chain:   codex × {p}", file=sys.stderr)
            prompt = build_codex_prompt(p, round_dir, pack, out)
            rc = run_invoke(SCRIPTS_DIR / "invoke-codex.sh", prompt, codex_dir, args.audit_id, args.round, p)
            if rc == 42:
                _emit(args.audit_id, "error", source="chain.py", message=f"codex × {p}: rate limit", exit_code=42)
                fail(f"codex × {p}: RATE LIMIT — pausing audit", code=42)
            if rc != 0:
                _emit(args.audit_id, "error", source="chain.py", message=f"codex × {p}: failed", exit_code=rc)
                fail(f"codex × {p}: failed (rc={rc})", code=rc)
        _emit(args.audit_id, "step_done", round=args.round, step="codex")

    # Step 3 — OpenCode
    if args.only_step in ("all", "opencode"):
        print("chain: Step 3 (OpenCode per-persona walks)", file=sys.stderr)
        _emit(args.audit_id, "step_start", round=args.round, step="opencode")
        opencode_dir = round_dir / "opencode"
        for p in run_personas:
            out = opencode_dir / f"{p}.json"
            if args.resume_existing and findings_output_is_valid(out, p):
                print(f"chain:   opencode × {p}: existing valid output, skipping", file=sys.stderr)
                continue
            print(f"chain:   opencode × {p}", file=sys.stderr)
            prompt = build_opencode_prompt(p, round_dir, pack, out, all_personas)
            rc = run_invoke(SCRIPTS_DIR / "invoke-opencode.sh", prompt, opencode_dir,
                            args.audit_id, args.round, p)
            if rc == 42:
                _emit(args.audit_id, "error", source="chain.py", message=f"opencode × {p}: rate limit", exit_code=42)
                fail(f"opencode × {p}: RATE LIMIT — pausing audit", code=42)
            if rc != 0:
                _emit(args.audit_id, "error", source="chain.py", message=f"opencode × {p}: failed", exit_code=rc)
                fail(f"opencode × {p}: failed (rc={rc})", code=rc)
        _emit(args.audit_id, "step_done", round=args.round, step="opencode")

    # Convergence
    print("chain: Convergence", file=sys.stderr)
    _emit(args.audit_id, "step_start", round=args.round, step="converge")
    rc = subprocess.run(
        ["python3", str(SCRIPTS_DIR / "converge.py"),
         "--audit-id", args.audit_id,
         "--round", str(args.round),
         "--round-dir", str(round_dir)],
        check=False
    ).returncode
    # converge.py exit codes: 0=agree, 1=disagree, 2=incomplete, 3=user_decision_required
    _decision = {0: "agree", 1: "disagree", 2: "incomplete", 3: "user_decision_required"}.get(rc, f"exit_{rc}")
    _emit(args.audit_id, "converge_done", round=args.round, decision=_decision)
    _emit(args.audit_id, "step_done", round=args.round, step="converge")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orchestrate Steps 2/3 + convergence")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_adv = sub.add_parser("adversaries", help="Run Step 2 + Step 3 + converge for one round")
    p_adv.add_argument("--audit-id", required=True)
    p_adv.add_argument("--round", type=int, required=True)
    p_adv.add_argument("--round-dir", required=True)
    p_adv.add_argument("--pack", required=True)
    p_adv.add_argument("--personas", required=True, help="Comma-separated")
    p_adv.add_argument(
        "--only-step",
        choices=["all", "codex", "opencode"],
        default="all",
        help="Run only one adversarial backend before convergence (default: all)",
    )
    p_adv.add_argument(
        "--only-persona",
        action="append",
        default=[],
        help="Restrict to one persona; repeat or pass comma-separated values",
    )
    p_adv.add_argument(
        "--resume-existing",
        action="store_true",
        help="Skip selected persona outputs that already validate against findings.schema.json",
    )
    args = parser.parse_args(argv)
    return cmd_adversaries(args)


if __name__ == "__main__":
    sys.exit(main())
