# minsky

A stateful, multi-CLI adversarial-audit harness.

`/minsky <mode> [scope]` triangulates Claude Code, Codex, and OpenCode through
a **sequential deliberation chain** with stable expert personas carrying
**persistent memory across rounds**.
Every audit is recorded in a queryable sqlite DB (`.minsky/audits.db` at the project
root) with optional linkage to a per-project research log.

The name is from Marvin Minsky's *Society of Mind* — many narrow specialists negotiating —
but **stateful**, so personas remember prior rounds and must either strengthen or retract
earlier claims rather than restate them.

## Public release v1.0.0 — what's in this distribution

Minsky was developed for a specific dissertation project (MARS — early-Christian
heterodoxy reconstruction at Vrije Universiteit Amsterdam). This public release
ships the harness and three example personas; the dissertation-specific source
corpus, audit history, and provenance integrations remain project-internal:

- **Three personas** are shipped: `marcion-heresiologist` (heresiology /
  patristic source criticism), `ml-finetuning-phd` (LLM evaluation methodology),
  and `performance-studies-roach` (performance theory / surrogation). Each is
  composed for a specific research domain; treat them as worked examples for
  composing your own personas.
- **`personas/source-corpus/` files are placeholders.** The works listed there
  were relevant to the writing of one specific paper in the reference deployment.
  They are retained only as examples of how a persona can point to local source
  material. They are not required for minsky itself. To run a similar persona
  with source-grounded fidelity, supply your own legally obtained source corpus.
- **Documentation throughout uses MARS as a real-world example.** Specific
  references to `patristic_sources/`, `condensed_phd_context.md`, and similar
  are documented as the original use case; adapt for your own project.

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
                            project context + (round 2+) prior rounds + persona memory)

Step 1   Claude self-report-and-audit  (host Claude, in-skill via Write tool)
           Section A — what was done (factual; no evaluation)
           Section B — per-persona candidate findings (hypotheses for adversaries to test)

Step 2   Codex per-persona walks       (1 codex exec call per active persona)
Step 3   OpenCode per-persona walks    (1 opencode run --agent minsky-reviewer call per
                                        active persona; sees ALL prior step outputs raw)

Convergence  converge.py  — schema-validate + self-consistency check (grep cited file
                            for verbatim quoted_line) + DB write + decision

Step 4   Claude synthesis              (host Claude; ultrathink)
           Per-finding resolution: addressed | carried_forward | retracted | user_overruled
           If Codex+OpenCode unanimously challenged a Step 1 finding → flag user_decision_required
                                            (Claude does not silently overrule both adversaries)
