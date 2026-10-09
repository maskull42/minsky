# Sample audit cycle (V6 + V7)

A complete real audit cycle is preserved at:

```
<project-root>/codex-audits/auto-paths-minsky-test-artifacts-v6-synthetic-md-main-2026-04-28/
```

This was the V6 (round 1) + V7 (round 2 stateful) verification cycle from the minsky
build. The artifact under audit is a **synthetic** Hermeneut STA training pair at
`.minsky/test-artifacts/v6-synthetic.md` — not real research data. It contains two
deliberately-planted defects:

1. The reconstructed Marcionite gloss originally used the term `homoousios` (a Nicene-era
   theological category), which is a 4th-c anachronism applied to a 2nd-c figure.
2. The methodology note claimed "validation through standard quality controls" without
   specifying any rubric, agreement metric, or leakage check.

## What the chain produced (round 1)

- **All 4 reviewer agents** (Claude self-audit + Codex × 2 personas + OpenCode/DeepSeek × 2 personas) **independently converged** on both planted issues.
- **19 findings** total, **all 19 verified** by the self-consistency check.
- **Codex's marcion-heresiologist walk** surfaced an additional finding the host Claude self-audit missed: the `Apostolikon recension` source citation lacked reconstructive provenance (which scholar's reconstruction? which line of which source file?).
- **Codex's ml-finetuning-phd walk** framed a meta-finding: "the very claim of validation IS the quality-gate failure" — the upstream gate that allowed this artifact to be tagged 'validated' is the real defect, not just this one artifact.
- **OpenCode/DeepSeek's marcion-heresiologist walk** went beyond the artifact: independently read MARS's `patristic_sources/marcion.apostolos.adolf_von_harnack.json` and cited the actual reconstructed Greek text of Galatians 1:11–12 (line 117 of the Harnack file) as evidence — the kind of cross-corpus investigation a single-shot LLM call cannot perform.

## What the chain produced (round 2, after partial fix)

The artifact was partially revised: `homoousios` removed and replaced with period-appropriate vocabulary, an Apostolikon-recension citation added, a Provenance block added — BUT the methodology note's `standard quality controls` and the bare fidelity claim were left unchanged.

The chain correctly:

- **Retracted** findings on what was addressed (anachronism, conflation, source-provenance).
- **Carried forward** findings on what wasn't (methodology defects, fidelity claim).
- **Surfaced new findings on the revised material**:
  - The new Provenance block cites Gal 1.12 (Harnack line 117) but the rewritten gloss also draws on Gal 1.11 (Harnack lines 106–111) which was uncited.
  - The phrase **"fallen Israel"** in the rewritten gloss has **zero attestation** anywhere in the MARS corpus (OpenCode's marcion-heresiologist walk grep'd across all patristic_sources, profiles, and prior generation artifacts to verify) — pure invention by the proposer.
- Verdicts shifted from `false` (round 1: pure rejection) to `partial` (round 2: real progress, remaining defects flagged).

## What this demonstrates

- The deliberation chain works end-to-end across 3 lineages × 2 personas.
- The self-consistency check catches both real misquotes and (after the substring-tolerant refinement) preserves real citations that differ only in trailing punctuation.
- The stateful-Minsky design earns its name: per-persona memory journals preserved across rounds let agents retract, strengthen, or extend prior claims rather than restate them.
- Each agent does meaningful multi-step investigation — reading related corpus files, cross-referencing citations, running their own searches — not one-shot prompt-response.

## What's in the audit directory

```
codex-audits/auto-paths-minsky-test-artifacts-v6-synthetic-md-main-2026-04-28/
├── round-1/  (164 KB pack, 19 findings, decision: disagree → user_override)
├── round-2/  (263 KB pack with prior-rounds + persona-memory, 15 findings, decision: disagree)
└── memory/
    ├── marcion-heresiologist.jsonl  (2 entries, one per round)
    └── ml-finetuning-phd.jsonl      (2 entries, one per round)
```

Plus the corresponding rows in `<project-root>/.minsky/audits.db`:

```
sqlite3 .minsky/audits.db "SELECT * FROM audits WHERE audit_id LIKE '%v6-synthetic%'"
sqlite3 .minsky/audits.db "SELECT step, persona, severity, COUNT(*) FROM findings
                            WHERE audit_id LIKE '%v6-synthetic%' GROUP BY step, persona, severity"
```

And the TITAN daily_logs entry: `d3bec7c7-5683-4616-abe0-40d59f4c07a1` (2026-04-28).

## How to read the cycle

If you want to see the full chain in action, walk the artifacts in this order:

1. The artifact under review: `.minsky/test-artifacts/v6-synthetic.md` (round 2 version)
2. Round 1's pack: `round-1/pack.xml` (artifact + ask + schema + MARS context)
3. Round 1's Step 1 self-report: `round-1/claude-self/report.md` (Section A factual + Section B candidates)
4. Round 1's per-(model, persona) findings: `round-1/{claude-self/findings,codex,opencode}/*.json`
5. Round 1's convergence: `round-1/converge.json` + `round-1/consensus.md`
6. Round 1's synthesis: `round-1/claude-synth/decisions.md` + `resolutions.json`
7. Round 2's pack (now larger): `round-2/pack.xml` (note `<unchanged-since-round-1>` annotations)
8. Round 2's Step 1 — note the report's accountability table for round-1 findings: `round-2/claude-self/report.md`
9. Round 2's per-(model, persona) findings — note retractions and new findings: `round-2/{codex,opencode}/*.json`
10. Round 2's convergence: `round-2/converge.json` + `round-2/consensus.md`

This is what a single `/minsky audit` invocation produces in the wild.
