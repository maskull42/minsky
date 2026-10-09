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
import json
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

from provenance import load_round_scope, parse_step_models, parse_string_list, reconcile_artifact


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

You are running Step 2 of the deliberation chain. Step 1 (registered host self-audit) outputs are at:

- `{round_dir / 'claude-self' / 'report.md'}` — Section A (factual: what was done)
   and Section B (per-persona candidate findings — hypotheses for you to test)
- `{round_dir / 'claude-self' / 'findings' / (persona + '.json')}` — the host's
   first-pass findings under your specific persona lens

**Treat Section B as hypotheses to TEST against the original artifact, not assertions to accept.**
Confirm, refute, or extend. If the host flagged something that doesn't actually hold under your
lens, retract it. If the host missed something, add it. Compatibility directory names do not
identify the current host model; use this round's scope and report for that identity.

# Your task

Investigate the artifact rigorously. Respect the task-specific scope and exclusions in the
pack: generic persona suggestions do not authorise unrelated database or outcome inspection.
Use your tools to read in-scope files, grep cross-references, and follow permitted citation chains.

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

- The registered host's Step 1 self-audit (compatibility directory names do not identify its model):
  - `{round_dir / 'claude-self' / 'report.md'}` (Section A factual + Section B candidate findings)
  - `{round_dir / 'claude-self' / 'findings' / (persona + '.json')}`

- Codex's Step 2 findings (one file per active persona, raw — no compilation):
{codex_paths}

You may CHALLENGE Codex's framing, evidence, or severity calibration if your reading
disagrees. Codex is not authoritative; treat its critiques as hypotheses just like the host's.
You may also confirm Codex's findings if independent investigation supports them — but use
your own evidence, not Codex's.

# Your task

Respect the task-specific scope and exclusions in the pack: generic persona suggestions do not
authorise unrelated database or outcome inspection. Audit the artifact under the {persona} lens.
Produce a JSON file at `{output_path}`
conforming to the schema embedded in the pack's `<findings-schema>` block. Self-validate
(e.g. with `jq`) before declaring done.

If you find nothing worth flagging, emit `verdict.agree = "true"` with empty findings.

