---
name: minsky
description: |
  Sequential, multi-CLI adversarial-audit harness for MARS PhD work products.
  Triangulates Claude (Opus 5), Codex (GPT-5.6-sol), and OpenCode (Gemini 3.8 Flash @ high thinking)
  in a 4-step deliberation chain (Claude self-report → Codex per-persona →
  OpenCode per-persona meta-adversarial → Claude synthesis), with stable expert
  personas (the .minsky/binding.yaml default.active set — currently 6: marcion-heresiologist,
  ml-finetuning-phd, performance-studies-roach, provenance-reproducibility,
  phd-documentation-currency, marcion-textual-critic)
  carrying memory across rounds.
  Modes: audit | plan | draft | eval | bug-hunt. All audits persist to a queryable
  sqlite DB at .minsky/audits.db with linkage to phd_work_log.md and git commits.
  Use ultrathink throughout; this is high-stakes work for the dissertation.
arguments: mode rest
argument-hint: <audit|plan|draft|eval|bug-hunt> [task-id | paths... | --since <spec>]
disable-model-invocation: false
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

> **Public release note (v2.0.0):** this protocol is written as run in its reference deployment (MARS). Steps that
> name its paths, `documentation/phd_work_log.md`, the `/work-log` skill or TITAN reporting (Phase F step 15) are
> deployment-specific: adapt or skip them. `titan-push.py` is not shipped.

> **2026-09-05 scoped recovery:** the shared `edit` gate controls Write as well as Edit, and Write
> checks git-worktree-relative paths. Current `minsky-reviewer` permits only two exact cellwise-plan
> round-2 Gemini outputs and one synthetic canary. Seven no-provider capability checks passed; other
> audit outputs remain denied until explicitly scoped and tested. This is NOT a general Write enablement.
> The earlier text-only failed result remains failed. See `.claude/rules/batch-operations.md` and
> `round-2/recovery-1/runtime-amendment.md`; recovery completion is recorded in the campaign handoff.

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
`--dangerously-skip-permissions`. Under OpenCode 1.18.23 the repository-wide wildcard deny
otherwise wins during permission merging, so `.opencode/agents/minsky-reviewer.md` must
explicitly re-enable Read/Grep/Glob/Write and then deny credential paths. Bash stays disabled;
Write and Edit share a gate restricted to exact authorised output paths (not all cwd files).
Re-probe actual successful Write and denied out-of-scope Write/Edit, Bash and credential-like Read
after any agent/configuration change. A displayed enabled tool is not proof its required path works.

---

## Args

- `$0` (required): mode — one of `audit`, `plan`, `draft`, `eval`, `bug-hunt`
- `$1+` (optional): scope args, interpreted by `scope-detect.py`:
  - explicit task-id: `/minsky audit my-named-task` (slug, no slashes)
  - delta (no args): `/minsky audit` — scope = changes since last audit on this branch
  - paths: `/minsky audit path/to/file.md src/pipeline/`
    - Paths are emitted repo-relative. A path outside the repo is refused unless `--allow-external` is passed (since
      W10a, 2026-10-06; ruling W10-H).
    - The scope JSON records `"allow_external": true|false` in every mode.
  - time: `/minsky audit --since "2 hours ago"`

---

## Protocol — execute in order, each step blocks the next on failure

### 2026-09-05 call-record contract (applies to new rounds)

The active host may differ from Claude when explicitly directed by the researcher; record its actual
identity rather than inferring it from `claude-self` / `claude-synth` compatibility directory names.
Do not invent a host effort setting, raw request, exit code, token usage or hidden context. A host that
lacks those observables records `not_exposed` / unavailable with reasons. External legs still use the
explicitly authorised models and personae; no substitution is implied by a different host.

**Order per round (round scope 1.1, since 2026-10-02):** build the pack FIRST (step 9), then register the
immutable round scope with that pack, then run self prep. `--pack` hashes the already-built pack into
`round-scope.json` (`pack_sha256`); registration also records `git_head` and the tracked `dirty_paths`.

```
.venv/bin/python .claude/skills/minsky/scripts/provenance.py register-round \
  --audit-id <id> --round <N> --round-dir <dir> \
  --pack <dir>/pack.xml \
  --personas '<JSON array>' --models '<JSON array of model@effort>' \
  --step-models '<JSON object binding claude_self,codex,opencode,claude_synth>'
```