```

LLM calls per round (with default 2 active personas): **6** = 1 Claude self + 2 Codex +
2 OpenCode + 1 Claude synth. Wall-clock typically 25–35 min/round (Codex ~3–4 min,
OpenCode ~7–9 min — agents do real multi-step investigation, not
one-shot prompt-response).

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
single-lens depth over multi-lens breadth. Costs N more API calls per agent per round —
acceptable when using sufficiently capable/large-context models.

### No silent fallbacks

Per the project's rigor norms: if a CLI is missing, an agent fails
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
| Python 3.11+ | Yes | system |
| Python `jsonschema` | Yes | `python3 -m pip install jsonschema` if absent |
| sqlite3 | Yes | system |
| jq | Yes | `brew install jq` |

Unsafe unattended mode is optional. By default, run with normal tool approvals.
Only use CLI sandbox/approval bypass flags in a trusted local repository after
reviewing the scope and understanding that agents may read or write local files.

### One-time setup

1. **OpenCode auth + provider config**

   Configure OpenCode for the provider/model you want to use. minsky invokes
   OpenCode through the `minsky-reviewer` agent. If your OpenCode setup requires
   an explicit model selector, export it before running minsky:

   ```bash
   export OPENCODE_MODEL='provider/model'
   ```

   Verify:

   ```bash
   cd <project-root> && opencode models
   ```

2. **Custom OpenCode agent definition** at `<project-root>/.opencode/agents/minsky-reviewer.md`.
   The public release ships a templated version under `.opencode/agents/minsky-reviewer.md`
   with `$HOME`-prefixed permission rules; edit the `read:` and `external_directory:`
   allow rules to match your research workspace path(s). Read-only outside cwd; write
   only inside cwd; bash permission policy denies destructive patterns (rm -rf, sudo,
   git push --force, DROP TABLE, etc.) and allows safe operations.

   Verify: `cd <project-root> && opencode agent list | grep minsky-reviewer`

   Keep the explicit deny rules for secrets and destructive commands intact. Use
   unattended bypass flags only in a trusted local repository after reviewing the
   scope.

3. **Initialize the audit DB** (idempotent):
   ```
   python3 <project-root>/.claude/skills/minsky/scripts/audit-db.py initdb
   ```

4. **Persona binding** at `<project-root>/.minsky/binding.yaml` (see
   `binding-example/binding.yaml.example` for a template):
   ```yaml
   default:
     active: [marcion-heresiologist, ml-finetuning-phd]
   ```

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

/minsky-history unlogged                        # closed but missing research-log linkage
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
│   ├── pack-build.py                     XML pack assembler (humanities-tuned: no size cap)
│   ├── scope-detect.py                   resolves invocation into canonical scope
│   ├── claude-self-audit.py              prep+validate Step 1 outputs
│   ├── claude-synth.py                   prep+validate Step 4 outputs
│   ├── invoke-codex.sh                   wrap codex exec per (persona) call
│   ├── invoke-opencode.sh                wrap opencode run per (persona) call
│   ├── converge.py                       schema-validate + self-consistency + DB write
│   ├── audit-db.py                       sqlite helpers (initdb/open/close/insert/query)
│   ├── memory-update.py                  append per-persona JSONL memory entries
│   ├── history.py                        backs /minsky-history queries
│   ├── work-log-stub.py                  optional research-log helper
│   └── destructive-check.sh              PreToolUse hook (extends GodModeSkill regex)
└── examples/
    └── sample-round-output/              committed reference audit cycle

.claude/commands/
└── minsky-history.md                     /minsky-history slash command frontend

.opencode/agents/
└── minsky-reviewer.md                    custom OpenCode agent (read-only outside cwd)

.minsky/                                  project-root, NOT under .claude/
├── binding.yaml                          active personas config
└── audits.db                             sqlite (committed; sqlite git-diff driver
                                          recommended; see "git diff cleanliness" below)
```

---

## Persona library

Three example personas ship with the public release. The active set is configured
in `.minsky/binding.yaml`.

1. **`marcion-heresiologist`** — second-century Christianity, polemical-source criticism,
   anachronism detection. Catches Nicene-era vocabulary on a 2nd-c figure; conflation of
   polemical caricature with reconstructed position; ignored manuscript variants;
   uncited claims about Marcion's positions.

2. **`ml-finetuning-phd`** — fine-tuning methodology, training-pair quality, eval design,
   dataset leakage, DPO/SFT pitfalls. Catches dataset leakage; evaluation-rubric drift;
   shortcut learning; provenance gaps; quality-gate false positives.

3. **`performance-studies-roach`** — performance theory, biopic studies,
   Stanislavsky-system scholarship, and surrogation theory. Catches unsupported
   theory transfers, practitioner-voice tier errors, and source-corpus grounding gaps.

### Adding a future persona

The persona file format is documented (see `personas/marcion-heresiologist.md` as a
template). To add a new persona:

1. Author `personas/<new-name>.md` with the same YAML frontmatter shape.
2. Add `<new-name>` to the `active` list in `<project-root>/.minsky/binding.yaml`.
3. Run `/minsky audit personas-new-name` against the new persona file itself, using the
   existing personas as reviewers (bootstrap-recursion: the system audits its own
   methodology before trusting it). Revise based on findings before going live.

