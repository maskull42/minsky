#!/usr/bin/env python3
"""
pack-build.py — assemble the audit pack for one round of /minsky.

Produces a single XML file (round-N/pack.xml) that every per-persona prompt in
this round will reference. Critical content (audit-meta, ask, artifact) appears
in the first section so the reviewer agent's first Read call orients itself; the
heavier reference material (project context, doc-drift, prior rounds, source
corpus citations) follows.

For round N > 1, prior-round outputs and per-persona memory journals are
included; unchanged stable context is preserved with <unchanged-since-round-1/>
annotations rather than dropped. Large-context reviewer models can make this
affordable, but scope should still be reviewed before running.

There is NO upper bound on pack size. If a pack is enormous, that is a signal
to investigate scope, not to silently truncate.

Usage:
  pack-build.py \\
    --audit-id <id> \\
    --round <n> \\
    --mode <audit|plan|draft|eval|bug-hunt> \\
    --files <path1> [<path2> ...] \\
    --output <round-dir>/pack.xml \\
    [--prior-rounds-dir <prev-round-dir>]
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import xml.sax.saxutils as sx
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent          # .claude/skills/minsky
REPO_ROOT = SKILL_DIR.parent.parent.parent                  # project root
# PHD_FRAME_PATH: optional project-context document embedded as <phd-frame>
# in every pack. The reference deployment (MARS) keeps this at
# phd_project_context/condensed_phd_context.md; users can override via the
# MINSKY_PHD_FRAME env-var (path relative to project root, or absolute).
# If the file does not exist, the <phd-frame> block is omitted gracefully.
PHD_FRAME_PATH = Path(os.environ.get(
    "MINSKY_PHD_FRAME",
    str(REPO_ROOT / "phd_project_context" / "condensed_phd_context.md"),
))
if not PHD_FRAME_PATH.is_absolute():
    PHD_FRAME_PATH = REPO_ROOT / PHD_FRAME_PATH
# DOC_DRIFT_PATH: optional documentation-drift register embedded as
# <doc-drift-warnings>. Override via MINSKY_DOC_DRIFT env-var.
DOC_DRIFT_PATH = Path(os.environ.get(
    "MINSKY_DOC_DRIFT",
    str(REPO_ROOT / "DOCUMENTATION_DRIFT_REGISTER.md"),
))
if not DOC_DRIFT_PATH.is_absolute():
    DOC_DRIFT_PATH = REPO_ROOT / DOC_DRIFT_PATH
SCHEMAS_DIR = SKILL_DIR / "schemas"
MODES_DIR = SKILL_DIR / "modes"


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def repo_relative(path: Path) -> Path | None:
    try:
        return path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return None


def is_protected_repo_path(rel: Path) -> bool:
    parts = rel.parts
    rel_posix = rel.as_posix()
    if not parts:
        return False
    if any(part in {".git", ".ssh", ".aws", ".gnupg"} for part in parts):
        return True
    if any(part.startswith(".env") for part in parts):
        return True
    if rel_posix == ".minsky/audits.db":
        return True
    if parts[0] == "codex-audits":
        return True
    return False


def validate_artifact_path(path: Path, allow_external: bool) -> tuple[Path, str]:
    resolved = path.resolve()
    rel = repo_relative(resolved)
    if rel is None:
        if allow_external:
            return resolved, str(resolved)
        sys.exit(f"pack-build: refusing artifact path outside repo without --allow-external: {resolved}")
    if is_protected_repo_path(rel):
        sys.exit(f"pack-build: refusing protected artifact path: {rel.as_posix()}")
    return resolved, rel.as_posix()


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def cdata(content: str) -> str:
    """Wrap content in CDATA to preserve verbatim formatting; escape any nested ']]>'."""
    safe = content.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{safe}]]>"


def read_text_safe(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def read_mode_template(mode: str) -> str:
    template = MODES_DIR / f"{mode}.md"
    if not template.is_file():
        sys.exit(f"pack-build: missing mode template at {template}")
    return template.read_text(encoding="utf-8")


def read_findings_schema() -> str:
    schema = SCHEMAS_DIR / "findings.schema.json"
    if not schema.is_file():
        sys.exit(f"pack-build: missing findings schema at {schema}")
    return schema.read_text(encoding="utf-8")


def collect_artifact(files: list[str], allow_external: bool) -> list[tuple[str, str]]:
    out = []
    for f in files:
        p = Path(f)
        if not p.is_absolute():
            p = REPO_ROOT / p
        if not p.exists():
            sys.exit(f"pack-build: artifact path does not exist: {f}")
        p, display_path = validate_artifact_path(p, allow_external)
        if p.is_dir():
            for sub in sorted(p.rglob("*")):
                if sub.is_file():
                    sub, sub_display_path = validate_artifact_path(sub, allow_external)
                    out.append((sub_display_path, read_text_safe(sub)))
        else:
            out.append((display_path, read_text_safe(p)))
    return out


def build_audit_meta(audit_id: str, round_n: int, mode: str, files: list[str]) -> str:
    return (
        "  <audit-meta>\n"
        f"    <audit-id>{sx.escape(audit_id)}</audit-id>\n"
        f"    <round>{round_n}</round>\n"
        f"    <mode>{sx.escape(mode)}</mode>\n"
        f"    <generated-at>{now_iso()}</generated-at>\n"
        f"    <artifact-count>{len(files)}</artifact-count>\n"
        "  </audit-meta>\n"
    )


def build_ask(mode: str, round_n: int) -> str:
    template = read_mode_template(mode)
    body = "\n".join("    " + line for line in template.splitlines())
    round_note = ""
    if round_n > 1:
        round_note = (
            "\n    NOTE: This is round " + str(round_n) + " of the deliberation. Prior-round\n"
            "    findings appear in <prior-rounds>. Treat them as hypotheses to test\n"
            "    against the artifact in this round, not assertions to accept. Personas\n"
            "    may retract, strengthen, or extend prior findings — but should not\n"
            "    merely restate them.\n"
        )
    return f"  <ask>\n{body}{round_note}\n  </ask>\n"


def build_schema_block() -> str:
    schema = read_findings_schema()
    return (
        "  <findings-schema>\n"
        "    Each per-(tool, persona) call must produce a JSON file conforming to this\n"
        "    JSON Schema. Self-validate before declaring done. converge.py is a safety net,\n"
        "    not the primary mechanism.\n"
        f"    {cdata(schema)}\n"
        "  </findings-schema>\n"
    )


def build_artifact_block(items: list[tuple[str, str]]) -> str:
    parts = ["  <artifact>\n"]
    for path, content in items:
        parts.append(f"    <file path={sx.quoteattr(path)}>\n")
        parts.append(f"      {cdata(content)}\n")
        parts.append("    </file>\n")
    parts.append("  </artifact>\n")
    return "".join(parts)


def build_phd_frame(round_n: int) -> str:
    text = read_text_safe(PHD_FRAME_PATH)
    if not text:
        return ""
    annotation = ' unchanged-since-round-1="true"' if round_n > 1 else ""
    return (
        f"  <phd-frame source={sx.quoteattr(str(PHD_FRAME_PATH.relative_to(REPO_ROOT)))}{annotation}>\n"
        f"    {cdata(text)}\n"
        "  </phd-frame>\n"
    )


def build_doc_drift(round_n: int) -> str:
    text = read_text_safe(DOC_DRIFT_PATH)
    if not text:
        return ""
    annotation = ' unchanged-since-round-1="true"' if round_n > 1 else ""
    return (
        f"  <doc-drift-warnings source={sx.quoteattr(str(DOC_DRIFT_PATH.relative_to(REPO_ROOT)))}{annotation}>\n"
        "    Reviewers: parts of project documentation are known to be stale or contradictory.\n"
        "    Use the following register to discount specific docs that may misdirect findings.\n"
        f"    {cdata(text)}\n"
        "  </doc-drift-warnings>\n"
    )


def _safe_rel(path: Path) -> str:
    """Best-effort relative-to-REPO_ROOT conversion; falls back to absolute path str."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def build_prior_rounds(prior_dir: Path | None, round_n: int) -> str:
    """For round 2+, embed prior-round outputs verbatim (raw inter-step view)."""
    if round_n == 1 or prior_dir is None:
        return ""
    prior_dir = prior_dir.resolve()
    if not prior_dir.is_dir():
        sys.exit(f"pack-build: prior-rounds-dir does not exist: {prior_dir}")
    parts = ["  <prior-rounds>\n"]
    parts.append(f"    <prior-round number=\"{round_n - 1}\">\n")
    for step_dir in ("claude-self", "codex", "opencode", "claude-synth"):
        full = prior_dir / step_dir
        if not full.is_dir():
            continue
        parts.append(f"      <step name={sx.quoteattr(step_dir)}>\n")
        for f in sorted(full.rglob("*")):
            if f.is_file():
                parts.append(f"        <output path={sx.quoteattr(_safe_rel(f))}>\n")
                parts.append(f"          {cdata(read_text_safe(f))}\n")
                parts.append("        </output>\n")
        parts.append("      </step>\n")
    consensus = prior_dir / "consensus.md"
    if consensus.is_file():
        parts.append(f"      <consensus path={sx.quoteattr(_safe_rel(consensus))}>\n")
        parts.append(f"        {cdata(read_text_safe(consensus))}\n")
        parts.append("      </consensus>\n")
    parts.append("    </prior-round>\n")
    parts.append("  </prior-rounds>\n")
    return "".join(parts)


