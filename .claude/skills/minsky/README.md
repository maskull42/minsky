# minsky

> **Scoped recovery, 2026-09-05:** researcher-authorised relative-path rules repair the shared
> Write/Edit gate for only the two cellwise-plan round-2 Gemini outputs and a synthetic canary.
> Seven no-provider capability checks passed. All other audit output paths still require explicit
> scoping and verification. No broad mutation permission is enabled. The original text-only failed
> attempt is preserved; see `SKILL.md`, `round-2/recovery-1/runtime-amendment.md` and the 09-05 handoff
> for recovery completion, not the old failed terminal.

A stateful, multi-CLI adversarial-audit harness, documented here as deployed in its reference deployment
(MARS, a PhD project at Vrije Universiteit Amsterdam). Paths, the TITAN reporting step and the work-log step are
deployment-specific; the repository README covers a general installation.

`/minsky <mode> [scope]` triangulates a host synthesizer (Claude Code by default),
Codex (`gpt-5.6-sol`), and OpenCode + `gemini-3.8-flash` at high thinking through a
**sequential deliberation chain** with stable expert personas (the `.minsky/binding.yaml`
`default.active` set — currently 6: marcion-heresiologist, ml-finetuning-phd,
performance-studies-roach, provenance-reproducibility, phd-documentation-currency,
marcion-textual-critic) carrying
**persistent memory across rounds**. Every audit is recorded in a queryable sqlite DB
(`<MARS>/.minsky/audits.db`) with linkage to `documentation/phd_work_log.md` and to
TITAN (Supabase) for NWO reporting.

The name is from Marvin Minsky's *Society of Mind* — many narrow specialists negotiating —
but **stateful**, so personas remember prior rounds and must either strengthen or retract
earlier claims rather than restate them.

---

## When to use

- A finished work product (training pair batch, hermeneutical extraction, dialogue
  scenario, dissertation paragraph, methodology note, code change) that you want
  scrutinized before commit/export/inclusion: `/minsky audit ...`
- A planning artifact you want challenged before any execution: `/minsky plan ...`
- A drafted document section: `/minsky draft ...`
- An evaluation rubric or sample evaluations: `/minsky eval ...`
- A messy area where latent issues may be hiding: `/minsky bug-hunt ...`

In all five modes the deliberation chain is the same; only the per-persona ask
scaffolding (in `modes/<mode>.md`) differs.

---

## Architecture (the deliberation chain)

```
Phase 0  scope-detect.py  — resolves invocation into canonical scope
Phase 1  pack-build.py    — assembles round-N pack.xml (artifact + ask + schema +
                            MARS context + (round 2+) prior rounds + persona memory)

Step 1   Host self-report-and-audit    (host model, in-skill via Write tool;
                                        compatibility path: claude-self/)
           Section A — what was done (factual; no evaluation)
           Section B — per-persona candidate findings (hypotheses for adversaries to test)

Step 2   Codex per-persona walks       (1 codex exec call per active persona)
Step 3   OpenCode per-persona walks    (1 opencode run --agent minsky-reviewer call per
                                        active persona; sees ALL prior step outputs raw)

Convergence  converge.py  — schema-validate + self-consistency check (grep cited file
                            for verbatim quoted_line) + DB write + decision

Step 4   Host synthesis                (host model; maximum reasoning;
                                        compatibility path: claude-synth/)
           Per-finding resolution: addressed | carried_forward | retracted | user_overruled
           If Codex+OpenCode unanimously challenged a Step 1 finding → flag user_decision_required
                                            (Claude does not silently overrule both adversaries)
```

With the six default personae, a round has **12 external CLI model calls** (6 Codex +
6 OpenCode) plus the host's self-audit and synthesis reasoning phases: 14 model phases
in conceptual accounting. Wall-clock is workload- and provider-dependent; the 2026-09-04
Gemini 3.8 audit observed successful OpenCode walks from roughly 4 to 17 minutes and one
explicit 1,800-second recovery override. Do not budget from the older DeepSeek timings.

### Why sequential, not parallel quorum

GodModeSkill (the prior art) runs three agents in parallel and applies lineage-weighted
quorum. We diverge: each step in our chain reads ALL prior outputs RAW. This means
OpenCode can challenge Codex's framing, not just the original artifact (meta-adversarial
review). Verified in V7: OpenCode's marcion-heresiologist round-2 walk independently
investigated MARS's `patristic_sources/marcion.apostolos.adolf_von_harnack.json`,
confirmed that the proposer's Provenance citation was correct as far as it went, and
surfaced TWO new findings the prior round missed (an uncited Gal 1.11 dependency, and
the term "fallen Israel" which has zero attestation anywhere in the MARS corpus).

### Why per-persona serial (not multi-persona-in-one-call)

When Codex is given a single-persona prompt (only marcion-heresiologist instructions, no
ML lens in the prompt at all), it cannot drift to its more comfortable lens. Forces
single-lens depth over multi-lens breadth. It costs N calls per external lineage per round;
register the actual provider usage/cost envelope rather than assuming it is negligible.

### No silent fallbacks

Per MARS CLAUDE.md and your global rigor policy: if a CLI is missing, an agent fails
to produce conforming output, OpenCode's permission grammar can't express what we
need, or any other failure occurs — we surface it loudly and resolve, never paper over
with a degraded path. Rate-limit detection is gated on non-zero exit code (a quoted
phrase about "rate limit" in legitimate audit findings does not trigger pause; only a
real CLI rate-limit error does).

---

## Installation

### Prerequisites

| Tool | Required | Install |
|---|---|---|
| Claude Code | Already running this skill | n/a |
| Codex CLI | Yes | `npm i -g @openai/codex` (verify: `codex --version`) |
| OpenCode | Yes | `npm i -g opencode-ai@latest` (or `brew install anomalyco/tap/opencode` if Xcode CLT is current) |
| Project Python 3.12 | Yes | `<MARS>/.venv/bin/python` (never the macOS system Python) |
| Python `jsonschema` | Yes | installed in the project `.venv` |
| sqlite3 | Yes | system |
| jq | Yes | `brew install jq` |

For unattended `/minsky` runs, start the host Claude Code process with
`claude --dangerously-skip-permissions ...` or
`claude --permission-mode bypassPermissions ...`. The singular
`--dangerously-skip-permission` is not a valid Claude Code flag.

### One-time setup

