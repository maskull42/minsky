#!/usr/bin/env bash
#
# invoke-codex.sh — wrap one Codex per-(persona) call for the minsky chain.
#
# Receives a prompt on stdin, a target cwd (where Codex's workspace lives), and
# an audit-id + round + persona for provenance recording. Writes Codex's output
# (the JSON findings file) inside cwd at the path the prompt names. Returns
# non-zero on any failure; never silently retries or falls back.
#
# Usage:
#   invoke-codex.sh \
#       --cwd <abs-path-to-output-dir> \
#       --audit-id <id> \
#       --round <n> \
#       --persona <name> \
#       < prompt.txt
#
# The prompt itself must:
#   - tell Codex what to audit (the artifact)
#   - embed the JSON schema for findings
#   - name the exact output path inside cwd
#   - instruct self-validation before declaring done

set -euo pipefail

: "${CODEX_TIMEOUT_SECONDS:=1800}"
: "${CODEX_KILL_GRACE_SECONDS:=60}"

# parse args
CWD=""; AUDIT_ID=""; ROUND=""; PERSONA=""; INPUT_FILES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cwd) CWD="$2"; shift 2 ;;
    --audit-id) AUDIT_ID="$2"; shift 2 ;;
    --round) ROUND="$2"; shift 2 ;;
    --persona) PERSONA="$2"; shift 2 ;;
    --input-file) INPUT_FILES+=("$2"); shift 2 ;;
    *) echo "invoke-codex: unknown arg: $1" >&2; exit 64 ;;
  esac
done
for v in CWD AUDIT_ID ROUND PERSONA; do
  if [[ -z "${!v}" ]]; then
    flag="$(printf '%s' "$v" | tr '[:upper:]_' '[:lower:]-')"
    echo "invoke-codex: missing required arg --${flag}" >&2
    exit 64
  fi
done

mkdir -p "$CWD"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../../.." && pwd)"
PROGRESS_PY="$SKILL_DIR/scripts/progress.py"
VALIDATE_FINDINGS_PY="$SKILL_DIR/scripts/validate-findings.py"

# Best-effort progress emitter; never fail the walk on a progress error.
emit_progress() {
  if [[ -x "$PROGRESS_PY" ]] || [[ -f "$PROGRESS_PY" ]]; then
    python3 "$PROGRESS_PY" emit --audit-id "$AUDIT_ID" "$@" 2>/dev/null || true
  fi
}

# ★ 2026-08-18 PROVENANCE FIX. This script previously invoked `codex exec` with NO --model and stamped
# the literal "gpt-5.5" into the audit DB + progress events. The codex CLI default is
# model = "gpt-5.6-sol", model_reasoning_effort = "medium" (~/.codex/config.toml), so minsky audits have
# been running gpt-5.6-sol@medium while RECORDING gpt-5.5 — a false model stamp in a dissertation-cited
# record (same defect class as the R3 challenger variant stamp guarded 2026-08-18). Model and effort are
# now EXPLICIT, env-overridable, and stamped from the SAME variables passed on the wire.
# NOTE (behaviour deliberately unchanged): the effort default is what minsky has actually been running
# (medium). The R3 challenger's codex leg runs at max — raise CODEX_EFFORT to audit at that rigour; that
# is a cost/quality decision for the researcher, not a bug fix, so it is not made here.
: "${CODEX_MODEL:=gpt-5.6-sol}"
: "${CODEX_EFFORT:=medium}"
CODEX_MODEL_STAMP="${CODEX_MODEL}@${CODEX_EFFORT}"

OUTPUT_PATH="$CWD/${PERSONA}.json"
rm -f "$OUTPUT_PATH"

# Preserve immutable evidence for every attempt. Retries get new call IDs and
# cannot overwrite prior prompts, raw logs, validation logs, or snapshots.
CALL_UID="$(python3 "$SKILL_DIR/scripts/provenance.py" new-uid)"
LOG_DIR="$CWD/_codex_logs"
mkdir -p "$LOG_DIR"
PROMPT_FILE="$LOG_DIR/${PERSONA}.round-${ROUND}.${CALL_UID}.prompt.txt"
LOG_FILE="$LOG_DIR/${PERSONA}.round-${ROUND}.${CALL_UID}.log"
VALIDATION_LOG="$LOG_DIR/${PERSONA}.round-${ROUND}.${CALL_UID}.validation.log"
OUTPUT_SNAPSHOT="$LOG_DIR/${PERSONA}.round-${ROUND}.${CALL_UID}.output.json"
cat > "$PROMPT_FILE"

