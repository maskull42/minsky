"""Mechanical W2 checks; refuse evidence-tree writes and accidental live archive reads."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import conftest as isolation
from test_hardening import PERSONA, provenance, record_call, register_round
from test_lifecycle_host_verify_round import cas_put, make_round, make_round_with_raw_log, recorded_objects
from byte_sources import RAW_MANIFEST_HEADER, ArchiveSource, Resolution
import round_verify


def _round(root: Path, *, host: bool = False,
           context: Path | None = None) -> tuple[Path, Path]:
    register_round(root)
    output = root / "result.json"
    output.write_text("{}\n", encoding="utf-8")
    manifest = record_call(
        root, uid="w2hostcall" if host else "w2codexcall", step="claude_self" if host else "codex",
        model="gpt-6-astra" if host else "gpt-5.6-sol", effort="not_exposed" if host else "medium",
        persona=None if host else PERSONA, outputs=[output], invoked_at="2026-09-05T10:00:00Z",
        origin="host" if host else "wrapper", raw_response=not host,
        usage={"status": "total-only", "total_tokens": 1},
        context_files=[context] if context is not None else None,
    )
    return output, manifest


def _verify(root: Path, output: Path, *args: str, mode: str = "live") -> tuple[int, dict]:
    code = provenance.main([
        "verify-round", "--round-dir", str(root), "--mode", mode, *args,
        "--json-out", str(output),
    ])
    return code, json.loads(output.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # These writes are confined to the brief's explicitly authorised fixture repository.
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo),
         "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        text=True, capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    assert result.returncode == 0, result.stderr
    return result


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    return path


def _commit(repo: Path, *paths: str) -> str:
    _git(repo, "add", "--", *paths)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "fixture")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _store(root: Path, manifest_path: Path) -> Path:
    root.mkdir()
    (root / "STORE_ID").write_text("mars-minsky-cas-v1\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = [manifest[field] for field in ("prompt", "response", "round_scope", "preflight")]
    for field in ("outputs", "referenced_context", "runtime_snapshots"):
        records.extend(manifest[field])
    for record in records:
        if "sha256" not in record:
            continue
        sha = record["sha256"]
        original = Path(record["path"])
        assert provenance.sha256_file(original) == sha
        blob = root / "sha256" / sha[:2] / sha[2:4] / sha
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(original.read_bytes())
    return root


def _tsv(path: Path, header: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, delimiter="\t", lineterminator="\n",
                                quoting=csv.QUOTE_NONE)
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("flag,value", [
    ("--no-live", None), ("--store", "store"), ("--pack", "pack"),
    ("--remap", "/old=/new"),
])
def test_cli_refuses_archive_flags_with_live(tmp_path: Path, capsys, flag: str,
                                           value: str | None) -> None:
    root = tmp_path / "round-2"
    _round(root)
    verdict = tmp_path / "verdict.json"
    args = [flag] if value is None else [flag, value]
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        _verify(root, verdict, *args)
    assert exc.value.code == 2
    assert flag in capsys.readouterr().err
    assert not verdict.exists()


@pytest.mark.parametrize("flag", ["--anchor-repo", "--anchor-prefix"])
def test_cli_refuses_unpaired_anchor_flags(tmp_path: Path, capsys, flag: str) -> None:
    root = tmp_path / "round-2"
    _round(root)
    verdict = tmp_path / "verdict.json"
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        _verify(root, verdict, flag, str(tmp_path) if flag == "--anchor-repo" else "round-2")
    assert exc.value.code == 2
    assert flag in capsys.readouterr().err
    assert not verdict.exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_cli_refuses_existing_json_out(tmp_path: Path, capsys, symlink: bool) -> None:
    root = tmp_path / "round-2"
    _round(root)
    verdict = tmp_path / "verdict.json"
    if symlink:
        verdict.symlink_to(tmp_path / "absent-target.json")
    else:
        verdict.write_bytes(b"keep this verdict")
    before = isolation._lstat_census(tmp_path)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        _verify(root, verdict)
    assert exc.value.code == 2
    assert str(verdict) in capsys.readouterr().err
    assert isolation._lstat_census(tmp_path) == before


@pytest.mark.parametrize("symlink", [False, True])
def test_cli_refuses_json_out_inside_round(tmp_path: Path, capsys, symlink: bool) -> None:
    root = tmp_path / "round-2"
    _round(root)
    verdict = root / "verdict.json"
    if symlink:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        verdict = alias / "verdict.json"
    before = isolation._lstat_census(root)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        _verify(root, verdict)
    assert exc.value.code == 2
    assert "refusing to write a verdict inside the round directory" in capsys.readouterr().err
    assert isolation._lstat_census(root) == before


def test_clean_live_round_is_schema_valid_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "round-2"
    _round(root)
    before = isolation._lstat_census(root)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    code, verdict = _verify(root, first)
    assert code == 0 and verdict["verdict"] == "ok-live"
    assert verdict["canonical"]["result"] == "ok"
    assert verdict["preservation"]["result"] == "ok"
    assert verdict["canonical"]["outputs"][0]["output"] == "result.json"
    for output in verdict["canonical"]["outputs"]:
        assert [guard for guard, _ in output["trace"]] == list(provenance.GUARDS)
        assert all(status in {"pass", "not-reached"} for _, status in output["trace"])
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(round_verify.SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(verdict)
    assert _verify(root, second)[0] == 0
    assert first.read_bytes() == second.read_bytes()
    assert isolation._lstat_census(root) == before


def test_no_runtime_recorded_lists_host_call(tmp_path: Path) -> None:
    root = tmp_path / "round-2"
    _round(root, host=True)
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 0 and verdict["verdict"] == "ok-live"
    assert verdict["no_runtime_recorded"] == ["w2hostcall"]
    response = next(obj for obj in verdict["preservation"]["objects"] if obj["kind"] == "response")
    assert response["status"] == "recorded-absent"
    assert response["exit_status"] == "ok"
    assert not response["sha256"] and not response["recorded_path"]


@pytest.mark.parametrize("missing", [False, True])
def test_legacy_runtime_is_never_ok_live(tmp_path: Path, missing: bool) -> None:
    root = tmp_path / "round-2"
    _, path = _round(root)
    runtime = tmp_path / "legacy-runtime"
    runtime.write_bytes(b"old runtime")
    record = provenance.file_record(str(runtime))
    preflight = next((root / "provenance").glob("*.preflight.json"))
    receipt = json.loads(preflight.read_text(encoding="utf-8"))
    receipt.update(runtime_files=[record], runtime_snapshots=[])
    preflight.write_text(json.dumps(receipt), encoding="utf-8")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(runtime_files=[record], runtime_snapshots=[],
                    preflight=provenance.file_record(str(preflight)))
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if missing:
        runtime.unlink()
    else:
        runtime.write_bytes(b"updated runtime")
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 1 and verdict["verdict"] == "legacy-runtime-unverifiable"
    assert verdict["canonical"]["result"] == "legacy-runtime-unverifiable"
    output = verdict["canonical"]["outputs"][0]
    assert ["runtime_hash", "fail"] in output["trace"]
    assert verdict["preservation"]["result"] == "ok-with-losses"
    runtime_object = next(obj for obj in verdict["preservation"]["objects"] if obj["kind"] == "runtime_file")
    assert runtime_object["status"] == "legacy-runtime-unverifiable"


def test_archive_context_drift_and_no_live(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path / "repo")
    root = repo / "audit" / "round-2"
    context = tmp_path / "context.txt"
    context.write_text("recorded context", encoding="utf-8")
    _, manifest = _round(root, context=context)
    _commit(repo, "audit/round-2")
    store = _store(tmp_path / "store", manifest)
    context.write_text("changed live context", encoding="utf-8")
    before = isolation._lstat_census(root)
    code, verdict = _verify(root, tmp_path / "drift.json", "--store", str(store), mode="archive")
    assert code == 0 and verdict["verdict"] == "ok-archive-context-drifted"
    assert verdict["context_drift"] == "drifted"
    assert verdict["context_drift_paths"] == [str(context)]
    original_open = Path.open
    original_is_file = Path.is_file

    def guarded_open(path: Path, *args, **kwargs):
        assert path != context, "--no-live opened the recorded context"
        return original_open(path, *args, **kwargs)

    def guarded_is_file(path: Path) -> bool:
        assert path != context, "--no-live inspected the recorded context"
        return original_is_file(path)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    code, verdict = _verify(root, tmp_path / "no-live.json", "--store", str(store), "--no-live", mode="archive")
    assert code == 0 and verdict["verdict"] == "ok-archive"
    assert verdict["context_drift"] == "not-checked" and verdict["context_drift_paths"] == []
    assert all(obj["resolved_via"] == "cas" for obj in verdict["preservation"]["objects"])
    assert isolation._lstat_census(root) == before


def test_git_anchor_detects_edited_manifest_bytes(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    root = repo / "audit" / "round-2"
    _, manifest = _round(root)
    commit = _commit(repo, "audit/round-2")
    _, verdict = _verify(root, tmp_path / "anchored.json")
    assert verdict["anchor_repo"] == str(repo)
    assert verdict["anchor_prefix"] == "audit/round-2"
    assert verdict["manifest_anchors"] == [{
        "manifest": manifest.name, "sha256": provenance.sha256_file(manifest), "anchor": "git",
        "commit": commit, "register_line": None, "reason": "",
    }]
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    _, changed = _verify(root, tmp_path / "changed.json")
    assert changed["manifest_anchors"][0]["anchor"] == "none"
    assert "not in any commit" in changed["manifest_anchors"][0]["reason"]


def test_register_anchor_uses_committed_register(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    root = repo / "audit" / "round-2"
    _, manifest = _round(root)
    raw = root.parent / "lifecycle" / "raw_manifest.tsv"
    _tsv(raw, RAW_MANIFEST_HEADER, [{
        "path": f"round-2/provenance/{manifest.name}", "type": "file", "symlink_target": "",
        "bytes": str(manifest.stat().st_size), "sha256": provenance.sha256_file(manifest),
        "mtime_ns": str(manifest.stat().st_mtime_ns), "mode": "0644", "git_tracked": "0",
        "class": "lifecycle", "hash_bound": "1", "hardlink_group": "", "symlink_map": "",
    }])
    register = repo / round_verify.REGISTER_PATH
    _tsv(register, round_verify.REGISTER_HEADER, [{
        "utc": "2026-10-01T00:00:00Z", "audit_id": "test-audit", "audit_dir": "audit",
        "event": "seal", "state_after": "sealed", "class": "code-only",
        "citation_resolvability": "pass", "manifest_sha256": provenance.sha256_file(raw),
        "pack_sha256": "", "copy_location": "", "verdict_json": "", "actor": "test",
        "note": "fixture", "lock_id": "",
    }])
    _commit(repo, round_verify.REGISTER_PATH, "audit/lifecycle/raw_manifest.tsv")
    register.write_text("working copy is deliberately invalid\n", encoding="utf-8")
    _, verdict = _verify(root, tmp_path / "verdict.json", "--anchor-repo", str(repo),
                         "--anchor-prefix", "audit/round-2")
    assert verdict["manifest_anchors"][0] == {
        "manifest": manifest.name, "sha256": provenance.sha256_file(manifest), "anchor": "register",
        "commit": None, "register_line": 2, "reason": "",
    }


def test_no_manifests_fails_with_verdict(tmp_path: Path) -> None:
    root = tmp_path / "round-2"
    register_round(root)
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 1 and verdict["verdict"] == "fail:no call manifests in round"
    assert verdict["canonical"]["result"] == "fail"


@pytest.mark.parametrize("kind", ["call", "preflight", "scope"])
def test_round_loader_errors_are_captured(tmp_path: Path, capsys, kind: str) -> None:
    root = tmp_path / "round-2"
    _, manifest = _round(root)
    broken = (manifest if kind == "call" else root / "round-scope.json" if kind == "scope"
              else next((root / "provenance").glob("*.preflight.json")))
    broken.write_text("{broken", encoding="utf-8")
    capsys.readouterr()
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 1 and verdict["verdict"].startswith("fail:load-error:")
    assert str(broken) in verdict["verdict"]
    assert verdict["errors"] and not capsys.readouterr().err


def test_core_loader_exit_has_complete_trace(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "round-2"
    _round(root)
    original = provenance.load_call_manifests
    calls = 0

    def fail_in_core(path: Path) -> list[dict]:
        nonlocal calls
        calls += 1
        if calls > 1:
            provenance.fail(f"synthetic loader refusal: {path}")
        return original(path)

    monkeypatch.setattr(provenance, "load_call_manifests", fail_in_core)
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 1
    output = verdict["canonical"]["outputs"][0]
    assert output["reason"] == f"load-error: provenance: synthetic loader refusal: {root}"
    assert output["trace"][0] == ["covering_manifest", "fail"]
    assert all(state == "not-reached" for _, state in output["trace"][1:])


def test_scope_load_error_keeps_preservation_objects(tmp_path: Path) -> None:
    root = tmp_path / "round-2"
    _round(root)
    (root / "round-scope.json").write_text("{broken", encoding="utf-8")
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 1 and verdict["canonical"]["result"] == "fail"
    output = verdict["canonical"]["outputs"][0]
    assert ["round_scope_loads", "fail"] in output["trace"]
    assert verdict["preservation"]["objects"]
    scope_object = next(obj for obj in verdict["preservation"]["objects"] if obj["kind"] == "round_scope")
    assert scope_object["status"] == "mismatch"
    assert verdict["no_runtime_recorded"] == ["w2codexcall"]


def test_orphan_preflight_keeps_prepared_only_status(tmp_path: Path) -> None:
    root = tmp_path / "round-2"
    _, manifest = _round(root)
    manifest.unlink()
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 1 and verdict["verdict"] == "fail:no call manifests in round"
    objects = verdict["preservation"]["objects"]
    assert {obj["kind"] for obj in objects} == {"prompt", "round_scope"}
    assert all(obj["receipt"] == "preflight" and obj["exit_status"] == "prepared-only" for obj in objects)
    assert verdict["preservation"]["result"] == "ok"


def test_cli_process_returns_verdict_exit_code(tmp_path: Path) -> None:
    root = tmp_path / "round-2"
    _round(root)
    result = subprocess.run([
        sys.executable, str(Path(provenance.__file__)), "verify-round", "--round-dir", str(root),
        "--mode", "live", "--json-out", str(tmp_path / "verdict.json"),
    ], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""


@pytest.mark.parametrize("defect", ["missing-key", "unknown-status", "wrong-type", "unknown-verdict"])
def test_structural_validation_without_jsonschema(tmp_path: Path, monkeypatch, defect: str) -> None:
    root = tmp_path / "round-2"
    _round(root)
    _, verdict = _verify(root, tmp_path / "verdict.json")
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    round_verify.validate_verdict(verdict)
    if defect == "missing-key":
        verdict.pop("errors")
    elif defect == "unknown-status":
        verdict["canonical"]["outputs"][0]["status"] = "unknown"
    elif defect == "wrong-type":
        verdict["errors"] = "not an array"
    else:
        verdict["verdict"] = "unrecognised"
    with pytest.raises(ValueError, match="verdict"):
        round_verify.validate_verdict(verdict)


def test_canonical_fallback_is_per_manifest(tmp_path: Path) -> None:
    root, _, logs = make_round_with_raw_log(tmp_path / "audit")
    raw_log = logs["attemptlog01"]
    unprepared = root / "unprepared.json"
    unprepared.write_text("{}\n", encoding="utf-8")
    record_call(root, uid="unprepared01", step="codex", model="gpt-5.6-sol", effort="medium",
                persona=PERSONA, outputs=[unprepared], invoked_at="2026-10-01T00:00:10Z",
                usage={"status": "total-only", "total_tokens": 1})
    next((root / "provenance").glob("*.unprepared01.preflight.json")).unlink()
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 1 and verdict["verdict"] == "fail:no prepared attempt covers output"
    outputs = {item["output"]: item for item in verdict["canonical"]["outputs"]}
    assert set(outputs) == {f"codex/{PERSONA}.json", "unprepared.json"}
    assert outputs["unprepared.json"]["reason"] == "no prepared attempt covers output"
    assert [item["output"] for item in verdict["canonical"]["attempt_evidence"]] == [
        raw_log.relative_to(root).as_posix(),
    ]


def test_uncovered_target_wins_over_other_manifest_attempt_evidence(tmp_path: Path) -> None:
    root, _, logs = make_round_with_raw_log(tmp_path / "audit")
    raw_log = logs["attemptlog01"]
    record_call(root, uid="unprepared01", step="codex", model="gpt-5.6-sol", effort="medium",
                persona=PERSONA, outputs=[raw_log], invoked_at="2026-10-01T00:00:10Z",
                usage={"status": "total-only", "total_tokens": 1})
    next((root / "provenance").glob("*.unprepared01.preflight.json")).unlink()
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 1 and verdict["verdict"] == "fail:no prepared attempt covers output"
    assert {item["output"] for item in verdict["canonical"]["outputs"]} == {
        f"codex/{PERSONA}.json", raw_log.relative_to(root).as_posix(),
    }
    assert verdict["canonical"]["attempt_evidence"] == []


def test_covered_target_wins_over_prior_manifest_attempt_evidence(tmp_path: Path) -> None:
    root, _, logs = make_round_with_raw_log(tmp_path / "audit")
    raw_log = logs["attemptlog01"]
    code, before = _verify(root, tmp_path / "before.json")
    assert code == 0 and before["verdict"] == "ok-live"
    assert [item["output"] for item in before["canonical"]["attempt_evidence"]] == [
        raw_log.relative_to(root).as_posix(),
    ]
    record_call(root, uid="coveredlog01", step="codex", model="gpt-5.6-sol", effort="medium",
                persona=PERSONA, outputs=[raw_log], invoked_at="2026-10-01T00:00:10Z",
                usage={"status": "total-only", "total_tokens": 1})
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 0 and verdict["verdict"] == "ok-live"
    outputs = {item["output"]: item for item in verdict["canonical"]["outputs"]}
    assert set(outputs) == {f"codex/{PERSONA}.json", raw_log.relative_to(root).as_posix()}
    assert outputs[raw_log.relative_to(root).as_posix()]["call_uid"] == "coveredlog01"
    assert verdict["canonical"]["attempt_evidence"] == []


def test_missing_superseded_raw_log_fails_preservation(tmp_path: Path) -> None:
    root, output, logs = make_round_with_raw_log(tmp_path / "audit")
    raw_log = logs["attemptlog01"]
    record_call(root, uid="latestcall01", step="codex", model="gpt-5.6-sol", effort="medium",
                persona=PERSONA, outputs=[output], invoked_at="2026-10-01T00:00:10Z",
                usage={"status": "total-only", "total_tokens": 1})
    raw_log.unlink()
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 1 and verdict["verdict"] == f"fail:preservation missing output {raw_log.resolve()}"
    assert verdict["canonical"]["result"] == "ok"
    assert verdict["preservation"]["result"] == "fail"
    assert [item["output"] for item in verdict["canonical"]["attempt_evidence"]] == [
        raw_log.relative_to(root).as_posix(),
    ]


def test_canonical_ok_with_preservation_losses_stays_ok_live(tmp_path: Path) -> None:
    root = make_round(tmp_path / "audit", failed_first=True)
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 0 and verdict["verdict"] == "ok-live"
    assert verdict["canonical"]["result"] == "ok"
    assert verdict["canonical"]["attempt_evidence"] == []
    assert verdict["preservation"]["result"] == "ok-with-losses"


def _superseded_archive(tmp_path: Path) -> tuple[Path, Path, dict[str, Path], Path]:
    repo = _repo(tmp_path / "repo")
    root, output, logs = make_round_with_raw_log(repo / "audit", superseded=True)
    _commit(repo, "audit/round-2")
    latest = next((root / "provenance").glob("*.attemptlog01.call.json"))
    store = _store(tmp_path / "store", latest)
    for recorded, _ in recorded_objects(root):
        cas_put(store, Path(recorded))
    return root, output, logs, store


def test_archive_superseded_canonical_absence_is_an_explicit_loss(tmp_path: Path) -> None:
    root, output, _, store = _superseded_archive(tmp_path)
    later_sha = provenance.sha256_file(output)
    code, verdict = _verify(root, tmp_path / "verdict.json", "--store", str(store),
                            "--no-live", mode="archive")
    assert code == 0 and verdict["verdict"] == "ok-archive"
    assert verdict["canonical"]["result"] == "ok"
    assert verdict["preservation"]["result"] == "ok-with-losses"
    lost = next(obj for obj in verdict["preservation"]["objects"]
                if obj["call_uid"] == "attemptlog00" and obj["kind"] == "output"
                and obj["recorded_path"] == str(output.resolve()))
    assert lost["status"] == "superseded-unrecoverable"
    assert lost["exit_status"] == "error"
    assert lost["resolved_via"] == "cas"
    assert lost["detail"] == f"superseded: later recorded version {later_sha} resolves"
    assert not (store / "sha256" / lost["sha256"][:2] / lost["sha256"][2:4] / lost["sha256"]).exists()
    assert verdict["errors"] == []


def test_archive_absent_unique_raw_log_stays_missing(tmp_path: Path) -> None:
    root, _, logs, store = _superseded_archive(tmp_path)
    log = logs["attemptlog00"]
    cas_put(store, log).unlink()
    code, verdict = _verify(root, tmp_path / "verdict.json", "--store", str(store),
                            "--no-live", mode="archive")
    assert code == 1 and verdict["verdict"] == f"fail:preservation missing output {log.resolve()}"
    assert verdict["canonical"]["result"] == "ok"
    assert verdict["preservation"]["result"] == "fail"
    missing = next(obj for obj in verdict["preservation"]["objects"]
                   if obj["recorded_path"] == str(log.resolve()))
    assert missing["status"] == "missing"
    assert missing["exit_status"] == "error"
    assert log.is_file(), "--no-live cannot use the intact live log"


def test_archive_without_either_canonical_version_stays_missing(tmp_path: Path) -> None:
    root, output, _, store = _superseded_archive(tmp_path)
    cas_put(store, output).unlink()
    code, verdict = _verify(root, tmp_path / "verdict.json", "--store", str(store),
                            "--no-live", mode="archive")
    assert code == 1 and verdict["verdict"] == f"fail:output is missing: codex/{PERSONA}.json"
    assert verdict["preservation"]["result"] == "fail"
    objects = [obj for obj in verdict["preservation"]["objects"]
               if obj["kind"] == "output" and obj["recorded_path"] == str(output.resolve())]
    assert {obj["call_uid"] for obj in objects} == {"attemptlog00", "attemptlog01"}
    assert all(obj["status"] == "missing" for obj in objects)
    assert verdict["errors"] == []


@pytest.mark.parametrize("field,kind", [
    ("prompt", "prompt"), ("response", "response"), ("round_scope", "round_scope"),
    ("preflight", "preflight"), ("outputs", "output"), ("referenced_context", "context"),
    ("runtime_snapshots", "runtime_snapshot"), ("runtime_files", "runtime_file"),
])
def test_absent_recorded_object_uses_another_receipts_verified_version(
        tmp_path: Path, minsky_store: Path, field: str, kind: str) -> None:
    evidence = tmp_path / "evidence"
    evidence.write_bytes(b"earlier recorded bytes")
    earlier = provenance.file_record(str(evidence))
    evidence.write_bytes(b"later recorded bytes")
    later = provenance.file_record(str(evidence))
    cas_put(minsky_store, evidence)
    evidence.unlink()
    receipts = []
    for uid, record in (("earlier", earlier), ("later", later)):
        receipt = {"call_uid": uid, "step": "codex", "exit_status": "error",
                   field: [record] if field in {"outputs", "referenced_context", "runtime_snapshots",
                                                "runtime_files"} else record}
        receipts.append(("call", tmp_path / f"{uid}.call.json", receipt))
    source = ArchiveSource(store_root=minsky_store, pack=None, remaps=[], no_live=True)
    errors = []
    objects = round_verify._preservation(receipts[:1], receipts, source, errors)
    assert errors == []
    assert len(objects) == 1 and objects[0]["kind"] == kind
    assert objects[0]["exit_status"] == "error"
    if kind == "runtime_file":
        assert objects[0]["status"] == "legacy-runtime-unverifiable"
        assert objects[0]["resolved_via"] == "unresolved"
    else:
        assert objects[0]["status"] == "superseded-unrecoverable"
        assert objects[0]["resolved_via"] == "cas"
        assert objects[0]["detail"] == f"superseded: later recorded version {later['sha256']} resolves"


@pytest.mark.parametrize("probe", ["multiple", "same-receipt", "wrong-hash", "os-error", "value-error"])
def test_superseded_probe_is_deterministic_and_refuses_unverified_versions(
        tmp_path: Path, monkeypatch, probe: str) -> None:
    recorded = str(tmp_path / "absent-output")
    own_sha, first_sha, second_sha = "a" * 64, "b" * 64, "c" * 64
    receipt = {"call_uid": "earlier", "step": "codex", "exit_status": "error",
               "outputs": [{"path": recorded, "sha256": own_sha}]}
    own_path = tmp_path / "earlier.call.json"
    receipts = [("call", own_path, receipt)]
    all_receipts = list(receipts)
    for uid, sha in (("second", second_sha), ("first", first_sha)):
        other = {"call_uid": uid, "step": "codex",
                 "outputs": [{"path": recorded, "sha256": sha}]}
        all_receipts.append(("call", own_path if probe == "same-receipt" else tmp_path / f"{uid}.call.json",
                             other))
    source = ArchiveSource(store_root=None, pack=None, remaps=[], no_live=True)
    calls = []

    def resolve(path: str, sha: str | None, *, store_ref: dict | None = None) -> Resolution:
        assert path == recorded and store_ref is None
        calls.append(sha)
        if sha == own_sha:
            return Resolution(False, None, "unresolved", "")
        if probe in {"os-error", "value-error"} and sha == first_sha:
            raise (OSError if probe == "os-error" else ValueError)("later-version probe refused")
        return Resolution(True, own_sha if probe == "wrong-hash" else sha, "cas", "verified fixture")

    monkeypatch.setattr(source, "resolve", resolve)
    errors = []
    objects = round_verify._preservation(receipts, all_receipts, source, errors)
    if probe == "multiple":
        assert calls == [own_sha, first_sha]
        assert objects[0]["status"] == "superseded-unrecoverable"
        assert objects[0]["detail"] == f"superseded: later recorded version {first_sha} resolves"
        assert objects[0]["resolved_via"] == "cas"
    else:
        assert objects[0]["status"] == "missing"
        assert objects[0]["detail"] == ""
        assert calls == ([own_sha] if probe == "same-receipt" else [own_sha, first_sha, second_sha]
                         if probe == "wrong-hash" else [own_sha, first_sha])
    assert errors == ([f"resolution-error: output {recorded}: later-version probe refused"]
                      if probe in {"os-error", "value-error"} else [])


@pytest.mark.parametrize("detail", [
    "store-id-mismatch", "store-ref-invalid:invalid sha256", "store-config-error:invalid config",
])
def test_original_store_ref_failure_stays_missing(tmp_path: Path, monkeypatch, detail: str) -> None:
    recorded = str(tmp_path / "absent-snapshot")
    own_sha, later_sha = "a" * 64, "b" * 64
    ref = {"store_id": "foreign-store", "sha256": own_sha}
    receipt = {"call_uid": "earlier", "step": "codex", "exit_status": "error",
               "runtime_snapshots": [{"path": recorded, "sha256": own_sha, "store_ref": ref}]}
    later = {"call_uid": "later", "step": "codex",
             "runtime_snapshots": [{"path": recorded, "sha256": later_sha}]}
    receipts = [("call", tmp_path / "earlier.call.json", receipt)]
    all_receipts = [*receipts, ("call", tmp_path / "later.call.json", later)]
    source = ArchiveSource(store_root=None, pack=None, remaps=[], no_live=True)
    calls = []

    def resolve(path: str, sha: str | None, *, store_ref: dict | None = None) -> Resolution:
        assert path == recorded
        calls.append(sha)
        if sha == own_sha:
            assert store_ref == ref
            return Resolution(False, None, "unresolved", detail)
        assert sha == later_sha and store_ref is None
        return Resolution(True, later_sha, "cas", "verified fixture")

    monkeypatch.setattr(source, "resolve", resolve)
    errors = []
    objects = round_verify._preservation(receipts, all_receipts, source, errors)
    assert calls == [own_sha] and errors == []
    assert len(objects) == 1 and objects[0]["kind"] == "runtime_snapshot"
    assert objects[0]["status"] == "missing"
    assert objects[0]["detail"] == detail and objects[0]["resolved_via"] == "unresolved"
    assert objects[0]["exit_status"] == "error"


@pytest.mark.parametrize("exception", [OSError, ValueError])
def test_original_resolution_exception_stays_missing(tmp_path: Path, monkeypatch, exception: type) -> None:
    recorded = str(tmp_path / "absent-output")
    own_sha, later_sha = "a" * 64, "b" * 64
    receipt = {"call_uid": "earlier", "step": "codex", "exit_status": "error",
               "outputs": [{"path": recorded, "sha256": own_sha}]}
    later = {"call_uid": "later", "step": "codex",
             "outputs": [{"path": recorded, "sha256": later_sha}]}
    receipts = [("call", tmp_path / "earlier.call.json", receipt)]
    all_receipts = [*receipts, ("call", tmp_path / "later.call.json", later)]
    source = ArchiveSource(store_root=None, pack=None, remaps=[], no_live=True)
    calls = []

    def resolve(path: str, sha: str | None, *, store_ref: dict | None = None) -> Resolution:
        assert path == recorded and store_ref is None
        calls.append(sha)
        if sha == own_sha:
            raise exception("original resolution refused")
        assert sha == later_sha
        return Resolution(True, later_sha, "cas", "verified fixture")

    monkeypatch.setattr(source, "resolve", resolve)
    errors = []
    objects = round_verify._preservation(receipts, all_receipts, source, errors)
    detail = f"resolution-error: output {recorded}: original resolution refused"
    assert calls == [own_sha] and errors == [detail]
    assert len(objects) == 1 and objects[0]["kind"] == "output"
    assert objects[0]["status"] == "missing"
    assert objects[0]["detail"] == detail and objects[0]["resolved_via"] == "unresolved"
    assert objects[0]["exit_status"] == "error"


@pytest.mark.parametrize("expected_outputs", [None, [1]], ids=["null", "non-string"])
@pytest.mark.parametrize("mode", ["live", "archive"])
def test_malformed_expected_outputs_writes_failure_verdict_with_all_targets(
        tmp_path: Path, expected_outputs: list | None, mode: str) -> None:
    root, output, logs = make_round_with_raw_log(tmp_path / "audit")
    preflight = next((root / "provenance").glob("*.attemptlog01.preflight.json"))
    receipt = json.loads(preflight.read_text(encoding="utf-8"))
    receipt["expected_outputs"] = expected_outputs
    preflight.write_text(json.dumps(receipt), encoding="utf-8")
    out = tmp_path / "verdict.json"
    code, verdict = _verify(root, out, mode=mode)
    assert out.is_file() and code == 1 and verdict["verdict"].startswith("fail:")
    assert verdict["canonical"]["result"] == "fail"
    assert {item["output"] for item in verdict["canonical"]["outputs"]} == {
        output.relative_to(root).as_posix(), logs["attemptlog01"].relative_to(root).as_posix(),
    }
    assert all(item["status"] == "fail" for item in verdict["canonical"]["outputs"])
    assert verdict["canonical"]["attempt_evidence"] == []


@pytest.mark.parametrize("defect", ["old-schema", "missing-evidence", "extra-field", "persona-type"])
def test_verdict_11_schema_refuses_old_or_invalid_attempt_evidence(
        tmp_path: Path, monkeypatch, defect: str) -> None:
    root, _, _ = make_round_with_raw_log(tmp_path / "audit")
    code, verdict = _verify(root, tmp_path / "verdict.json")
    assert code == 0 and verdict["schema"] == "minsky-verify-round/1.1"
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(round_verify.SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(verdict)
    if defect == "old-schema":
        verdict["schema"] = "minsky-verify-round/1"
    elif defect == "missing-evidence":
        verdict["canonical"].pop("attempt_evidence")
    elif defect == "extra-field":
        verdict["canonical"]["attempt_evidence"][0]["extra"] = "refused"
    else:
        verdict["canonical"]["attempt_evidence"][0]["persona"] = 1
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(verdict)
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    with pytest.raises(ValueError, match="verdict"):
        round_verify.validate_verdict(verdict)