`register-round` (like `audit-db.py open` and `chain.py`) refuses to run in a linked git worktree unless
`codex-audits/` and `.minsky/` are absent from it. The override is `MINSKY_ALLOW_WORKTREE=1`, and it is
recorded in the round scope. Run audits in the main checkout.

The host must preserve its visible task note and call `provenance.py prepare` **before it writes any of
the phase's outputs** (Step 1 report and findings; Step 4 decisions and resolutions). It then calls
`provenance.py record` afterwards, covering every output plus the pack/upstream context. A preflight
written after the outputs is still hash-correct, but its time order is wrong. Disclose such a slip in the
round's notes; never back-date it.
Use each subcommand's `--help` for required fields. `record` requires the matching `--preflight-path`;
host-only `--response-unavailable-reason` and `--exit-code-not-exposed` represent actual unavailable
telemetry, not failures disguised as successful provider calls. Hash the supplied artifacts exactly.
`--record-db` indexes the immutable sidecar and validates the round-specific scope without rewriting a
broader historical audit registration. The sidecar is authoritative for cache/reasoning/total/cost and
unavailable fields that the legacy compact DB columns cannot express.

Both wrappers now persist a unique prompt and pre-dispatch receipt before the external call, then raw
logs, output snapshots and terminal provenance. They compare pre/post prompt/context/runtime hashes and
enforce an explicit timeout (1,800 seconds default plus 60-second kill grace; any change is registered
before the round). Convergence requires current terminal success, correct phase/model, and matching
artifact hashes. Missing/malformed usage is not zero; genuine Codex aggregate-only usage is total-only.
Stable finding UIDs and exact dedupe support synthesis coverage; semantic consolidation remains the host's
judgment. Never retrofit missing evidence into a historical round.

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

4. Read active personas from `<repo>/.minsky/binding.yaml` (AUTHORITATIVE). The current
   `default.active` set is **6 personae**: `marcion-heresiologist`, `ml-finetuning-phd`,
   `performance-studies-roach`, `provenance-reproducibility`, `phd-documentation-currency`,
   `marcion-textual-critic`
   (provenance + roach + phd-doc were promoted to default 2026-05-28; the older "v1 default = 2"
   prose was stale — corrected 2026-06-03 per the Adamantius Track-2.5 round-1 phd-doc finding).
   Allow user override: if `--personas a,b,c` is in
   `$@`, use that comma-separated list instead.

### Phase B — User confirmation

5. Show the user a summary table:
   - audit_id, mode, scope_kind, scope_description
   - branch, commit_at_start
   - files (count + first ~10 paths)
   - active personas
   - models that will be used (Claude Opus 5 [host], gpt-5.6-sol@<effort> [Codex], gemini-3.8-flash@high [OpenCode])
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
     --models '["claude-opus-5@high","gpt-5.6-sol@medium","gemini-3.8-flash@high"]'
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

    a′. **Before writing any Step 1 output**, run `provenance.py prepare --origin host --step claude_self`
       for each host call: one for the report, and one per persona findings file. Use the same model@effort
       string as the registered `claude_self` binding.
       - Pass one `--expected-output-path` per file the call will write; the flag repeats.
       - `verify-round` selects canonical targets from these declarations. A call manifest's outputs that
         a same-step, same-persona preflight declared are canonical targets.
         - Its other outputs, such as raw logs, are not targets. When no target covers them, they are listed as
           `attempt_evidence`.
         - The latest attempt's other outputs still count. Each target's `sibling_outputs` guard requires all
           of them unchanged, so a changed latest raw log fails the target.
         - Superseded attempts' outputs are checked for preservation only.
         - A manifest with no declared output at all keeps every output as a target (fail-closed: nothing
           is hidden).
       - After step (f), run `provenance.py record` with the matching `--preflight-path` and every
         `--output-path`.
       - Step 4 follows the same pattern (`--step claude_synth`) around `decisions.md` and `resolutions.json`.

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

