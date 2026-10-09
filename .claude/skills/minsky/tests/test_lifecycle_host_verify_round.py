"""Host-owned verify-round tests: T2 (moved round), T18 (two dimensions), T19 (manifest anchor).

Written line by line by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 1 Oct 2026 (v0.2 §3.1–3.5, §9; LC-F8, F9,
F22, F26). They drive only the public CLI (`provenance.py verify-round`), so they test the contract, not internal names.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from test_hardening import PERSONA, finding_doc, provenance, record_call, register_round  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True,
                         env={**os.environ, **GIT_ENV})
    return out.stdout


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cas_put(store: Path, path: Path) -> Path:
    digest = sha(path)
    target = store / "sha256" / digest[:2] / digest[2:4] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(path, target)
    return target


def verify(round_dir: Path, out: Path, *extra: str) -> tuple[int, dict[str, Any]]:
    code = provenance.main(["verify-round", "--round-dir", str(round_dir), *extra, "--json-out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8"))


def make_round(audit_dir: Path, *, failed_first: bool = False, failed_output_missing: bool = False) -> Path:
    """A real round written by prepare/record (no provider): one Codex wrapper call, optionally after a failed attempt."""
    round_dir = audit_dir / "round-2"
    register_round(round_dir)
    output = round_dir / "codex" / f"{PERSONA}.json"
    output.parent.mkdir(parents=True)
    if failed_first:
        if not failed_output_missing:
            output.write_text(json.dumps({"attempt": 1}), encoding="utf-8")
        record_call(round_dir, uid="attempt0001", step="codex", model="gpt-5.6-sol", effort="medium",
                    outputs=[output], persona=PERSONA, invoked_at="2026-10-01T00:00:00Z", status="error",
                    usage={"status": "total-only", "total_tokens": 3})
    output.write_text(json.dumps(finding_doc()), encoding="utf-8")
    record_call(round_dir, uid="attempt0002", step="codex", model="gpt-5.6-sol", effort="medium",
                outputs=[output], persona=PERSONA, invoked_at="2026-10-01T00:00:05Z",
                usage={"status": "total-only", "total_tokens": 9})
    return round_dir


def recorded_objects(round_dir: Path) -> list[tuple[str, str]]:
    """Every (path, sha256) any receipt in the round records (prompts, responses, outputs, contexts, scope, preflight)."""
    found: set[tuple[str, str]] = set()
    for receipt in sorted((round_dir / "provenance").glob("*.json")):
        data = json.loads(receipt.read_text(encoding="utf-8"))
        records = [data.get("prompt"), data.get("response"), data.get("round_scope"), data.get("preflight"),
                   *(data.get("outputs") or []), *(data.get("referenced_context") or []),
                   *(data.get("runtime_snapshots") or [])]
        for rec in records:
            if isinstance(rec, dict) and rec.get("sha256") and rec.get("path"):
                found.add((rec["path"], rec["sha256"]))
    return sorted(found)


def committed_repo(tmp: Path, name: str) -> tuple[Path, Path]:
    repo = tmp / name
    repo.mkdir()
    git(repo, "init", "-q")
    audit_dir = repo / "codex-audits" / "fixture-audit"
    return repo, audit_dir


# --------------------------------------------------------------------------------------------- T2 moved round

def test_t2_moved_round_fails_live_and_passes_archive(tmp_path: Path) -> None:
    repo, audit_dir = committed_repo(tmp_path, "repo")
    original_parent = tmp_path / "recorded-at"
    round_dir = make_round(original_parent)
    store = tmp_path / "cas"
    for path, _ in recorded_objects(round_dir):
        if Path(path).is_file():
            cas_put(store, Path(path))
    audit_dir.parent.mkdir(parents=True)
    shutil.move(str(original_parent), str(audit_dir))          # the round moves under a renamed parent
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    moved = audit_dir / "round-2"

    code, live = verify(moved, tmp_path / "live.json", "--mode", "live")
    assert code == 1 and live["verdict"].startswith("fail:")

    code, arch = verify(moved, tmp_path / "archive.json", "--mode", "archive", "--no-live", "--store", str(store))
    assert code == 0, arch["verdict"]
    assert arch["verdict"] == "ok-archive"
    assert {a["anchor"] for a in arch["manifest_anchors"]} == {"git"}
    assert all(o["status"] == "ok" for o in arch["canonical"]["outputs"])

    code, remapped = verify(moved, tmp_path / "remap.json", "--mode", "archive",
                            "--remap", f"{original_parent}={audit_dir}")
    assert code == 0 and remapped["verdict"] == "ok-archive"
    assert remapped["remap_log"], "every remap use is logged"


def test_t2_corrupted_cas_blob_never_passes(tmp_path: Path) -> None:
    """Mutant killed: 'skip the re-hash after CAS resolution' — a blob with the right NAME and wrong bytes must fail."""
    repo, audit_dir = committed_repo(tmp_path, "repo")
    original_parent = tmp_path / "recorded-at"
    round_dir = make_round(original_parent)
    store = tmp_path / "cas"
    output = round_dir / "codex" / f"{PERSONA}.json"
    blobs = {path: cas_put(store, Path(path)) for path, _ in recorded_objects(round_dir) if Path(path).is_file()}
    corrupted = blobs[str(output.resolve())]
    corrupted.chmod(0o644)
    corrupted.write_text('{"tampered": true}', encoding="utf-8")
    audit_dir.parent.mkdir(parents=True)
    shutil.move(str(original_parent), str(audit_dir))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")

    code, arch = verify(audit_dir / "round-2", tmp_path / "a.json", "--mode", "archive", "--no-live",
                        "--store", str(store))
    assert code == 1 and arch["verdict"].startswith("fail:")
    assert any(o["detail"].startswith("cas-corrupt:") for o in arch["preservation"]["objects"]
               if o["kind"] == "output"), arch["preservation"]


def test_t2_symlinked_subdirectory_at_record_time_maps_back(tmp_path: Path) -> None:
    """LC-F26 (prospective): outputs recorded through a symlinked `codex/` map back via the raw manifest's symlink_map."""
    repo, audit_dir = committed_repo(tmp_path, "repo")
    elsewhere = tmp_path / "elsewhere" / "codex"
    elsewhere.mkdir(parents=True)
    staging = tmp_path / "recorded-at"
    round_dir = staging / "round-2"
    register_round(round_dir)
    (round_dir / "codex").symlink_to(elsewhere)
    output = round_dir / "codex" / f"{PERSONA}.json"
    output.write_text(json.dumps(finding_doc()), encoding="utf-8")
    record_call(round_dir, uid="symlinked01", step="codex", model="gpt-5.6-sol", effort="medium",
                outputs=[output], persona=PERSONA, invoked_at="2026-10-01T00:00:00Z",
                usage={"status": "total-only", "total_tokens": 9})
    store = tmp_path / "cas"
    for path, _ in recorded_objects(round_dir):
        cas_put(store, Path(path))
    lifecycle = staging / "lifecycle"
    lifecycle.mkdir()
    header = "path\ttype\tsymlink_target\tbytes\tsha256\tmtime_ns\tmode\tgit_tracked\tclass\thash_bound\thardlink_group\tsymlink_map\n"
    row = f"round-2/codex\tsymlink\t{elsewhere}\t\t\t\t\t0\tother\t0\t\t{elsewhere.resolve()}\n"
    (lifecycle / "raw_manifest.tsv").write_text(header + row, encoding="utf-8")
    (round_dir / "codex").unlink()                              # the link is gone after the move; content lives in the CAS
    audit_dir.parent.mkdir(parents=True)
    shutil.move(str(staging), str(audit_dir))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")

    code, arch = verify(audit_dir / "round-2", tmp_path / "a.json", "--mode", "archive", "--no-live",
                        "--store", str(store))
    assert code == 0, arch["verdict"]
    assert [o["output"] for o in arch["canonical"]["outputs"]] == [f"codex/{PERSONA}.json"]


