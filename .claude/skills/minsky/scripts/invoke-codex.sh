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

: "${CODEX_MODEL_LABEL:=codex}"

# parse args
CWD=""; AUDIT_ID=""; ROUND=""; PERSONA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cwd) CWD="$2"; shift 2 ;;
    --audit-id) AUDIT_ID="$2"; shift 2 ;;
    --round) ROUND="$2"; shift 2 ;;
    --persona) PERSONA="$2"; shift 2 ;;
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
PROGRESS_PY="$SKILL_DIR/scripts/progress.py"
VALIDATE_FINDINGS_PY="$SKILL_DIR/scripts/validate-findings.py"

# Best-effort progress emitter; never fail the walk on a progress error.
emit_progress() {
  if [[ -x "$PROGRESS_PY" ]] || [[ -f "$PROGRESS_PY" ]]; then
    python3 "$PROGRESS_PY" emit --audit-id "$AUDIT_ID" "$@" 2>/dev/null || true
  fi
}

INVOKED_AT="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z"))')"
START="$(python3 -c 'import time; print(time.time())')"

OUTPUT_PATH="$CWD/${PERSONA}.json"
rm -f "$OUTPUT_PATH"

emit_progress --event persona_walk_start \
  --round "$ROUND" --step codex --persona "$PERSONA" --model "$CODEX_MODEL_LABEL"

# Run codex; capture both streams
TMPLOG="$(mktemp)"
LOG_DIR="$CWD/_codex_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${PERSONA}.round-${ROUND}.log"
VALIDATION_LOG="$LOG_DIR/${PERSONA}.round-${ROUND}.validation.log"
trap 'rm -f "$TMPLOG"' EXIT

set +e
# Use the explicit Codex bypass flag rather than its --yolo alias so logs state
# exactly what is being bypassed: approvals and sandboxing.
( cd "$CWD" && codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox - ) > "$TMPLOG" 2>&1
EXIT="$?"
set -e
cp "$TMPLOG" "$LOG_FILE"

DURATION="$(python3 -c "import time; print(round(time.time() - $START, 2))")"

# Detect rate-limit / failure signatures.
# Rate-limit detection is gated on non-zero exit — agents may legitimately quote
# the phrase "rate limit" in audit findings about other code, so a bare-phrase
# match against successful output is a false positive. Real Codex rate-limits
# exit non-zero with one of the specific error patterns below.
RATE_LIMIT_REGEX='(rate.?limit.?(exceeded|reached)|quota.?exceeded|usage.?limit.?(exceeded|reached)|too.?many.?requests|HTTP/?[12]?\.?[01]?[[:space:]]*429|^[[:space:]]*429[[:space:]]+(too|too-many)|"code"[[:space:]]*:[[:space:]]*"(rate_limit_exceeded|insufficient_quota)")'
if [[ "$EXIT" -ne 0 ]] && grep -qiE "$RATE_LIMIT_REGEX" "$TMPLOG"; then
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

# Extract Codex's total token count from stderr/stdout.
# Codex CLI emits a single "tokens used\n<NUMBER>" block (total, not split by input/output).
# We record the total in output_tokens (Codex doesn't expose input/output split).
TOTAL_TOKENS="$(grep -A 1 '^tokens used' "$TMPLOG" 2>/dev/null | tail -1 | tr -d ' ,' | grep -E '^[0-9]+$' | head -1)"
TOKEN_ARGS=""
if [[ -n "$TOTAL_TOKENS" ]]; then
  TOKEN_ARGS="--output-tokens $TOTAL_TOKENS"
fi

# Record provenance unconditionally (success or failure)
python3 "$SKILL_DIR/scripts/audit-db.py" insert-provenance \
  --audit-id "$AUDIT_ID" \
  --round "$ROUND" \
  --step "codex" \
  --model "$CODEX_MODEL_LABEL" \
  --persona "$PERSONA" \
  --invoked-at "$INVOKED_AT" \
  --duration "$DURATION" \
  --output-path "$OUTPUT_PATH" \
  --exit-status "$EXIT_STATUS" \
  $TOKEN_ARGS >/dev/null

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
  --round "$ROUND" --step codex --persona "$PERSONA" --model "$CODEX_MODEL_LABEL" \
  --duration-s "$DURATION" --exit-status "$EXIT_STATUS" \
  ${PROGRESS_VERDICT_ARGS[@]+"${PROGRESS_VERDICT_ARGS[@]}"}
set -u

# Stream codex output for the orchestrator to see
cat "$TMPLOG"

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