CODEX_PATH="$(command -v codex || true)"
if [[ -z "$CODEX_PATH" ]]; then
  echo "invoke-codex: codex executable not found" >&2
  exit 1
fi
CODEX_VERSION="$(codex --version 2>&1 | head -1)"
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_BIN="$(command -v timeout)"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_BIN="$(command -v gtimeout)"
else
  echo "invoke-codex: required GNU timeout command not found (tried timeout, gtimeout)" >&2
  exit 1
fi
RUNTIME_JSON="$(python3 - "$CODEX_PATH" "$CODEX_MODEL" "$CODEX_EFFORT" \
  "$TIMEOUT_BIN" "$CODEX_TIMEOUT_SECONDS" "$CODEX_KILL_GRACE_SECONDS" <<'PY'
import json, sys
print(json.dumps({
    "executable": sys.argv[1],
    "argv": [sys.argv[4], f"--kill-after={sys.argv[6]}s", f"{sys.argv[5]}s",
             sys.argv[1], "exec", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox", "--model", sys.argv[2],
             "-c", f"model_reasoning_effort={sys.argv[3]}",
             "-c", "project_doc_max_bytes=0", "-"],
    "stdin_source": "durable prompt file",
    "timeout": {"kind": "gnu-timeout", "executable": sys.argv[4],
                "wall_clock_seconds": int(sys.argv[5]),
                "kill_grace_seconds": int(sys.argv[6])},
    "observable_subset": "explicit argv, stdin prompt, declared context, executable/wrapper hashes",
    "not_recorded": ["credential values", "hidden service state", "unreferenced global client configuration"],
}, separators=(",", ":")))
PY
)"

RUNTIME_FILE_ARGS=(--runtime-file "$0" --runtime-file "$CODEX_PATH" --runtime-file "$TIMEOUT_BIN")
RUNTIME_FILE_ARGS+=(--runtime-file "$(command -v python3)" --runtime-file "$SKILL_DIR/scripts/provenance.py" --runtime-file "$VALIDATE_FINDINGS_PY")
CONTEXT_RECORD_ARGS=()
set +u
for context_file in "${INPUT_FILES[@]}"; do
  CONTEXT_RECORD_ARGS+=(--context-file "$context_file")
done
set -u

# Immutable pre-dispatch receipt: exact prompt, declared context, executable,
# wrapper, command/model/effort, and explicit timeout policy are hashed before
# the model process can observe or mutate anything.
PREFLIGHT_PATH="$(python3 "$SKILL_DIR/scripts/provenance.py" prepare \
  --call-uid "$CALL_UID" \
  --audit-id "$AUDIT_ID" \
  --round "$ROUND" \
  --round-dir "$CWD/.." \
  --step codex \
  --persona "$PERSONA" \
  --origin wrapper \
  --provider openai \
  --model "$CODEX_MODEL" \
  --effort "$CODEX_EFFORT" \
  --client-name codex-cli \
  --client-version "$CODEX_VERSION" \
  --prompt-path "$PROMPT_FILE" \
  --expected-output-path "$OUTPUT_PATH" \
  ${RUNTIME_FILE_ARGS[@]+"${RUNTIME_FILE_ARGS[@]}"} \
  ${CONTEXT_RECORD_ARGS[@]+"${CONTEXT_RECORD_ARGS[@]}"} \
  --runtime-json "$RUNTIME_JSON")"

INVOKED_AT="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.UTC).isoformat(timespec="microseconds").replace("+00:00","Z"))')"
START="$(python3 -c 'import time; print(time.time())')"

emit_progress --event persona_walk_start \
  --round "$ROUND" --step codex --persona "$PERSONA" --model "$CODEX_MODEL_STAMP"

