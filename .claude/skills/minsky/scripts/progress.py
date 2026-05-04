#!/usr/bin/env python3
"""
progress.py — emit structured progress events for a minsky audit.

Each event is one JSON line appended to:
    <repo-root>/codex-audits/<audit-id>/progress.ndjson

Append-only (O_APPEND, sub-PIPE_BUF guarantees atomicity on POSIX); fsync'd
before close so events are durable on disk before the caller proceeds. The
file is part of the audit's permanent on-disk record alongside pack.xml,
claude-self/, codex/, opencode/, claude-synth/, and consensus.md — citable
in the dissertation methods chapter as the runtime progress trace.

Event types: see KNOWN_EVENTS below. Schema: schemas/progress.schema.json.
Prose spec: schemas/progress.md.

Two interfaces:

  CLI:
      progress.py emit --audit-id <id> --event <name> [convenience flags]
      progress.py emit --audit-id <id> --event <name> --field K=V [--field K=V ...]

  Python API (importable):
      from progress import emit
      emit(audit_id, "step_start", round=1, step="codex")

Path resolution: the script lives at
.claude/skills/minsky/scripts/progress.py; repo root is `parent.parent.parent.parent`.

Usage examples:

  progress.py emit --audit-id paper-2026-x --event audit_open \\
      --mode draft --scope-kind paths

  progress.py emit --audit-id paper-2026-x --event persona_walk_done \\
      --step codex --persona marcion-heresiologist \\
      --duration-s 312 --exit-status ok --verdict partial --finding-count 5 \\
      --severity-breakdown '{"high":1,"medium":3,"low":1}'

  progress.py emit --audit-id paper-2026-x --event error \\
      --message "OpenCode provider timeout after 1800s" --exit-code 124 --source invoke-opencode.sh
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

SCHEMA_VERSION = "1.1"

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
AUDIT_BASE = REPO_ROOT / "codex-audits"

# Event types. Adding a new one requires updating progress.schema.json and
# progress.md; see schemas/progress.md for the full per-event contract.
KNOWN_EVENTS = {
    "audit_open",
    "round_start",
    "round_done",
    "step_start",
    "step_done",
    "persona_walk_start",
    "persona_walk_done",
    "converge_done",
    "audit_close",
    "warning",
    "error",
}

# POSIX guarantees atomic O_APPEND writes for buffers strictly smaller than
# PIPE_BUF. PIPE_BUF is 4096 on Linux and >=512 on every POSIX system; macOS
# is 512 by default but supports up to 4096 for regular files. We use 512 as
# the conservative cross-platform line cap to guarantee no interleaving even
# under the hostile-OS interpretation; events that exceed this limit are
# refused rather than written non-atomically. Most events fit well under 512;
# the exception is severity_breakdown / agreement_matrix payloads, which are
# pre-flattened to short forms.
MAX_EVENT_LINE_BYTES = 512


def now_iso() -> str:
    """ISO 8601 UTC timestamp, seconds precision, matching audit-db convention."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def progress_path(audit_id: str) -> Path:
    """Resolve the canonical progress NDJSON path for an audit-id."""
    return AUDIT_BASE / audit_id / "progress.ndjson"


def emit(audit_id: str, event: str, **fields) -> Path:
    """Append one event to the audit's progress NDJSON.

    Returns the path written to. The write is atomic on POSIX for lines under
    MAX_EVENT_LINE_BYTES via O_APPEND, and fsync'd before close so events are
    durable on disk before this function returns.

    Raises ValueError on unknown event names, malformed audit-id, or
    over-size payloads.
    """
    if event not in KNOWN_EVENTS:
        raise ValueError(f"unknown event '{event}'; known: {sorted(KNOWN_EVENTS)}")
    if not audit_id or "/" in audit_id or audit_id.startswith("."):
        raise ValueError(
            f"audit_id must be a non-empty slug without slashes or leading '.'; "
            f"got {audit_id!r}"
        )

    record = {
        "ts": now_iso(),
        "schema_version": SCHEMA_VERSION,
        "event": event,
        "audit_id": audit_id,
        **fields,
    }

    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded = line.encode("utf-8")
    if len(encoded) > MAX_EVENT_LINE_BYTES:
        raise ValueError(
            f"event line is {len(encoded)} bytes, exceeds POSIX-atomic limit "
            f"of {MAX_EVENT_LINE_BYTES}. Shorten the payload (e.g. drop full "
            f"agreement_matrix; emit summary only). Event: {event}, "
            f"audit_id: {audit_id}"
        )

    path = progress_path(audit_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)

    return path


