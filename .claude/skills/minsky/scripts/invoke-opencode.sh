#!/usr/bin/env bash
#
# invoke-opencode.sh — wrap one OpenCode minsky-reviewer agent call
# per persona for the minsky chain.
#
# Receives a prompt on stdin, a target cwd, and an audit-id + round + persona
# for provenance. Writes OpenCode's output (the JSON findings file) inside cwd
# at the path the prompt names. Returns non-zero on any failure; never silently
# retries or falls back.
#
# Usage:
#   invoke-opencode.sh \
#       --cwd <abs-path-to-output-dir> \
#       --audit-id <id> \
#       --round <n> \
#       --persona <name> \
#       < prompt.txt

set -euo pipefail

# Wall-clock timeout for the underlying `opencode run` invocation. Set high
# enough to accommodate a thorough OpenCode/provider call for a large pack
# (~200 KB) but bounded so provider no-response hangs fail loud rather than
# wedge the entire chain. Override per invocation by
# exporting OPENCODE_TIMEOUT_SECONDS before calling. Two-stage termination:
# SIGTERM first; SIGKILL after `OPENCODE_KILL_GRACE_SECONDS` if still alive.
: "${OPENCODE_TIMEOUT_SECONDS:=1800}"      # 30 min
: "${OPENCODE_KILL_GRACE_SECONDS:=60}"     # +60s before SIGKILL
: "${OPENCODE_MODEL:?Set OPENCODE_MODEL to your configured OpenCode model, e.g. provider/model}"
: "${OPENCODE_FORMAT:=json}"

CWD=""; AUDIT_ID=""; ROUND=""; PERSONA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cwd) CWD="$2"; shift 2 ;;
    --audit-id) AUDIT_ID="$2"; shift 2 ;;
    --round) ROUND="$2"; shift 2 ;;
    --persona) PERSONA="$2"; shift 2 ;;
    *) echo "invoke-opencode: unknown arg: $1" >&2; exit 64 ;;
  esac
done
for v in CWD AUDIT_ID ROUND PERSONA; do
  if [[ -z "${!v}" ]]; then
    flag="$(printf '%s' "$v" | tr '[:upper:]_' '[:lower:]-')"
    echo "invoke-opencode: missing required arg --${flag}" >&2
    exit 64
  fi
done

mkdir -p "$CWD"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROGRESS_PY="$SKILL_DIR/scripts/progress.py"
VALIDATE_FINDINGS_PY="$SKILL_DIR/scripts/validate-findings.py"
MODEL_NAME="${OPENCODE_MODEL##*/}"

# Best-effort progress emitter; never fail the walk on a progress error.
emit_progress() {
  if [[ -f "$PROGRESS_PY" ]]; then
    python3 "$PROGRESS_PY" emit --audit-id "$AUDIT_ID" "$@" 2>/dev/null || true
  fi
}

# Provider credentials are expected to be available to OpenCode through the
# user's normal OpenCode configuration or process environment. This wrapper
# deliberately leaves dot-env files to OpenCode or the parent process.

INVOKED_AT="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z"))')"
START="$(python3 -c 'import time; print(time.time())')"

OUTPUT_PATH="$CWD/${PERSONA}.json"
rm -f "$OUTPUT_PATH"

# Read prompt from stdin into a temp file (opencode run takes the message as an argument,
# not from stdin in current versions — confirm with V1)
PROMPT_FILE="$(mktemp)"
TMPLOG=""
trap 'rm -f "${PROMPT_FILE:-}" "${TMPLOG:-}"' EXIT
cat > "$PROMPT_FILE"

TMPLOG="$(mktemp)"
LOG_DIR="$CWD/_opencode_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${PERSONA}.round-${ROUND}.events.ndjson"
VALIDATION_LOG="$LOG_DIR/${PERSONA}.round-${ROUND}.validation.log"

if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_BIN="$(command -v timeout)"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_BIN="$(command -v gtimeout)"
else
  echo "invoke-opencode: required GNU timeout command not found (tried timeout, gtimeout)" >&2
  exit 1
fi

emit_progress --event persona_walk_start \
  --round "$ROUND" --step opencode --persona "$PERSONA" --model "$MODEL_NAME"

OPENCODE_CMD=(
  "$TIMEOUT_BIN" "--kill-after=${OPENCODE_KILL_GRACE_SECONDS}s" "${OPENCODE_TIMEOUT_SECONDS}s"
  opencode run
  --agent minsky-reviewer
  --dangerously-skip-permissions
  --model "$OPENCODE_MODEL"
  --format "$OPENCODE_FORMAT"
  --title "minsky ${AUDIT_ID} r${ROUND} ${PERSONA}"
)
if [[ -n "${OPENCODE_VARIANT:-}" ]]; then
  OPENCODE_CMD+=(--variant "$OPENCODE_VARIANT")
fi
OPENCODE_CMD+=("$(cat "$PROMPT_FILE")")

set +e
( cd "$CWD" && "${OPENCODE_CMD[@]}" ) > "$TMPLOG" 2>&1
EXIT="$?"
set -e

cp "$TMPLOG" "$LOG_FILE"

DURATION="$(python3 -c "import time; print(round(time.time() - $START, 2))")"