set +e
# Use the explicit Codex bypass flag rather than its --yolo alias so logs state
# exactly what is being bypassed: approvals and sandboxing.
# ★ 2026-08-18: -c project_doc_max_bytes=0 keeps the repo AGENTS.md OUT of this leg's context.
# AGENTS.md opens with "MANDATORY: PhD Work Logging ... push to TITAN ... Failure to log is a project
# integrity violation" — the exact instruction that made a full-access challenger leg self-log 3
# entries to phd_work_log.md + TITAN on 2026-07-10. This leg must keep write access (it writes
# $OUTPUT_PATH), so removing the instruction from context is the available mitigation; tightening
# the sandbox to -s workspace-write is a separate, testable change.
( cd "$CWD" && "$TIMEOUT_BIN" "--kill-after=${CODEX_KILL_GRACE_SECONDS}s" "${CODEX_TIMEOUT_SECONDS}s" \
    "$CODEX_PATH" exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
    --model "$CODEX_MODEL" -c "model_reasoning_effort=$CODEX_EFFORT" -c project_doc_max_bytes=0 - \
    < "$PROMPT_FILE" ) > "$LOG_FILE" 2>&1
EXIT="$?"
set -e

DURATION="$(python3 -c "import time; print(round(time.time() - $START, 2))")"

# Detect rate-limit / failure signatures.
# Rate-limit detection is gated on non-zero exit — agents may legitimately quote
# the phrase "rate limit" in audit findings about other code, so a bare-phrase
# match against successful output is a false positive. Real Codex rate-limits
# exit non-zero with one of the specific error patterns below.
RATE_LIMIT_REGEX='(rate.?limit.?(exceeded|reached)|quota.?exceeded|usage.?limit.?(exceeded|reached)|too.?many.?requests|HTTP/?[12]?\.?[01]?[[:space:]]*429|^[[:space:]]*429[[:space:]]+(too|too-many)|"code"[[:space:]]*:[[:space:]]*"(rate_limit_exceeded|insufficient_quota)")'
if [[ "$EXIT" -eq 124 || "$EXIT" -eq 137 ]]; then
  EXIT_STATUS="timeout"
elif [[ "$EXIT" -ne 0 ]] && grep -qiE "$RATE_LIMIT_REGEX" "$LOG_FILE"; then
  EXIT_STATUS="rate-limit"
elif [[ "$EXIT" -ne 0 ]]; then
  EXIT_STATUS="error"
elif [[ ! -f "$OUTPUT_PATH" ]]; then
  EXIT_STATUS="error"
elif ! python3 "$VALIDATE_FINDINGS_PY" "$OUTPUT_PATH" --persona "$PERSONA" > "$VALIDATION_LOG" 2>&1; then
  EXIT_STATUS="schema-invalid"
else
  EXIT_STATUS="ok"
fi

# Codex exposes one aggregate count, not a defensible input/output split.
# Record total-only; never relabel it as output tokens or fill unknowns with zero.
TOTAL_TOKENS="$(grep -A 1 '^tokens used' "$LOG_FILE" 2>/dev/null | tail -1 | tr -d ' ,' | grep -E '^[0-9]+$' | head -1 || true)"
if [[ -n "$TOTAL_TOKENS" ]]; then
  USAGE_JSON="{\"status\":\"total-only\",\"total_tokens\":${TOTAL_TOKENS}}"
else
  USAGE_JSON='{"status":"unavailable","source":"Codex CLI emitted no parseable aggregate token count"}'
fi

# Snapshot any output, including schema-invalid output, before another attempt
# can replace the canonical persona path.
OUTPUT_RECORD_ARGS=(--output-path "$OUTPUT_PATH")
if [[ -f "$OUTPUT_PATH" ]]; then
  cp "$OUTPUT_PATH" "$OUTPUT_SNAPSHOT"
  OUTPUT_RECORD_ARGS+=(--output-path "$OUTPUT_SNAPSHOT")
