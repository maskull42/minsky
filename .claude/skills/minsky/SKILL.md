---
name: minsky
description: |
  Sequential, multi-CLI adversarial-audit harness for MARS PhD work products.
  Triangulates Claude (Opus 4.7), Codex (GPT-5.5), and OpenCode (DeepSeek V4 Pro)
  in a 4-step deliberation chain (Claude self-report → Codex per-persona →
  OpenCode per-persona meta-adversarial → Claude synthesis), with stable expert
  personas (marcion-heresiologist, ml-finetuning-phd) carrying memory across rounds.
  Modes: audit | plan | draft | eval | bug-hunt. All audits persist to a queryable
  sqlite DB at .minsky/audits.db with linkage to phd_work_log.md and git commits.
  Use ultrathink throughout; this is high-stakes work for the dissertation.
arguments: mode rest
argument-hint: <audit|plan|draft|eval|bug-hunt> [task-id | paths... | --since <spec>]
disable-model-invocation: true
allowed-tools: |
  Read Edit Write Grep Glob Bash
  Bash(codex exec *) Bash(opencode run *)
  Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*) Bash(bash ${CLAUDE_SKILL_DIR}/scripts/*)
  Bash(python ${CLAUDE_SKILL_DIR}/scripts/*)
  Bash(sqlite3 *) Bash(jq *)
  Bash(git log *) Bash(git diff *) Bash(git status *) Bash(git rev-parse *) Bash(git show *)
  Bash(mkdir -p codex-audits/*) Bash(mkdir -p .minsky/*)
---

# /minsky — stateful multi-CLI adversarial-audit harness

This skill orchestrates a sequential deliberation chain across three coding agents
(Claude Code, Codex, OpenCode) with stable expert personas, persistent audit history
in a queryable sqlite DB, and per-persona memory across rounds. The architecture is
documented in `${CLAUDE_SKILL_DIR}/README.md`. Per the rigor norms in MARS CLAUDE.md
and your global policy, **errors loud, never silent fallback**.

This is high-stakes work. Use **ultrathink** throughout. Especially in Step 4 synthesis
(the most consequential reasoning step — deciding what actually changes based on three
agents' adversarial findings), apply maximum thinking depth.

**Permission mode requirement.** For unattended Minsky runs, launch the host Claude Code
session with `claude --dangerously-skip-permissions ...` or
`claude --permission-mode bypassPermissions ...`. The Minsky wrappers launch Codex with
`--dangerously-bypass-approvals-and-sandbox` (`--yolo` alias) and OpenCode with
`--dangerously-skip-permissions`; the OpenCode agent still keeps explicit deny rules for
secrets and destructive commands.

---

## Args

- `$0` (required): mode — one of `audit`, `plan`, `draft`, `eval`, `bug-hunt`
- `$1+` (optional): scope args, interpreted by `scope-detect.py`:
  - explicit task-id: `/minsky audit my-named-task` (slug, no slashes)
  - delta (no args): `/minsky audit` — scope = changes since last audit on this branch
  - paths: `/minsky audit path/to/file.md src/pipeline/`
  - time: `/minsky audit --since "2 hours ago"`

---

## Protocol — execute in order, each step blocks the next on failure

### Phase A — Validate args and resolve scope

1. If `$0` is missing or not in {audit, plan, draft, eval, bug-hunt}, print usage and stop.

2. Initialize the audit DB if absent (idempotent):
   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/audit-db.py initdb
   ```

3. Resolve invocation scope:
   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/scope-detect.py $0 $@
   ```
   Capture the JSON output. It contains: `audit_id`, `scope_kind`, `files`, `branch`,
   `commit_at_start`, `scope_description`. **Stop on any non-zero exit.**

4. Read active personas from `<repo>/.minsky/binding.yaml`. v1 default: `marcion-heresiologist`,
   `ml-finetuning-phd` (both always active). Allow user override: if `--personas a,b,c` is in
   `$@`, use that comma-separated list instead.

### Phase B — User confirmation

5. Show the user a summary table:
   - audit_id, mode, scope_kind, scope_description
   - branch, commit_at_start
   - files (count + first ~10 paths)
   - active personas
   - models that will be used (Claude Opus 4.7 [host], GPT-5.5 [Codex], DeepSeek V4 Pro [OpenCode])
6. For modes `audit | bug-hunt | eval`, ask: **"Proceed with this audit, adjust the scope, or
   cancel?"** For `plan | draft`, the artifact may not exist yet on disk — interpret `files`
   accordingly and ask the user for any additional context they want included.
7. If the user adjusts, re-resolve scope and repeat Phase B. If they cancel, exit (no DB
   row written).

### Phase C — Open audit row

8. Once confirmed:
   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/audit-db.py open \\
     --audit-id <id> --branch <branch> --commit <commit_at_start> \\
     --mode <mode> --scope <scope_kind> --scope-description "<desc>" \\
     --files '<json-array>' --personas '<json-array>' \\
     --models '["claude-opus-4-7","gpt-5.5","deepseek-v4-pro"]'
   ```

### Phase D — Round loop (max 3 rounds; each round below = "round N")

**Real-time progress instrumentation (since v1.1, 2026-05-03).** Every audit
emits structured NDJSON events to `codex-audits/<audit-id>/progress.ndjson`,
populated by `audit-db.py` (open/close), `chain.py` (step boundaries +
converge_done), and `invoke-codex.sh` / `invoke-opencode.sh` (per-persona walk
start + done with verdict, finding count, severity breakdown). The host
should emit progress events at Step 1 and Step 4 boundaries via:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/progress.py emit \\
  --audit-id <id> --event step_start --round <N> --step claude_self
# ... do the host work ...
python3 ${CLAUDE_SKILL_DIR}/scripts/progress.py emit \\
  --audit-id <id> --event step_done --round <N> --step claude_self
```

For a parent supervising session, the events can be tailed in real time:

```
python3 ${CLAUDE_SKILL_DIR}/scripts/progress-tail.py <audit-id>
```

Schema: `${CLAUDE_SKILL_DIR}/schemas/progress.schema.json` (JSON Schema 2020-12)
and `${CLAUDE_SKILL_DIR}/schemas/progress.md` (prose spec).

9. **Build pack** for round N:
   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/pack-build.py \\
     --audit-id <id> --round <N> --mode <mode> \\
     --files <file1> <file2> ... \\
     --output codex-audits/<id>/round-<N>/pack.xml \\
     [--prior-rounds-dir codex-audits/<id>/round-<N-1>/]   # only if N>1
   ```

10. **Step 1 — Claude self-report-and-audit** (you, the host Claude — this is your turn).

    a. Run prep (creates dirs, writes a checklist):
       ```
       python3 ${CLAUDE_SKILL_DIR}/scripts/claude-self-audit.py prep \\
         --audit-id <id> --round <N> \\
         --personas <comma-separated> \\
         --round-dir codex-audits/<id>/round-<N>
       ```

    b. Read the checklist at `codex-audits/<id>/round-<N>/claude-self/_step1_checklist.md`.

    c. Read the pack at `codex-audits/<id>/round-<N>/pack.xml` in full. Use ultrathink to
       reason about the artifact across both persona lenses.

    d. Write `codex-audits/<id>/round-<N>/claude-self/report.md` with TWO clearly-headed
       sub-sections:
        - **Section A — What was done.** Factual report. List of files touched, design
          decisions made, tradeoffs declared, attempted-and-abandoned approaches. NO
          self-evaluation. Adversaries will read this as ground-truth context.
        - **Section B — Per-persona candidate findings.** Heading per active persona; under
          each, your first-pass guess at what an adversary in that lens would flag. These
          are HYPOTHESES for adversaries to test, not assertions.

    e. For each active persona, write `codex-audits/<id>/round-<N>/claude-self/findings/<persona>.json`
       conforming to `${CLAUDE_SKILL_DIR}/schemas/findings.schema.json`. Each finding must
       cite `evidence` with `file_path`, `line_number`, and a *verbatim* `quoted_line`.

    f. Validate:
       ```
       python3 ${CLAUDE_SKILL_DIR}/scripts/claude-self-audit.py validate \\
         --audit-id <id> --round <N> --personas <comma> --round-dir <dir>
       ```
       Loud-fail if validation fails — fix and re-validate; do not continue with bad
       outputs.

11. **Steps 2 + 3 + Convergence** (deterministic; one chain.py call):
    ```
    python3 ${CLAUDE_SKILL_DIR}/scripts/chain.py adversaries \\
      --audit-id <id> --round <N> \\
      --round-dir codex-audits/<id>/round-<N> \\
      --pack codex-audits/<id>/round-<N>/pack.xml \\
      --personas <comma>
    ```
    Recovery options for incomplete runs:
    - `--only-step codex|opencode` runs only one adversarial backend before convergence.
    - `--only-persona <slug>` is repeatable and also accepts comma-separated values.
    - `--resume-existing` skips already-valid persona JSON outputs.

    Exit code interpretation:
    - **0** = `agree` (all per-persona verdicts agree across Codex/OpenCode)
    - **1** = `disagree` (at least one persona challenged)
    - **2** = `incomplete` (a step output is missing or schema-invalid)
    - **3** = `user_decision_required` (Codex+OpenCode unanimously challenged a Claude
      Step 1 finding — you must adjudicate in Step 4 with explicit user input)
    - **42** = `paused-rate-limit` — handle per Phase E rate-limit branch

12. **Step 4 — Claude synthesis** (your turn again).

    a. Run prep:
       ```
       python3 ${CLAUDE_SKILL_DIR}/scripts/claude-synth.py prep \\
         --audit-id <id> --round <N> \\
         --round-dir codex-audits/<id>/round-<N>
       ```

    b. Read the checklist + `converge.json` + `consensus.md` + every per-persona JSON
       across all 3 steps. Use **ultrathink**.

    c. Decide per-finding resolution: `addressed`, `carried_forward`, `retracted`, or
       `user_overruled`. If chain.py exit was 3 (`user_decision_required`), explicitly
       surface those findings at the top of `decisions.md` and present them to the user
       with three options: (a) treat as addressed (retract them), (b) carry forward to
       round N+1, (c) accept the override and record reasoning. **Never silently overrule
       both adversaries.**

    d. Write `codex-audits/<id>/round-<N>/claude-synth/decisions.md` (markdown summary)
       and `codex-audits/<id>/round-<N>/claude-synth/resolutions.json` (machine-readable
       resolution table per the checklist).

    e. Validate:
       ```
       python3 ${CLAUDE_SKILL_DIR}/scripts/claude-synth.py validate \\
         --audit-id <id> --round <N> --round-dir <dir>
       ```

### Phase E — Decision branch

13. Based on the chain.py exit code AND your synthesis judgment:

    - **agree** + your synthesis confirms: present `decisions.md` summary to user, ask if
      they accept proposed `addressed` changes. If they accept, you may use Edit/Write to
      apply them (per your normal Edit confirmations). Then proceed to Phase F.

    - **disagree** + you can articulate concrete remediation: present `decisions.md` to
      user with the three options:
        i. **Revise and run round N+1** — apply your proposed changes, then loop back to
           step 9 with N+1.
        ii. **Accept with explicit override** — user reads the carried-forward findings,
            decides to ship anyway with documented reasoning. Audit row gets
            `convergence_status=user_override`. Proceed to Phase F.
        iii. **Abandon this audit** — `convergence_status=abandoned`. Proceed to Phase F.

    - **incomplete** — surface the failure (which step / which persona / what error).
      Audit row stays open with `convergence_status=incomplete`. Do **not** retry silently.
      If the user chooses recovery, prefer a targeted command such as:
      ```
      python3 ${CLAUDE_SKILL_DIR}/scripts/chain.py adversaries \\
        --audit-id <id> --round <N> \\
        --round-dir codex-audits/<id>/round-<N> \\
        --pack codex-audits/<id>/round-<N>/pack.xml \\
        --personas <comma> \\
        --only-step opencode --only-persona <failed-persona> --resume-existing
      ```
      Then continue from Step 4 if convergence succeeds.

    - **paused-rate-limit** (chain.py exit 42) — surface to user with three options:
        i. Wait N minutes and retry the failed step with `--only-step`,
           `--only-persona`, and `--resume-existing`.
        ii. Abandon (`convergence_status=abandoned`).
        iii. Skip this lineage for the remainder of this audit and continue with the
             others — explicit user override, recorded as `convergence_status=user_override`
             with a note. **Never select this on your own.**

### Phase F — Close + work-log

14. Close the audit row:
    ```
    python3 ${CLAUDE_SKILL_DIR}/scripts/audit-db.py close \\
      --audit-id <id> --commit $(git rev-parse HEAD) \\
      --rounds <N> --convergence <status> \\
      --summary codex-audits/<id>/round-<N>/consensus.md
    ```

15. **Work-log entry** (per project convention; mandatory in the reference deployment
    after meaningful work):

    Generate the work-log entry stub:
    ```
    python3 ${CLAUDE_SKILL_DIR}/scripts/work-log-stub.py draft \
      --audit-id <id> --duration-hours <h>
    ```
    Show the user the proposed markdown; on confirm:
    ```
    python3 ${CLAUDE_SKILL_DIR}/scripts/work-log-stub.py append --audit-id <id> --duration-hours <h>
    ```
    (this appends to `documentation/phd_work_log.md` AND records the path in
    `audits.work_log_entry_path`).

    **Public release note**: the reference deployment also pushes work-log entries to
    a project-internal Supabase provenance store (TITAN). That integration
    (`scripts/titan-push.py`) is not shipped in the public release. The audit-DB
    schema's `titan_log_id` / `titan_pushed_at` columns remain (NULL for public
    users) so users who fork and re-integrate their own provenance backend can do so
    without schema changes.

16. Print final summary: audit_id, decision, location of round-N artifacts, and (if any)
    proposed changes the user accepted.

---

## Failure handling — never silent

- Any non-zero exit from any helper script: surface stderr verbatim to user; pause; ask
  what to do (retry, abandon, debug). Do not auto-continue.
- Schema-invalid agent output: chain.py / converge.py mark this as `incomplete`. Surface
  the specific schema errors. Decide whether to re-prompt the failing agent or escalate
  to user.
- Rate-limit detected in agent output: handled by Phase E rate-limit branch; **never
  auto-skip a lineage.**

## Rigor norms (inherited from CLAUDE.md and global policy)

- Distinguish verified facts from inferences from unresolved uncertainty.
- Never claim a check ran cleanly unless it actually completed.
- Loud failure preferred over silent fallback.
- All audit findings will be reviewed by humans and may be cited in the dissertation
  methods chapter or defense. Be defensible.
- Use ultrathink for every consequential reasoning step (Step 1 candidate findings;
  Step 4 synthesis decisions; rate-limit branch decisions).
