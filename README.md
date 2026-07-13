# minsky

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20021747.svg)](https://doi.org/10.5281/zenodo.20021747)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A stateful, multi-CLI adversarial-audit harness for research artifacts.

`/minsky <mode> [scope]` triangulates Claude Code, Codex, and OpenCode through
a sequential deliberation chain with stable expert personas carrying persistent
memory across rounds.

The name is from Marvin Minsky's *Society of Mind* — many narrow specialists
negotiating — but **stateful**, so personas remember prior rounds and must
either strengthen or retract earlier claims rather than restate them.

## Status

**v1.0.1 - current public release** (May 2026). This Zenodo-integration patch
has no functional changes from v1.0.0. The software was verified through V11
in the reference deployment (a PhD project at Vrije Universiteit Amsterdam on
early-Christian heterodoxy reconstruction). See `CHANGELOG.md` for what this
release includes vs. the project-internal version.

## Documentation

- **`.claude/skills/minsky/README.md`** — full architecture, persona library,
  schema, integration patterns, known epistemic limits, verification record.
- **`.claude/skills/minsky/SKILL.md`** — the operational protocol (Phase A
  through Phase F) the host Claude Code session executes.
- **`.claude/skills/minsky/examples/sample-round-output/README.md`** —
  annotated walk-through of a verification audit cycle.

## Quick install

1. Copy `.claude/skills/minsky/` into your project at the same path.
2. Copy `.opencode/agents/minsky-reviewer.md` into your project at the same
   path; **edit the `read:` and `external_directory:` allow rules** to point
   at your research workspace (the file is templated with `$HOME` expansion
   and clearly-marked `EDIT:` lines).
3. Copy `.minsky/binding.yaml.example` to `.minsky/binding.yaml` (no edits
   needed unless you customise the active persona set).
4. Configure OpenCode for the model/provider you want to use, then set
   `OPENCODE_MODEL` in the environment if your OpenCode setup requires an
   explicit model selector.
5. Initialize the audit DB: `python3 .claude/skills/minsky/scripts/audit-db.py initdb`

See `.claude/skills/minsky/README.md` § Installation for the full prerequisite
list (Claude Code CLI, Codex CLI, OpenCode CLI versions).

## What's in this release

- The `minsky` skill: orchestrator, scripts, schemas, modes
- Three example personas: `marcion-heresiologist`, `ml-finetuning-phd`,
  `performance-studies-roach`
- Templated OpenCode agent (`minsky-reviewer.md`) with universal credential
  deny rules + per-user research-workspace allow rules
- Worked example of a verification audit cycle

## What's NOT in this release

- **Source-corpus full text**: the `personas/source-corpus/` files are
  placeholders. The works listed there were relevant to the writing of one
  specific paper in the reference deployment and are retained in this public
  release only as examples of how a domain-specific persona can point to local
  source material. They are not required for minsky itself, and full texts are
  not redistributed. See
  `.claude/skills/minsky/personas/source-corpus/README.md`.
- **Project-internal audit history**: the reference deployment's
  `.minsky/audits.db` and `codex-audits/` directory contain real
  dissertation-work audit records and remain project-internal.

## License

MIT — see `LICENSE`.

## Citation

If you use minsky in research, please cite the software via Zenodo:

> Elrod, A.G. 2026. *minsky: A Sequential, Multi-CLI Adversarial-Audit
> Harness for High-Stakes Scholarly Work.* Zenodo. https://doi.org/10.5281/zenodo.20021747

The concept DOI [10.5281/zenodo.20021747](https://doi.org/10.5281/zenodo.20021747)
covers all versions and is the recommended citation for general use; for
exact-version reproducibility, cite the version DOI of the release you used
(e.g., v1.0.1 = [10.5281/zenodo.20021748](https://doi.org/10.5281/zenodo.20021748)).
