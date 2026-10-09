#!/usr/bin/env python3
"""
work-log-stub.py — draft a phd_work_log.md entry from a closed audit row.

Per MARS CLAUDE.md, every meaningful work block (>15 min) requires a work-log
entry. An audit cycle qualifies. This script drafts the entry; the user reviews
and either accepts (we append it) or edits (they paste the revised version
themselves and run --commit to record it in the audit DB).

The work-log format is the established RICH-PROSE entry (see the Ongoing Log in
documentation/phd_work_log.md): `### YYYY-MM-DD — <title>` + Timestamp/duration,
Categories, Technical work, Critical observations, Theoretical framework relevance,
Methodology relevance, Research relevance, Big picture, Coding work, TITAN receipt.
The stub renders that skeleton with the audit's derived facts filled in and
⟨FILL⟩ markers where the researcher's own judgment prose is REQUIRED; `append`
REFUSES while any ⟨FILL⟩ marker remains (no sparse table-only entries — minsky
round-2 fix, repo-cleanup-context-arch audit 2026-06-11).

Categories per CLAUDE.md: Coding, Research, Writing, Methodology,
Theoretical Framework, Administration. Most audits land in Methodology + Research.

Usage:
  work-log-stub.py draft  --audit-id <id> [--duration-hours <h>]
       prints the proposed markdown to stdout
  work-log-stub.py append --audit-id <id> [--duration-hours <h>]
       appends to documentation/phd_work_log.md AND records the entry path
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
WORK_LOG = REPO_ROOT / "documentation" / "phd_work_log.md"


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


def estimate_duration_hours(audit: dict) -> float:
    """Derive duration from started_at..finished_at. FAIL LOUD if underivable.

    No silent fallback (minsky round-2, repo-cleanup-context-arch audit 2026-06-11):
    a fabricated default duration in a dissertation/NWO-cited record is a
    provenance defect. If timestamps are missing/malformed, the caller must pass
    --duration-hours explicitly.
    """
    s = audit.get("started_at")
    f = audit.get("finished_at")
    if not s or not f:
        fail(
            f"audit row has missing timestamps (started_at={s!r}, finished_at={f!r}) — "
            f"cannot derive duration. Pass --duration-hours explicitly (no silent default)."
        )
    try:
        s_dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        f_dt = datetime.datetime.fromisoformat(f.replace("Z", "+00:00"))
        return round((f_dt - s_dt).total_seconds() / 3600, 1)
    except ValueError as e:
        fail(
            f"audit row timestamps unparseable ({e}) — pass --duration-hours explicitly "
            f"(no silent default)."
        )


def render_entry(audit: dict, duration_hours: float, summary: dict) -> str:
    date = audit.get("started_at", "")[:10] or datetime.date.today().isoformat()
    mode = audit.get("mode", "audit")
    scope = audit.get("scope_description", "(no scope description)")
    convergence = audit.get("convergence_status") or "(audit not yet closed)"
    rounds = audit.get("num_rounds", 1)
    n_findings = sum(v["count"] for v in summary.values())
    n_verified = sum(v["verified"] for v in summary.values())
    n_critical_high = sum(
        v["count"] for k, v in summary.items()
        if k.endswith("/critical") or k.endswith("/high")
    )

    # Derive models + personas from the audits.db row — NEVER hardcode (a false
    # model attribution in a dissertation-cited record is a provenance defect;
    # minsky round-1 F14, a reference-deployment audit).
    import json as _json
    _MODEL_NAMES = {
        "claude-opus-4-8": "Claude Opus 4.8", "claude-opus-4-7": "Claude Opus 4.7",
        "claude-fable-5": "Claude Fable 5",
        "gpt-5.5": "Codex GPT-5.5",                      # pre-2026-08-18 audits (stamp was stale; see invoke-codex.sh)
        "gpt-5.6-sol@medium": "Codex GPT-5.6-sol @ medium",
        "gpt-5.6-sol@max": "Codex GPT-5.6-sol @ max",
        "gemini-3.7-flash@high": "OpenCode Gemini 3.7 Flash @ high thinking",  # historical audits, 2026-08-18..2026-09-04
        "gemini-3.8-flash@high": "OpenCode Gemini 3.8 Flash @ high thinking",  # Minsky default since 2026-09-04
        "MiniMax-M3": "OpenCode MiniMax M3@max",         # 2026-07-01 .. 2026-08-05
        "deepseek-v4-flash": "OpenCode DeepSeek V4 Flash",  # 2026-08-05 .. 2026-08-18 (credit exhausted)
        "deepseek-v4-pro": "OpenCode DeepSeek V4 Pro",  # retained for pre-2026-07-01 audit provenance
    }
    _models_raw = audit.get("models_used")
    _personas_raw = audit.get("personas_active")
    if not _models_raw or not _personas_raw:
        raise SystemExit(
            f"work-log-stub: audits.db row for {audit.get('audit_id')} lacks models/personas "
            f"columns — refusing to draft with guessed attribution (no silent fallback)."
        )
    _models = [_MODEL_NAMES.get(m, m) for m in _json.loads(_models_raw)] \
        if isinstance(_models_raw, str) else [_MODEL_NAMES.get(m, m) for m in _models_raw]
    _personas = _json.loads(_personas_raw) if isinstance(_personas_raw, str) else _personas_raw
    _models_txt = (f"{_models[0]} (host self-audit), " +
                   ", ".join(_models[1:-1]) + (", " if len(_models) > 2 else "") +
                   f"and {_models[-1]} (per-persona adversarial)") if len(_models) >= 2 else _models[0]

    description = (
        f"Ran a `/minsky {mode}` adversarial audit ({rounds} round{'s' if rounds != 1 else ''}) "
        f"across {_models_txt}, with the {', '.join(_personas)} persona"
        f"{'s' if len(_personas) != 1 else ''}. Scope: {scope}. "
        f"Surfaced {n_findings} findings ({n_critical_high} critical/high), "
        f"{n_verified} verified by self-consistency check against cited sources. "
        f"Convergence: {convergence}. "
        f"Audit ID `{audit['audit_id']}`; full record in `{audit.get('summary_md_path') or '(pending close)'}` "
        f"and `.minsky/audits.db`."
    )

    return (
        f"### {date} — /minsky {mode} `{audit['audit_id']}` (⟨FILL: one-line outcome⟩)\n\n"
        f"**Timestamp and duration:** {date}, ~{duration_hours}h. Logged on {datetime.date.today().isoformat()}.\n\n"
        f"**Categories:** Methodology, Research⟨FILL: adjust if needed⟩.\n\n"
        f"**Technical work:** {description} ⟨FILL: what was remediated/decided, per round⟩\n\n"
        f"**Critical observations / rigor stance:** ⟨FILL: what the cross-model gate caught that the host missed, retractions with evidence, honest caveats⟩\n\n"
        f"**Theoretical framework relevance:** ⟨FILL⟩\n\n"
        f"**Methodology relevance:** ⟨FILL: what this audit demonstrates for the cross-model apparatus⟩\n\n"
        f"**Research relevance:** ⟨FILL: which RQ this serves⟩\n\n"
        f"**Big picture:** ⟨FILL: chapter/phase⟩\n\n"
        f"**Coding work:** ⟨FILL: scripts/files touched, committable vs fenced⟩\n\n"
        f"**TITAN:** (pending push)\n"
    )


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
    # The skeleton REQUIRES researcher completion: refuse to append a sparse entry
    # (no machine-generated stand-in for researcher judgment — work-log skill contract).
    if "⟨FILL" in entry:
        fail(
            "rendered entry still contains ⟨FILL⟩ markers — the rich-prose sections are "
            "the researcher's judgment and must be completed. Author the entry directly in "
            "documentation/phd_work_log.md (use `draft` for the skeleton), then record the "
            "audit linkage via audit-db.py close."
        )
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
         # No silent fallback: an unclosed/NULL convergence must NOT be stamped
         # as a successful "agree" (minsky round-2 fix). Close the audit first.
         "--convergence", audit.get("convergence_status") or fail(
             f"audit {args.audit_id} has no convergence_status — close it via "
             f"audit-db.py close before appending the work-log entry."
         ),
         "--summary", audit.get("summary_md_path") or "",
         "--work-log", rel_path],
        check=True,
    )
    print(f"work-log-stub: appended entry to {WORK_LOG} and linked to audit DB", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft a phd_work_log entry from an audit")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("draft", "append"):
        s = sub.add_parser(name)
        s.add_argument("--audit-id", required=True)
        s.add_argument("--duration-hours", type=float, default=None)
    args = parser.parse_args(argv)
    return {"draft": cmd_draft, "append": cmd_append}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
