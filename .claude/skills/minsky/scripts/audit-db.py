#!/usr/bin/env python3
"""
Minsky audit DB helper.

Subcommands:
  initdb         — create the DB and tables (idempotent)
  open           — insert a new audit row at start of an audit
  close          — finalize an audit row
  insert-finding — insert a denormalized finding row
  insert-provenance — record one LLM call's metadata
  set-titan      — record TITAN push receipt
  query          — small set of canned queries

DB location: <repo-root>/.minsky/audits.db (override with --db PATH).
Repo root is detected by walking up from cwd looking for a .git, .minsky, or pyproject.toml marker.
Progress uses MINSKY_PROGRESS_ROOT when set; otherwise --db derives <db-parent>/progress/,
and the default DB uses <repo-root>/codex-audits/.

All operations are idempotent where they can be (PRIMARY KEY on audit_id, IF NOT EXISTS on tables).
Any unexpected error exits non-zero with a descriptive message — no silent fallbacks.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

# Best-effort import of the progress emitter. Wrapped so that a malformed or
# missing progress.py never breaks an audit-db operation; audit DB integrity
# always takes precedence over progress observability.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    import progress as _progress  # type: ignore[import-not-found]
except Exception:
    _progress = None

from provenance import parse_string_list

_PROGRESS_ROOT: Path | None = None


def _emit_safe(audit_id: str, event: str, **fields) -> None:
    """Emit a progress event, swallowing any failure."""
    if _progress is None:
        return
    try:
        if _PROGRESS_ROOT is not None and "MINSKY_PROGRESS_ROOT" not in os.environ:
            _progress.emit_to(_PROGRESS_ROOT, audit_id, event, **fields)
        else:
            _progress.emit(audit_id, event, **fields)
    except Exception as exc:
        print(f"audit-db: progress emit failed (non-fatal): {exc}", file=sys.stderr)

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schemas" / "audit-db.sql"


def find_repo_root(start: Path | None = None) -> Path:
    p = (start or Path.cwd()).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / ".git").exists() or (candidate / ".minsky").exists():
            return candidate
    raise SystemExit(
        f"audit-db.py: cannot locate repo root from {p}; "
        "expected a .git or .minsky directory in cwd or an ancestor"
    )


def db_path(override: str | None) -> Path:
    if override:
        return Path(override).resolve()
    root = find_repo_root()
    return root / ".minsky" / "audits.db"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def cmd_initdb(args: argparse.Namespace) -> int:
    path = db_path(args.db)
    if not SCHEMA_FILE.is_file():
        sys.exit(f"audit-db.py: schema file missing at {SCHEMA_FILE}")
    schema = SCHEMA_FILE.read_text()
    conn = connect(path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()
    print(f"initdb: schema applied at {path}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    from worktree_guard import WorktreeRefused, check_not_linked_worktree

    try:
        check_not_linked_worktree(Path.cwd())
        check_not_linked_worktree(_SCRIPTS_DIR.parent.parents[2])
    except WorktreeRefused as exc:
        print(f"audit-db.py open: {exc}", file=sys.stderr)
        return 1
    path = db_path(args.db)
    started_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    files = parse_string_list(args.files, "--files")
    personas = parse_string_list(args.personas, "--personas", persona=True)
    models = parse_string_list(args.models, "--models", model=True)
    files_json = json.dumps(files, separators=(",", ":"))
    personas_json = json.dumps(personas, separators=(",", ":"))
    models_json = json.dumps(models, separators=(",", ":"))
    conn = connect(path)
    try:
        conn.execute(
            """
            INSERT INTO audits (
              audit_id, started_at, branch, git_commit_at_start, mode,
              scope_kind, scope_description, files_audited, personas_active, models_used,
              total_input_tokens, total_output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                args.audit_id,
                started_at,
                args.branch,
                args.commit,
                args.mode,
                args.scope,
                args.scope_description,
                files_json,
                personas_json,
                models_json,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        sys.exit(f"audit-db.py open: integrity error (audit_id already exists?): {e}")
    finally:
        conn.close()

    # Emit progress event. Personas/models/files arrive as JSON-array strings;
    # parse defensively so a malformed input doesn't break the emit.
    def _maybe_json_list(s):
        try:
            v = json.loads(s) if s else None
            return v if isinstance(v, list) else None
        except (json.JSONDecodeError, TypeError):
            return None

    _emit_safe(
        args.audit_id, "audit_open",
        mode=args.mode,
        scope_kind=args.scope,
        branch=args.branch,
        commit_at_start=args.commit,
        personas=_maybe_json_list(personas_json),
        models=_maybe_json_list(models_json),
    )

    print(f"open: audit_id={args.audit_id} at {path}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    path = db_path(args.db)
    finished_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = connect(path)
    try:
        cur = conn.execute(
            """
            UPDATE audits
            SET finished_at = ?,
                git_commit_at_finish = ?,
                num_rounds = ?,
                convergence_status = ?,
                summary_md_path = ?,
                work_log_entry_path = COALESCE(?, work_log_entry_path),
                total_input_tokens = ?,
                total_output_tokens = ?
            WHERE audit_id = ?
            """,
            (
                finished_at,
                args.commit,
                args.rounds,
                args.convergence,
                args.summary,
                args.work_log,
                args.input_tokens,
                args.output_tokens,
                args.audit_id,
            ),
        )
        if cur.rowcount == 0:
            sys.exit(f"audit-db.py close: audit_id {args.audit_id!r} not found")
        conn.commit()
    finally:
        conn.close()

    _emit_safe(
        args.audit_id, "audit_close",
        convergence_status=args.convergence,
        num_rounds=args.rounds,
    )

    print(f"close: audit_id={args.audit_id} status={args.convergence}")
    return 0


def cmd_insert_finding(args: argparse.Namespace) -> int:
    path = db_path(args.db)
    conn = connect(path)
    try:
        values = (
            args.audit_id,
            args.round,
            args.step,
            args.persona,
            args.severity,
            args.category,
            args.claim,
            args.evidence_file,
            args.evidence_line,
            args.evidence_quoted_line,
            args.suggestion,
            int(args.verified) if args.verified is not None else None,
            args.resolution,
        )
        # Exact idempotence for rerun-safe convergence.  Semantic merging remains
        # an adjudicator decision; only byte-equivalent denormalized rows collapse.
        duplicate = conn.execute(
            """
            SELECT finding_id FROM findings
            WHERE audit_id = ? AND round_number = ? AND step = ? AND persona = ?
              AND severity = ? AND category = ? AND claim = ?
              AND evidence_file IS ? AND evidence_line IS ?
              AND evidence_quoted_line IS ? AND suggestion IS ?
              AND verified IS ? AND resolution IS ?
            LIMIT 1
            """,
            values,
        ).fetchone()
        if duplicate:
            print(f"duplicate: finding_id={duplicate[0]}")
            return 0
        conn.execute(
            """
            INSERT INTO findings (
              audit_id, round_number, step, persona, severity, category, claim,
              evidence_file, evidence_line, evidence_quoted_line, suggestion,
              verified, resolution
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        conn.commit()
    finally:
        conn.close()
    return 0


def cmd_insert_provenance(args: argparse.Namespace) -> int:
    path = db_path(args.db)
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT personas_active, models_used FROM audits WHERE audit_id = ?",
            (args.audit_id,),
        ).fetchone()
        if row is None:
            sys.exit(f"audit-db.py insert-provenance: audit_id {args.audit_id!r} not found")
        if args.round_scope:
            scope_path = Path(args.round_scope).resolve()
            try:
                scope = json.loads(scope_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                sys.exit(f"audit-db.py insert-provenance: invalid round scope {scope_path}: {exc}")
            if scope.get("audit_id") != args.audit_id or scope.get("round_number") != args.round:
                sys.exit("audit-db.py insert-provenance: round scope audit_id/round mismatch")
            registered_personas = scope.get("personas") or []
            registered_models = scope.get("models") or []
            expected_model = (scope.get("step_models") or {}).get(args.step)
            if expected_model != args.model:
                sys.exit(
                    f"audit-db.py insert-provenance: model {args.model!r} is not bound "
                    f"to phase {args.step!r} in {scope_path}"
                )
        else:
            registered_personas = json.loads(row[0])
            registered_models = json.loads(row[1])
        if args.model not in registered_models:
            sys.exit(
                f"audit-db.py insert-provenance: model {args.model!r} is not registered "
                f"for audit {args.audit_id!r}"
            )
        if args.persona is not None and args.persona not in registered_personas:
            sys.exit(
                f"audit-db.py insert-provenance: persona {args.persona!r} is not registered "
                f"for audit {args.audit_id!r}"
            )
        conn.execute(
            """
            INSERT INTO provenance (
              audit_id, round_number, step, model, persona, invoked_at,
              duration_seconds, input_tokens, output_tokens, output_path, exit_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.audit_id,
                args.round,
                args.step,
                args.model,
                args.persona,
                args.invoked_at,
                args.duration,
                args.input_tokens,
                args.output_tokens,
                args.output_path,
                args.exit_status,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return 0


def cmd_set_titan(args: argparse.Namespace) -> int:
    path = db_path(args.db)
    pushed_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE audits SET titan_log_id = ?, titan_pushed_at = ? WHERE audit_id = ?",
            (args.titan_log_id, pushed_at, args.audit_id),
        )
        if cur.rowcount == 0:
            sys.exit(f"audit-db.py set-titan: audit_id {args.audit_id!r} not found")
        conn.commit()
    finally:
        conn.close()
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    path = db_path(args.db)
    conn = connect(path)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM audits"
        clauses, params = [], []
        if args.branch:
            clauses.append("branch = ?")
            params.append(args.branch)
        if args.status:
            clauses.append("convergence_status = ?")
            params.append(args.status)
        if args.since:
            clauses.append("started_at >= ?")
            params.append(args.since)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    print(json.dumps(rows, indent=2))
    return 0


def cmd_last_audit_commit(args: argparse.Namespace) -> int:
    """Return git_commit_at_finish of most recent finished audit on a given branch.

    Used by scope-detect.py for the 'delta' scope mode.
    Prints empty string if there is no prior audit.
    """
    path = db_path(args.db)
    conn = connect(path)
    try:
        row = conn.execute(
            """
            SELECT git_commit_at_finish FROM audits
            WHERE branch = ? AND finished_at IS NOT NULL
            ORDER BY finished_at DESC LIMIT 1
            """,
            (args.branch,),
        ).fetchone()
    finally:
        conn.close()
    print(row[0] if row else "")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minsky audit DB helper")
    parser.add_argument("--db", help="Override DB path (default: <repo>/.minsky/audits.db)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb", help="Create DB and apply schema (idempotent)")

    p_open = sub.add_parser("open", help="Insert a new audit row at start of audit")
    p_open.add_argument("--audit-id", required=True)
    p_open.add_argument("--branch", required=True)
    p_open.add_argument("--commit", required=True, help="git_commit_at_start (full sha)")
    p_open.add_argument("--mode", required=True, choices=["audit","plan","draft","eval","bug-hunt"])
    p_open.add_argument("--scope", required=True, choices=["explicit","delta","paths","time"])
    p_open.add_argument("--scope-description", default="")
    p_open.add_argument("--files", required=True, help="Explicit non-empty JSON array of audited paths")
    p_open.add_argument("--personas", required=True, help="Explicit JSON array of defined persona slugs")
    p_open.add_argument("--models", required=True, help="Explicit JSON array; every model includes @effort")

    p_close = sub.add_parser("close", help="Finalize audit row")
    p_close.add_argument("--audit-id", required=True)
    p_close.add_argument("--commit", required=True, help="git_commit_at_finish")
    p_close.add_argument("--rounds", type=int, required=True)
    p_close.add_argument("--convergence", required=True,
        choices=["agree","disagree","user_override","incomplete","abandoned","paused-rate-limit"])
    p_close.add_argument("--summary", default=None, help="Path to consensus.md")
    p_close.add_argument("--work-log", default=None, help="Path to phd_work_log.md entry (nullable)")
    p_close.add_argument("--input-tokens", type=int, default=None)
    p_close.add_argument("--output-tokens", type=int, default=None)

    p_f = sub.add_parser("insert-finding", help="Insert a denormalized finding row")
    p_f.add_argument("--audit-id", required=True)
    p_f.add_argument("--round", type=int, required=True)
    p_f.add_argument("--step", required=True, choices=["claude_self","codex","opencode","claude_synth"])
    p_f.add_argument("--persona", required=True)
    p_f.add_argument("--severity", required=True, choices=["critical","high","medium","low"])
    p_f.add_argument("--category", required=True)
    p_f.add_argument("--claim", required=True)
    p_f.add_argument("--evidence-file")
    p_f.add_argument("--evidence-line", type=int)
    p_f.add_argument("--evidence-quoted-line")
    p_f.add_argument("--suggestion")
    p_f.add_argument("--verified", type=int, choices=[0, 1], default=None)
    p_f.add_argument("--resolution",
        choices=["addressed","carried_forward","retracted","user_overruled"], default=None)

    p_p = sub.add_parser("insert-provenance", help="Record one LLM-call's metadata")
    p_p.add_argument("--audit-id", required=True)
    p_p.add_argument("--round", type=int, required=True)
    p_p.add_argument("--step", required=True)
    p_p.add_argument("--model", required=True)
    p_p.add_argument("--persona")
    p_p.add_argument("--invoked-at", required=True)
    p_p.add_argument("--duration", type=float)
    p_p.add_argument("--input-tokens", type=int)
    p_p.add_argument("--output-tokens", type=int)
    p_p.add_argument("--output-path", required=True)
    p_p.add_argument(
        "--round-scope",
        help="Immutable per-round scope sidecar; when supplied, validate phase model/persona here",
    )
    p_p.add_argument("--exit-status", default="ok",
        choices=["ok","error","rate-limit","timeout","schema-invalid","usage-invalid",
                 "input-drift","exit-code-mismatch"])

    p_t = sub.add_parser("set-titan", help="Record TITAN push receipt")
    p_t.add_argument("--audit-id", required=True)
    p_t.add_argument("--titan-log-id", required=True)

    p_q = sub.add_parser("query", help="Canned audit queries")
    p_q.add_argument("--branch")
    p_q.add_argument("--status")
    p_q.add_argument("--since")
    p_q.add_argument("--limit", type=int, default=20)

    p_l = sub.add_parser("last-audit-commit", help="Print git_commit_at_finish of most recent audit on branch")
    p_l.add_argument("--branch", required=True)

    args = parser.parse_args(argv)
    global _PROGRESS_ROOT
    _PROGRESS_ROOT = Path(args.db).resolve().parent / "progress" if args.db is not None else None
    handlers = {
        "initdb": cmd_initdb,
        "open": cmd_open,
        "close": cmd_close,
        "insert-finding": cmd_insert_finding,
        "insert-provenance": cmd_insert_provenance,
        "set-titan": cmd_set_titan,
        "query": cmd_query,
        "last-audit-commit": cmd_last_audit_commit,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