# Detect timeout / rate-limit / generic failure signatures.
#
# `timeout` semantics: exit 124 = SIGTERM fired at OPENCODE_TIMEOUT_SECONDS;
# exit 137 = SIGKILL fired after the grace period. Either case is a
# provider no-response hang, fail-loud (do NOT silently retry).
#
# Rate-limit detection is gated on non-zero exit — agents may legitimately
# quote the phrase "rate limit" in audit findings about other code, so a
# bare-phrase match against successful output is a false positive. Real
# OpenCode/provider rate-limits exit non-zero with one of the specific
# error patterns below.
RATE_LIMIT_REGEX='(rate.?limit.?(exceeded|reached)|quota.?exceeded|usage.?limit.?(exceeded|reached)|too.?many.?requests|HTTP/?[12]?\.?[01]?[[:space:]]*429|^[[:space:]]*429[[:space:]]+(too|too-many)|"code"[[:space:]]*:[[:space:]]*"(rate_limit_exceeded|insufficient_quota)")'
if [[ "$EXIT" -eq 124 || "$EXIT" -eq 137 ]]; then
  EXIT_STATUS="timeout"
elif [[ "$EXIT" -ne 0 ]] && grep -qiE "$RATE_LIMIT_REGEX" "$TMPLOG"; then
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

# Extract token counts from `opencode run --format json` step-finish events.
# OpenCode reports per-API-call usage in each step-finish record. Sum them so
# provenance captures the total paid input/output token load for this walk.
TOKEN_ARGS=()
TOKEN_COUNTS="$(
  python3 - "$TMPLOG" <<'PY'
import json
import sys

input_total = 0
output_total = 0
seen = False
with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
    for line in fh:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = rec.get("part") or {}
        tokens = part.get("tokens") or rec.get("tokens") or {}
        if not isinstance(tokens, dict):
            continue
        if "input" in tokens or "output" in tokens:
            seen = True
            input_total += int(tokens.get("input") or 0)
            output_total += int(tokens.get("output") or 0)
if seen:
    print(input_total, output_total)
PY
)"
if [[ -n "$TOKEN_COUNTS" ]]; then
  read -r INPUT_TOKENS OUTPUT_TOKENS <<< "$TOKEN_COUNTS"
  if [[ "$INPUT_TOKENS" =~ ^[0-9]+$ && "$OUTPUT_TOKENS" =~ ^[0-9]+$ ]]; then
    TOKEN_ARGS+=(--input-tokens "$INPUT_TOKENS" --output-tokens "$OUTPUT_TOKENS")
  fi
fi

python3 "$SKILL_DIR/scripts/audit-db.py" insert-provenance \
  --audit-id "$AUDIT_ID" \
  --round "$ROUND" \
  --step "opencode" \
  --model "$MODEL_NAME" \
  --persona "$PERSONA" \
  --invoked-at "$INVOKED_AT" \
  --duration "$DURATION" \
  --output-path "$OUTPUT_PATH" \
  --exit-status "$EXIT_STATUS" \
  ${TOKEN_ARGS[@]+"${TOKEN_ARGS[@]}"} >/dev/null

# Extract verdict + finding count + severity breakdown from the JSON output.
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
# variable" AND silently exits 0 (verified 2026-05-03). That would subvert
# the explicit `exit 1` fail-loud below. Two defenses:
#   1. Use conditional expansion `${arr[@]+"${arr[@]}"}` so an empty array
#      expands to nothing without triggering set-u.
#   2. Disable nounset for the emit call as belt-and-suspenders, so a future
#      progress.py change cannot subvert this wrapper's exit code.
set +u
emit_progress --event persona_walk_done \
  --round "$ROUND" --step opencode --persona "$PERSONA" --model "$MODEL_NAME" \
  --duration-s "$DURATION" --exit-status "$EXIT_STATUS" \
  ${PROGRESS_VERDICT_ARGS[@]+"${PROGRESS_VERDICT_ARGS[@]}"}
set -u

cat "$TMPLOG"

if [[ "$EXIT_STATUS" != "ok" ]]; then
  echo "" >&2
  echo "invoke-opencode: FAILED (status=$EXIT_STATUS exit=$EXIT)" >&2
  echo "  audit_id=$AUDIT_ID round=$ROUND persona=$PERSONA" >&2
  echo "  output_path=$OUTPUT_PATH" >&2
  echo "  log_path=$LOG_FILE" >&2
  echo "  validation_log=$VALIDATION_LOG" >&2
  echo "  duration=${DURATION}s" >&2
  if [[ "$EXIT_STATUS" == "timeout" ]]; then
    echo "  >>> TIMEOUT after ${OPENCODE_TIMEOUT_SECONDS}s (grace +${OPENCODE_KILL_GRACE_SECONDS}s)." >&2
    echo "  >>> Likely provider no-response hang. Fail-loud per design." >&2
    echo "  >>> To raise the limit for an explicitly slow run, export OPENCODE_TIMEOUT_SECONDS=N before invocation." >&2
    exit 1
  fi
  if [[ "$EXIT_STATUS" == "rate-limit" ]]; then
    echo "  >>> RATE LIMIT detected. Audit should pause (paused-rate-limit)." >&2
    exit 42
  fi
  if [[ "$EXIT_STATUS" == "schema-invalid" ]]; then
    echo "  >>> SCHEMA INVALID. OpenCode wrote output, but it failed findings.schema.json validation." >&2
    if [[ -s "$VALIDATION_LOG" ]]; then
      sed -n '1,80p' "$VALIDATION_LOG" >&2 || true
    fi
    exit 1
  fi
  if grep -qiE '(permission requested|rejected permission|permission denied|auto-rejecting)' "$TMPLOG"; then
    echo "  >>> Permission failure detected in OpenCode log. Check minsky-reviewer permissions and log_path." >&2
  fi
  if [[ -s "$TMPLOG" ]]; then
    echo "  >>> Last OpenCode log lines:" >&2
    tail -80 "$TMPLOG" >&2 || true
  fi
  exit 1
fi
