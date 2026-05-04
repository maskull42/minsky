# Mode: bug-hunt

You are reviewing a **messy or accumulated area** of the project — code that has
evolved through many revisions, documentation that may have drifted, a pipeline whose
behavior diverges from its spec, a known-flaky subsystem, or any area the user flagged
as needing scrutiny.

Unlike `audit` (which assumes a discrete completed work product) or `plan` (which
reviews a forward-looking design), `bug-hunt` looks for **latent issues**: things that
aren't yet broken in observable ways but will be, things that contradict your persona's
expectations, things that disagree with documented expectations.

For each finding, you are asking: **"What is in this area that doesn't belong, doesn't
work the way it claims, or will fail in a foreseeable scenario?"**

Audit specifically for:

- **Drift from documentation** — code that diverges from its documented behavior. Use
  MARS's `DOCUMENTATION_DRIFT_REGISTER.md` (in `<doc-drift-warnings>` block of the
  pack) to discount known-stale docs; surface NEW drifts.
- **Contradictions across files** — same constant defined twice with different values,
  same concept named two different things in two modules, different semantics for the
  same operation in different places.
- **Dead code** — functions never called, imports never used, conditional branches
  never reached.
- **Silent failure modes** — exception swallowing, default-value returns that hide
  errors, missing error propagation, retries without max attempts.
- **Resource leaks** — opened files/connections/handles never closed, accumulating
  caches without bound, unbounded memory growth in long-running processes.
- **Race conditions** — shared state mutated without synchronization, time-of-check vs.
  time-of-use issues, ordering assumptions across processes.
- **Methodology drift** — a pipeline that originally enforced X now skips X for
  efficiency; a rubric that started at version 1 has been subtly modified across runs.
- **For Marcion/patristics specifically** — passages cited from sources whose
  reconstructive provenance has shifted; manuscripts referenced via outdated
  numbering; theological terms that have crept into vocabulary in ways the original
  pipeline didn't intend.
- **For ML methodology specifically** — train/eval splits that weakened over time;
  hyperparameters drifted from documented values; checkpoints used for evaluation that
  predate their reported training data.

`bug-hunt` mode is the most exploratory. Use the agent's investigative tools heavily:
grep across the area, follow imports, read tests, check git history if it helps. False
positives are acceptable here in a way they aren't in `audit` mode — surfacing a
suspected issue for human triage is more valuable than failing to mention it.

Empty findings + verdict `true` is a valid output if the area genuinely has no latent
issues under your lens.