No code changes required. Future expansion candidates the user has discussed:
`textual-criticism`, `rag-engineer`, and `philosophy-of-ai`.

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
│   ├── pack.xml                                    artifact + ask + schema + project context
│   ├── claude-self/
│   │   ├── _step1_checklist.md                     prompt for the host Claude
│   │   ├── report.md                               Section A factual + Section B candidates
│   │   └── findings/
│   │       ├── marcion-heresiologist.json
│   │       └── ml-finetuning-phd.json
│   ├── codex/
│   │   ├── marcion-heresiologist.json
│   │   └── ml-finetuning-phd.json
│   ├── opencode/
│   │   ├── marcion-heresiologist.json
│   │   └── ml-finetuning-phd.json
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

Plus rows in `<project-root>/.minsky/audits.db`:

| Table | Per | Holds |
|---|---|---|
| `audits` | one row per `/minsky` invocation | metadata: timestamps, branch, commits, mode, scope, personas, models, rounds, convergence_status, summary path, research-log path |
| `findings` | one row per finding (denormalized across rounds and steps) | severity, category, claim, evidence file/line/quoted_line, suggestion, verified flag, resolution |
| `provenance` | one row per LLM call | model, persona, timestamps, duration, token counts, output path, exit_status |

---

## Pack ordering and humanities-context choices

GodModeSkill (the prior art) hardcaps packs at 800 KB. **We strip that cap.** Humanities
audits routinely need tens of thousands of lines of source material (multiple manuscript
variants, scholar opinions, reconstructed-text variants). Large-context reviewer models
can make large packs affordable.

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
`verified=false`. In V6 (round 1), all 19 findings verified after the substring fix
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

## Integration with project conventions

The harness was built around a specific dissertation project's conventions; the
table below documents those conventions as a worked example. Adapt for your
own project's naming and provenance practices.

| Convention | minsky integration |
|---|---|
| Project research log | `work-log-stub.py draft|append` can be adapted to generate an entry from a closed audit row |
| `codex-audits/` dated subdir convention | minsky writes into `codex-audits/<audit_id>/` |
| Provenance | local sqlite provenance rows in `audits.db` |
| `phd_project_context/condensed_phd_context.md` | embedded as `<phd-frame>` block in every pack if present (graceful fallback if absent) |
| `DOCUMENTATION_DRIFT_REGISTER.md` | embedded as `<doc-drift-warnings>` block if present (reviewers discount findings anchored on stale docs) |
| Pre-existing `.claude/agents/` (domain-tuned subagents) | not yet wired in; v2 enhancement could route Step 1 per-persona to a domain-tuned subagent |

Project-local provenance integrations from the reference deployment are not part
of the public release. The public distribution keeps local sqlite provenance only.

---

## Verification record (V1–V11)

The plan defined 11 numbered verification tests. As of the last release:

| V | Test | Status |
|---|---|---|
| V1 | Agent-harness contract for both Codex and OpenCode | ✅ PASS |
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
Project-local provenance integrations from the reference deployment are not part
of the public verification record.

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
   correctly investigated the project's primary-sources directory — but if a
   persona-relevant source isn't in the repo, the agent can't reach it. For external
   scholarly verification, human-in-the-loop is still required.

4. **Persona-memory token bloat over many rounds.** Each round appends to the JSONL
   journal. After ~5 rounds, prompts may strain even large-context models. v2
   enhancement: summarize older memory entries via a small Claude call before injection.

5. **Branch-context for delta-mode scope detection.** "Since last audit" assumes a
   branch workflow. On `main`-only workflow, "since last audit" effectively means
   "since the last audit ever," which may be too broad for routine use.

6. **Cost is not currently tracked in $.** Token counts are recorded in `provenance`
   for computational-provenance purposes (useful in the methods chapter) but no dollar
   arithmetic. Revisit if this becomes a constraint. Codex token totals are
   captured from stderr; OpenCode input
   and output tokens are parsed from `opencode run --format json` step-finish events.

7. **Pretraining overlap with project source corpora.** The underlying LLMs used
   through Claude Code, Codex, and OpenCode plausibly have public-domain source material in
   their pretraining distributions — Tertullian, Epiphanius, Adamantius, and other
   patristic texts are public-domain and almost certainly appear in Common Crawl. When
   such an LLM "audits" an artifact derived from that same corpus, the verification
   may be partially leaking signal from pretraining rather than from independent
   evaluation. The self-consistency check verifies citation-existence (mitigates
   hallucination) but does not address this deeper concern. Mitigation: human-in-the-
   loop verification of high-stakes findings before any consequential action; treating
   the chain's outputs as candidate concerns to be confirmed by a human reviewer with
   independent access to primary sources, not as final adjudications.