# CLI ---------------------------------------------------------------------

# Convenience flags map directly to JSON fields in the emitted event. Adding
# a new convenience flag here is purely ergonomic; the same value can always
# be passed via --field K=V instead.
_CLI_FIELD_MAP = [
    # (cli_attr, json_key, type-hint-only)
    ("round", "round"),
    ("step", "step"),
    ("persona", "persona"),
    ("model", "model"),
    ("duration_s", "duration_s"),
    ("exit_status", "exit_status"),
    ("verdict", "verdict"),
    ("finding_count", "finding_count"),
    ("decision", "decision"),
    ("convergence_status", "convergence_status"),
    ("num_rounds", "num_rounds"),
    ("total_findings", "total_findings"),
    ("message", "message"),
    ("source", "source"),
    ("exit_code", "exit_code"),
    ("mode", "mode"),
    ("scope_kind", "scope_kind"),
    ("branch", "branch"),
    ("commit_at_start", "commit_at_start"),
]


def _parse_field(arg: str) -> tuple[str, object]:
    """Parse --field K=V; V is JSON-parsed if possible, else kept as string."""
    if "=" not in arg:
        raise argparse.ArgumentTypeError(f"--field expects K=V; got {arg!r}")
    k, v = arg.split("=", 1)
    try:
        return k, json.loads(v)
    except json.JSONDecodeError:
        return k, v


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit a minsky progress event (append-only NDJSON)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emit", help="Append one progress event")
    e.add_argument("--audit-id", required=True)
    e.add_argument("--event", required=True, choices=sorted(KNOWN_EVENTS))
    e.add_argument(
        "--field",
        action="append",
        default=[],
        help="K=V (V is JSON-parsed if possible; else string). Repeatable.",
    )
    for cli_attr, _ in _CLI_FIELD_MAP:
        flag = "--" + cli_attr.replace("_", "-")
        if cli_attr in ("round", "finding_count", "exit_code", "num_rounds", "total_findings"):
            e.add_argument(flag, type=int)
        elif cli_attr == "duration_s":
            e.add_argument(flag, type=float)
        else:
            e.add_argument(flag)
    e.add_argument(
        "--severity-breakdown",
        help='JSON object e.g. \'{"high":1,"medium":3}\'',
    )
    e.add_argument(
        "--personas",
        help='JSON array e.g. \'["marcion-heresiologist","ml-finetuning-phd"]\'',
    )
    e.add_argument(
        "--models",
        help='JSON array e.g. \'["claude-code","codex","opencode"]\'',
    )

    s = sub.add_parser("path", help="Print the canonical progress.ndjson path for an audit-id")
    s.add_argument("--audit-id", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "path":
        print(progress_path(args.audit_id))
        return 0

    if args.cmd == "emit":
        fields: dict[str, object] = {}
        for arg in args.field:
            k, v = _parse_field(arg)
            fields[k] = v
        for cli_attr, json_key in _CLI_FIELD_MAP:
            v = getattr(args, cli_attr, None)
            if v is not None:
                fields[json_key] = v
        if args.severity_breakdown:
            fields["severity_breakdown"] = json.loads(args.severity_breakdown)
        if args.personas:
            fields["personas"] = json.loads(args.personas)
        if args.models:
            fields["models"] = json.loads(args.models)

        try:
            path = emit(args.audit_id, args.event, **fields)
        except ValueError as exc:
            print(f"progress: {exc}", file=sys.stderr)
            return 1

        # Caller-facing log on stderr (stdout reserved for piping).
        print(f"progress: emitted {args.event} → {path}", file=sys.stderr)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
