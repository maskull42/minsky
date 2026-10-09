-- Minsky audit DB schema
-- Lives at <MARS>/.minsky/audits.db
-- Initialized via: python audit-db.py initdb
--
-- Three tables:
--   audits      — one row per audit run (top-level metadata)
--   findings    — denormalized per-finding rows (queryable across audits)
--   provenance  — per-LLM-call computational record (model, tokens, paths)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS audits (
  audit_id              TEXT PRIMARY KEY,
  started_at            TIMESTAMP NOT NULL,
  finished_at           TIMESTAMP,
  branch                TEXT NOT NULL,
  git_commit_at_start   TEXT NOT NULL,
  git_commit_at_finish  TEXT,
  mode                  TEXT NOT NULL CHECK (mode IN ('audit','plan','draft','eval','bug-hunt')),
  scope_kind            TEXT NOT NULL CHECK (scope_kind IN ('explicit','delta','paths','time')),
  scope_description     TEXT,
  files_audited         TEXT,                       -- JSON array
  personas_active       TEXT NOT NULL,              -- JSON array
  models_used           TEXT NOT NULL,              -- JSON array
  num_rounds            INTEGER NOT NULL DEFAULT 0,
  convergence_status    TEXT CHECK (convergence_status IN
                          ('agree','disagree','user_override','incomplete','abandoned','paused-rate-limit')),
  summary_md_path       TEXT,
  work_log_entry_path   TEXT,
  titan_log_id          TEXT,
  titan_pushed_at       TIMESTAMP,
  total_input_tokens    INTEGER,                    -- NULL = unavailable; never infer zero
  total_output_tokens   INTEGER                     -- NULL = unavailable; never infer zero
);

CREATE INDEX IF NOT EXISTS idx_audits_branch_finish ON audits(branch, git_commit_at_finish);
CREATE INDEX IF NOT EXISTS idx_audits_started_at ON audits(started_at);
CREATE INDEX IF NOT EXISTS idx_audits_status ON audits(convergence_status);

CREATE TABLE IF NOT EXISTS findings (
  finding_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  audit_id              TEXT NOT NULL REFERENCES audits(audit_id) ON DELETE CASCADE,
  round_number          INTEGER NOT NULL,
  step                  TEXT NOT NULL CHECK (step IN ('claude_self','codex','opencode','claude_synth')),
  persona               TEXT NOT NULL,
  severity              TEXT NOT NULL CHECK (severity IN ('critical','high','medium','low')),
  category              TEXT NOT NULL,
  claim                 TEXT NOT NULL,
  evidence_file         TEXT,
  evidence_line         INTEGER,
  evidence_quoted_line  TEXT,
  suggestion            TEXT,
  verified              BOOLEAN,
  resolution            TEXT CHECK (resolution IN
                          ('addressed','carried_forward','retracted','user_overruled', NULL))
);

CREATE INDEX IF NOT EXISTS idx_findings_audit ON findings(audit_id);
CREATE INDEX IF NOT EXISTS idx_findings_persona ON findings(persona);
CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity);
CREATE INDEX IF NOT EXISTS idx_findings_resolution ON findings(resolution);

CREATE TABLE IF NOT EXISTS provenance (
  call_id               INTEGER PRIMARY KEY AUTOINCREMENT,
  audit_id              TEXT NOT NULL REFERENCES audits(audit_id) ON DELETE CASCADE,
  round_number          INTEGER NOT NULL,
  step                  TEXT NOT NULL,
  model                 TEXT NOT NULL,              -- e.g. claude-opus-4-7, gpt-5.5, deepseek-v4-pro
  persona               TEXT,
  invoked_at            TIMESTAMP NOT NULL,
  duration_seconds      REAL,
  input_tokens          INTEGER,
  output_tokens         INTEGER,
  output_path           TEXT NOT NULL,
  exit_status           TEXT                        -- terminal status; complete detail lives in call sidecar
);

CREATE INDEX IF NOT EXISTS idx_provenance_audit ON provenance(audit_id);
CREATE INDEX IF NOT EXISTS idx_provenance_model ON provenance(model);