---

## git diff cleanliness for `.minsky/audits.db`

The audit DB is committed to git for dissertation auditability. Sqlite files diff
poorly by default. **Recommended one-time setup** (not yet automated):

```bash
git config diff.sqlite3.binary true
git config diff.sqlite3.textconv 'sqlite3 "$1" .dump'

cat >> <project-root>/.gitattributes <<'EOF'
.minsky/audits.db diff=sqlite3
EOF
```

After this, `git diff .minsky/audits.db` shows readable SQL dumps instead of binary diff
markers.

---

## What landed in v1.1 (2026-05-03)

- **OpenCode timeout hardening** — `invoke-opencode.sh` wraps
  `opencode run` in `timeout --kill-after=60s 1800s`. Detects exit codes
  124/137 and reports EXIT_STATUS="timeout". Fixes provider no-response hangs.
  Tunable per invocation via
  `OPENCODE_TIMEOUT_SECONDS`, `OPENCODE_KILL_GRACE_SECONDS`,
  `OPENCODE_MODEL`, and optional `OPENCODE_VARIANT` env vars.

- **Durable adversary logs + schema gate** — invoke wrappers now delete stale
  persona JSON before each fresh walk, persist raw logs under `_opencode_logs/`
  or `_codex_logs/`, and run `scripts/validate-findings.py` before marking a
  walk `ok`. If the agent writes malformed JSON, progress/provenance records
  `schema-invalid` and the chain halts before convergence.

- **Unsafe unattended launch mode is optional** — normal tool approvals are the
  default recommendation. Only use CLI sandbox/approval bypass flags in a
  trusted local repository after reviewing the audit scope and understanding
  that agents may read or write local files.

- **OpenCode permission tightening** — `minsky-reviewer` uses OpenCode's
  documented last-match rule: narrow research-root allows first, credential
  denies last. The previous broad Desktop allow could both over-permit secrets
  and still leave non-interactive audit runs vulnerable to permission prompts.

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
- **Domain-specific Claude subagent routing in Step 1** (use project-local
  `.claude/agents/` for tuned domain framing).
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

- **textual-criticism persona** — for artifacts involving manuscript variants,
  apparatus readings, philological precision in Greek/Latin/Syriac. The current
  marcion-heresiologist persona's preferred-questions only partly cover this scope.
- **rag-engineer persona** — for retrieval/embedding/reranking quality concerns.
- **philosophy-of-ai persona** — for epistemological warrant claims (the dissertation's
  "epistemologically productive performances" framing).

The persona file format is documented (above) so any of these can be added without
code changes — author the file, add to `.minsky/binding.yaml`, and run
`/minsky audit personas-<new-name>` against it (using existing personas as reviewers)
before going live.

---

## Reference: where things live

| Topic | File / docs |
|---|---|
| Skill entry point | `.claude/skills/minsky/SKILL.md` |
| Audit DB schema | `.claude/skills/minsky/schemas/audit-db.sql` |
| Findings schema (agent output contract) | `.claude/skills/minsky/schemas/findings.schema.json` |
| Persona file format | `.claude/skills/minsky/personas/marcion-heresiologist.md` (use as template) |
| Mode ask scaffolding | `.claude/skills/minsky/modes/<mode>.md` |
| GodModeSkill upstream (verbatim lifts) | https://github.com/99xAgency/GodModeSkill — `work-pack-build` lines 300–416 (XML schema, pack ordering); `work-converge` lines 232–274 (self-consistency); `work` line 490 (destructive regex) |
| Worked example | `.claude/skills/minsky/examples/sample-round-output/README.md` — annotated walk-through of a verification cycle |

---

*minsky is candidate methodology infrastructure for structured adversarial
review. Use it as a working tool whose findings are candidate concerns for
human review, not as established methodology or final adjudication.*
