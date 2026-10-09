#!/usr/bin/env python3
"""
history.py — query the minsky audit DB.

Backs the /minsky-history slash command. Provides canned queries so users don't
have to write SQL. All output is human-readable to stdout (markdown table) or
JSON (with --json).

Subcommands:
  list             list audits matching filters
  show             show full detail of a single audit (by audit_id)
  findings         query findings rows across audits
  unlogged         audits closed without a phd_work_log entry / TITAN push (Phase 5)

Common filters:
  --since DATE      ISO date or "N days ago"; matches started_at >= DATE
  --status STATUS   convergence_status filter
  --branch BRANCH   branch filter
  --persona PERSONA persona filter (findings subcommand)
  --severity LEVEL  severity filter (findings subcommand)
  --mode MODE       mode filter
  --limit N         max rows (default 20)
  --json            machine-readable JSON output
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent


def find_repo_root(start: Path | None = None) -> Path:
    p = (start or Path.cwd()).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / ".git").exists() or (candidate / ".minsky").exists():
            return candidate
    sys.exit(f"history: cannot locate repo root from {p}")


def db_path(override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    return find_repo_root() / ".minsky" / "audits.db"


def parse_since(spec: str) -> str:
    """Parse ISO date or 'N days/hours ago' into ISO timestamp."""
    spec = spec.strip()
    # ISO date
    if re.match(r"^\d{4}-\d{2}-\d{2}", spec):
        return spec
    # "N days ago" / "N hours ago"
    m = re.match(r"^(\d+)\s+(day|days|hour|hours|minute|minutes)\s+ago$", spec, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = {
            "minute": datetime.timedelta(minutes=n),
            "minutes": datetime.timedelta(minutes=n),
            "hour": datetime.timedelta(hours=n),
            "hours": datetime.timedelta(hours=n),
            "day": datetime.timedelta(days=n),
            "days": datetime.timedelta(days=n),
        }[unit]
        return (datetime.datetime.now(datetime.UTC) - delta).isoformat(timespec="seconds").replace("+00:00", "Z")
    if spec.lower() == "today":
        return datetime.date.today().isoformat()
    if spec.lower() == "yesterday":
        return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    sys.exit(f"history: unrecognized --since spec: {spec!r}")


def render_audits_table(rows: list[dict]) -> str:
    if not rows:
        return "_(no audits matched)_\n"
    cols = ["audit_id", "started_at", "branch", "mode", "scope_kind",
            "num_rounds", "convergence_status"]
    out = ["| " + " | ".join(cols) + " |",
           "| " + " | ".join(["---"] * len(cols)) + " |"]
    for r in rows:
        out.append("| " + " | ".join(
            (str(r.get(c) or "") if c != "audit_id" else f"`{r.get(c) or ''}`")
            for c in cols
        ) + " |")
    return "\n".join(out) + "\n"


def render_findings_table(rows: list[dict]) -> str:
    if not rows:
        return "_(no findings matched)_\n"
    cols = ["audit_id", "round_number", "step", "persona", "severity",
            "category", "verified", "resolution"]
    out = ["| " + " | ".join(cols + ["claim"]) + " |",
           "| " + " | ".join(["---"] * (len(cols) + 1)) + " |"]
    for r in rows:
        claim = (r.get("claim") or "")[:120]
        out.append(
            "| " + " | ".join(str(r.get(c) or "") for c in cols)
            + f" | {claim} |"
        )
    return "\n".join(out) + "\n"


def cmd_list(args: argparse.Namespace) -> int:
    path = db_path(args.db)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM audits"
    clauses, params = [], []
    if args.since:
        clauses.append("started_at >= ?")
        params.append(parse_since(args.since))
    if args.status:
        clauses.append("convergence_status = ?")
        params.append(args.status)
    if args.branch:
        clauses.append("branch = ?")
        params.append(args.branch)
    if args.mode:
        clauses.append("mode = ?")
        params.append(args.mode)
    if args.unresolved:
        # any finding for this audit that is carried_forward
        clauses.append("audit_id IN (SELECT DISTINCT audit_id FROM findings WHERE resolution = 'carried_forward')")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(args.limit))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(render_audits_table(rows))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = db_path(args.db)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    audit = conn.execute("SELECT * FROM audits WHERE audit_id = ?", (args.audit_id,)).fetchone()
    if audit is None:
        sys.exit(f"history show: audit_id {args.audit_id!r} not found")
    findings = [dict(r) for r in conn.execute(
        "SELECT * FROM findings WHERE audit_id = ? ORDER BY round_number, step, persona, severity",
        (args.audit_id,)).fetchall()]
    provenance = [dict(r) for r in conn.execute(
        "SELECT * FROM provenance WHERE audit_id = ? ORDER BY invoked_at",
        (args.audit_id,)).fetchall()]
    conn.close()
    audit = dict(audit)
    if args.json:
        print(json.dumps({"audit": audit, "findings": findings, "provenance": provenance}, indent=2))
        return 0
    print(f"# Audit `{audit['audit_id']}`\n")
    for k in ("started_at", "finished_at", "branch", "git_commit_at_start",
              "git_commit_at_finish", "mode", "scope_kind", "scope_description",
              "num_rounds", "convergence_status", "summary_md_path",
              "work_log_entry_path", "titan_log_id", "titan_pushed_at"):
        v = audit.get(k)
        if v not in (None, ""):
            print(f"- **{k}**: {v}")
    print(f"\n## Findings ({len(findings)})\n")
    print(render_findings_table(findings))
    print(f"\n## Provenance ({len(provenance)} LLM calls)\n")
    if provenance:
        cols = ["round_number", "step", "model", "persona", "duration_seconds",
                "input_tokens", "output_tokens", "exit_status"]
        print("| " + " | ".join(cols) + " |")
        print("| " + " | ".join(["---"] * len(cols)) + " |")
        for p in provenance:
            print("| " + " | ".join(str(p.get(c) or "") for c in cols) + " |")
    return 0


def cmd_findings(args: argparse.Namespace) -> int:
    path = db_path(args.db)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM findings"
    clauses, params = [], []
    if args.persona:
        clauses.append("persona = ?")
        params.append(args.persona)
    if args.severity:
        clauses.append("severity = ?")
        params.append(args.severity)
    if args.unresolved:
        clauses.append("resolution = 'carried_forward'")
    if args.since:
        clauses.append("audit_id IN (SELECT audit_id FROM audits WHERE started_at >= ?)")
        params.append(parse_since(args.since))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY audit_id, round_number, step, persona, severity LIMIT ?"
    params.append(int(args.limit))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(render_findings_table(rows))
    return 0


def cmd_unlogged(args: argparse.Namespace) -> int:
    """Audits closed but missing TITAN push or work-log link."""
    path = db_path(args.db)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """
        SELECT * FROM audits
        WHERE finished_at IS NOT NULL
          AND (work_log_entry_path IS NULL OR titan_log_id IS NULL)
        ORDER BY finished_at DESC
        LIMIT ?
        """, (int(args.limit),)).fetchall()]
    conn.close()
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        if not rows:
            print("_(no unlogged audits — every closed audit has work-log + TITAN linkage)_")
            return 0
        print(f"## {len(rows)} unlogged audit(s)\n")
        print(render_audits_table(rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query the minsky audit DB")
    parser.add_argument("--db", help="Override DB path (default: <repo>/.minsky/audits.db)")
    parser.add_argument("--json", action="store_true", help="Output as JSON instead of markdown")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_l = sub.add_parser("list")
    p_l.add_argument("--since", help="ISO date or 'N days ago'")
    p_l.add_argument("--status", help="convergence_status")
    p_l.add_argument("--branch")
    p_l.add_argument("--mode", choices=["audit","plan","draft","eval","bug-hunt"])
    p_l.add_argument("--unresolved", action="store_true",
                     help="Only audits with at least one carried_forward finding")
    p_l.add_argument("--limit", type=int, default=20)

    p_s = sub.add_parser("show")
    p_s.add_argument("audit_id")

    p_f = sub.add_parser("findings")
    p_f.add_argument("--persona")
    p_f.add_argument("--severity", choices=["critical","high","medium","low"])
    p_f.add_argument("--since")
    p_f.add_argument("--unresolved", action="store_true")
    p_f.add_argument("--limit", type=int, default=50)

    p_u = sub.add_parser("unlogged")
    p_u.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)
    handlers = {"list": cmd_list, "show": cmd_show, "findings": cmd_findings, "unlogged": cmd_unlogged}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
