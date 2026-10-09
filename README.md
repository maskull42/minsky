# minsky

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20021747.svg)](https://doi.org/10.5281/zenodo.20021747)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A stateful, multi-CLI adversarial-audit harness for research artifacts.

`/minsky <mode> [scope]` triangulates Claude Code, Codex and OpenCode through a sequential deliberation chain. Its stable
expert personas carry persistent memory across rounds. Every model call leaves a hash-bound provenance record. A
content-addressed runtime store and an audit lifecycle preserve, pack, copy and verify the evidence of each finished audit.

The name is from Marvin Minsky's *Society of Mind*: many narrow specialists negotiating. Unlike that society, the personas
are **stateful**. They remember prior rounds and must either strengthen or retract earlier claims rather than restate them.

## Status

**v2.0.0, the current public release (October 2026).** It is a major release with breaking changes; see `CHANGELOG.md`,
"Breaking changes and migration". It ships the full current harness of the reference deployment, a PhD project at Vrije
Universiteit Amsterdam on reconstructing early-Christian heterodoxy:
- the call-provenance contract;
- the content-addressed runtime store;
- the audit lifecycle;
- nine personas.

**Deployment-specific passages.** The skill's own `SKILL.md` and `README.md` describe the harness as it runs in that
deployment. Passages naming its research-log or reporting integrations are marked as deployment-specific.

## Documentation

- **`.claude/skills/minsky/SKILL.md`**: the operational protocol (Phase A through Phase F) that the host session executes,
  plus the runtime store, the lifecycle verbs and `verify-round`.
- **`.claude/skills/minsky/README.md`**: the architecture, persona library, store specification, known epistemic limits and
  verification record.
- **`.claude/skills/minsky/examples/sample-round-output/README.md`**: an annotated walk-through of a verification audit cycle.

## Requirements

- Claude Code (the host).
- Codex CLI and OpenCode CLI (the adversarial legs), each configured for the models you will use.
- Python **3.11 or newer**, with `jsonschema`, `python-dotenv` and `zstandard` (`pip install -r
  .claude/skills/minsky/requirements.txt`). The test suite also needs `pytest`.
- git, sqlite3 and jq.

## Install

1. Copy `.claude/skills/minsky/`, `.opencode/agents/minsky-reviewer.md.template` and `.minsky/binding.yaml.example` into your
   project at the same paths.
   - Optional: `scripts/git-hooks/` (the append-only guard for the lifecycle's tracked TSV files) and `pytest.ini`.
2. Run the setup script from your project root:
   ```bash
   bash .claude/skills/minsky/scripts/setup.sh [--store-root /absolute/path] [--python /path/to/python3.11+]
   ```
   It never overwrites an existing file, and a second run changes nothing. It:
   - renders `config/store.json` from `config/store.json.example`;
   - initialises and checks the runtime store. The default is `$HOME/Library/Application Support/minsky/store` on macOS, or
     `${XDG_DATA_HOME:-$HOME/.local/share}/minsky/store` elsewhere;
   - copies the reviewer template to `.opencode/agents/minsky-reviewer.md`;
   - copies the binding example to `.minsky/binding.yaml`;
   - initialises the audit database.
3. **Edit `.opencode/agents/minsky-reviewer.md`** at its `EDIT:` markers.
   - Set your research workspace path(s).
   - For each audit round, add the exact output paths the OpenCode leg may write. Everything else is denied.
   - After every change, re-run a no-provider canary probe (`SKILL.md`, "OpenCode write scope").
4. Configure provider credentials for OpenCode and Codex as their own documentation describes.
5. Optional: install the git hooks with `git config core.hooksPath scripts/git-hooks`.

## What's in this release

- The `minsky` skill:
  - orchestrator, wrappers and convergence;
  - call provenance (`provenance.py`: register-round, prepare, record, verify-round);
  - the runtime store (`store.py`);
  - the lifecycle CLI (`minsky-lifecycle.py`);
  - schemas, modes and nine personas.
- `scripts/setup.sh`, `config/store.json.example` and the reviewer template.
- `scripts/publish_check.py`, which refuses a tree that contains machine paths, e-mail addresses, credential-like content,
  databases or audit records.
- The test suite (`.claude/skills/minsky/tests/`) and its mutation-matrix runner (`tests/tools/kill_matrix.py`).

## What's NOT in this release

- **Source-corpus full texts.** The files in `personas/source-corpus/` are placeholders: citations, rationale and fingerprints
  only, as in v1. See `.claude/skills/minsky/personas/source-corpus/README.md`.
- **The reference deployment's audit history, store and records:** `.minsky/audits.db`, `codex-audits/`, its lifecycle register
  and its mutation-matrix run record.
- **Its networked reporting integration** (`titan-push.py`) and its one-off migration helpers.

## License

MIT; see `LICENSE`.

## Citation

If you use minsky in research, please cite the software via Zenodo:

> Elrod, A.G. 2026. *minsky: A Sequential, Multi-CLI Adversarial-Audit Harness for High-Stakes Scholarly Work.* Zenodo.
> https://doi.org/10.5281/zenodo.20021747

The concept DOI [10.5281/zenodo.20021747](https://doi.org/10.5281/zenodo.20021747) covers all versions and is the recommended
citation for general use. For exact-version reproducibility, cite the version DOI of the release you used, which is listed in
`CITATION.cff` and on the Zenodo record.
