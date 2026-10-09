#!/usr/bin/env bash
#
# invoke-opencode.sh — wrap one OpenCode (currently Gemini 3.8 Flash at high
# thinking, minsky-reviewer agent) per-(persona) call for the minsky chain.
#
# Receives a prompt on stdin, a target cwd, and an audit-id + round + persona
# for provenance. Writes OpenCode's output (the JSON findings file) inside cwd
# at the path the prompt names. Returns non-zero on any failure; never silently
# retries or falls back.
#
# By default imports the configured provider credential from <repo-root>/.env;
# OpenCode resolves `{env:...}` from process env, not from the .env file directly.
# MINSKY_OPENCODE_CREDENTIALS=opencode explicitly uses OpenCode's own credentials.
#
# Usage:
#   invoke-opencode.sh \
#       --cwd <abs-path-to-output-dir> \
#       --audit-id <id> \
#       --round <n> \
#       --persona <name> \
#       < prompt.txt

set -euo pipefail

: "${MINSKY_OPENCODE_CREDENTIALS=repo-dotenv}"
case "$MINSKY_OPENCODE_CREDENTIALS" in
  repo-dotenv|opencode) ;;
  # Never echo the value: a mis-set variable could hold a credential.
  *) echo "invoke-opencode: invalid MINSKY_OPENCODE_CREDENTIALS (expected repo-dotenv or opencode)" >&2; exit 64 ;;
esac
readonly MINSKY_OPENCODE_CREDENTIALS

# Wall-clock timeout for the underlying `opencode run` invocation. Bounded so
# silent provider stalls fail-loud rather than wedge the entire chain.
# Override per invocation by exporting OPENCODE_TIMEOUT_SECONDS before
# calling. Two-stage termination: SIGTERM first; SIGKILL after
# `OPENCODE_KILL_GRACE_SECONDS` if still alive.
#
# Tuning history:
#   v1.1 (2026-04-29): 1800s (30 min) — initial conservative bound from V11.
#   v1.1.3 (2026-05-04): 1020s (17 min) — historical fail-fast bound.
#   v1.2 (2026-09-05): 1800s (30 min) — explicit round-2 external-leg bound,
#     registered in the pre-dispatch receipt alongside the kill grace.
: "${OPENCODE_TIMEOUT_SECONDS:=1800}"      # 30 min
: "${OPENCODE_KILL_GRACE_SECONDS:=60}"     # +60s before SIGKILL
# Adversarial OpenCode leg — model history: deepseek/deepseek-v4-pro -> minimax-coding-plan/MiniMax-M3
# (2026-07-01, Token-Plan subscription) -> deepseek/deepseek-v4-flash (2026-08-05, subscription ended)
# -> google/gemini-3.7-flash @ HIGH thinking (2026-08-18, DeepSeek API credit exhausted)
# -> ★ google/gemini-3.8-flash @ HIGH thinking (2026-09-04, user-directed Minsky default change).
# The HIGH thinking level is NOT set by --variant (opencode's config schema exposes only {disabled} per
# variant for a config-defined model); it comes from the repo opencode.json:
#   provider.google.models."gemini-3.8-flash".options.thinkingConfig.thinkingLevel = "high"
# Measured through this exact invocation path: high 1,033 vs low 428 reasoning tokens (2.4x).
# OPENCODE_VARIANT is therefore PROVENANCE ONLY here — it is stamped into MODEL_NAME/audit records.
# Override both env vars to swap models again.
: "${OPENCODE_MODEL:=google/gemini-3.8-flash}"
: "${OPENCODE_VARIANT:=high}"
: "${OPENCODE_FORMAT:=json}"

CWD=""; AUDIT_ID=""; ROUND=""; PERSONA=""; INPUT_FILES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cwd) CWD="$2"; shift 2 ;;
    --audit-id) AUDIT_ID="$2"; shift 2 ;;
    --round) ROUND="$2"; shift 2 ;;
    --persona) PERSONA="$2"; shift 2 ;;
    --input-file) INPUT_FILES+=("$2"); shift 2 ;;
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
REPO_ROOT="$(cd "$SKILL_DIR/../../.." && pwd)"
PROGRESS_PY="$SKILL_DIR/scripts/progress.py"
VALIDATE_FINDINGS_PY="$SKILL_DIR/scripts/validate-findings.py"
PROVIDER_NAME="${OPENCODE_MODEL%%/*}"
MODEL_NAME="${OPENCODE_MODEL##*/}"
MODEL_STAMP="${MODEL_NAME}@${OPENCODE_VARIANT}"