11. **Steps 2 + 3 + Convergence** (deterministic; one chain.py call).

    **Preferred launcher (since 2026-10-02): `scripts/launch-chain.py`.** It checks before any provider
    call that `--codex-model`/`--codex-effort` equal the registered `codex` binding and that
    `--opencode-variant` matches the `opencode` effort. It also refuses a run label whose chain files
    already exist. It then starts `chain.py adversaries` detached, with `.venv/bin` prepended to `PATH`,
    and writes `chain[-<run-label>].{stdout.log,stderr.log,exit,pid,env.txt}` into the round directory
    (`env.txt` records the model, effort and timeout variables and the chain argv):
    ```
    .venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/launch-chain.py \
      --audit-id <id> --round <N> --round-dir <round dir> --pack <round dir>/pack.xml \
      --personas <comma> --models '<same JSON array as registration>' --step-models '<same JSON as registration>' \
      --codex-model gpt-6.1-sol --codex-effort xhigh [--codex-timeout 3600] \
      --opencode-model google/gemini-3.8-flash --opencode-variant high --run-label <label>
    ```
    - **Before the first launch of a new audit: OpenCode write scope.** `minsky-reviewer` writes only to
      exact paths listed in `.opencode/agents/minsky-reviewer.md` (Write and Edit share one gate).
      - Each audit's OpenCode output paths are added there **only on the researcher's first-hand
        authorisation for that audit**.
      - After any change, run a no-provider canary probe through `opencode debug agent minsky-reviewer
        --tool …`. Pattern: `round-N/_capability/permission_canaries.py` in the 2026-10-02 smoke audit. It
        runs eight cases, with synthetic files only:
        - three must be allowed: a Write and a Read of the allow-listed file, and a Read of the pack;
        - five must be denied: an unlisted Write, a source-tree Write, an Edit, Bash, and a credential-like
          Read.
      - Record the JSON result and the agent file's sha256 in `_capability/`. Remove the synthetic fixtures,
        recording their hashes. Only then launch.
    - Run `launch-chain.py` with **`.venv/bin/python`**: it starts `chain.py` with its own interpreter
      (`sys.executable`).
      It passes `--models` to `chain.py` comma-joined. It drops every inherited `OPENCODE_*` variable and
      sets only the ones given on its command line.
    - **Direct `chain.py` runs (including the recovery commands below):** first export
      `CODEX_MODEL`/`CODEX_EFFORT` (and `OPENCODE_MODEL`/`OPENCODE_VARIANT` if they are not the defaults) to
      match `--step-models`.
      - `invoke-codex.sh` otherwise falls back to its defaults (`gpt-5.6-sol@medium`).
      - `prepare` then refuses the unbound model stamp before the provider call. No paid call is made, but the
        walk fails loudly.
    - **Read the decision from `converge.json`, never from the launcher's or chain's exit code alone.**
    - **Evidence outside the repo** (since W10a, 2026-10-06; ruling W10-H):
      - Convergence verifies a finding's evidence only when its `file_path` resolves inside the repo, symlinks
        followed. So a `codex-audits/` entry archived as a symlink to `/Volumes` counts as outside.
      - Any other finding stays unverified, with `_verification_error`, unless the run passes `--allow-external-evidence`:
        - `chain.py adversaries --allow-external-evidence`;
        - `launch-chain.py … -- --allow-external-evidence`, where `env.txt` records it in `CHAIN_ARGV`;
        - `converge.py --allow-external-evidence`.
      - `converge.json` records `evidence_scope {allow_external_evidence, repo_root, refused_external}`, and
        `consensus.md` carries one line with the same facts.
      - `refused_external` counts only findings that verification reached. A round stopped `incomplete` at a schema or
        provenance gate skips verification, so it records 0.
    - **Codex helper artefacts:** a Codex leg may leave probe scripts, probe results or copies in `codex/`.
      - Any top-level `*.json` there that is not a registered persona's findings makes convergence report
        `incomplete` ("unregistered persona JSON").
      - Other helper files are pulled into the next round's pack.
      - Move them unchanged to `codex/_extra_artifacts/`, with a NOTE that lists their sha256, and re-run
      `.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/converge.py --audit-id <id> --round <N> --round-dir <dir>`.
      Do this before building the next round's pack: `pack-build.py --prior-rounds-dir` skips only
      `_`-prefixed directories.

    The equivalent direct call:
    ```
    python3 ${CLAUDE_SKILL_DIR}/scripts/chain.py adversaries \\
      --audit-id <id> --round <N> \\
      --round-dir codex-audits/<id>/round-<N> \\
      --pack codex-audits/<id>/round-<N>/pack.xml \\
      --personas <comma> --models <comma-separated-model@effort-stamps> \\
      --step-models '<same JSON phase/model mapping as round-scope.json>'
    ```
    Recovery options for incomplete runs:
    - `--only-step codex|opencode` runs only one adversarial backend before convergence.
    - `--only-persona <slug>` is repeatable and also accepts comma-separated values.
    - `--resume-existing` skips already-valid persona JSON outputs.

    Recovery invocations must also pass the same explicit `--models` and `--step-models` as registration.
    “Existing valid” now means schema-valid AND matching latest successful terminal/request/context hashes;
    a JSON file beside a failed terminal is not silently resumed as success.

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

