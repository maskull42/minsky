# minsky audit progress event spec (v1.0)

This document is the prose specification for the per-audit progress NDJSON
stream that minsky emits during a `/minsky` invocation. It is the citable
methodological reference for real-time audit observability — a complement
to the audit DB row (which records *outcomes*) and the per-step JSON
findings files (which record *content*).

## Where the events live

```
<repo-root>/codex-audits/<audit-id>/progress.ndjson
```

Path is deterministically derived from `audit-id`. Resolved by
`scripts/progress.py:progress_path()` from the script's location (same
`<repo-root>` root all other minsky scripts use).

The file is part of the audit's permanent on-disk record. It should be
committed alongside `pack.xml`, `claude-self/`, `codex/`, `opencode/`,
`claude-synth/`, `consensus.md`, and `converge.json`.

## Format

NDJSON: one JSON object per line, terminated by `\n`. Each line is a
self-contained event. The file is **append-only**; no rewrites or
in-place edits. Atomic per line under the POSIX-atomic O_APPEND guarantee
(line cap: 512 bytes); events that exceed the cap are refused at emit
time (rather than risk a non-atomic interleaved write).

Events are durable on disk before the emitting call returns
(`progress.py:emit()` calls `os.fsync()` before close). A process crash
after `emit()` returns cannot lose the event.

## Event-level required fields

Every event has at minimum:

| Field | Type | Description |
|---|---|---|
| `ts` | string | ISO 8601 UTC timestamp, seconds precision (e.g. `"2026-05-03T07:41:40Z"`). Same convention as the audit DB's `started_at`/`finished_at` columns. |
| `schema_version` | string | Version of `progress.py` at emit time (currently `"1.1"`). |
| `event` | string | One of the event types listed below. |
| `audit_id` | string | The audit-id slug. Identical to the directory name under `codex-audits/`. |

## Event types

### `audit_open`

Emitted by `audit-db.py open` when the audit DB row is created.

| Optional field | Type | Description |
|---|---|---|
| `mode` | string | `audit` / `plan` / `draft` / `eval` / `bug-hunt`. |
| `scope_kind` | string | `explicit` / `delta` / `paths` / `time`. |
| `branch` | string | git branch at audit start. |
| `commit_at_start` | string | git commit SHA at audit start. |
| `personas` | array<string> | Active persona slugs. |
| `models` | array<string> | Models that will be used. |

### `round_start` / `round_done`

Emitted by `chain.py` at the boundaries of a deliberation round.

| Field | Type | Description |
|---|---|---|
| `round` | int (≥ 1) | Round number. Required. |
| `convergence_status` | string | Only on `round_done`. Mirror of converge.py's per-round verdict. |

### `step_start` / `step_done`

Emitted at each protocol step boundary inside a round.

| Field | Type | Description |
|---|---|---|
| `step` | string | One of `claude_self`, `codex`, `opencode`, `converge`, `claude_synth`. Required. |
| `round` | int | Round number (recommended for cross-event correlation). |

`claude_self` and `claude_synth` are host-Claude steps; the host emits
these directly. `codex`, `opencode`, `converge` are emitted by `chain.py`.

### `persona_walk_start` / `persona_walk_done`

Emitted by `invoke-codex.sh` and `invoke-opencode.sh` (and by the host
for `claude_self` walks) at each per-persona walk boundary.

| Field | Type | `_start` | `_done` |
|---|---|---|---|
| `step` | string | required (`claude_self` / `codex` / `opencode`) | required |
| `persona` | string | required | required |
| `model` | string | recommended | recommended |
| `round` | int | recommended | recommended |
| `duration_s` | number | — | required |
| `exit_status` | string | — | required (`ok` / `error` / `timeout` / `rate-limit` / `schema-invalid`) |
| `verdict` | string | — | optional (`true` / `false` / `partial`) |
| `finding_count` | int | — | optional |
| `severity_breakdown` | object | — | optional, e.g. `{"critical":0,"high":1,"medium":3,"low":1}` |

