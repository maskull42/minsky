#!/usr/bin/env bash
#
# destructive-check.sh — PreToolUse hook for the minsky skill.
#
# Wired in SKILL.md frontmatter:
#   hooks:
#     PreToolUse:
#       - matcher: "Bash"
#         command: ${CLAUDE_SKILL_DIR}/scripts/destructive-check.sh
#
# Receives a JSON event on stdin describing the tool call. For Bash calls, we
# inspect the command and either pass-through (exit 0) or block (exit 2 with
# stderr message). Lifted from GodModeSkill's destructive regex with PhD-
# specific protected paths added.
#
# Loud failure: when blocking, exit code 2 with a clear message. Per the
# no-silent-fallback principle, we never quietly degrade — we either allow
# the command or reject it with a reason.

set -euo pipefail

# Read the hook payload (JSON on stdin)
PAYLOAD="$(cat 2>/dev/null || true)"
# Extract the bash command if present
CMD="$(printf '%s' "$PAYLOAD" | jq -r '.tool_input.command // .input.command // ""' 2>/dev/null || true)"
if [[ -z "$CMD" ]]; then
  # Not a Bash event with a parseable command — allow
  exit 0
fi

# Generic destructive patterns (use grep -qE for portable ERE matching).
# Patterns lifted from GodModeSkill's `work` script line ~490, expanded.
DESTRUCTIVE_PATTERNS=(
  '(^|[^a-zA-Z0-9_-])rm[[:space:]]+-[rR]?[fF]([[:space:]]|$)'
  '(^|[^a-zA-Z0-9_-])rm[[:space:]]+-[fF][rR]([[:space:]]|$)'
  '(^|[^a-zA-Z0-9_-])sudo([[:space:]]|$)'
  'git[[:space:]]+push[[:space:]]+.*--force'
  'git[[:space:]]+push[[:space:]]+-f([[:space:]]|$)'
  'git[[:space:]]+reset[[:space:]]+--hard'
  'git[[:space:]]+checkout[[:space:]]+--[[:space:]]'
  'git[[:space:]]+clean[[:space:]]+-[fdx]+'
  '\bDROP[[:space:]]+TABLE'
  '\bDROP[[:space:]]+DATABASE'
  '\bTRUNCATE([[:space:]]|$)'
  'dd[[:space:]]+if=.*[[:space:]]+of='
  'mkfs(\.|[[:space:]])'
  '>[[:space:]]*/dev/sd[a-z]'
  'chmod[[:space:]]+-R[[:space:]]+777'
  'curl.*\|[[:space:]]*sh([[:space:]]|$)'
  'wget.*\|[[:space:]]*sh([[:space:]]|$)'
)

PROTECTED_PATHS=(
  'data/web_app\.db'
  'web/data/mars\.db'
  '(^|[/[:space:]])mars\.db([[:space:]]|$)'
  'data/pipeline_state/state\.db'
  'qdrant_storage/'
  'documentation/phd_work_log\.md'
  'phd_project_context/'
  '\.minsky/audits\.db'
  'patristic_sources/'
)

# Check generic destructive patterns via grep -E
for pat in "${DESTRUCTIVE_PATTERNS[@]}"; do
  if printf '%s' "$CMD" | grep -qE "$pat"; then
    echo "destructive-check: BLOCKED — destructive pattern matched:" >&2
    echo "  pattern: $pat" >&2
    echo "  command: $CMD" >&2
    echo "If you really need to run this, do it outside the minsky skill." >&2
    exit 2
  fi
done

# Detect write-style operations
is_write_op=0
case " $CMD " in
  *" > "*|*">"*|*" >> "*|*">>"*) is_write_op=1 ;;
esac
if printf '%s' "$CMD" | grep -qE '(^|[[:space:]])(rm|mv|tee|truncate|sed[[:space:]]+-i)([[:space:]]|$)'; then
  is_write_op=1
fi
if printf '%s' "$CMD" | grep -qiE '\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b'; then
  is_write_op=1
fi

if [[ "$is_write_op" -eq 1 ]]; then
  for path_re in "${PROTECTED_PATHS[@]}"; do
    if printf '%s' "$CMD" | grep -qE "$path_re"; then
      echo "destructive-check: BLOCKED — write-style operation against PhD-protected path:" >&2
      echo "  pattern: $path_re" >&2
      echo "  command: $CMD" >&2
      echo "Reads of these paths are fine; writes from inside the skill are blocked." >&2
      exit 2
    fi
  done
fi

# Default: allow
exit 0