### Phase F — Close + (deferred to Phase 5 of implementation) work-log + TITAN push

14. Close the audit row:
    ```
    python3 ${CLAUDE_SKILL_DIR}/scripts/audit-db.py close \\
      --audit-id <id> --commit $(git rev-parse HEAD) \\
      --rounds <N> --convergence <status> \\
      --summary codex-audits/<id>/round-<N>/consensus.md
    ```

14b. **Lifecycle close-out**, due within 7 days of `finished`: `classify` → `seal` → `pack` → one verified
    off-boot copy (methodology-bearing audits also need the second copy within 30 days). See "Runtime store and
    audit lifecycle" below. `seal` needs real-data gate G1, and the copies need G2. Until both are met, the
    close-out cannot run, and `census` reports the audit `overdue-first-copy` once 7 days have passed.

15. **Work-log + TITAN push** (Phase 5 integration is COMPLETE; per MARS CLAUDE.md these
    are mandatory after meaningful work):

    a. Generate the work-log entry stub:
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

    b. Register the local audit entry with the canonical `/work-log` helper:
       ```
       .venv/bin/python .claude/skills/work-log/scripts/work_log_push.py defer \
         --entry-heading "### YYYY-MM-DD — exact local title" --audit-id <id>
       ```
       Paste the queue ID into the local entry. **Do not generate a TITAN description during the
       60-minute cooldown.** Check `status`; when due, compose one combined summary of all queued
       work following `/work-log`'s five-section contract and `preview` → `push --combined` with
       each `--entry-id`. A general combined row may cover multiple audits; receipts update their
       `audits.titan_log_id` only after delivery. Carry pending IDs/time at handoff. No early final
       push or `--force` bypass. `amend` corrects existing rows under the same interval.

    Loud-fail on auth or insert error per the no-silent-fallback principle.

16. Print final summary: audit_id, decision, location of round-N artifacts, and (if any)
    proposed changes the user accepted.

---

## Runtime store and audit lifecycle (plan v0.2, ratified 2026-10-01; merged 2026-10-02/06)

Binding plan: `documentation/plans/minsky_artifact_lifecycle_plan_v0.2_2026-10-01.md`. The researcher's rulings:
`documentation/plans/minsky_lifecycle_2026-09-30/RULINGS.md`. The README section of the same name holds the full
specification. This section is the operating summary.

### Runtime store (content-addressed; LC-STORE)
- **What it holds.** The runtime copies every call records as evidence of what ran: program binaries, interpreters
  and wrapper scripts. They are kept once, named by sha256, and shared by all audits. With the store enabled, rounds no longer write
  `provenance/runtime-blobs/` (≈145 MB per OpenCode version per round, before the store).
  - For **code-only** audits only runtime blobs enter the store; their other hash-bound files stay pack-only (LC-F1).
  - For **methodology-bearing** audits, `seal` also ingests every hash-bound file.
    - **Exception:** external referenced context whose current bytes no longer match the recorded sha256. `seal` lists
      it as `context-drifted-not-ingested`; only a copy the store already holds stays covered.
- **Root:** the absolute path in `config/store.json`, written by `scripts/setup.sh` (default
  `$HOME/Library/Application Support/minsky/store` on macOS, `${XDG_DATA_HOME:-$HOME/.local/share}/minsky/store` elsewhere).