# --------------------------------------------------------------------------------------- T18 two dimensions

def test_t18_failed_then_successful_attempt_live(tmp_path: Path) -> None:
    round_dir = make_round(tmp_path / "audit", failed_first=True, failed_output_missing=True)
    code, verdict = verify(round_dir, tmp_path / "v.json", "--mode", "live")
    assert code == 0 and verdict["verdict"] == "ok-live"
    assert verdict["canonical"]["result"] == "ok"
    assert verdict["preservation"]["result"] == "ok"
    first = [o for o in verdict["preservation"]["objects"] if o["call_uid"] == "attempt0001"]
    assert first and {o["exit_status"] for o in first} == {"error"}      # never relabelled successful
    assert {o["status"] for o in first if o["kind"] == "output"} == {"recorded-absent"}
    assert {o["status"] for o in first if o["kind"] != "output"} == {"ok"}


def test_t18_superseded_bytes_live_are_an_explicit_loss(tmp_path: Path) -> None:
    round_dir = make_round(tmp_path / "audit", failed_first=True)
    code, verdict = verify(round_dir, tmp_path / "v.json", "--mode", "live")
    assert code == 0 and verdict["canonical"]["result"] == "ok"
    assert verdict["preservation"]["result"] == "ok-with-losses"
    lost = [o for o in verdict["preservation"]["objects"]
            if o["call_uid"] == "attempt0001" and o["kind"] == "output"]
    assert [o["status"] for o in lost] == ["superseded-unrecoverable"]
    assert lost[0]["exit_status"] == "error"


