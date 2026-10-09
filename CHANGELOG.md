# Changelog

## v2.0.0 — 2026-10 (major release)

This release ships the full current harness of the reference deployment: everything from v1.0.1 up to its October 2026 state.
The v1.0.1 public hardening (repository-bounded evidence and scope) is preserved. It is now part of the shared code base
rather than a public-only patch.

### Breaking changes and migration

- **Python 3.11 or newer is required.** The scripts use `datetime.UTC`. `scripts/setup.sh` refuses older interpreters.
- **Round registration and model bindings are required.**
  - Each round is registered before any host or provider phase:
    `provenance.py register-round --audit-id … --round N --round-dir … --pack <round>/pack.xml --personas '<JSON>'
    --models '<JSON model@effort list>' --step-models '<JSON binding of claude_self, codex, opencode, claude_synth>'`.
  - This writes an immutable `round-scope.json` (schema 1.1: git head, tracked dirty paths, pack sha256).
  - `chain.py adversaries` and `scripts/launch-chain.py` refuse calls whose model and effort do not match the registration.
  - **Migration:** register every new round. Rounds made with v1 stay as they are and are never retrofitted.
- **Host phases are recorded.**
  - `provenance.py prepare` runs before the host writes a phase's outputs, declaring each output with `--expected-output-path`.
  - `provenance.py record` runs afterwards.
  - The provider wrappers record their own calls.
  - Receipts are schema 1.1.
- **A runtime store is required.**
  - `config/store.json` must name an absolute store root. Run `scripts/setup.sh`, or copy `config/store.json.example` and run
    `scripts/store.py init --root <absolute path>`.
  - Program binaries, interpreters and wrapper scripts are kept once in the content-addressed store, not copied into every round.
  - A missing or wrong store stops `prepare` before any provider call.
  - `"enabled": false` (with `disabled_by` and `reason`) is a recorded opt-out that keeps per-round copies.
- **OpenCode agent.**
  - `.opencode/agents/minsky-reviewer.md` is now rendered from `minsky-reviewer.md.template`.
  - The shared Write/Edit gate denies every path until you list each audit round's exact output paths.
  - Read the template's `EDIT:` markers.
- **Default adversarial models.** The wrappers default to `gpt-5.6-sol@medium` (Codex) and `google/gemini-3.8-flash` at high
  thinking (OpenCode). Every run should pass its models explicitly; the registration enforces them.

### New

- **Call provenance.** Unique prompt files, pre-dispatch receipts, raw logs, output snapshots and terminal manifests for every
  call. `provenance.py verify-round --mode live|archive` re-verifies a round:
  - canonical targets come from the declared outputs;
  - every hash-bound object is checked for preservation, superseded attempts included;
  - the verdicts are `ok-live`, `ok-archive`, `ok-archive-context-drifted`, `legacy-runtime-unverifiable`,
    `consistent-unanchored` or `fail:<reason>`.
- **Audit lifecycle** (`minsky-lifecycle.py`):
  - `census`, `classify` (methodology-bearing by default), `challenge`, `ruling` and `promote`;
  - `seal`, deterministic `pack`, `replicate` (copy A a directory; copy B a restic repository), `verify`, `offload` (a
    verified move, never of the last copy), `rehydrate` and `expire` (code-only audits, after a grace period);
  - `freeze` (an `audits.db` freeze outside `seal`, e.g. at a campaign close) and `freeze-restore-check` (a restore
    demonstration);
  - an append-only register;
  - a free-space guard whose passing checks are persisted. The exception is `seal`'s ingest check, which is only
    logged (a documented gap);
  - real-data gates in `config/real_data_gates.json`, which ship unmet. Set them only when your own preconditions hold.
- **Personas:** nine in total. New since v1: `provenance-reproducibility`, `phd-documentation-currency`,
  `marcion-textual-critic`, `agentic-context-engineering`, `research-infrastructure-stewardship` and `translation-fidelity`.
  Several are worked examples from the reference deployment.