- **Config:** `config/store.json` = `{"store_id": "mars-minsky-cas-v1", "root": "<absolute root>", "enabled": true}`.
  - `MINSKY_STORE_ROOT` (absolute) overrides the root; the receipt records `root_source`.
  - `enabled: false` is refused unless `disabled_by` and `reason` are given.
    - It is then a recorded opt-out: `prepare` writes per-round snapshots under `<round>/provenance/`.
    - The receipt's `store` block records the opt-out.
  - Check with `.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/store.py check`, which prints the resolved config and
    `"validation": "ok"`. Create a store once with `store.py init --root <absolute>`.
- **Layout:**
  - `<root>/STORE_ID` holds `mars-minsky-cas-v1`;
  - `<root>/sha256/<aa>/<bb>/<sha256>` holds the blobs, read-only (0444);
  - `<root>/index.tsv` is append-only (`sha256, bytes, first_source_path, first_seen_utc, first_audit_id,
    first_round`).
- **Records:** receipts are schema 1.1 with a `store` block. Each runtime snapshot carries its absolute CAS `path`
  and `store_ref: {store_id, sha256}`.
  - **Live verification** hashes the recorded absolute path while that file exists. It falls back to `store_ref`
    plus the configured root only when the file is absent.
  - **Archive verification** never reads the configured root. It resolves by sha256, in this order: the store
    passed as `--store`, then `--pack`, then explicit `--remap` prefixes.
  - So a relocation is proven by an archive run with `--store <new root>`, never by the config edit alone.
- **Loud failure:** the store must be present and correct before any provider call.
  - A missing, unmounted or unwritable root, or a missing or foreign `STORE_ID`, stops `provenance.py prepare`
    before any provider call.
  - A free-space refusal before an ingest stops it in the same way.
  - There is **no fallback** into the round directory.
- **Status:** a provenance anchor. Never delete the store or any blob in it. `expire` never touches CAS blobs.
  Time Machine is not a copy.
- **Relocation:** only by the researcher's ruling. The procedure is:
  1. copy the store;
  2. re-hash every blob at the destination;
  3. edit `store.json`;
  4. run `verify-round --mode archive --store <new root>` on every round, with `--pack` or `--remap` for the round's
     own files;
  5. only then retire the old root.

### Audit lifecycle (`scripts/minsky-lifecycle.py`)
- **States:** `open → finished → sealed → packed → replicated(n) → offloaded`, plus `rehydrated`, `promotion-pending`
  and (code-only only) `expired`.
- **Verbs:** `census`, `classify`, `challenge`, `ruling`, `promote`, `seal`, `pack`, `replicate`, `verify`,
  `offload --prepare|--execute|--resume`, `rehydrate`, `expire [--execute]`,
  `freeze --reason ad-b|campaign-close [--label <campaign>]`, `freeze-restore-check`,
  `lock-status`, `lock-clear`. `archive` is a thin wrapper that stops at the first refusal. No verb chains silently
  into the next.
  - Each step holds the per-audit lifecycle lock for its whole read-check-act-append sequence.
  - Composite operations release the lock between steps:
    - `archive` calls each verb with its own lock;
    - `promote` takes a separate lock for each copy it creates, and `promotion-pending` guards the gaps.
  - Run `<verb> --help` for flags.
- **Class:** `classify` defaults to **methodology-bearing**.
  - `code-only` is refused while any trigger fires: a `config/methodology_triggers.txt` glob, an unknown input, a
    work-log trigger, or an unanswered `challenge`. A recorded `downgrade:` ruling lifts that refusal.
  - `code-only` also needs a non-empty `--statement-file`.
  - After `seal`, a class is upgraded only through `promote`.
- **Close-out deadline (LC-SEAL as amended by LC-F2):**
  - within 7 days of `finished`: `seal` + `pack` + at least one verified off-boot copy;
  - methodology-bearing audits also need the second copy within 30 days;
  - `census` flags `overdue-first-copy` and `overdue-second-copy` until the copies exist.
