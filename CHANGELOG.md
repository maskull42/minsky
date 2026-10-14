# Changelog

## v2.1.1 — 2026-10-14 (patch release)

### Fixed

- **The Codex leg runs with Codex memory turned off.** When Codex memory is enabled, Codex CLI adds the user's personal memory
  (`~/.codex/memories`) to each `codex exec` as a developer instruction (observed with codex-cli 0.162.1). The existing
  `-c project_doc_max_bytes=0`, which keeps repository `AGENTS.md` files out, does not stop it.
  - `scripts/invoke-codex.sh` now passes `-c memories.use_memories=false -c memories.generate_memories=false` after
    `-c project_doc_max_bytes=0`, and records both settings in the preflight and call-manifest `runtime.argv`.
  - The second setting, as named, also stops Codex from generating memories from audit runs; that effect is not tested here.
  - A new test checks the executed argv and both receipts against a fake `codex`; three new mutants.

### Earlier audits

- Audits made with an earlier version while your Codex memory was enabled may have received that memory as a hidden
  instruction. Their records do not show it. Whether Codex memory is enabled by default was not established for this release;
  check your Codex configuration.
- No migration is needed.

## v2.1.0 — 2026-10-13 (minor release)

### Changed

- **The OpenCode leg defaults to Gemini 4 Argon on Google Cloud Vertex AI** (`google-vertex/gemini-4-argon`, high thinking,
  location `global`), replacing Gemini 3.8 Flash through an API key.
  - Credentials: `GOOGLE_APPLICATION_CREDENTIALS` (an absolute path to a service-account key file) and `GOOGLE_CLOUD_PROJECT`,
    imported from your project's `.env`. With `MINSKY_OPENCODE_CREDENTIALS=opencode`, application-default credentials are
    used and `GOOGLE_VERTEX_PROJECT` must be set. API-key variables are removed from the environment of every Vertex call.
  - Argon is declared in minsky's own `config/opencode-vertex.json`, which the wrapper passes through `OPENCODE_CONFIG`; a
    preset `OPENCODE_CONFIG` is refused. Your repository's `opencode.json` is still loaded by OpenCode, but Argon does not
    depend on it. Argon's context window and price were not published at release time, so neither is declared.
  - The previous model stays available ONLY by explicit selection: `OPENCODE_MODEL=google/gemini-3.8-flash` with
    `GOOGLE_API_KEY`. There is no automatic fallback.

### Added

- **A Vertex identity check before each OpenCode call.** After `provenance.py prepare` has validated the store, a one-word
  request must return HTTP 200 with `modelVersion` exactly equal to the requested model. Otherwise the call is recorded as an
  `error` and OpenCode is not run. Its sanitized receipt is recorded as a call output. It proves what Vertex served at dispatch
  time, not what served the OpenCode call itself.
- **Unknown prices are never recorded as zero.** When every OpenCode step reports cost 0 with positive tokens, the usage is
  recorded as `complete-unpriced` with `price: "unknown"` and no `cost_usd`. Mixed priced and unpriced steps fail accounting.
- **The publish check refuses a public tree whose real-data gates are not all unmet** (rule `gates-not-unmet`).
- Requirements: `google-auth` and `requests`.

### Fixed

- **Frozen audit-database copies are created owner-only (0600)** and verified, for the freeze copy and the restore-check copy.
  A destination that does not keep the mode is refused. Before, copies took the process umask (typically 0644).
- **Two gate tests no longer depend on the installed gates file.** They had assumed the gates were unmet.

### Migration

- To use the new default, create or choose a Google Cloud project with Vertex AI, make `gemini-4-argon` available at
  `global`, create a service-account key, and add the two variables above to `.env`.
- To stay on the previous model, set `OPENCODE_MODEL=google/gemini-3.8-flash`.

## v2.0.1 — 2026-10-09 (patch release)

### Fixed

- **False refusals by the live-writer check on macOS.** `seal` and `freeze` refuse while another process holds the audit
  directory or the audits database (`lsof`). Spotlight's indexer reads freshly written files and could trigger a false,
  intermittent refusal.
  - The check now ignores a holder only if its executable is exactly Spotlight's `mdworker_shared` or `mds` (read from
    lsof's `txt` record) AND it holds every file read-only. Each ignored holder is logged.
  - A Spotlight holder that cannot be confirmed read-only is re-checked up to 3 times, then refused.
  - Every other holder is refused as before.
  - The check still fails closed: it never passes a writer.
- **Spotlight-safe test temp.** When `MINSKY_TEST_TMP` is unset, the test suite defaults to a temp root ending in
  `.noindex`, and the mutation-matrix runner's temporary directories do too. Spotlight skips such directories.
  - You can also exclude your audit directories in System Settings → Spotlight → Privacy.

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