- **Tooling:**
  - `scripts/launch-chain.py` (a detached chain launch with recorded environment);
  - `scripts/setup.sh`;
  - `scripts/publish_check.py`;
  - optional git hooks (`scripts/git-hooks/`) guarding the lifecycle's tracked TSV files.
- **Tests:** a full pytest suite with a mutation-matrix runner (`tests/tools/kill_matrix.py`).

### Reference-deployment integrations (present but inert unless configured)

- **Research-log and reporting steps.** `work-log-stub.py`, Phase F of `SKILL.md`, and the `titan_*` columns in the audit
  database belong to the reference deployment. `titan-push.py` is not shipped.
- **`expire` refuses** when it cannot check the reference deployment's reporting records. Treat that as a documented limit.
- **By default the OpenCode wrapper reads one selected provider key from a repository `.env`.** It parses that file, never
  sources it, and supports the google and deepseek providers.
  - `MINSKY_OPENCODE_CREDENTIALS=opencode` relies on OpenCode's own credentials instead, for any provider.
  - The mode is recorded with each call.
  - See `.claude/skills/minsky/README.md`.


## v1.0.1 — 2026-05-04

Patch release to enable Zenodo archive integration. No functional changes
from v1.0.0.

Zenodo records minted (post-release):
- Concept DOI: [10.5281/zenodo.20021747](https://doi.org/10.5281/zenodo.20021747) — covers all versions; recommended for general citation.
- v1.0.1 version DOI: [10.5281/zenodo.20021748](https://doi.org/10.5281/zenodo.20021748) — use when version-specificity matters.

## v1.0.0 — 2026-05-04 (initial public release)

First public release of minsky. Verified through V11 in the reference
deployment (MARS — Marcion Reconstruction System, a PhD project at
Vrije Universiteit Amsterdam).

### Differences vs. project-internal version

The public release is derived from the reference deployment with the
following transformations:

- **`personas/source-corpus/` files replaced with placeholders**.
  Each placeholder retains the citation, the persona's rationale for
  consulting the work, and a SHA-256 fingerprint of the reference markdown
  extraction. Full-text extractions of copyrighted
  works are not redistributed. Researchers reproducing the persona must
  supply their own copies under fair-use research provisions.
- **Project-internal provenance integrations removed**. The public release
  keeps local sqlite provenance only. Project-specific networked reporting
  integrations are not shipped.
- **Hardcoded `/Users/...` paths templated**. The `.opencode/agents/minsky-reviewer.md`
  agent file uses `$HOME` expansion (confirmed working in opencode-ai
  1.14.29+ via empirical test) for credential-deny rules; research-workspace
  allow rules are documented as `EDIT:` markers requiring per-installation
  customisation. The `pack-build.py` script honors `MINSKY_PHD_FRAME` and
  `MINSKY_DOC_DRIFT` env-vars (with sensible defaults that gracefully
  fall back when the project does not ship those documents).
- **MARS-internal cross-references** in personas and docs are framed as
  "the reference deployment" or "an example use case"; the substantive
  content (Marcion variants, condensed_phd_context.md references, etc.)
  is preserved as worked-example documentation of how a domain-specific
  persona can be composed.
- **`scripts/source-corpus-rehash.py` removed** (it referenced a
  hardcoded upstream repository path; researchers reproducing the
  corpus can run their own equivalent).

### What's verified

- All three personas load and operate (within the placeholder-corpus
  caveat for performance-studies-roach).
- `$HOME` expansion in opencode 1.14.29 permission rules: confirmed
  empirically.
- Skill scripts run from a clean clone (graceful fallback when
  optional project-context documents are absent).

### Known limits at v1.0.0

- The `performance-studies-roach` persona is heavily MARS-coupled and
  ships as a worked example rather than a fully generic template.
  Researchers in adjacent fields will need to compose their own
  performance-theory personas with their own source corpus.
- The audit-DB schema is intentionally local-first. Future releases may
  parameterise additional provenance backends.
