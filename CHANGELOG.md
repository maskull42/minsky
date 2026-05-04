# Changelog

## v1.0.1 — 2026-05-04

Patch release to enable Zenodo archive integration. No functional changes
from v1.0.0.

## v1.0.0 — 2026-05-04 (initial public release)

First public release of minsky. Verified through V11 in the reference
deployment (MARS — Marcion Reconstruction System, a PhD project at
Vrije Universiteit Amsterdam).

### Differences vs. project-internal version

The public release is derived from the reference deployment with the
following transformations:

- **`personas/source-corpus/` files replaced with placeholders**.
  Each placeholder retains the citation, the persona's rationale for
  consulting the work, and a SHA-256 fingerprint of the persona-author's
  reference markdown extraction. Full-text extractions of copyrighted
  works are not redistributed. Researchers reproducing the persona must
  supply their own copies under fair-use research provisions.
- **TITAN Supabase provenance push removed**. The reference deployment
  pushes audit work-log entries to a project-internal Supabase store
  (TITAN) for NWO open-science reporting. The integration script
  (`scripts/titan-push.py`) is project-local and not shipped. The
  audit-DB schema retains `titan_log_id` / `titan_pushed_at` columns
  (NULL for public users) so researchers who fork and add their own
  provenance backend do not need to migrate the schema.
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
- The audit-DB schema retains MARS-deployment artifacts (TITAN columns,
  specific finding categories) that may not match other research
  workflows. Future releases may parameterise these.
