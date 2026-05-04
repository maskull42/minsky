# minsky

A stateful, multi-CLI adversarial-audit harness for research artifacts.

`/minsky <mode> [scope]` triangulates three coding agents — Claude Code (Opus 4.7),
Codex (GPT-5.5), and OpenCode + DeepSeek V4 Pro — through a sequential
deliberation chain with stable expert personas carrying persistent memory
across rounds.

The name is from Marvin Minsky's *Society of Mind* — many narrow specialists
negotiating — but **stateful**, so personas remember prior rounds and must
either strengthen or retract earlier claims rather than restate them.

## Status

**v1.0.0 — initial public release** (May 2026). Verified through V11 in the
reference deployment (a PhD project at Vrije Universiteit Amsterdam on
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
4. Add a DeepSeek API key to your project's `.env` as `deepseek_api=sk-...`
   (the OpenCode wrapper sources `.env` before each `opencode run` call).
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
  placeholders (citation + persona-rationale only). The original works
  (Bingham 2010, Carnicke 2009, Custen 1992, Kemp 2012, Roach 1996,
  Stanislavsky 1989, Stewart 2024, Whyman 2008, etc.) are © their
  publishers and not redistributed. See
  `.claude/skills/minsky/personas/source-corpus/README.md` for the
  placeholder system overview.
- **TITAN Supabase provenance push**: the reference deployment pushes
  audit work-log entries to a project-internal Supabase store (TITAN)
  for institutional reporting. That integration (`scripts/titan-push.py`)
  is project-local and not included.
- **Project-internal audit history**: the reference deployment's
  `.minsky/audits.db` and `codex-audits/` directory contain real
  dissertation-work audit records and remain project-internal.

## License

MIT — see `LICENSE`.

## Citation

If you use minsky in research, please cite:

> Elrod, Andrew. 2026. *Auditioning Marcion: Agentic Harnesses,
> Source-Grounded Evaluation, and the Improvisation Test for Synthetic
> Theological Agents*. Workshop paper, in preparation.
