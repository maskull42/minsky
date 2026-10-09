#!/usr/bin/env python3
"""
pack-build.py — assemble the audit pack for one round of /minsky.

Produces a single XML file (round-N/pack.xml) that every per-persona prompt in
this round will reference. Critical content (audit-meta, ask, artifact) appears
in the first section so the reviewer agent's first Read call orients itself; the
heavier reference material (PhD frame, doc-drift, prior rounds, source corpus
citations) follows.

For round N > 1, prior-round outputs and per-persona memory journals are
included; stable context (PhD frame, doc-drift) is preserved rather than
dropped. No "unchanged" claim is emitted without a prior-hash comparison.

There is no routine upper bound on pack size (humanities-scale packs are large
by design). But two safety rails exist to prevent a corrupt/runaway pack like
the 2026-05-29 16GB incident:
  1. Binary artifact files are NEVER embedded — they are detected (NUL byte in
     the first sniff window) and replaced with a loud <file binary="true" .../>
     placeholder (path + size + sha256), since minsky packs are text prompts and
     a raw binary embed bloats the pack and corrupts the XML.
  2. A hard MAX_PACK_BYTES ceiling: if the assembled pack exceeds it, pack-build
     ABORTS LOUDLY and writes nothing. This is "investigate scope, not silently
     truncate" — an enormous pack is a signal of a bug (binary slipped in,
     duplicated content), not something to quietly clip.

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
import hashlib
import os
import sys
import xml.sax.saxutils as sx
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent          # .claude/skills/minsky
REPO_ROOT = SKILL_DIR.parent.parent.parent                  # MARS root
PHD_FRAME_PATH = REPO_ROOT / "phd_project_context" / "condensed_phd_context.md"
DOC_DRIFT_PATH = REPO_ROOT / "DOCUMENTATION_DRIFT_REGISTER.md"
SCHEMAS_DIR = SKILL_DIR / "schemas"
MODES_DIR = SKILL_DIR / "modes"

# --- safety rails (see module docstring; added after the 2026-05-29 16GB incident) ---
BINARY_SNIFF_BYTES = 1024 * 1024            # inspect first 1 MB for a NUL byte
# ~20x the largest legitimate pack (~10.5 MB); override via env for testing/tuning.
MAX_PACK_BYTES = int(os.environ.get("MINSKY_MAX_PACK_BYTES", 200 * 1024 * 1024))


def looks_binary(path: Path) -> bool:
    """Treat a file as binary if a NUL byte appears in its first BINARY_SNIFF_BYTES.

    NUL never occurs in valid UTF-8 text, so this is a fast, low-false-positive
    test that catches DBs, embeddings/tensors, images, etc. Minsky packs are
    text prompts — binary artifacts must not be embedded."""
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(BINARY_SNIFF_BYTES)
    except OSError as exc:
        sys.exit(f"pack-build: cannot read artifact {path}: {exc}")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
    except OSError as exc:
        sys.exit(f"pack-build: cannot hash artifact {path}: {exc}")
    return h.hexdigest()


def now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _artifact_entry(p: Path, rel: str) -> tuple[str, str | None, dict]:
    """Read one artifact file. Binary files are NOT embedded: they get a None
    body and a metadata dict so build_artifact_block emits a loud placeholder."""
    if looks_binary(p):
        size = p.stat().st_size
        print(f"pack-build: WARNING skipped binary artifact (not embedded): {rel} "
              f"({size:,} bytes)", file=sys.stderr)
        return (rel, None, {"binary": True, "bytes": size, "sha256": file_sha256(p)})
    return (rel, read_text_safe(p), {"binary": False})


def collect_artifact(files: list[str]) -> list[tuple[str, str | None, dict]]:
    out = []
    for f in files:
        p = Path(f)
        if not p.is_absolute():
            p = REPO_ROOT / p
        if not p.exists():
            sys.exit(f"pack-build: artifact path does not exist: {f}")
        if p.is_dir():
            for sub in sorted(p.rglob("*")):
                if sub.is_file() and not sub.name.startswith("."):
                    out.append(_artifact_entry(sub, str(sub.relative_to(REPO_ROOT))))
        else:
            rel = (str(p.relative_to(REPO_ROOT)) if str(p).startswith(str(REPO_ROOT))
                   else str(p))
            out.append(_artifact_entry(p, rel))
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
        "    Each per-(model, persona) call must produce a JSON file conforming to this\n"
        "    JSON Schema. Self-validate before declaring done. converge.py is a safety net,\n"
        "    not the primary mechanism.\n"
        f"    {cdata(schema)}\n"
        "  </findings-schema>\n"
    )


def build_artifact_block(items: list[tuple[str, str | None, dict]]) -> str:
    parts = ["  <artifact>\n"]
    for path, content, info in items:
        if info.get("binary"):
            parts.append(
                f"    <file path={sx.quoteattr(path)} binary=\"true\" "
                f"bytes=\"{info.get('bytes', 0)}\" "
                f"sha256={sx.quoteattr(info.get('sha256', ''))} "
                "note=\"binary artifact not embedded — reference by path\"/>\n"
            )
            continue
        parts.append(f"    <file path={sx.quoteattr(path)}>\n")
        parts.append(f"      {cdata(content or '')}\n")
        parts.append("    </file>\n")
    parts.append("  </artifact>\n")
    return "".join(parts)


def build_phd_frame(round_n: int) -> str:
    text = read_text_safe(PHD_FRAME_PATH)
    if not text:
        return ""
    return (
        f"  <phd-frame source={sx.quoteattr(str(PHD_FRAME_PATH.relative_to(REPO_ROOT)))}>\n"
        f"    {cdata(text)}\n"
        "  </phd-frame>\n"
    )


def build_doc_drift(round_n: int) -> str:
    source = sx.quoteattr(str(DOC_DRIFT_PATH.relative_to(REPO_ROOT)))
    if not DOC_DRIFT_PATH.is_file():
        return (
            f"  <doc-drift-warnings source={source} status=\"missing\">\n"
            "    No active documentation drift register exists at the canonical path.\n"
            "    This is an explicit absence, not evidence that documentation is current.\n"
            "  </doc-drift-warnings>\n"
        )
    text = read_text_safe(DOC_DRIFT_PATH)
    if not text.strip():
        return (
            f"  <doc-drift-warnings source={source} status=\"empty\">\n"
            "    The active documentation drift register exists but contains no entries.\n"
            "  </doc-drift-warnings>\n"
        )
    return (
        f"  <doc-drift-warnings source={source} status=\"active\">\n"
        "    Reviewers: parts of MARS documentation are known to be stale or contradictory.\n"
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
        return '  <persona-memory status="not-applicable">No prior-round journals requested.</persona-memory>\n'
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
            # Skip raw CLI stream/log dirs (_codex_logs/, _opencode_logs/, any _*-prefixed
            # dir): they are gitignored transport debris, NOT deliberation outputs, and
            # embedding them bloated a round-2 pack to 7.8MB (2026-06-11, repo-cleanup
            # audit). The deliberation record = the per-persona JSONs + synthesis files.
            if any(part.startswith("_") for part in f.relative_to(full).parts[:-1]):
                continue
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
        return ('  <persona-memory status="absent">Optional journal directory was not created; '
                'prior-round artifacts remain separate evidence.</persona-memory>\n')
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
    args = parser.parse_args(argv)

    items = collect_artifact(args.files)

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

    payload = "".join(parts)
    # Safety rail: refuse to write a pathologically large pack (binary slipped in,
    # duplicated/runaway content). Abort loudly — do NOT truncate. Guard on character
    # count (cheap, OOM-safe; bytes >= chars, so this never under-counts a UTF-8 pack).
    if len(payload) > MAX_PACK_BYTES:
        sys.exit(
            f"pack-build: REFUSING to write — assembled pack is {len(payload):,} characters "
            f"(> MAX_PACK_BYTES={MAX_PACK_BYTES:,}). A legitimate minsky pack is far smaller. "
            f"This signals a binary artifact slipped in or a runaway/duplicated build "
            f"(cf. the 2026-05-29 16GB pack incident). No file was written — investigate scope."
        )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload, encoding="utf-8")

    # Print a small summary to stderr for the orchestrator
    size = output_path.stat().st_size
    print(f"pack-build: wrote {output_path} ({size:,} bytes; {len(items)} artifact files)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