def build_persona_memory(prior_dir: Path | None, round_n: int) -> str:
    """For round 2+, also embed per-persona memory journals so each persona reads its own past."""
    if round_n == 1 or prior_dir is None:
        return ""
    memory_dir = (prior_dir.resolve()).parent / "memory"
    if not memory_dir.is_dir():
        return ""
    parts = ["  <persona-memory>\n"]
    for f in sorted(memory_dir.iterdir()):
        if f.suffix == ".jsonl":
            persona = f.stem
            parts.append(f"    <memory persona={sx.quoteattr(persona)} path={sx.quoteattr(_safe_rel(f))}>\n")
            parts.append(f"      {cdata(read_text_safe(f))}\n")
            parts.append("    </memory>\n")
    parts.append("  </persona-memory>\n")
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a /minsky audit pack")
    parser.add_argument("--audit-id", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--mode", required=True, choices=["audit","plan","draft","eval","bug-hunt"])
    parser.add_argument("--files", nargs="+", required=True, help="Artifact paths (file or dir)")
    parser.add_argument("--output", required=True, help="Output path for pack.xml")
    parser.add_argument("--prior-rounds-dir", default=None,
        help="Directory of the previous round (e.g. codex-audits/<id>/round-1/) for round 2+")
    parser.add_argument("--allow-external", action="store_true",
        help="Permit artifact paths outside the repo root. Use only when intentionally auditing external files.")
    args = parser.parse_args(argv)

    items = collect_artifact(args.files, args.allow_external)

    parts = ["<minsky-audit-pack>\n"]
    # Critical content first (orientation block)
    parts.append(build_audit_meta(args.audit_id, args.round, args.mode, args.files))
    parts.append(build_ask(args.mode, args.round))
    parts.append(build_schema_block())
    parts.append(build_artifact_block(items))
    # Reference material after
    parts.append(build_phd_frame(args.round))
    parts.append(build_doc_drift(args.round))
    parts.append(build_prior_rounds(Path(args.prior_rounds_dir) if args.prior_rounds_dir else None, args.round))
    parts.append(build_persona_memory(Path(args.prior_rounds_dir) if args.prior_rounds_dir else None, args.round))
    parts.append("</minsky-audit-pack>\n")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(parts), encoding="utf-8")

    # Print a small summary to stderr for the orchestrator
    size = output_path.stat().st_size
    print(f"pack-build: wrote {output_path} ({size:,} bytes; {len(items)} artifact files)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
