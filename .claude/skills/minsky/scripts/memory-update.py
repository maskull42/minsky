#!/usr/bin/env python3
"""
memory-update.py — append per-persona memory entries after a round closes.

Per-persona memory journals live at:
  codex-audits/<audit-id>/memory/<persona>.jsonl

Each round, this script appends ONE entry per active persona, summarizing:
  - which round
  - per-step verdict (claude_self / codex / opencode)
  - per-step findings under that persona's lens
  - resolutions assigned in claude-synth/resolutions.json
  - links back to the source files for full detail

In round N+1, pack-build.py reads these JSONL files and embeds them in the
<persona-memory> block of the new pack so each persona sees its own past.

This is what makes "stateful Minsky" stateful: personas don't restart each round;
they accumulate context, accountability, and a reason to either strengthen or
retract earlier claims.

Usage:
  memory-update.py \\
    --audit-id <id> \\
    --round <n> \\
    --round-dir <path-to-round-N/> \\
    --personas marcion-heresiologist,ml-finetuning-phd
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path


def fail(msg: str, code: int = 1) -> "None":
    print(f"memory-update: {msg}", file=sys.stderr)
    sys.exit(code)


def now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_json(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"{path}: invalid JSON: {e}")


def collect_step_findings(round_dir: Path, persona: str) -> dict:
    """Return per-step (verdict, findings) under this persona for this round."""
    out: dict[str, dict] = {}
    locations = [
        ("claude_self", round_dir / "claude-self" / "findings" / f"{persona}.json"),
        ("codex",       round_dir / "codex" / f"{persona}.json"),
        ("opencode",    round_dir / "opencode" / f"{persona}.json"),
    ]
    for step, path in locations:
        data = load_json(path)
        if data is None:
            continue
        out[step] = {
            "verdict": (data.get("verdict") or {}).get("agree"),
            "verdict_reasoning": (data.get("verdict") or {}).get("reasoning"),
            "findings": data.get("findings") or [],
            "source_path": str(path),
        }
    return out


def collect_resolutions_for(persona: str, round_dir: Path) -> list[dict]:
    """Return per-finding resolutions from claude-synth/resolutions.json scoped to this persona."""
    res_path = round_dir / "claude-synth" / "resolutions.json"
    data = load_json(res_path)
    if not isinstance(data, list):
        return []
    return [r for r in data if r.get("persona") == persona]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append per-persona memory entries after a round")
    parser.add_argument("--audit-id", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--round-dir", required=True)
    parser.add_argument("--personas", required=True, help="Comma-separated active personas")
    args = parser.parse_args(argv)

    round_dir = Path(args.round_dir).resolve()
    if not round_dir.is_dir():
        fail(f"round-dir not found: {round_dir}")
    audit_dir = round_dir.parent
    memory_dir = audit_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    personas = [p.strip() for p in args.personas.split(",") if p.strip()]
    if not personas:
        fail("no personas provided")

    written = 0
    for persona in personas:
        steps = collect_step_findings(round_dir, persona)
        resolutions = collect_resolutions_for(persona, round_dir)
        entry = {
            "round": args.round,
            "audit_id": args.audit_id,
            "persona": persona,
            "appended_at": now_iso(),
            "steps": steps,
            "resolutions": resolutions,
            "summary": {
                "step_verdicts": {s: v.get("verdict") for s, v in steps.items()},
                "finding_counts": {s: len(v.get("findings") or []) for s, v in steps.items()},
                "resolution_counts": {
                    r: sum(1 for x in resolutions if x.get("resolution") == r)
                    for r in ("addressed", "carried_forward", "retracted", "user_overruled")
                },
            },
        }
        out_path = memory_dir / f"{persona}.jsonl"
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        written += 1
        print(f"memory-update: appended round-{args.round} entry for {persona} -> {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
