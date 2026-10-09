#!/usr/bin/env python3
"""
progress-tail.py — format an audit's progress NDJSON as human-readable status lines.

Designed to be the `command` of a Claude Code `Monitor` invocation. Each NDJSON
event becomes a single stdout line (one notification in the parent session),
formatted for at-a-glance comprehension. Exits 0 when an `audit_close` event is
read; survives writer slowness via tail -F semantics.
Progress is read under MINSKY_PROGRESS_ROOT when set, else <repo-root>/codex-audits/.

Usage:
    progress-tail.py <audit-id> [--include-walk-start] [--no-wait-for-file]

Behavior:
- If the progress file doesn't exist yet, polls every second for up to
  --wait-seconds (default 600) before giving up. This lets the tail be armed
  via Monitor BEFORE the audit subprocess starts writing.
- Reads each line as JSON, emits a human-readable line per event.
- For minimal noise, persona_walk_start events are skipped by default
  (the corresponding _done event carries the meaningful signal). Use
  --include-walk-start to surface them.
- Exits 0 on `audit_close`; exits 0 on EOF if --once was passed (replay mode).

Format examples:
    [07:41:40Z] OPEN  paper-2026-x  audit  paths  main@da1325e  3 personas
    [07:46:15Z] STEP> R1 codex
    [07:49:33Z] WALK< R1 codex × marcion-heresiologist  3m18s  ok  verdict=partial  5 findings (1H/3M/1L)
    [07:50:00Z] WALK< R1 codex × ml-finetuning-phd  3m11s  ok  verdict=false  8 findings (5H/2M/1L)
    [07:53:22Z] STEP< R1 codex
    [08:32:15Z] CONVG R1 disagree
    [08:38:09Z] CLOSE paper-2026-x  disagree  1 rounds
    [08:38:09Z] WARN  invoke-opencode.sh: TIMEOUT after 1800s (V11 pattern)
    [08:38:09Z] ERROR opencode × marcion-heresiologist: failed  exit=1

This script is intentionally tolerant of malformed or unknown event types;
unknown events are surfaced as `[<ts>] EVENT <event-name> <raw-record>` rather
than dropped, so the operator never silently loses signal.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from progress import progress_path as _progress_path


def progress_path(audit_id: str) -> Path:
    """Use the shared resolver; refuse an invalid MINSKY_PROGRESS_ROOT."""
    return _progress_path(audit_id)


def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    s = float(seconds)
    if s < 60:
        return f"{s:.0f}s"
    m = int(s // 60)
    rem = int(s - m * 60)
    return f"{m}m{rem:02d}s"


def fmt_severity(sev: dict | None) -> str:
    if not sev or not isinstance(sev, dict):
        return ""
    parts = []
    for label, key in [("C", "critical"), ("H", "high"), ("M", "medium"), ("L", "low")]:
        v = sev.get(key, 0)
        if v:
            parts.append(f"{v}{label}")
    return "/".join(parts) if parts else ""


def fmt_ts(ts: str) -> str:
    # "2026-05-03T07:41:40Z" -> "[07:41:40Z]"
    if not ts or "T" not in ts:
        return f"[{ts or '????????'}]"
    return f"[{ts.split('T', 1)[1]}]"


def fmt_event(rec: dict) -> str | None:
    """Return a one-line human-readable string, or None to skip."""
    ts = fmt_ts(rec.get("ts", ""))
    event = rec.get("event", "")
    audit_id = rec.get("audit_id", "")
    rnd = rec.get("round")
    rstr = f"R{rnd} " if rnd is not None else ""

    if event == "audit_open":
        commit = (rec.get("commit_at_start") or "")[:7]
        personas = rec.get("personas") or []
        return (
            f"{ts} OPEN  {audit_id}  {rec.get('mode','?')}  "
            f"{rec.get('scope_kind','?')}  "
            f"{rec.get('branch','?')}@{commit}  "
            f"{len(personas)} persona{'s' if len(personas)!=1 else ''}"
        )

    if event == "round_start":
        return f"{ts} ROUND> {rstr.strip()}"
    if event == "round_done":
        cs = rec.get("convergence_status", "")
        return f"{ts} ROUND< {rstr.strip()}  {cs}"

    if event == "step_start":
        return f"{ts} STEP> {rstr}{rec.get('step','?')}"
    if event == "step_done":
        return f"{ts} STEP< {rstr}{rec.get('step','?')}"

    if event == "persona_walk_start":
        return (
            f"{ts} WALK> {rstr}{rec.get('step','?')} × {rec.get('persona','?')} "
            f"({rec.get('model','?')})"
        )

    if event == "persona_walk_done":
        dur = fmt_duration(rec.get("duration_s"))
        status = rec.get("exit_status", "?")
        verdict = rec.get("verdict")
        verdict_str = f"  verdict={verdict}" if verdict else ""
        n = rec.get("finding_count")
        sev = fmt_severity(rec.get("severity_breakdown"))
        sev_str = f" ({sev})" if sev else ""
        n_str = f"  {n} finding{'s' if n != 1 else ''}{sev_str}" if n is not None else ""
        status_str = status.upper() if status != "ok" else status
        return (
            f"{ts} WALK< {rstr}{rec.get('step','?')} × {rec.get('persona','?')}"
            f"  {dur}  {status_str}{verdict_str}{n_str}"
        )

    if event == "converge_done":
        unr = rec.get("unresolved_count")
        unr_str = f"  ({unr} unresolved)" if unr else ""
        return f"{ts} CONVG {rstr}{rec.get('decision','?')}{unr_str}"

    if event == "audit_close":
        cs = rec.get("convergence_status", "?")
        rounds = rec.get("num_rounds")
        total = rec.get("total_findings")
        bits = [cs]
        if rounds is not None:
            bits.append(f"{rounds} round{'s' if rounds != 1 else ''}")
        if total is not None:
            bits.append(f"{total} finding{'s' if total != 1 else ''}")
        return f"{ts} CLOSE {audit_id}  " + "  ".join(bits)

    if event == "warning":
        src = rec.get("source")
        src_str = f"{src}: " if src else ""
        return f"{ts} WARN  {src_str}{rec.get('message','')}"

    if event == "error":
        src = rec.get("source")
        src_str = f"{src}: " if src else ""
        ec = rec.get("exit_code")
        ec_str = f"  exit={ec}" if ec is not None else ""
        return f"{ts} ERROR {src_str}{rec.get('message','')}{ec_str}"

    # Unknown event — surface raw rather than drop
    return f"{ts} EVENT {event}  {json.dumps(rec, separators=(',',':'))}"


def tail_progress(path: Path, args) -> int:
    """Tail the file; emit formatted lines; exit 0 on audit_close."""

    # Wait for the file to appear (the audit subprocess may still be starting).
    waited = 0
    poll_interval_s = 1.0
    while not path.is_file():
        if not args.wait_for_file:
            print(f"progress-tail: file does not exist: {path}", file=sys.stderr)
            return 1
        if waited >= args.wait_seconds:
            print(
                f"progress-tail: gave up waiting for {path} after {args.wait_seconds}s",
                file=sys.stderr,
            )
            return 1
        time.sleep(poll_interval_s)
        waited += poll_interval_s

    # Replay-mode: read once and exit at EOF.
    if args.once:
        with path.open() as f:
            for line in f:
                process_line(line, args)
        return 0

    # Live-tail mode: follow growth, exit on audit_close.
    #
    # Implementation note: open in BINARY mode so tell()/seek() are byte
    # offsets (text-mode tell() is opaque on multi-byte input). We read
    # whatever bytes are available since the last position, split on \n,
    # emit full lines, and buffer any trailing partial line for the next
    # iteration. This avoids the `for line in f` next()/tell() incompatibility
    # entirely.
    seen_close = False
    close_grace_until = 0.0
    pending_partial = b""
    with path.open("rb") as f:
        while True:
            chunk = f.read()  # reads all bytes from current pos to EOF
            if chunk:
                buffer = pending_partial + chunk
                lines = buffer.split(b"\n")
                # Last element is either b"" (clean EOF) or a partial line
                # (writer hasn't finished). Buffer the partial for next iteration.
                pending_partial = lines[-1]
                for raw in lines[:-1]:
                    if not raw.strip():
                        continue
                    try:
                        decoded = raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        print(
                            f"[--:--:--Z] BADLN  utf-8 decode failed: {exc}: {raw[:80]!r}",
                            flush=True,
                        )
                        continue
                    if process_line(decoded + "\n", args) == "close":
                        seen_close = True
                        close_grace_until = time.time() + 2.0
                continue
            # EOF: maybe wait for more, maybe finalize.
            if seen_close and time.time() >= close_grace_until:
                return 0
            time.sleep(0.5)


def process_line(line: str, args) -> str | None:
    """Format and emit one line. Returns 'close' if the event was audit_close."""
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except json.JSONDecodeError as exc:
        print(f"[--:--:--Z] BADLN  malformed JSON: {exc}: {line[:120]!r}", flush=True)
        return None

    event = rec.get("event")
    if event == "persona_walk_start" and not args.include_walk_start:
        return None

    formatted = fmt_event(rec)
    if formatted is not None:
        print(formatted, flush=True)

    return "close" if event == "audit_close" else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Tail and format a minsky audit progress NDJSON for parent monitoring."
    )
    parser.add_argument("audit_id", help="Audit ID (matches codex-audits/<audit-id>/).")
    parser.add_argument(
        "--include-walk-start",
        action="store_true",
        help="Also emit persona_walk_start events (default: skip; the matching _done event carries the signal).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Read the file once and exit at EOF (replay mode for completed audits).",
    )
    parser.add_argument(
        "--no-wait-for-file",
        dest="wait_for_file",
        action="store_false",
        default=True,
        help="Exit 1 immediately if the progress file doesn't exist (default: wait).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=600,
        help="How long to wait for the progress file to appear (default: 600s).",
    )
    args = parser.parse_args(argv)

    path = progress_path(args.audit_id)
    return tail_progress(path, args)


if __name__ == "__main__":
    sys.exit(main())