- **Copies (LC-COPIES):**
  - Copy A is an off-boot directory passed as `--copy-a-root` (in the reference deployment, an external volume).
  - Copy B is the BLOAT-S restic repository.
    - Its snapshots tagged `minsky-lifecycle` are never pruned: every `forget` in `scripts/mars_offsite_backup.sh`
      passes `--keep-tag minsky-lifecycle` (installed 2026-10-06, `d3ce1816`).
    - `rehydrate --from A|B` restores into a scratch directory (`--into`) or in place.
- **`offload`** is a verified move, never of the last copy.
  - It writes `lifecycle/STUB.json` and `TOMBSTONE.md`.
  - It appends one row to `documentation/cold_storage_ledger.md` in that ledger's seven-column schema.
- **`expire`** is for code-only raw packs only, 90 days after `seal` (LC-GRACE). It is a dry run unless given
  `--execute`, and the `expired` row states exactly what remains verifiable.
- **`freeze`** freezes `audits.db` outside `seal` (ruling AD-B-F, 2026-10-08; `RULINGS.md` §11).
  - It uses the same function, the same ledger and the same lock discipline as `seal`.
  - **Reasons and ids:**
    - `--reason ad-b` uses id `freeze.AD-B`, for the AD-B freeze;
    - `--reason campaign-close --label <campaign>` uses id `freeze.campaign-close.<campaign>`, for the ruled
      campaign-close cadence.
  - **Reserved ids:** ids starting with `freeze.` are reserved. An id that names an audit in the audits database or
    the register is refused.
  - **Refusals:** a hot journal (`-journal`, or a non-empty `-wal`, beside the given or the resolved path), and an
    open handle on the database (lsof).
  - **Files:** `<utc>_<id>_freeze.db`, written to `--freeze-dir` (default
    `~/Library/Application Support/MARS/frozen/minsky_audits_db`) and to the off-boot `--freeze-copy`.
  - **Output:** one JSON line with the sha256, the bytes and both ledger row numbers.
  - **Not behind G1**, because it produces G1's evidence. `seal` still is.
  - **Restore check:** `freeze-restore-check` on a `freeze.` row writes its `restore-demo` ledger row and no register
    event.
- **Tracked records:**
  - `documentation/minsky_audit_lifecycle_register.tsv` (the register, append-only);
  - `documentation/minsky_store_index.tsv` (the store rows each sealed audit uses);
  - `documentation/minsky_audits_db_freezes.tsv` (the `audits.db` freezes made at `seal` and by `freeze`).
  - Each file is created by its first real write.
  - Pre-commit GUARD 4 refuses any staged change to these TSVs that is not a pure append of complete LF-terminated
    lines: the HEAD bytes must stay a prefix, and deletion, renaming away or a type change is refused.
    - **Initialisation exception:** the commit that first adds a TSV is not content-checked. Review a TSV's first
      commit by hand.
    - The only override is `MARS_ALLOW_TSV_CORRECTION=1` with `MARS_ALLOW_TSV_CORRECTION_REASON`. Each use is logged
      in the git common dir.
    - `pre-merge-commit` applies all the guards to clean merges. **Not covered:** fast-forward merges and
      `--no-verify`.
- **Free space (§4.4; W11–W12):** these steps check `free − incoming ≥ max(5 GiB, production floor) + 2 GiB` on the
  volume they write to:
  - every store ingest (in `prepare` and in `seal`);
  - `pack`;
  - `replicate --copy A`;
  - the offload prepare;
  - `rehydrate`;
  - both promotion checks.

  `replicate --copy B` writes to a remote restic repository and makes no local check.
  - The production floor is `MIN_FREE_BYTES` in `scripts/r3_continuous.py`, read by a **static parse that never
    executes the module**.
    - The parse accepts only an integer expression of constants with `+ * **` and exactly one binding of the name
      anywhere in the module.
    - It refuses anything else rather than guess: a computed value, a second binding, an import or star import that
      may rebind it.
    - Documented limit: a floor computed at run time cannot be followed and must be refused.
  - Each check is logged at INFO.
  - **Where a passing check is persisted:**
    - `prepare` puts its ingest checks in the preflight receipt's `store` block.
    - `pack`, `replicate --copy A`, the offload prepare, `rehydrate` and the promotion checks write
      `lifecycle/free_space_checks/<sha256>.json`, cited in their register rows as `free_space_sha256:<sha256>`.
    - **Gap (found 2026-10-06, not yet fixed):** `seal`'s ingest check is logged but not persisted.
  - `pack` order: check → sidecar pre-flight → plan once → write and verify → record → row. Identical sealed trees
    pack to identical bytes.