1. **OpenCode auth (adversarial leg = Gemini 3.8 Flash @ high thinking, 2026-09-04 user-directed switch)**

   OpenCode is the harness; **Gemini 3.8 Flash at HIGH thinking** is the brain inside it (NOT a direct
   API call). It runs through the repo's `opencode.json` `google` provider block
   (`npm: @ai-sdk/google`, `apiKey {env:GOOGLE_GENERATIVE_AI_API_KEY}`), which also pins the thinking
   level — `--variant` is INERT for a config-defined model (opencode's schema exposes only `{disabled}`
   per variant), so `OPENCODE_VARIANT=high` here is PROVENANCE ONLY:

   ```
   provider.google.models."gemini-3.8-flash".options.thinkingConfig.thinkingLevel = "high"
   ```

   Auth: `.env` carries `GOOGLE_API_KEY`; opencode's google provider reads
   `GOOGLE_GENERATIVE_AI_API_KEY` and ONLY that name (wrong name => ProviderAuthError, rc=1, EMPTY
   stderr). `invoke-opencode.sh` synthesises it. The invoke wrapper defaults
   `OPENCODE_MODEL=google/gemini-3.8-flash` and `OPENCODE_VARIANT=high`.
   Verify: `cd <MARS> && opencode models | grep -i gemini` (needs the key exported).

   **Credential source (`MINSKY_OPENCODE_CREDENTIALS`; W10d, 2026-10):**
   - **Unset or `repo-dotenv` (the default):** the behaviour above.
     - The wrapper refuses without `<repo-root>/.env`.
     - It imports only the selected provider's key (google or deepseek) through `credential-value.py`, which parses
       the file and never sources it.
     - It refuses if that key is empty.
   - **`opencode`:** skips the `.env` check, the import and the key check. OpenCode then uses its own configured
     credentials (for example `opencode auth login`), and any provider prefix is allowed.
   - **Any other value** refuses with exit 64 before anything is written. The value is not echoed.
   - **Recording:** the mode is recorded as `credential_source` in the preflight receipt and in the call manifest. No
     credential value or key name is recorded.

   Model history for this leg: `deepseek/deepseek-v4-pro` -> `minimax-coding-plan/MiniMax-M3`
   (2026-07-01) -> `deepseek/deepseek-v4-flash` (2026-08-05) -> `google/gemini-3.7-flash@high`
   (2026-08-18, DeepSeek credit exhausted) -> `google/gemini-3.8-flash@high` (2026-09-04,
   user-directed Minsky default change). Audit records stamp the leg model, so cross-round
   comparisons must stratify by it.

   **To swap again:** set `OPENCODE_MODEL` (+ add the matching provider block to `opencode.json`;
   for a config-defined model set the thinking/effort option there, not via `--variant`).

2. **Custom OpenCode agent definition** at `<MARS>/.opencode/agents/minsky-reviewer.md`
   (already committed to the repo). Under OpenCode 1.18.23, repository-level
   `tools: {"*": false}` otherwise remains a wildcard deny, so the agent explicitly
   re-enables Read/Grep/Glob/Write and then applies credential-path denies. Bash and Edit
   are disabled: shell execution can bypass Read-path and provider-environment boundaries.

   Verify: `cd <MARS> && opencode agent list | grep minsky-reviewer`

   The OpenCode wrapper runs `opencode run --dangerously-skip-permissions`, which
   auto-approves requests not explicitly denied by this agent. Keep credential denies
   after broad allows because OpenCode uses the last matching rule. Verify pack Read and
   Bash denial with no-provider debug calls after any agent/config update.

3. **Initialize the audit DB** (idempotent):
   ```
   <MARS>/.venv/bin/python <MARS>/.claude/skills/minsky/scripts/audit-db.py initdb
   ```

4. **Persona binding** at `<MARS>/.minsky/binding.yaml`:
   ```yaml
   default:
     active: [marcion-heresiologist, ml-finetuning-phd, performance-studies-roach, provenance-reproducibility, phd-documentation-currency, marcion-textual-critic]
   ```

5. **Research-log reporting (reference deployment only).** The reference deployment pushes each closed audit to
   its project tracker through its own work-log skill. That integration (`titan-push.py`) is not shipped in the
   public release; record closed audits in your own research log instead.

---

## Invocation

```
/minsky <mode> [scope-args]
```

`<mode>` is one of: `audit`, `plan`, `draft`, `eval`, `bug-hunt`.

**Scope modes** (all four supported):