def test_t18_superseded_bytes_recovered_from_cas(tmp_path: Path) -> None:
    repo, audit_dir = committed_repo(tmp_path, "repo")
    round_dir = make_round(audit_dir, failed_first=True)
    store = tmp_path / "cas"
    first_manifest = next((round_dir / "provenance").glob("*.attempt0001.call.json"))
    superseded = json.loads(first_manifest.read_text(encoding="utf-8"))["outputs"][0]
    blob = store / "sha256" / superseded["sha256"][:2] / superseded["sha256"][2:4] / superseded["sha256"]
    blob.parent.mkdir(parents=True)
    blob.write_text(json.dumps({"attempt": 1}), encoding="utf-8")
    for path, _ in recorded_objects(round_dir):
        if Path(path).is_file():
            cas_put(store, Path(path))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    code, verdict = verify(round_dir, tmp_path / "v.json", "--mode", "archive", "--no-live", "--store", str(store))
    assert code == 0 and verdict["verdict"] == "ok-archive"
    assert verdict["preservation"]["result"] == "ok"
    out = [o for o in verdict["preservation"]["objects"] if o["call_uid"] == "attempt0001" and o["kind"] == "output"]
    assert [(o["status"], o["resolved_via"]) for o in out] == [("ok", "cas")]


# ---------------------------------------------------------------------------------------- T19 manifest anchor