fi
# Sidecar is the complete computational record; the DB row stays a compact
# query index. Both are written for successful and failed calls.
CALL_MANIFEST="$(python3 "$SKILL_DIR/scripts/provenance.py" record \
  --call-uid "$CALL_UID" \
  --audit-id "$AUDIT_ID" \
  --round "$ROUND" \
  --round-dir "$CWD/.." \
  --step "codex" \
  --persona "$PERSONA" \
  --origin wrapper \
  --provider openai \
  --model "$CODEX_MODEL" \
  --effort "$CODEX_EFFORT" \
  --invoked-at "$INVOKED_AT" \
  --duration "$DURATION" \
  --exit-code "$EXIT" \
  --exit-status "$EXIT_STATUS" \
  --client-name codex-cli \
  --client-version "$CODEX_VERSION" \
  --prompt-path "$PROMPT_FILE" \
  --preflight-path "$PREFLIGHT_PATH" \
  --response-path "$LOG_FILE" \
  ${OUTPUT_RECORD_ARGS[@]+"${OUTPUT_RECORD_ARGS[@]}"} \
  ${RUNTIME_FILE_ARGS[@]+"${RUNTIME_FILE_ARGS[@]}"} \
  ${CONTEXT_RECORD_ARGS[@]+"${CONTEXT_RECORD_ARGS[@]}"} \
  --runtime-json "$RUNTIME_JSON" \
  --usage-json "$USAGE_JSON" \
  --record-db)"
EXIT_STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_status"])' "$CALL_MANIFEST")"

# Extract verdict + finding count + severity breakdown from the JSON output
# (only valid for EXIT_STATUS=ok; falls back to nulls otherwise).
PROGRESS_VERDICT_ARGS=()
if [[ "$EXIT_STATUS" == "ok" ]] && [[ -f "$OUTPUT_PATH" ]] && command -v jq >/dev/null 2>&1; then
  V=$(jq -r '.verdict.agree // empty' "$OUTPUT_PATH" 2>/dev/null)
  N=$(jq -r '.findings | length' "$OUTPUT_PATH" 2>/dev/null)
  SEV=$(jq -c '[.findings[].severity] | sort | group_by(.) | map({(.[0]): length}) | add // {}' "$OUTPUT_PATH" 2>/dev/null)
  [[ -n "$V" ]] && PROGRESS_VERDICT_ARGS+=(--verdict "$V")
  [[ -n "$N" ]] && PROGRESS_VERDICT_ARGS+=(--finding-count "$N")
  [[ -n "$SEV" && "$SEV" != "null" ]] && PROGRESS_VERDICT_ARGS+=(--severity-breakdown "$SEV")
fi

# bash 3.2 quirk: `"${empty_array[@]}"` under `set -u` prints "unbound
# variable" AND silently exits 0. Use conditional expansion + temporarily
# drop nounset so progress emission can never subvert this wrapper's exit
# code. See invoke-opencode.sh for the longer rationale.
set +u
emit_progress --event persona_walk_done \
  --round "$ROUND" --step codex --persona "$PERSONA" --model "$CODEX_MODEL_STAMP" \
  --duration-s "$DURATION" --exit-status "$EXIT_STATUS" \
  ${PROGRESS_VERDICT_ARGS[@]+"${PROGRESS_VERDICT_ARGS[@]}"}
set -u

# Stream codex output for the orchestrator to see
cat "$LOG_FILE"

if [[ "$EXIT_STATUS" != "ok" ]]; then
  echo "" >&2
  echo "invoke-codex: FAILED (status=$EXIT_STATUS exit=$EXIT)" >&2
  echo "  audit_id=$AUDIT_ID round=$ROUND persona=$PERSONA" >&2
  echo "  output_path=$OUTPUT_PATH" >&2
  echo "  log_path=$LOG_FILE" >&2
  echo "  validation_log=$VALIDATION_LOG" >&2
  echo "  duration=${DURATION}s" >&2
  if [[ "$EXIT_STATUS" == "rate-limit" ]]; then
    echo "  >>> RATE LIMIT detected. Audit should pause (paused-rate-limit)." >&2
    exit 42
  fi
  if [[ "$EXIT_STATUS" == "schema-invalid" ]]; then
    echo "  >>> SCHEMA INVALID. Codex wrote output, but it failed findings.schema.json validation." >&2
    if [[ -s "$VALIDATION_LOG" ]]; then
      sed -n '1,80p' "$VALIDATION_LOG" >&2 || true
    fi
  fi
  exit 1
fi