| Invocation | Mode | What it audits |
|---|---|---|
| `/minsky audit my-named-task` | explicit | Skill asks you for the artifacts interactively (use this for high-stakes named milestones you'll want to find later in the DB) |
| `/minsky audit` (no args) | delta | git diff from last audit on this branch (queried from audits.db) to HEAD + uncommitted/untracked |
| `/minsky audit path/to/file.md src/pipeline/` | paths | Exactly these paths (uncommitted edits included) |
| `/minsky audit --since "2 hours ago"` | time | git log --since= for that window + uncommitted/untracked |

User confirmation is required before the chain starts. You can adjust scope, add/remove
personas, or cancel at the confirmation step.

---

## Querying audit history

```
/minsky-history list                            # recent 20 audits
/minsky-history list --since "7 days ago"       # past week
/minsky-history list --status disagree          # ended in disagreement
/minsky-history list --unresolved               # have at least one carried_forward finding

/minsky-history show <audit_id>                 # full detail (metadata + findings + provenance)

/minsky-history findings --persona marcion-heresiologist --severity critical
/minsky-history findings --unresolved

/minsky-history unlogged                        # closed but missing work-log/TITAN linkage
```

Add `--json` to any query for machine-readable output.

---

## Bundle layout

```
.claude/skills/minsky/
├── SKILL.md                              entry point (frontmatter + chain protocol prose)
├── README.md                             this file
├── personas/
│   ├── marcion-heresiologist.md          active in v1
│   └── ml-finetuning-phd.md              active in v1
├── modes/{audit,plan,draft,eval,bug-hunt}.md
├── schemas/
│   ├── findings.schema.json              JSON schema for per-finding agent output
│   ├── verdict.schema.json               JSON schema for per-step verdict (consensus.md)
│   └── audit-db.sql                      sqlite DDL (used by audit-db.py initdb)
├── scripts/
│   ├── chain.py                          orchestrates Steps 2+3+converge for one round
│   ├── pack-build.py                     XML pack assembler (humanities-tuned; skips binary artifacts + 200MB abort rail)
│   ├── scope-detect.py                   resolves invocation into canonical scope
│   ├── claude-self-audit.py              prep+validate Step 1 outputs
│   ├── claude-synth.py                   prep+validate Step 4 outputs
│   ├── invoke-codex.sh                   wrap codex exec per (persona) call
│   ├── invoke-opencode.sh                wrap opencode run per (persona) call
│   ├── converge.py                       schema-validate + self-consistency + DB write
│   ├── audit-db.py                       sqlite helpers (initdb/open/close/insert/query)
│   ├── memory-update.py                  append per-persona JSONL memory entries
│   ├── history.py                        backs /minsky-history queries
│   ├── work-log-stub.py                  draft phd_work_log.md entry from audit
│   ├── titan-push.py                     push to TITAN Supabase + record receipt
│   └── destructive-check.sh              PreToolUse hook (extends GodModeSkill regex)
└── examples/
    └── sample-round-output/              committed reference audit cycle

.claude/commands/
└── minsky-history.md                     /minsky-history slash command frontend

.opencode/agents/
└── minsky-reviewer.md                    custom OpenCode agent (Read/Grep/Glob on approved roots;
                                          Write cwd-only; Bash/Edit denied)

.minsky/                                  project-root, NOT under .claude/
├── binding.yaml                          active personas config
└── audits.db                             sqlite (committed; sqlite git-diff driver
                                          recommended; see "git diff cleanliness" below)
```

---

## Persona library

The current `default.active` set (`.minsky/binding.yaml`) is **six personae**: the original two described
below; `performance-studies-roach`, `provenance-reproducibility`, and
`phd-documentation-currency` (promoted 2026-05-28); and `marcion-textual-critic` (added
2026-06-26 and relevance-gated). Each has its own file under `personas/`. The original two:

1. **`marcion-heresiologist`** — second-century Christianity, polemical-source criticism,
   anachronism detection. Catches Nicene-era vocabulary on a 2nd-c figure; conflation of
   polemical caricature with reconstructed position; ignored manuscript variants;
   uncited claims about Marcion's positions.

2. **`ml-finetuning-phd`** — fine-tuning methodology, training-pair quality, eval design,
   dataset leakage, DPO/SFT pitfalls. Catches dataset leakage; evaluation-rubric drift;
   shortcut learning; provenance gaps; quality-gate false positives.

### Adding a future persona

The persona file format is documented (see `personas/marcion-heresiologist.md` as a
template). To add a new persona:

1. Author `personas/<new-name>.md` with the same YAML frontmatter shape.
2. Add `<new-name>` to the `active` list in `<MARS>/.minsky/binding.yaml`.
3. Run `/minsky audit personas-new-name` against the new persona file itself, using the
   existing personas as reviewers (bootstrap-recursion: the system audits its own
   methodology before trusting it). Revise based on findings before going live.

No code changes required. Future expansion candidates the user has discussed include
`rag-engineer` and `philosophy-of-ai`; textual criticism and performance studies are already active.

### Persona file shape

```yaml
---
name: <slug>
expertise: <one-line domain summary>
training-summary: |
  <multi-line>
adversarial-stance: |
  <how this persona engages the artifact under review>
red-flags-must-catch:
  - <bullet>
  - ...
preferred-questions:
  - <bullet>
  - ...
relevance-rubric: |
  <when this persona is on / off; v1 personas are always on>
---
# Persona body

<prose: how to read the audit pack, how to investigate, how to write findings,
what this persona is NOT, rigor norms>
```

---

## What gets written to disk per audit

```
codex-audits/<audit_id>/
├── round-1/
│   ├── pack.xml                                    artifact + ask + schema + MARS context
│   ├── claude-self/
│   │   ├── _step1_checklist.md                     prompt for the host Claude
│   │   ├── report.md                               Section A factual + Section B candidates
│   │   └── findings/
│   │       ├── marcion-heresiologist.json
│   │       ├── ml-finetuning-phd.json
│   │       └── ... one file per active persona
│   ├── codex/
│   │   ├── marcion-heresiologist.json
│   │   └── ... one file per active persona
│   ├── opencode/
│   │   ├── marcion-heresiologist.json
│   │   └── ... one file per active persona
│   ├── converge.json                               structured verdict + agreement matrix
│   ├── consensus.md                                human-readable per-step output
│   └── claude-synth/
│       ├── _step4_checklist.md
│       ├── decisions.md                            per-finding resolutions + concrete changes
│       └── resolutions.json                        machine-readable resolution table
├── round-2/  (if applicable)
│   └── ... same shape; pack.xml includes prior-rounds + persona-memory
└── memory/
    ├── marcion-heresiologist.jsonl                 append-only across rounds
    └── ml-finetuning-phd.jsonl
```

Plus rows in `<MARS>/.minsky/audits.db`:

| Table | Per | Holds |
|---|---|---|
| `audits` | one row per `/minsky` invocation | metadata: timestamps, branch, commits, mode, scope, personas, models, rounds, convergence_status, summary path, work_log path, titan_log_id |
| `findings` | one row per finding (denormalized across rounds and steps) | severity, category, claim, evidence file/line/quoted_line, suggestion, verified flag, resolution |
| `provenance` | one row per LLM call | model, persona, timestamps, duration, token counts, output path, exit_status |

---

## Runtime store and audit lifecycle

Added 2026-10-06 by the lifecycle IMPLEMENT session (`ebef6afd`).
- **Binding plan:** `documentation/plans/minsky_artifact_lifecycle_plan_v0.2_2026-10-01.md` (sha256 `bcf6c72b…4b0f`,
  ratified 2026-10-01).
- **Rulings:** `documentation/plans/minsky_lifecycle_2026-09-30/RULINGS.md`. §2 there is the authoritative store
  specification ("Store location: agreed. make sure this is well documented"). This section restates it for users.
- **Merged** in `d3a889cd` (2026-10-02), `dc897e4c` (2026-10-02) and `7410f8f4` (2026-10-06).
- `SKILL.md` carries the operating summary: the verbs, the gates and the `verify-round` semantics.
- **New bundle files.** The "Bundle layout" tree above predates these files:
  - `scripts/store.py`, `free_space.py`, `byte_sources.py`, `round_verify.py`, `launch-chain.py`, `worktree_guard.py`,
    `minsky-lifecycle.py` and `lifecycle_{register,seal,citations,pack,copies,offload,expire}.py`;
  - `config/store.json`, `free_space.json`, `methodology_triggers.txt` and `real_data_gates.json`;
  - `schemas/verify-round.schema.json` and `round-layout.schema.json`;
  - the `tests/test_lifecycle_*.py` suite.
  - Outside the bundle: `scripts/git-hooks/pre-commit` (GUARD 4) and `scripts/git-hooks/pre-merge-commit`.

### The runtime store

**What it is.** One content-addressed store (CAS) for the whole harness. Files in it are named by their sha256. It
holds the runtime copies every call records as evidence of what ran: program binaries, interpreters and wrapper
scripts.
- These copies replace the per-round `<round>/provenance/runtime-blobs/<sha256>` copies. Those cost ≈151 MB per round,
  and ≈425 MB per round for the Codex-hosted runner.
- **Code-only audits:** only runtime blobs ever enter the store (ruling LC-F1). Their prompts, responses and outputs
  stay in the audit's pack, so `expire` really frees them.
- **Methodology-bearing audits:** `seal` also ingests every hash-bound file of the audit.
  - **Exception:** external referenced context whose current bytes no longer match the recorded sha256. `seal` lists
    it as `context-drifted-not-ingested`; only a copy the store already holds stays covered.

**Where it is.**

| Period | Absolute root | Volume |
|---|---|---|
| Reference deployment, now | `~/Library/Application Support/MARS/minsky-store/` | boot Data volume |
| A new installation | the root that `scripts/setup.sh` writes into `config/store.json` (default `$HOME/Library/Application Support/minsky/store` on macOS, `${XDG_DATA_HOME:-$HOME/.local/share}/minsky/store` elsewhere) | any local volume |
| After the consolidation | `MARS_HOME/runtime/application-support/minsky-store/` (`MARS_HOME = <SSD mount>/MARS`) | the SSD |

**Configuration** (`config/store.json`):
```json
{"store_id": "mars-minsky-cas-v1", "root": "<absolute root>", "enabled": true}
```
- **Override:** the environment variable `MINSKY_STORE_ROOT` (it must be absolute) overrides `root`. The preflight
  receipt records `root_source` (`store.json` or `env:MINSKY_STORE_ROOT`).
- **Disabling:** `enabled: false` is accepted only with `disabled_by` and `reason`. It is an explicit, recorded
  opt-out: `prepare` then writes per-round snapshots under `<round>/provenance/` as before 2026-10-02, and the
  receipt's `store` block records `disabled`, `disabled_by` and `reason`. The no-fallback rule below concerns an
  enabled store that fails.
- **Moving the store** takes one edit to `store.json`, plus the verified copy of the store itself (below).
- **Commands:**
  - `store.py init --root <absolute>` creates a store;
  - `store.py check` prints the resolved configuration and `"validation": "ok"` or the refusal.

**Layout.**
```
<root>/STORE_ID                       the text mars-minsky-cas-v1
<root>/sha256/<aa>/<bb>/<sha256>      the files, read-only (0444)
<root>/index.tsv                      append-only: sha256, bytes, first_source_path, first_seen_utc,
                                      first_audit_id, first_round
```

**Records.**
- Receipts are schema 1.1 with a `store` block.
- Each runtime snapshot record carries its absolute CAS `path` and a `store_ref: {store_id, sha256}`.
  - **Live verification** hashes the recorded absolute path while that file exists. It resolves through `store_ref`
    and the configured root only when the file is absent.
  - **Archive-mode verification** resolves by sha256 and re-hashes the bytes. It looks in the store given as
    `--store`, then the `--pack`, then explicit `--remap` prefixes. It never reads the configured root or the recorded
    live path.
  - (RULINGS §2 states the intent as "verification resolves through `store_ref` and the configured root". As built,
    that holds for live verification of an absent file only. A relocation is therefore proven by an archive run with
    `--store <new root>`.)
- **Tracked anchor:** at each `seal`, the store rows the audit uses are appended to `documentation/minsky_store_index.tsv`.
  Git therefore records which blobs exist and what they hashed to.

**Loud failure.** These conditions stop `provenance.py prepare` **before any provider call**:
- a missing, unmounted or unwritable root;
- a missing or mismatched `STORE_ID`;
- too little free space for an ingest (below).

There is **no fallback** into the round directory.

**Status and protection.**
- The store is a provenance anchor: never delete it, and never delete an individual blob. `expire` never touches CAS
  blobs, because audits share them.
- It is replicated with the audit archives to copy A and copy B, with every file re-hashed at the destination. There
  is no sampling.
- Time Machine is not counted as a copy.

**Who may change it, and how to relocate it.** The location changes only by the researcher's ruling. The move to the
SSD is part of the consolidation's copy → verify → switch protocol:
1. copy the store;
2. re-hash every blob;
3. edit `store.json`;
4. run `verify-round --mode archive --store <new root>` on every round, with `--pack` or `--remap` for the round's
   own files;
5. only then retire the old root.

**State on 2026-10-06 (11:0x CEST, read-only check):** `store.py check` returned `ok`.
- **Contents:** 14 blobs, 151,567,709 bytes.
- **Index rows by first-seen audit:** 11 from the 2026-10-02 smoke audit (one 144,800,738-byte OpenCode binary
  among them), and one ≈25 KB blob each from three later audits.
- **What that shows:** those later audits reused the existing runtime blobs and added only small wrapper variants.

### The audit lifecycle

`scripts/minsky-lifecycle.py` moves a finished audit through
`open → finished → sealed → packed → replicated(n) → offloaded`, with `rehydrated`, `promotion-pending` and, for
code-only audits, `expired`.

**Classes (ruling RT′ 7c).**
- An audit is **methodology-bearing** unless `classify` accepts `code-only`.
- `code-only` needs a statement file, and no trigger may fire. The triggers are:
  - a glob in `config/methodology_triggers.txt`;
  - an unknown input;
  - a work-log trigger;
  - an unanswered `challenge`.

  A recorded `downgrade:` ruling lifts that refusal.

**Deadlines.**
- **LC-SEAL, amended by LC-F2:** within 7 days of `finished`, the audit is sealed, packed and has at least one
  verified off-boot copy.
- **Methodology-bearing audits:** a second copy within 30 days.
- **LC-GRACE:** a code-only raw pack may be expired 90 days after `seal`, when every other condition holds.

**Copies (LC-COPIES).**
- **Copy A:** an off-boot directory passed as `--copy-a-root` (in the reference deployment, an external volume).
  - Its layout is `packs/<audit-id>.tar.zst`, `cas/sha256/<aa>/<bb>/<sha256>` and `freezes/`.
- **Copy B:** the BLOAT-S restic repository.
  - Snapshots tagged `minsky-lifecycle` are permanent: `scripts/mars_offsite_backup.sh` passes `--keep-tag
    minsky-lifecycle` to every `forget`, installed 2026-10-06 in `d3ce1816`.
  - See `documentation/cold_storage_ledger.md`, the row of 2026-10-06.

**Packs.**
- One deterministic `<audit-id>.tar.zst` per audit: PAX members sorted by path.
  - Every member carries `MINSKY.mtime_ns`.
  - Regular-file members also carry `MINSKY.sha256`; directories and internal symlinks do not.
- CAS-resident blobs are listed in `CAS_REFS.tsv` instead of being packed again.

**Files a lifecycle writes inside an audit directory (`<audit>/lifecycle/`):**
- `classification.md`;
- `raw_manifest.tsv`, `digest.json`, `DIGEST.md`, `CONTROLS.tsv` and `verdicts/` (from `seal`);
- `free_space_checks/<sha256>.json` (one per passing free-space check of a lifecycle step);
- `offload_intent.json`, `STUB.json` and `TOMBSTONE.md` (from `offload`);
- `EXPIRED.tsv` (from `expire`).

**Tracked records**, each created by its first real write:
- `documentation/minsky_audit_lifecycle_register.tsv` (every lifecycle event);
- `documentation/minsky_store_index.tsv`;
- `documentation/minsky_audits_db_freezes.tsv` (the `audits.db` freezes taken at `seal` and by the `freeze` verb);
- rows appended by `offload` to `documentation/cold_storage_ledger.md`.

Pre-commit GUARD 4 keeps the three TSVs append-only once they are tracked, and `pre-merge-commit` extends every guard
to clean merges. The commit that first adds a TSV is not content-checked; review it by hand.

**Free space.**
- These steps refuse unless `free − incoming ≥ max(5 GiB, production floor) + 2 GiB` on the volume they write to:
  - every store ingest (in `prepare` and in `seal`);
  - `pack`;
  - `replicate --copy A`;
  - the offload prepare;
  - `rehydrate`;
  - both promotion checks.

  `replicate --copy B` writes to a remote restic repository and makes no local check.
  - With today's production floor (`MIN_FREE_BYTES = 5 * 2**30` in `scripts/r3_continuous.py`), that is 7 GiB
    (7.52 GB).
  - The floor is read by a static parse of that file, never by importing it.
- The check is logged at INFO. A passing check is persisted as follows:
  - `prepare`'s ingest checks go into the preflight receipt's `store` block;
  - the lifecycle steps above write `lifecycle/free_space_checks/<sha256>.json`, cited as `free_space_sha256:` in
    their register row.
  - **Gap (found 2026-10-06, not yet fixed):** `seal`'s ingest check is logged but not persisted.

**Real-data gates.** `config/real_data_gates.json` must record `met: true` with evidence, on the researcher's ruling:
- **G1** before any real `seal`;
- **G2** before any real `replicate`, `promote` copy or `offload`;
- **G3** before `census --record`.

On 2026-10-06 all three are unmet.

---

## Pack ordering and humanities-context choices

GodModeSkill (the prior art) hardcaps packs at 800 KB. **We strip that cap.** Humanities
audits routinely need tens of thousands of lines of source material (multiple manuscript
variants, scholar opinions, reconstructed-text variants). Pack size must be registered
against the current model/provider context and cost envelope; an older provider's context
window is not a standing guarantee.

The pack ordering (critical content in first ~2000 lines; reference material after) is
a **navigation aid for the reviewer agent's first Read call**, NOT a token cap. The
agent's first read orients it; subsequent reads chunk-traverse the reference material
as needed.

Round 2+ packs preserve the prior round's reference material with `<unchanged-since-round-1>`
annotations rather than dropping it (the user's stated preference for humanities work).
Net effect: round-2 packs are *larger* than round-1 packs, not smaller — fine given the
context window.

---

## Self-consistency hallucination check

Lifted verbatim from GodModeSkill (`work-converge.py` lines 232–274), with two
refinements for humanities-corpus realities:

1. **Whitespace-normalized exact match** at the cited line ± a small off-by-one window,
   then whole-file fallback.
2. **Substring containment** (≥30 chars + ≥70% overlap) — catches real citations that
   differ only in trailing punctuation, JSON-quote escaping, or partial-line excerpts,
   while still requiring meaningful overlap.

Findings whose verbatim `quoted_line` cannot be located in the cited file are marked
`verified=false`.

**Evidence must lie inside the repository (since 2026-10-06; ruling W10-H).** This is a back-port of public v1.0.1's
hardening.
- A cited `file_path` is checked only when it resolves inside the repo root, symlinks followed. A relative path must not
  escape the root.
- Otherwise the finding is `verified=false`, with `_verification_error`, unless the run passes
  `--allow-external-evidence` (`converge.py`, `chain.py adversaries`, or `launch-chain.py … --`).
- `converge.json`'s `evidence_scope` and a line in `consensus.md` record whether external evidence was allowed and how
  many citations were refused.
- `scope-detect.py` likewise refuses audit paths outside the repo unless `--allow-external` is passed, and records the
  setting as `allow_external` in the scope JSON. In V6 (round 1), all 19 findings verified after the substring fix
caught a real Greek-text citation that differed only by a trailing comma.

This catches one specific failure mode: **hallucinated citations** (an agent
claiming "line 42 of file X says Y" when the file's line 42 doesn't say Y). It
does NOT verify that the *interpretation* of a real cited line is correct, that
the line is being read in proper rhetorical context, or that the polemical
framing of a Tier C/D witness is being correctly distinguished from a Tier A
direct quotation. For those failure modes, the persona's lens (red-flags +
preferred-questions) is the defense, and ultimately human review is required.

In V6 + V7 + the bootstrap-recursion self-audit, the check correctly flagged
unverified findings (including some of the host Claude's own Step 1 candidate
findings — a useful failure mode where the system catches its own author).

---

## Integration with MARS conventions

| MARS convention | minsky integration |
|---|---|
| `documentation/phd_work_log.md` (mandatory work log) | `work-log-stub.py draft|append` generates the entry from a closed audit row |
| TITAN Supabase push (mandatory NWO reporting) | `titan-push.py preview|push` authenticates + inserts + records receipt |
| `codex-audits/` dated subdir convention | minsky writes into `codex-audits/<audit_id>/` |
| `logs/api_costs.jsonl` provenance | provenance rows in `audits.db`; raw provider logs remain authoritative for cost/cache/reasoning fields the DB does not yet preserve |
| `phd_project_context/condensed_phd_context.md` | embedded as `<phd-frame>` block in every pack |
| `DOCUMENTATION_DRIFT_REGISTER.md` | intended `<doc-drift-warnings>` input; current configured register is archived/stale and round 1 silently omitted the block, so round 2 must record an explicit current status rather than claim it was embedded |
| Pre-existing `.claude/agents/` (e.g. `patristic-layer3-reviewer`) | not yet wired in; v2 enhancement could route Step 1 per-persona to a domain-tuned subagent |

---

## Verification record (V1–V11)

The plan defined 11 numbered verification tests. As of the last release:

| V | Test | Status |
|---|---|---|
| V1 | Agent-harness contract for Codex and the then-configured OpenCode provider | ✅ PASS (historical; re-probe permissions/model identity after provider or OpenCode changes) |
| V2 | Schema-conforming agent output | ✅ PASS (all 19 round-1 + 15 round-2 V6/V7 findings validated against schema) |
| V3 | Self-consistency check flags non-existent quoted_line as verified=false | ✅ PASS |
| V4 | Audit DB initdb/open/close/query | ✅ PASS |
| V5 | All four scope modes (explicit / delta / paths / time) | ✅ PASS |
| V6 | End-to-end smoke test on **synthetic verification fixture** (`.minsky/test-artifacts/v6-synthetic.md` — explicitly NOT real PhD work; planted defects for verification) | ✅ PASS — all 4 reviewers converged on planted issues; Codex caught a finding the host missed; OpenCode independently investigated `patristic_sources/` |
| V7 | Round-2 stateful behavior — personas retract or strengthen, never restate | ✅ PASS — after partial fix, agents correctly retracted addressed findings, carried forward unaddressed ones, surfaced new findings on revised material ("fallen Israel" with zero MARS-corpus attestation) |
| V8a | Loud failure on missing CLI | ⏸ deferred — principle implicitly proven by V6 rate-limit false-positive incident |
| V8b | Rate-limit simulation | ⏸ deferred — same as V8a |
| V9 | `/minsky-history` query subcommands | ✅ PASS |
| V10 | Mode coverage smoke tests for plan/draft/eval/bug-hunt | ⏸ deferred — only audit mode tested in V6/V7; the four other mode templates are authored but untested end-to-end |
| V11 | TITAN push (real network call) | ✅ PASS — daily_logs id `d3bec7c7-5683-4616-abe0-40d59f4c07a1` |

---

## Known epistemic limits

These are real and should be documented in your dissertation methods chapter when you
describe the system.

1. **A model "playing" a marcion-heresiologist is not actually a marcion-heresiologist.**
   The persona is a prompt-shaped lens; the underlying model has whatever it has from
   pre-training. Mitigation: every finding requires verbatim source citation; the
   self-consistency check verifies citations against actual files; the bootstrap-recursion
   protocol audits the persona files themselves before trusting them in production.

2. **Anchoring bias in sequential chains.** Each later step CAN over-weight prior steps'
   framings. Mitigated by: Section A/B split (Section B is presented as hypotheses, not
   assertions); explicit prompt instructions in Steps 2/3 to "test prior critiques
   against the original artifact, not accept them"; the original artifact is included
   verbatim in every step's prompt. Not eliminable in a sequential design.

3. **The agent's investigation is bounded by what's locally accessible.** OpenCode in V7
   correctly investigated MARS's own patristic_sources/ — but if a persona-relevant
   source isn't in the repo, the agent can't reach it. For external scholarly
   verification, human-in-the-loop is still required.

4. **Persona-memory token bloat over many rounds.** Each round appends to the JSONL
   journal. After ~5 rounds, prompts may strain even Gemini's 1M context. v2
   enhancement: summarize older memory entries via a small Claude call before injection.

5. **Branch-context for delta-mode scope detection.** "Since last audit" assumes a
   branch workflow. On `main`-only workflow, "since last audit" effectively means
   "since the last audit ever," which may be too broad for routine use.

6. **Cost is not normalized into the audit DB.** OpenCode raw events carry per-step
   cost and cache/reasoning usage, but the DB currently stores only uncached input and
   visible output aggregates; Codex's unsplit `tokens used` total is currently stored
   as output tokens. Treat DB token columns as incomplete until repaired, and derive any
   dollar figure from a de-duplicated immutable event-log inventory with the limitation
   stated. The 2026-09-04 audit did exactly that; it did not treat the sum as an invoice.

7. **Pretraining overlap with MARS source corpus.** The underlying LLMs (including
   Opus, GPT-5.6 Sol, and Gemini 3.8 Flash) plausibly have MARS's primary-source corpus material in
   their pretraining distributions — Tertullian, Epiphanius, Adamantius, and other
   patristic texts are public-domain and almost certainly appear in Common Crawl. When
   such an LLM "audits" an artifact derived from that same corpus, the verification
   may be partially leaking signal from pretraining rather than from independent
   evaluation. The self-consistency check verifies citation-existence (mitigates
   hallucination) but does not address this deeper concern. Mitigation: human-in-the-
   loop verification of high-stakes findings before any consequential action; treating
   the chain's outputs as candidate concerns to be confirmed by a human reviewer with
   independent access to primary sources, not as final adjudications.

8. **`verified=true` is a citation-location check, not claim verification.** The
   convergence helper only confirms that an approximately matching quoted line exists
   near the cited location. The host must still test the inference against code, data,
   and methodological context; it may narrow or reject an auditor's severity/remedy.

9. **Historical audit provenance has known gaps (through 2026-09-04).** Exact prompts were not automatically
   persisted, per-call OpenCode model stamps omit provider/effort, missing usage can be
   zero-filled, and host self/synthesis phases lack per-call DB provenance. A recovered
   JSON file can also exist beside an original `error` call. Until those are repaired,
   use affected rounds as discovery evidence and preserve the raw failures; do not label
   them perfectly replayable. The 2026-09-05 prospective hardening below does not repair old evidence.

### Prospective call records (2026-09-05)

New rounds require immutable `round-scope.json` with explicit personas, model@effort lists and a phase/model
map (`claude_self`, `codex`, `opencode`, `claude_synth`). `provenance.py register-round` creates it;
`chain.py adversaries` requires matching `--models` and `--step-models`. This permits an explicitly
authorised narrower round or different host without rewriting the original audit row.

External wrappers persist exact prompts and pre-dispatch prompt/context/runtime hashes, then unique raw
logs, snapshots and terminal sidecars. Hash drift, wrong-phase model, incompatible exit state and invalid
usage block convergence. OpenCode usage includes input/output/cache/reasoning/total and reported cost;
Codex aggregate-only usage is never mislabeled output. Missing prices/telemetry remain unavailable.
Each external walk has an explicit timeout plus termination grace; register overrides before the round.

Host self/synthesis uses `provenance.py prepare` then `record` to bind visible task notes, the pack/upstream
outputs and completed artifacts. An Astra host is recorded as Astra, not Sol or Claude merely because of
compatibility paths. Host-only unavailable transcript/exit-code/usage options are honest limitations;
these records are local reasoning-phase artifact records, not reconstructed full provider requests.
The DB remains a compact query index; sidecars hold the complete observable record. No exact remote
replay or full capture of hidden/global client state is claimed.

Convergence's stable finding index collapses only exact duplicates within a source identity. Synthesis
must cover every UID exactly once with the original claim and a reasoned resolution. Semantic clustering,
the truth of findings, severity and remedies are the host adjudicator's job, never a file-name heuristic.
The missing drift register now produces an explicit status instead of disappearing from the pack.
See `SKILL.md` for the invocation contract and `tests/test_hardening.py` for synthetic regression coverage.

---

## git diff cleanliness for `.minsky/audits.db`

The audit DB is committed to git for dissertation auditability. Sqlite files diff
poorly by default. **Recommended one-time setup** (not yet automated):

```bash
git config diff.sqlite3.binary true
git config diff.sqlite3.textconv 'sqlite3 "$1" .dump'

cat >> <MARS>/.gitattributes <<'EOF'
.minsky/audits.db diff=sqlite3
EOF
```

After this, `git diff .minsky/audits.db` shows readable SQL dumps instead of binary diff
markers.

---

## What landed in v1.1 (2026-05-03)

- **OpenCode timeout hardening** — `opencode.json` sets the Google provider
  `timeout=1200000` ms; Gemini 3.8's model block caps output at 65,536 tokens.
  `invoke-opencode.sh` wraps `opencode run` in GNU timeout and currently defaults
  to `timeout --kill-after=60s 1020s`. It detects exit codes 124/137 and reports
  `EXIT_STATUS="timeout"`. The historical 1,800-second bound addressed the V11
  DeepSeek-no-response hang; it was used again only as an explicitly disclosed
  recovery override on 2026-09-04 after an active Gemini investigation hit 1,020s.
  Tunable per invocation via
  `OPENCODE_TIMEOUT_SECONDS`, `OPENCODE_KILL_GRACE_SECONDS`,
  `OPENCODE_MODEL`, and optional `OPENCODE_VARIANT` env vars.

- **Durable adversary logs + schema gate** — invoke wrappers now delete stale
  persona JSON before each fresh walk, persist raw logs under `_opencode_logs/`
  or `_codex_logs/`, and run `scripts/validate-findings.py` before marking a
  walk `ok`. If the agent writes malformed JSON, progress/provenance records
  `schema-invalid` and the chain halts before convergence.

- **Permission-bypass launch mode** — `invoke-codex.sh` runs
  `codex exec --dangerously-bypass-approvals-and-sandbox` (the explicit form of
  the `--yolo` alias); `invoke-opencode.sh` runs
  `opencode run --dangerously-skip-permissions`. The host Claude Code session
  should be started with `--dangerously-skip-permissions` or
  `--permission-mode bypassPermissions` for fully unattended operation.

- **OpenCode permission tightening** — `minsky-reviewer` uses OpenCode's
  documented last-match rule: research-root allows first, credential denies last.
  OpenCode 1.18.23 additionally requires explicit agent-local tool enablement to
  overcome the repository's global wildcard deny. Bash/Edit remain disabled; a
  shell is not a safe substitute for a denied Read operation.

- **Targeted recovery flags** — `chain.py adversaries` accepts
  `--only-step codex|opencode`, repeatable/comma-separated `--only-persona`,
  and `--resume-existing`. Use these after an incomplete run to retry only the
  failed lineage while preserving valid existing outputs.

- **Real-time progress instrumentation** — every audit emits structured
  NDJSON events to `codex-audits/<audit-id>/progress.ndjson`:
    - `audit-db.py` (open/close) → `audit_open`, `audit_close`
    - `chain.py` → `step_start`, `step_done`, `converge_done`, `error`
    - `invoke-codex.sh` / `invoke-opencode.sh` → `persona_walk_start`,
      `persona_walk_done` (with `verdict`, `finding_count`,
      `severity_breakdown`, `exit_status`)
    - host (via `progress.py emit ...` calls in SKILL.md) → Step 1 / Step 4
      step boundaries
  Schema documented in `schemas/progress.schema.json` (JSON Schema 2020-12)
  and `schemas/progress.md` (prose spec). Append-only; atomic per line under
  the POSIX-atomic O_APPEND limit. A parent-supervising session can tail the
  formatted stream via `scripts/progress-tail.py <audit-id>`.

  Rationale: the V11 hang and the 2026-05-03 outline-audit hang were both
  invisible until manually diagnosed because the audit's runtime state was
  unobservable from the parent session. The instrumentation makes silence
  detectable (no `persona_walk_done` event after the expected duration =
  hang) and turns audit progress into a citable runtime trace.

## What's NOT in v1 (deferred to v1.2 / v2 / dissertation methods chapter)

**Surfaced by the bootstrap-recursion self-audit (`minsky-self-audit-2026-04-29`); see
the `decisions.md` for that audit for full rationale.**

### Implementation gaps (v1.2 candidates)

- **Broader `user_decision_required` detection in `converge.py`.** Currently only
  catches the IMPLICIT-retraction case (Codex + OpenCode silent on a Claude finding).
  Does NOT catch EXPLICIT challenges where adversaries return findings explicitly
  contradicting Claude's. Non-trivial heuristic to design well; revisit when production
  use surfaces how often this matters.
- **Finding-level agreement matrix.** The agreement matrix in `converge.json` stores
  only top-level verdict strings per (step, persona) — cannot compute finding-level
  inter-rater reliability or substantiate "reviewers converged on the same specific
  findings" claims. v1.2 could add text-similarity matching across personas/models.
- **MCP migration of the agent integration** (Bash subprocess + JSON file I/O is the
  current pattern — auditable, sufficient).
- **Per-mode persona binding overrides** (only default binding in v1).
- **Per-invocation `--personas a,b,c` override** (small addition).
- **Domain-specific Claude subagent routing in Step 1** (use MARS's existing
  `.claude/agents/` like `patristic-layer3-reviewer` for tuned domain framing).
- **Memory summarization for very-long-running audits** (>5 rounds; pack growth).
- **`parallel-within-step` flag for invoke wrappers** (currently sequential per persona;
  could `xargs -P` later).
- **Sqlite git-diff driver** auto-installed (currently a manual one-time setup;
  see "git diff cleanliness" section).

### Methodological work tied to the dissertation methods chapter

- **Comparative baseline study.** minsky's value claim — that 3-lineage sequential
  triangulation catches what simpler approaches miss — is supported only by the V6
  anecdote (Codex caught a finding the host missed) and the bootstrap-recursion result
  (Codex + OpenCode converged on 4 defects independently). A defensible methods
  chapter needs: take 5-10 real PhD artifacts, run each through (a) host-only,
  (b) host + 1 adversary, (c) parallel quorum, (d) the current sequential chain;
  measure number of unique findings, precision against expert review, reviewer
  disagreement, cost. Without this study, "sequential meta-review beats parallel" is
  asserted, not demonstrated.
- **Inter-rater reliability protocol.** Tied to the baseline study. Per-audit kappa
  coefficients, persona-pair agreement statistics, comparison against expert review
  on a held-out set.
- **External scholarly review of the persona files themselves.** The personas are the
  methodological artifact most exposed to scholarly challenge in defense. Internal
  bootstrap-recursion (minsky audits its own personas) is a useful sanity check but is
  not a substitute for an external Marcion scholar reviewing the marcion-heresiologist
  persona file, or an external ML methods reviewer reviewing the ml-finetuning-phd
  file.

### Persona library expansion (when the work calls for it)

- **marcion-textual-critic is active** — use it for artifacts involving manuscript
  variants, apparatus readings, and philological precision in Greek/Latin/Syriac; it is
  already in the six-persona default binding.
- **rag-engineer persona** — for retrieval/embedding/reranking quality concerns.
- **philosophy-of-ai persona** — for epistemological warrant claims (the dissertation's
  "epistemologically productive performances" framing).
- **performance-studies-roach persona** — for surrogation-theory grounding when
  performance metaphors are load-bearing.

The persona file format is documented (above) so any of these can be added without
code changes — author the file, add to `.minsky/binding.yaml`, and run
`/minsky audit personas-<new-name>` against it (using existing personas as reviewers)
before going live.

---

## Reference: where things live

| Topic | File / docs |
|---|---|
| Plan / design rationale | the reference deployment's design plan (not shipped) |
| Skill entry point | `.claude/skills/minsky/SKILL.md` |
| Audit DB schema | `.claude/skills/minsky/schemas/audit-db.sql` |
| Findings schema (agent output contract) | `.claude/skills/minsky/schemas/findings.schema.json` |
| Persona file format | `.claude/skills/minsky/personas/marcion-heresiologist.md` (use as template) |
| Mode ask scaffolding | `.claude/skills/minsky/modes/<mode>.md` |
| GodModeSkill upstream (verbatim lifts) | https://github.com/99xAgency/GodModeSkill — `work-pack-build` lines 300–416 (XML schema, pack ordering); `work-converge` lines 232–274 (self-consistency); `work` line 490 (destructive regex) |
| MARS CLAUDE.md — work log + TITAN protocol | `<MARS>/CLAUDE.md` |
| Worked example | `.claude/skills/minsky/examples/sample-round-output/README.md` — annotated walk-through of a verification cycle |

---

*minsky is **candidate** methodology infrastructure for the MARS PhD — intended
for citation in the dissertation methods chapter as part of the adversarial-
validation apparatus that aims to distinguish "model output that happens to
look defensible" from "model output that has survived structured multi-perspective
scrutiny against verifiable evidence." Whether it earns that citation depends on
how it performs on real PhD work, on the planned baseline-comparison study (see
"What's NOT in v1" below), and on external scholarly review of the persona files
themselves. Use it as a working tool, not as established methodology.*
# Recorder remediation, 2026-09-05

Prospective calls require positive external usage, strict NDJSON with separate OpenCode stderr,
latest-prepared-attempt reconciliation and all output siblings. Pre-call runtime blobs preserve captured
bytes across upgrades; old receipts retain explicit historical limitations. Credential files are parsed
only for selected keys, never sourced as configuration. Audit metadata placeholders are migrated only
with the scoped backed-up `migrate-usage-20260905.py` helper and evidence note. New/closed audit aggregates
default to unavailable NULL. Synthetic verification: 28 tests. Full native executable closure remains
separate work. The cellwise plan audit closed by researcher override with no round 3; no paid launch implied.

# Erratum, 2026-10-06: `.minsky/audits.db` is to be untracked (ruling AD-B)

Appended, not edited in place, as plan v0.2 §5 item 7 requires.

**What it corrects:**
- "Bundle layout" above says `audits.db … sqlite (committed; …)`.
- "git diff cleanliness" says "The audit DB is committed to git for dissertation auditability."

Both are superseded by the researcher's ruling **AD-B**, given first-hand in the bloat session and received
≤ 17:43:01 CEST, 30 Sep 2026: "Untrack audits.db going forward". The researcher chose it over the recommended AD-A.

**What the ruling means:**
- `.minsky/audits.db` leaves the index going forward (`git rm --cached` plus an ignore rule). Its historical
  versions stay in git history.
- It is preserved by **freezes**: SQL-dump or snapshot copies in `~/Library/Application Support/MARS/frozen/`, with
  their hashes in git.
  - The lifecycle `seal` takes a freeze and appends it to `documentation/minsky_audits_db_freezes.tsv`.
  - Since 2026-10-08 the `freeze` verb (ruling AD-B-F) also freezes outside `seal`.
    - `freeze --reason ad-b` makes the AD-B freeze.
    - `freeze --reason campaign-close --label <campaign>` makes the ruled campaign-close freeze.
    - It is not behind G1, which it serves.
  - The tracked lifecycle register (`documentation/minsky_audit_lifecycle_register.tsv`) is authoritative.
  - Plan v0.2 §7.1 also gives `audits.db` a rebuildable mirror table of the register. **That mirror is not
    implemented as of 2026-10-06; the TSV is the only register.**

**Status on 2026-10-06: NOT executed.**
- The file is still tracked: `git ls-files` lists it, and no ignore rule matches it.
- The untrack is the bloat session's step, and it never runs while an audit is writing the database. It belongs to
  real-data gate G1, together with off-device freeze replication and one demonstrated restore. Until G1 is met,
  `seal` refuses real audits.
- Until the untrack, stage `.minsky/audits.db` only at sanctioned checkpoints, never with `git add -A`.
- **2026-10-08:** the bootstrap circularity is resolved by the `freeze` verb. The only freeze producer had been `seal`,
  which G1 blocks. AD-B itself is still not executed.

**Unchanged:**
- `scripts/destructive-check.sh:61` keeps `.minsky/audits.db` among the protected paths.
- The sqlite git-diff driver advice above remains useful for reading the historical versions.
