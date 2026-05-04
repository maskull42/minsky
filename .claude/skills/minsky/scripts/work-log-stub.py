#!/usr/bin/env python3
"""
work-log-stub.py — draft a research-log entry from a closed audit row.

Some projects keep a local research log for meaningful work blocks. An audit
cycle qualifies. This script drafts the entry; the user reviews and either
accepts it or adapts it to the local convention.

Default research-log format:

  ### YYYY-MM-DD
  | Date | Duration | Category | Description |
  |------|----------|----------|-------------|
  | YYYY-MM-DD | ~XXh | <comma-separated categories> | <description> |

Categories per CLAUDE.md: Coding, Research, Writing, Methodology,
Theoretical Framework, Administration. Most audits land in Methodology + Research.

Usage:
  work-log-stub.py draft  --audit-id <id> [--duration-hours <h>]
       prints the proposed markdown to stdout
  work-log-stub.py append --audit-id <id> [--duration-hours <h>]
       appends to documentation/research_log.md AND records the entry path
       in audits.work_log_entry_path
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parent.parent.parent
WORK_LOG = REPO_ROOT / "documentation" / "research_log.md"


def fail(msg: str, code: int = 1):
    print(f"work-log-stub: {msg}", file=sys.stderr)
    sys.exit(code)


def db_path() -> Path:
    return REPO_ROOT / ".minsky" / "audits.db"


def fetch_audit(audit_id: str) -> dict:
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM audits WHERE audit_id = ?", (audit_id,)).fetchone()
    conn.close()
    if row is None:
        fail(f"audit_id not found: {audit_id}")
    return dict(row)


def fetch_findings_summary(audit_id: str) -> dict:
    conn = sqlite3.connect(str(db_path()))
    rows = conn.execute(
        """
        SELECT step, persona, severity, COUNT(*) AS c,
               SUM(CASE WHEN verified=1 THEN 1 ELSE 0 END) AS v
        FROM findings
        WHERE audit_id = ?
        GROUP BY step, persona, severity
        """, (audit_id,)).fetchall()
    conn.close()
    return {f"{r[0]}/{r[1]}/{r[2]}": {"count": r[3], "verified": r[4]} for r in rows}


def _json_list(value: object, default: list[str]) -> list[str]:
    if not value:
        return default
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return default
    return parsed if isinstance(parsed, list) else default


def estimate_duration_hours(audit: dict) -> float:
    """If duration not provided, derive from started_at..finished_at."""
    s = audit.get("started_at")
    f = audit.get("finished_at")
    if not s or not f:
        return 0.5
    try:
        s_dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        f_dt = datetime.datetime.fromisoformat(f.replace("Z", "+00:00"))
        return round((f_dt - s_dt).total_seconds() / 3600, 1)
    except ValueError:
        return 0.5


def render_entry(audit: dict, duration_hours: float, summary: dict) -> str:
    date = audit.get("started_at", "")[:10] or datetime.date.today().isoformat()
    mode = audit.get("mode", "audit")
    scope = audit.get("scope_description", "(no scope description)")
    convergence = audit.get("convergence_status", "unknown")
    rounds = audit.get("num_rounds", 1)
    n_findings = sum(v["count"] for v in summary.values())
    n_verified = sum(v["verified"] for v in summary.values())
    n_critical_high = sum(
        v["count"] for k, v in summary.items()
        if k.endswith("/critical") or k.endswith("/high")
    )
    models = _json_list(audit.get("models_used"), ["claude-code", "codex", "opencode"])
    personas = _json_list(audit.get("personas_active"), [])
    model_desc = ", ".join(models)
    persona_desc = ", ".join(personas) if personas else "configured personas"

    description = (
        f"Ran a `/minsky {mode}` adversarial audit ({rounds} round{'s' if rounds != 1 else ''}) "
        f"across {model_desc}, using {persona_desc}. Scope: {scope}. "
        f"Surfaced {n_findings} findings ({n_critical_high} critical/high), "
        f"{n_verified} verified by self-consistency check against cited sources. "
        f"Convergence: {convergence}. "
        f"Audit ID `{audit['audit_id']}`; full record in `{audit.get('summary_md_path', '?')}` "
        f"and `.minsky/audits.db`."
    )

    table = (
        f"| {date} | ~{duration_hours}h | Methodology, Research | {description} |"
    )
    return f"### {date}\n\n| Date | Duration | Category | Description |\n|------|----------|----------|-------------|\n{table}\n"


def cmd_draft(args: argparse.Namespace) -> int:
    audit = fetch_audit(args.audit_id)
    summary = fetch_findings_summary(args.audit_id)
    duration = args.duration_hours or estimate_duration_hours(audit)
    print(render_entry(audit, duration, summary))
    return 0


def cmd_append(args: argparse.Namespace) -> int:
    audit = fetch_audit(args.audit_id)
    summary = fetch_findings_summary(args.audit_id)
    duration = args.duration_hours or estimate_duration_hours(audit)
    if not WORK_LOG.is_file():
        fail(f"work log not found at {WORK_LOG}")

    entry = render_entry(audit, duration, summary)
    # Loud append: open in append mode, add a blank line + entry
    with WORK_LOG.open("a", encoding="utf-8") as f:
        f.write("\n" + entry + "\n")

    # Record path in audit DB
    rel_path = str(WORK_LOG.relative_to(REPO_ROOT))
    subprocess.run(
        ["python3", str(SKILL_DIR / "scripts" / "audit-db.py"), "close",
         "--audit-id", args.audit_id,
         "--commit", audit.get("git_commit_at_finish") or audit.get("git_commit_at_start"),
         "--rounds", str(audit.get("num_rounds", 1)),
         "--convergence", audit.get("convergence_status") or "agree",
         "--summary", audit.get("summary_md_path") or "",
         "--work-log", rel_path],
        check=True,
    )
    print(f"work-log-stub: appended entry to {WORK_LOG} and linked to audit DB", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft a research-log entry from an audit")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("draft", "append"):
        s = sub.add_parser(name)
        s.add_argument("--audit-id", required=True)
        s.add_argument("--duration-hours", type=float, default=None)
    args = parser.parse_args(argv)
    return {"draft": cmd_draft, "append": cmd_append}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