A `persona_walk_done` event with `exit_status="ok"` and
`finding_count=N` is the strongest single signal that a walk completed
substantively. `exit_status="timeout"` indicates the new
`OPENCODE_TIMEOUT_SECONDS` (default 1800s) limit fired.
`exit_status="schema-invalid"` means the agent wrote a JSON file, but the
wrapper rejected it before convergence because it did not validate against
`findings.schema.json`.

### `converge_done`

Emitted by `chain.py` after `converge.py` returns (between Step 3 and
Step 4).

| Field | Type | Description |
|---|---|---|
| `round` | int | Required. |
| `decision` | string | Required. `agree` / `disagree` / `user_decision_required` / `incomplete` (mirrors converge.py exit codes 0/1/3/2). |
| `unresolved_count` | int | Optional. Number of findings flagged for synthesis-time decision. |

### `audit_close`

Emitted by `audit-db.py close` when the audit DB row is finalized.

| Field | Type | Description |
|---|---|---|
| `convergence_status` | string | Required. Final convergence status. |
| `num_rounds` | int | Optional. |
| `total_findings` | int | Optional. |

### `warning`

Emitted by any script when a recoverable anomaly occurs.

| Field | Type | Description |
|---|---|---|
| `message` | string | Required. ≤ 200 chars. |
| `source` | string | Optional. Emitting script name. |

### `error`

Emitted by any script when a non-recoverable error occurs (the script
will subsequently exit non-zero).

| Field | Type | Description |
|---|---|---|
| `message` | string | Required. ≤ 200 chars. |
| `source` | string | Optional. Emitting script name. |
| `exit_code` | int | Optional. Non-zero exit code about to be returned. |

## Atomicity guarantees

- **Per-line atomicity**: writes are O_APPEND and bounded at 512 bytes,
  which is the conservative POSIX-atomic line cap. Concurrent writers
  cannot interleave bytes within a single line.
- **Durability**: `os.fsync()` is called before close. A process crash
  after emit returns cannot lose the event.
- **No retroactive edits**: the file is append-only by convention. The
  emitter does not support a `progress.py edit` or `progress.py rewrite`
  command.

## Reading the stream

For real-time observation by a parent Claude session or a human watcher:

```bash
# tail one event at a time, formatted human-readably
.claude/skills/minsky/scripts/progress-tail.py <audit-id>

# raw NDJSON
tail -F codex-audits/<audit-id>/progress.ndjson

# replay completed audit
cat codex-audits/<audit-id>/progress.ndjson | jq -c .
```

## Provenance commitment

This stream is part of the audit's evidentiary record. Every audit
referenced in the dissertation methods chapter should be checkable
against its own progress NDJSON. Reproducible runtime traces are a
methodological asset: the absence of structured progress is what made
silent provider hangs (V11 / 2026-05-03 outline-audit incident) hard
to detect; the presence of structured progress is what makes future
hangs detectable in seconds rather than tens of minutes.

## Versioning

`schema_version` is recorded on every event. The current version is
`"1.1"`. Future schema changes must:

- Bump `SCHEMA_VERSION` in `scripts/progress.py`
- Update `schemas/progress.schema.json`
- Update this file
- Add a `## Changelog` section below

Adding new event types is a backward-compatible change (consumers
should ignore unknown event names rather than fail). Adding new
optional fields to existing events is also backward-compatible.
Removing fields or changing field semantics is a breaking change and
requires a major version bump.

## Changelog

- **v1.0** (2026-05-03): initial release. Event types: audit_open,
  round_start/done, step_start/done, persona_walk_start/done,
  converge_done, audit_close, warning, error.
- **v1.1** (2026-05-03): `persona_walk_done.exit_status` adds
  `schema-invalid`; documentation corrected the progress-tail command name.