def test_t19_consistent_rewrite_is_unanchored_not_ok(tmp_path: Path) -> None:
    repo, audit_dir = committed_repo(tmp_path, "repo")
    round_dir = make_round(audit_dir)
    store = tmp_path / "cas"
    for path, _ in recorded_objects(round_dir):
        if Path(path).is_file():
            cas_put(store, Path(path))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    code, before = verify(round_dir, tmp_path / "before.json", "--mode", "archive", "--no-live", "--store", str(store))
    assert code == 0 and before["verdict"] == "ok-archive"

    output = round_dir / "codex" / f"{PERSONA}.json"
    output.write_text(json.dumps(finding_doc(findings=[])) + "\n", encoding="utf-8")   # a forged, self-consistent edit
    cas_put(store, output)
    manifest_path = next((round_dir / "provenance").glob("*.attempt0002.call.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for record in manifest["outputs"]:
        if record["path"] == str(output.resolve()):
            record.update(bytes=output.stat().st_size, sha256=sha(output))
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    code, after = verify(round_dir, tmp_path / "after.json", "--mode", "archive", "--no-live", "--store", str(store))
    assert after["canonical"]["result"] == "ok"                  # self-consistent: the gates alone cannot see it
    assert after["verdict"] == "consistent-unanchored"
    assert code == 1                                              # never ok
    anchors = {a["manifest"]: a for a in after["manifest_anchors"]}
    assert anchors[manifest_path.name]["anchor"] == "none"


# ------------------------------------------- T2/T18 addendum (2 Oct): raw attempt logs are evidence, not canonical outputs
# Written by the IMPLEMENT host after the G4 regression (2 Oct 2026, 08:48–08:51 CEST): 29 of 34 real rounds failed only
# because verify-round reconciled the wrappers' raw per-attempt logs (`_codex_logs/<persona>.round-N.<uid>.output.json`,
# `.stderr.log`) as canonical outputs. Their preflight never names them, and no production caller (converge, chain,
# claude-self-audit, claude-synth) ever reconciles them. The judgements: such logs are ATTEMPT EVIDENCE, not canonical
# targets; a manifest whose outputs no preflight covers keeps every output as a canonical target (fail-closed); the LATEST
# attempt's raw log stays guarded by the legacy `sibling_outputs` guard (T1 binds it; W2c's worker correctly refused to touch
# it); and a SUPERSEDED attempt's raw log, which no canonical gate sees, fails the round through the preservation dimension.

ATTEMPT_EVIDENCE_REASON = "not an expected output of any prepared attempt (preservation only)"


def _record_with_raw_log(round_dir: Path, output: Path, *, uid: str, status: str, invoked_at: str) -> Path:
    """One Codex call shaped like the real wrapper's: the preflight expects the canonical output; the manifest also
    records this attempt's raw log (record accepts a superset of the expected outputs). Returns the raw log."""
    raw_log = round_dir / "codex" / "_codex_logs" / f"{PERSONA}.round-2.{uid}.output.json"
    raw_log.parent.mkdir(parents=True, exist_ok=True)
    raw_log.write_text(f'{{"type": "raw stream line", "attempt": "{uid}"}}\n', encoding="utf-8")
    evidence = round_dir / "test-evidence"
    evidence.mkdir(exist_ok=True)
    prompt = evidence / f"{uid}.prompt.txt"
    prompt.write_text(f"exact prompt {uid}\n", encoding="utf-8")
    response = evidence / f"{uid}.response.log"
    response.write_text(f"raw response {uid}\n", encoding="utf-8")
    shared = ["--call-uid", uid, "--audit-id", "test-audit", "--round", "2", "--round-dir", str(round_dir),
              "--step", "codex", "--origin", "wrapper", "--provider", "openai", "--model", "gpt-5.6-sol",
              "--effort", "medium", "--client-name", "test-client", "--client-version", "1.0",
              "--prompt-path", str(prompt), "--runtime-json", "{}", "--persona", PERSONA]
    assert provenance.main(["prepare", *shared, "--expected-output-path", str(output)]) == 0
    preflight = next((round_dir / "provenance").glob(f"*.{uid}.preflight.json"))
    assert provenance.main(["record", *shared, "--preflight-path", str(preflight), "--exit-status", status,
                            "--usage-json", json.dumps({"status": "total-only", "total_tokens": 9}),
                            "--invoked-at", invoked_at, "--duration", "1.25",
                            "--exit-code", "0" if status == "ok" else "1", "--response-path", str(response),
                            "--output-path", str(output), "--output-path", str(raw_log)]) == 0
    manifest = json.loads(next((round_dir / "provenance").glob(f"*.{uid}.call.json")).read_text(encoding="utf-8"))
    assert {Path(o["path"]) for o in manifest["outputs"]} == {output.resolve(), raw_log.resolve()}
    return raw_log


def make_round_with_raw_log(audit_dir: Path, *, superseded: bool = False) -> tuple[Path, Path, dict[str, Path]]:
    """A round whose successful call (uid attemptlog01) records a raw log; with `superseded`, a failed earlier attempt
    (uid attemptlog00) recorded its own raw log first. Returns (round_dir, canonical output, {uid: raw log})."""
    round_dir = audit_dir / "round-2"
    register_round(round_dir)
    output = round_dir / "codex" / f"{PERSONA}.json"
    output.parent.mkdir(parents=True)
    logs = {}
    if superseded:
        output.write_text(json.dumps({"attempt": 0}), encoding="utf-8")
        logs["attemptlog00"] = _record_with_raw_log(round_dir, output, uid="attemptlog00", status="error",
                                                    invoked_at="2026-10-01T00:00:00Z")
    output.write_text(json.dumps(finding_doc()), encoding="utf-8")
    logs["attemptlog01"] = _record_with_raw_log(round_dir, output, uid="attemptlog01", status="ok",
                                                invoked_at="2026-10-01T00:00:05Z")
    return round_dir, output, logs


def test_t2_raw_attempt_log_is_attempt_evidence_not_a_canonical_target(tmp_path: Path) -> None:
    """Mutant killed: 'select every manifest output as a canonical target' (the G4 regression's 29 false failures)."""
    round_dir, _, _ = make_round_with_raw_log(tmp_path / "audit")
    code, verdict = verify(round_dir, tmp_path / "v.json", "--mode", "live")
    assert code == 0 and verdict["verdict"] == "ok-live", verdict["verdict"]
    assert verdict["schema"] == "minsky-verify-round/1.1"
    assert [o["output"] for o in verdict["canonical"]["outputs"]] == [f"codex/{PERSONA}.json"]
    log_key = f"codex/_codex_logs/{PERSONA}.round-2.attemptlog01.output.json"
    assert verdict["canonical"]["attempt_evidence"] == [{
        "step": "codex", "persona": PERSONA, "call_uid": "attemptlog01", "output": log_key,
        "reason": ATTEMPT_EVIDENCE_REASON,
    }]
    logs = [o for o in verdict["preservation"]["objects"] if o["kind"] == "output" and o["recorded_path"].endswith(
        "attemptlog01.output.json")]
    assert [o["status"] for o in logs] == ["ok"], "the log is still hash-checked, in the preservation dimension"


def test_t2_manifest_without_preflight_coverage_keeps_every_output_canonical(tmp_path: Path) -> None:
    """Fail-closed fallback: when no preflight covers any of a manifest's outputs (the pre-preflight era), every output
    stays a canonical target and fails with today's reason; nothing is quietly moved to attempt evidence."""
    round_dir, _, _ = make_round_with_raw_log(tmp_path / "audit")
    next((round_dir / "provenance").glob("*.attemptlog01.preflight.json")).unlink()
    code, verdict = verify(round_dir, tmp_path / "v.json", "--mode", "live")
    assert code == 1 and verdict["verdict"].startswith("fail:")
    outputs = {o["output"]: o for o in verdict["canonical"]["outputs"]}
    assert set(outputs) == {f"codex/{PERSONA}.json", f"codex/_codex_logs/{PERSONA}.round-2.attemptlog01.output.json"}
    assert all(o["status"] == "fail" for o in outputs.values())
    assert verdict["canonical"]["attempt_evidence"] == []


def test_t2_latest_raw_log_stays_guarded_by_the_legacy_sibling_guard(tmp_path: Path) -> None:
    """The legacy contract is not loosened: a changed raw log of the LATEST attempt still fails canonical acceptance
    (guard `sibling_outputs`), exactly as converge.py would refuse it today."""
    round_dir, _, logs = make_round_with_raw_log(tmp_path / "audit")
    logs["attemptlog01"].write_text('{"type": "edited after the call"}\n', encoding="utf-8")
    code, verdict = verify(round_dir, tmp_path / "v.json", "--mode", "live")
    assert code == 1 and verdict["canonical"]["result"] == "fail"
    trace = dict(verdict["canonical"]["outputs"][0]["trace"])
    assert trace["sibling_outputs"] == "fail", trace


def test_t18_tampered_raw_log_fails_the_round_through_preservation_live(tmp_path: Path) -> None:
    """Mutant killed: 'verdict ignores preservation'. A SUPERSEDED attempt's raw log is invisible to every canonical gate
    (they read the latest attempt only), so only the preservation dimension can fail the round."""
    round_dir, _, logs = make_round_with_raw_log(tmp_path / "audit", superseded=True)
    code, clean = verify(round_dir, tmp_path / "clean.json", "--mode", "live")
    assert code == 0 and clean["verdict"] == "ok-live", clean["verdict"]
    logs["attemptlog00"].write_text('{"type": "edited after the call"}\n', encoding="utf-8")
    code, verdict = verify(round_dir, tmp_path / "v.json", "--mode", "live")
    assert verdict["canonical"]["result"] == "ok"
    assert verdict["preservation"]["result"] == "fail"
    assert code == 1 and verdict["verdict"] == (
        f"fail:preservation mismatch output {logs['attemptlog00'].resolve()}"), verdict["verdict"]


def test_t18_corrupted_raw_log_blob_fails_archive_rehearsal_verdict(tmp_path: Path) -> None:
    """The offload rehearsal path (archive, --no-live, CAS only): an overwritten superseded output is an explicit loss
    (mutant killed: 'archive treats a superseded loss as missing'); a corrupted CAS copy of a superseded attempt's raw log
    is never ok-archive, although canonical acceptance is ok."""
    repo, audit_dir = committed_repo(tmp_path, "repo")
    round_dir, _, logs = make_round_with_raw_log(audit_dir, superseded=True)
    store = tmp_path / "cas"
    blobs = {path: cas_put(store, Path(path)) for path, _ in recorded_objects(round_dir) if Path(path).is_file()}
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    code, clean = verify(round_dir, tmp_path / "clean.json", "--mode", "archive", "--no-live", "--store", str(store))
    assert code == 0 and clean["verdict"] == "ok-archive", clean["verdict"]
    # The failed attempt's canonical bytes were overwritten before any snapshot, so no CAS holds them. Archive mode must
    # classify that loss exactly as live mode does (an explicit loss), never as `missing`, or no such round could offload.
    assert clean["preservation"]["result"] == "ok-with-losses"
    lost = [o for o in clean["preservation"]["objects"] if o["call_uid"] == "attemptlog00" and o["kind"] == "output"
            and o["recorded_path"].endswith(f"codex/{PERSONA}.json")]
    assert [o["status"] for o in lost] == ["superseded-unrecoverable"]
    corrupted = blobs[str(logs["attemptlog00"].resolve())]
    corrupted.chmod(0o644)
    corrupted.write_text('{"tampered": true}\n', encoding="utf-8")
    code, verdict = verify(round_dir, tmp_path / "bad.json", "--mode", "archive", "--no-live", "--store", str(store))
    assert verdict["canonical"]["result"] == "ok"
    assert code == 1 and verdict["verdict"].startswith("fail:preservation "), verdict["verdict"]


def test_t18_corrupted_superseded_blob_is_never_relabelled_a_known_loss(tmp_path: Path) -> None:
    """Astra W2d finding (high): a superseded output whose CAS blob EXISTS but is corrupt is damage, not the explicit loss
    of overwritten bytes. Only genuine absence (nothing found, no corrupt identity, no resolver error) may become
    `superseded-unrecoverable`. Mutant killed: 'treat any unresolved object as genuinely absent'."""
    repo, audit_dir = committed_repo(tmp_path, "repo")
    round_dir, output, _ = make_round_with_raw_log(audit_dir, superseded=True)
    store = tmp_path / "cas"
    for path, _ in recorded_objects(round_dir):
        if Path(path).is_file():
            cas_put(store, Path(path))
    manifest = json.loads(next((round_dir / "provenance").glob("*.attemptlog00.call.json")).read_text(encoding="utf-8"))
    superseded = next(o["sha256"] for o in manifest["outputs"] if o["path"] == str(output.resolve()))
    blob = store / "sha256" / superseded[:2] / superseded[2:4] / superseded
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text('{"not": "the recorded bytes"}', encoding="utf-8")       # a blob under the right name, wrong bytes
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    code, verdict = verify(round_dir, tmp_path / "v.json", "--mode", "archive", "--no-live", "--store", str(store))
    assert verdict["canonical"]["result"] == "ok"
    damaged = [o for o in verdict["preservation"]["objects"] if o["call_uid"] == "attemptlog00" and o["kind"] == "output"
               and o["recorded_path"] == str(output.resolve())]
    assert [o["status"] for o in damaged] == ["missing"] and damaged[0]["detail"].startswith("cas-corrupt:")
    assert code == 1 and verdict["verdict"] == f"fail:preservation missing output {output.resolve()}"