- **Real-data gates (`config/real_data_gates.json`, §8.2):**
  - **G1** (AD-B executed + freeze replication + one restore demonstrated) gates `seal`;
  - **G2** (BLOAT-S never-prune) gates `replicate`, `promote` copies and `offload`;
  - **G3** (RD-A+C) gates `census --record` (L6 migration).
  - Each gate needs `met: true` with non-empty `evidence`, set only on the researcher's ruling.
  - `freeze` is not gated (ruling AD-B-F). G1's conditions are unchanged; `freeze` and `freeze-restore-check` produce its
    freeze-replication and restore evidence.
  - **As of 2026-10-06 all three are `met: false`**, so no real `seal`, copy or offload can run.
  - Fixture audits whose audit directory AND DB lie under `MINSKY_TEST_TMP` are exempt. A test root containing the
    repository or the home directory is refused.

### `verify-round` (L1; schema `minsky-verify-round/1.1`)
```
.venv/bin/python ${CLAUDE_SKILL_DIR}/scripts/provenance.py verify-round --round-dir <dir> \
  --mode live|archive [--no-live] [--store <root>] [--pack <tar.zst>] [--remap /old/prefix=/new/prefix ...] \
  [--anchor-repo <repo> --anchor-prefix <prefix>] --json-out <new file>
```
- **Canonical targets** are selected by the `expected_outputs` declared at `prepare` (Step 1 a′ above).
  - Excluded outputs are listed in `canonical.attempt_evidence`.
  - The latest attempt's excluded outputs are still required by each target's `sibling_outputs` guard.
- **Preservation** re-hashes every hash-bound object of every receipt, superseded attempts included.
  - `ok`, `ok-with-losses` or `fail`.
  - `ok-with-losses` arises only from:
    - `legacy-runtime-unverifiable`;
    - `superseded-unrecoverable`: a superseded attempt's output was overwritten by a later attempt. Either the
      path now holds bytes that another receipt recorded for the same output, or the bytes are genuinely absent
      and another receipt's different sha resolves at that path.
  - Bytes that no receipt recorded are `mismatch`, and a failed resolution that is not a genuine absence is
    `missing`. Both make the result `fail`. A corrupted blob is never relabelled a known loss.
  - `ok-with-losses` leaves an `ok-…` verdict unchanged, so read `preservation.result`.
- **Verdict precedence:**
  1. `fail:<reason>` (canonical, load or anchor);
  2. `fail:preservation <status> <kind> <path>`;
  3. `legacy-runtime-unverifiable`;
  4. archive mode: `consistent-unanchored` (a call manifest with no git anchor), then `ok-archive-context-drifted`;
  5. `ok-live` / `ok-archive`.
- Exit 0 only for `ok-*`. The JSON is written once and never overwritten.

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

## Runtime environment gotcha — Python 3.11+ REQUIRED (use .venv, never system python3)

The minsky helper scripts (`audit-db.py`, `chain.py`, …) AND the `invoke-codex.sh` /
`invoke-opencode.sh` wrappers use `datetime.UTC` (added in Python **3.11**; the project
`.venv` is 3.12). The system `python3` on PATH is 3.10 → `audit-db.py open` crashes
(`AttributeError: module 'datetime' has no attribute 'UTC'`) and so does the chain's first
Codex per-persona subprocess (inline `python3 -c`), aborting the whole cross-model round
(`converge.json` never written; CHAIN_EXIT=1 is a FAILURE here, not a real `disagree`).

**Fix:** run every minsky helper with `.venv/bin/python`, AND for `chain.py` (which spawns
the bash wrappers' `python3 -c` subprocesses) **prepend `.venv/bin` to PATH**:
`export PATH="$(pwd)/.venv/bin:$PATH"` (`codex`/`opencode` still resolve via the rest of
PATH). The durable fix (Track-5 carry) is to patch the wrappers' `datetime.UTC` →
`datetime.timezone.utc`.
