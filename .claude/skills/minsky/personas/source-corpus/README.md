# source-corpus/

The `performance-studies-roach` persona makes load-bearing claims about
performance theory, biopic studies, surrogation theory, and the Stanislavsky
system. To investigate those claims rigorously the persona expects to find
markdown-extracted prose for the works listed below at the named paths.

This directory ships **placeholders only**. Each placeholder file documents:

- The work's bibliographic citation
- Why the persona references it (which red-flags and preferred-questions
  depend on it)
- The expected SHA-256 + byte size of the markdown extraction the persona
  was designed against (for byte-equivalence verification by researchers
  who reproduce the extraction)

To use the persona at full grounding fidelity, supply your own markdown
extractions of these works at the placeholder paths under fair-use research
provisions in your jurisdiction. The persona will operate without the
corpus (its `Read` calls will return placeholder content), but its
verification of cited passages becomes degraded grounding, not
source-criticism.

## Why placeholders rather than full extractions

The minsky public release does not redistribute copyrighted texts. The
works enumerated here are © their respective publishers (Rutgers UP,
Routledge, Cambridge UP, Columbia UP, Da Capo / Hachette, Simon &
Schuster, NPR, NYT, CSMonitor) and are made available under each
publisher's terms. Researchers running the persona must obtain their own
copies through institutional access, library borrowing, or purchase.

## How the persona uses the corpus

See `personas/performance-studies-roach.md` body, particularly the
*How to investigate* section and the *Source-corpus inventory* table
(lines 380–396 in the public release). The persona enumerates each
expected file, the canonical citation, and the specific passages the
persona's red-flag list depends on.

## Reproducing the corpus

A researcher with copies of the listed works can reproduce the corpus
by:

1. Obtaining each work (institutional library, JSTOR, publisher).
2. Extracting prose to markdown using a PDF-to-markdown tool of your
   choice (any extractor producing readable text will work; the persona's
   `Read` calls do not depend on a specific extractor's formatting).
3. Placing the resulting `.md` (or `.txt`) file at the path the
   placeholder names.
4. (Optional) verifying byte-identity to the persona's training extraction
   via `sha256sum <file> | grep <expected_sha256>` against the SHA-256
   recorded in each placeholder.

Step 4 is optional. The persona will function with any reasonable markdown
extraction; the SHA-256 is recorded so that researchers who want
byte-identical reproduction (e.g., for cross-machine audit replication)
have the expected value to verify against.