# Best-effort progress emitter; never fail the walk on a progress error.
emit_progress() {
  if [[ -f "$PROGRESS_PY" ]]; then
    python3 "$PROGRESS_PY" emit --audit-id "$AUDIT_ID" "$@" 2>/dev/null || true
  fi
}

# Source repo-root .env so the provider key enters the process environment (OpenCode resolves
# {env:...} from process env, not from the .env file).
ENV_FILE="$REPO_ROOT/.env"
if [[ "$MINSKY_OPENCODE_CREDENTIALS" == "repo-dotenv" ]]; then
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "invoke-opencode: required env file not found at $ENV_FILE" >&2
    exit 1
  fi
  case "$PROVIDER_NAME" in
    google) export GOOGLE_GENERATIVE_AI_API_KEY="$(python3 "$SKILL_DIR/scripts/credential-value.py" --env-file "$ENV_FILE" --credential GOOGLE_GENERATIVE_AI_API_KEY)" ;;
    deepseek) export deepseek_api="$(python3 "$SKILL_DIR/scripts/credential-value.py" --env-file "$ENV_FILE" --credential deepseek_api)" ;;
    *) echo "invoke-opencode: credential importer does not support this provider" >&2; exit 1 ;;
  esac
fi
# Freeze nonsecret settings after the credential-only import; dotenv cannot change dispatch.
readonly OPENCODE_MODEL OPENCODE_VARIANT OPENCODE_FORMAT OPENCODE_TIMEOUT_SECONDS OPENCODE_KILL_GRACE_SECONDS
readonly PROVIDER_NAME MODEL_NAME MODEL_STAMP
# ★ 2026-08-18: opencode's built-in `google` provider reads GOOGLE_GENERATIVE_AI_API_KEY and ONLY that
# name — with just GOOGLE_API_KEY (what .env carries) it fails ProviderAuthError with rc=1 and an EMPTY
# stderr, which is indistinguishable from a hung provider. Synthesise it, exactly as
# scripts/r3_l3_challenger_runner.py::_dotenv() and the L3 supervisor's adv_clean() do.
if [[ "$MINSKY_OPENCODE_CREDENTIALS" == "repo-dotenv" ]]; then
  if [[ -z "${GOOGLE_GENERATIVE_AI_API_KEY:-}" && -n "${GOOGLE_API_KEY:-}" ]]; then
    export GOOGLE_GENERATIVE_AI_API_KEY="$GOOGLE_API_KEY"
  fi
  # Fail loud if the key the CONFIGURED provider needs is absent (checked per provider prefix, so a
  # future swap back to deepseek/minimax still validates the right variable).
  case "$OPENCODE_MODEL" in
    google/*)   [[ -n "${GOOGLE_GENERATIVE_AI_API_KEY:-}" ]] || { echo "invoke-opencode: GOOGLE_GENERATIVE_AI_API_KEY (or GOOGLE_API_KEY) not set in $ENV_FILE" >&2; exit 1; } ;;
    deepseek/*) [[ -n "${deepseek_api:-}" ]] || { echo "invoke-opencode: deepseek_api not set in $ENV_FILE" >&2; exit 1; } ;;
  esac
fi

OUTPUT_PATH="$CWD/${PERSONA}.json"
rm -f "$OUTPUT_PATH"

# Persist exact attempt inputs and outputs before invoking the provider. Unique
# call IDs make retries append-only instead of silently replacing evidence.
CALL_UID="$(python3 "$SKILL_DIR/scripts/provenance.py" new-uid)"
LOG_DIR="$CWD/_opencode_logs"
mkdir -p "$LOG_DIR"
PROMPT_FILE="$LOG_DIR/${PERSONA}.round-${ROUND}.${CALL_UID}.prompt.txt"
LOG_FILE="$LOG_DIR/${PERSONA}.round-${ROUND}.${CALL_UID}.events.ndjson"
STDERR_FILE="$LOG_DIR/${PERSONA}.round-${ROUND}.${CALL_UID}.stderr.log"
VALIDATION_LOG="$LOG_DIR/${PERSONA}.round-${ROUND}.${CALL_UID}.validation.log"
OUTPUT_SNAPSHOT="$LOG_DIR/${PERSONA}.round-${ROUND}.${CALL_UID}.output.json"
cat > "$PROMPT_FILE"

OPENCODE_PATH="$(command -v opencode || true)"
if [[ -z "$OPENCODE_PATH" ]]; then
  echo "invoke-opencode: opencode executable not found" >&2
  exit 1
fi
OPENCODE_VERSION="$(opencode --version 2>&1 | head -1)"
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_BIN="$(command -v timeout)"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_BIN="$(command -v gtimeout)"
else
  echo "invoke-opencode: required GNU timeout command not found (tried timeout, gtimeout)" >&2
  exit 1
fi
RUNTIME_JSON="$(python3 - "$OPENCODE_PATH" "$OPENCODE_MODEL" "$OPENCODE_VARIANT" \
  "$OPENCODE_FORMAT" "$TIMEOUT_BIN" "$OPENCODE_TIMEOUT_SECONDS" \
  "$OPENCODE_KILL_GRACE_SECONDS" "$AUDIT_ID" "$ROUND" "$PERSONA" "$MINSKY_OPENCODE_CREDENTIALS" <<'PY'
import json, sys
print(json.dumps({
    "executable": sys.argv[1],
    "argv": [sys.argv[5], f"--kill-after={sys.argv[7]}s", f"{sys.argv[6]}s",
             sys.argv[1], "run", "--agent", "minsky-reviewer",
             "--dangerously-skip-permissions", "--model", sys.argv[2],
             "--format", sys.argv[4], "--title",
             f"minsky {sys.argv[8]} r{sys.argv[9]} {sys.argv[10]}",
             "--variant", sys.argv[3], "<exact-message-from-prompt-file>"],
    "message_source": "durable prompt file",
    "timeout": {"kind": "gnu-timeout", "executable": sys.argv[5],
                "wall_clock_seconds": int(sys.argv[6]),
                "kill_grace_seconds": int(sys.argv[7])},
    "observable_subset": "explicit argv, message prompt, declared context, executable/config/agent/wrapper hashes",
    "not_recorded": ["credential values", "hidden provider state", "unreferenced global client configuration"],
    "credential_source": sys.argv[11],
}, separators=(",", ":")))
PY
)"

RUNTIME_FILE_ARGS=(--runtime-file "$0" --runtime-file "$OPENCODE_PATH" --runtime-file "$TIMEOUT_BIN")
RUNTIME_FILE_ARGS+=(--runtime-file "$(command -v python3)" --runtime-file "$SKILL_DIR/scripts/provenance.py" --runtime-file "$VALIDATE_FINDINGS_PY" --runtime-file "$SKILL_DIR/scripts/credential-value.py")
[[ -f "$REPO_ROOT/opencode.json" ]] && RUNTIME_FILE_ARGS+=(--runtime-file "$REPO_ROOT/opencode.json")
[[ -f "$REPO_ROOT/.opencode/agents/minsky-reviewer.md" ]] && \
  RUNTIME_FILE_ARGS+=(--runtime-file "$REPO_ROOT/.opencode/agents/minsky-reviewer.md")
CONTEXT_RECORD_ARGS=()
set +u
for context_file in "${INPUT_FILES[@]}"; do
  CONTEXT_RECORD_ARGS+=(--context-file "$context_file")
done
set -u

PREFLIGHT_PATH="$(python3 "$SKILL_DIR/scripts/provenance.py" prepare \
  --call-uid "$CALL_UID" \
  --audit-id "$AUDIT_ID" \
  --round "$ROUND" \
  --round-dir "$CWD/.." \
  --step opencode \
  --persona "$PERSONA" \
  --origin wrapper \
  --provider "$PROVIDER_NAME" \
  --model "$MODEL_NAME" \
  --effort "$OPENCODE_VARIANT" \
  --client-name opencode-cli \
  --client-version "$OPENCODE_VERSION" \
  --prompt-path "$PROMPT_FILE" \
  --expected-output-path "$OUTPUT_PATH" \
  ${RUNTIME_FILE_ARGS[@]+"${RUNTIME_FILE_ARGS[@]}"} \
  ${CONTEXT_RECORD_ARGS[@]+"${CONTEXT_RECORD_ARGS[@]}"} \
  --runtime-json "$RUNTIME_JSON")"

INVOKED_AT="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.UTC).isoformat(timespec="microseconds").replace("+00:00","Z"))')"
START="$(python3 -c 'import time; print(time.time())')"

emit_progress --event persona_walk_start \
  --round "$ROUND" --step opencode --persona "$PERSONA" --model "$MODEL_STAMP"

OPENCODE_CMD=(
  "$TIMEOUT_BIN" "--kill-after=${OPENCODE_KILL_GRACE_SECONDS}s" "${OPENCODE_TIMEOUT_SECONDS}s"
  "$OPENCODE_PATH" run
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
( cd "$CWD" && "${OPENCODE_CMD[@]}" ) > "$LOG_FILE" 2> "$STDERR_FILE"
EXIT="$?"
set -e

DURATION="$(python3 -c "import time; print(round(time.time() - $START, 2))")"

# Detect timeout / rate-limit / generic failure signatures.
#
# `timeout` semantics: exit 124 = SIGTERM fired at OPENCODE_TIMEOUT_SECONDS;
# exit 137 = SIGKILL fired after the grace period. Either case is a
# Provider-no-response hang, fail-loud (do NOT silently retry).
#
# Rate-limit detection is gated on non-zero exit — agents may legitimately
# quote the phrase "rate limit" in audit findings about other code, so a
# bare-phrase match against successful output is a false positive. Real
# OpenCode/provider rate-limits exit non-zero with one of the specific
# error patterns below.
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

# Parse every OpenCode step-finish record strictly. A successful reviewer walk
# with incomplete billing evidence is not silently accepted as zero usage.
USAGE_JSON="$(python3 "$SKILL_DIR/scripts/provenance.py" parse-opencode-usage --log "$LOG_FILE")"
USAGE_STATUS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["status"])' "$USAGE_JSON")"
if [[ "$EXIT_STATUS" == "ok" ]] && [[ "$USAGE_STATUS" != "complete" ]]; then
  EXIT_STATUS="usage-invalid"
fi

OUTPUT_RECORD_ARGS=(--output-path "$OUTPUT_PATH" --output-path "$STDERR_FILE")
if [[ -f "$OUTPUT_PATH" ]]; then
  cp "$OUTPUT_PATH" "$OUTPUT_SNAPSHOT"
  OUTPUT_RECORD_ARGS+=(--output-path "$OUTPUT_SNAPSHOT")
fi

CALL_MANIFEST="$(python3 "$SKILL_DIR/scripts/provenance.py" record \
  --call-uid "$CALL_UID" \
  --audit-id "$AUDIT_ID" \
  --round "$ROUND" \
  --round-dir "$CWD/.." \
  --step "opencode" \
  --persona "$PERSONA" \
  --origin wrapper \
  --provider "$PROVIDER_NAME" \
  --model "$MODEL_NAME" \
  --effort "$OPENCODE_VARIANT" \
  --invoked-at "$INVOKED_AT" \
  --duration "$DURATION" \
  --exit-code "$EXIT" \
  --exit-status "$EXIT_STATUS" \
  --client-name opencode-cli \
  --client-version "$OPENCODE_VERSION" \
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
  --round "$ROUND" --step opencode --persona "$PERSONA" --model "$MODEL_STAMP" \
  --duration-s "$DURATION" --exit-status "$EXIT_STATUS" \
  ${PROGRESS_VERDICT_ARGS[@]+"${PROGRESS_VERDICT_ARGS[@]}"}
set -u

cat "$LOG_FILE"

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
    echo "  >>> Likely provider-no-response hang (V11 pattern). Fail-loud per design." >&2
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
  if [[ "$EXIT_STATUS" == "usage-invalid" ]]; then
    echo "  >>> USAGE INVALID. Output passed schema validation, but exact cost/token provenance is incomplete." >&2
    echo "  >>> usage=$USAGE_JSON" >&2
    exit 1
  fi
  if grep -qiE '(permission requested|rejected permission|permission denied|auto-rejecting)' "$LOG_FILE"; then
    echo "  >>> Permission failure detected in OpenCode log. Check minsky-reviewer permissions and log_path." >&2
  fi
  if [[ -s "$LOG_FILE" ]]; then
    echo "  >>> Last OpenCode log lines:" >&2
    tail -80 "$LOG_FILE" >&2 || true
  fi
  exit 1
fi