Do not modify any file outside your cwd. You are an auditor, not an editor.
"""


def run_invoke(script: Path, prompt: str, cwd: Path, audit_id: str, round_n: int,
               persona: str, context_files: list[Path]) -> int:
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [
        "bash", str(script),
        "--cwd", str(cwd),
        "--audit-id", audit_id,
        "--round", str(round_n),
        "--persona", persona,
    ]
    for context_file in context_files:
        cmd += ["--input-file", str(context_file.resolve())]
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


def findings_output_is_valid(path: Path, persona: str, *, round_dir: Path,
                             step: str, models: list[str]) -> bool:
    """Return True only for schema-valid output with latest terminal-ok provenance."""
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
    if rc != 0:
        return False
    ok, reason, _manifest = reconcile_artifact(
        round_dir, step=step, persona=persona, output_path=path,
        registered_models=models,
    )
    if not ok:
        print(f"chain: resume rejected for {step} × {persona}: {reason}", file=sys.stderr)
    return ok


def _converge_argv(args: argparse.Namespace, round_dir: Path) -> list[str]:
    """Build convergence argv; refuse implicit external-evidence permission."""
    argv = [sys.executable, str(SCRIPTS_DIR / "converge.py"),
            "--audit-id", args.audit_id,
            "--round", str(args.round),
            "--round-dir", str(round_dir)]
    if args.allow_external_evidence:
        argv.append("--allow-external-evidence")
    return argv


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
    if len(set(all_personas)) != len(all_personas):
        fail("--personas contains duplicates")
    # Round scope is explicit and immutable. This can differ from the historical
    # top-level audit row (for example a deliberately narrower round 2).
    persona_json = json.dumps(all_personas)
    parse_string_list(persona_json, "--personas", persona=True)
    all_models = [m.strip() for m in args.models.split(",") if m.strip()]
    model_json = json.dumps(all_models)
    parse_string_list(model_json, "--models", model=True)
    parse_step_models(args.step_models, all_models)
    register = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "provenance.py"), "register-round",
         "--audit-id", args.audit_id, "--round", str(args.round),
         "--round-dir", str(round_dir), "--personas", persona_json,
         "--models", model_json, "--step-models", args.step_models],
        text=True, capture_output=True,
    )
    if register.returncode:
        fail(register.stderr.strip() or register.stdout.strip())
    scope = load_round_scope(round_dir)
    preflight = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "claude-self-audit.py"), "validate",
         "--audit-id", args.audit_id, "--round", str(args.round),
         "--round-dir", str(round_dir), "--personas", ",".join(all_personas)],
        text=True, capture_output=True,
    )
    if preflight.returncode:
        fail(
            "Step 1 validation/provenance preflight failed before external calls:\n" +
            (preflight.stderr.strip() or preflight.stdout.strip())
        )
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
            if args.resume_existing and findings_output_is_valid(
                    out, p, round_dir=round_dir, step="codex", models=scope["models"]):
                print(f"chain:   codex × {p}: existing valid output, skipping", file=sys.stderr)
                continue
            print(f"chain:   codex × {p}", file=sys.stderr)
            prompt = build_codex_prompt(p, round_dir, pack, out)
            context_files = [
                pack,
                round_dir / "claude-self" / "report.md",
                round_dir / "claude-self" / "findings" / f"{p}.json",
            ]
            rc = run_invoke(
                SCRIPTS_DIR / "invoke-codex.sh", prompt, codex_dir,
                args.audit_id, args.round, p, context_files,
            )
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
            if args.resume_existing and findings_output_is_valid(
                    out, p, round_dir=round_dir, step="opencode", models=scope["models"]):
                print(f"chain:   opencode × {p}: existing valid output, skipping", file=sys.stderr)
                continue
            print(f"chain:   opencode × {p}", file=sys.stderr)
            prompt = build_opencode_prompt(p, round_dir, pack, out, all_personas)
            context_files = [
                pack,
                round_dir / "claude-self" / "report.md",
                round_dir / "claude-self" / "findings" / f"{p}.json",
                *[round_dir / "codex" / f"{persona}.json" for persona in all_personas],
            ]
            rc = run_invoke(
                SCRIPTS_DIR / "invoke-opencode.sh", prompt, opencode_dir,
                args.audit_id, args.round, p, context_files,
            )
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
        _converge_argv(args, round_dir),
        check=False
    ).returncode
    # converge.py exit codes: 0=agree, 1=disagree, 2=incomplete, 3=user_decision_required
    _decision = {0: "agree", 1: "disagree", 2: "incomplete", 3: "user_decision_required"}.get(rc, f"exit_{rc}")
    _emit(args.audit_id, "converge_done", round=args.round, decision=_decision)
    _emit(args.audit_id, "step_done", round=args.round, step="converge")
    return rc


def main(argv: list[str] | None = None) -> int:
    from worktree_guard import WorktreeRefused, check_not_linked_worktree

    try:
        check_not_linked_worktree(Path.cwd())
        check_not_linked_worktree(SKILL_DIR.parents[2])
    except WorktreeRefused as exc:
        fail(str(exc))
    parser = argparse.ArgumentParser(description="Orchestrate Steps 2/3 + convergence")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_adv = sub.add_parser("adversaries", help="Run Step 2 + Step 3 + converge for one round")
    p_adv.add_argument("--audit-id", required=True)
    p_adv.add_argument("--round", type=int, required=True)
    p_adv.add_argument("--round-dir", required=True)
    p_adv.add_argument("--pack", required=True)
    p_adv.add_argument("--personas", required=True, help="Comma-separated")
    p_adv.add_argument("--allow-external-evidence", action="store_true",
        help="Permit absolute evidence paths outside the repo root. Use only for intentional external audits.")
    p_adv.add_argument(
        "--models", required=True,
        help="Comma-separated explicit model@effort stamps for this round (including host model)",
    )
    p_adv.add_argument(
        "--step-models", required=True,
        help="JSON object binding claude_self/codex/opencode/claude_synth to registered models",
    )
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
