"""W4/T4/T5 KB fixtures; refuse missing stores and unverified relocated bytes."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import free_space
import store
from byte_sources import ArchiveSource, LiveSource, Resolution
from test_hardening import MODELS, PERSONA, SCRIPTS, provenance, record_call, register_round


def _config(path: Path, **overrides) -> Path:
    """Write a fixture config; refuse implicit production-root test writes."""
    value = {"store_id": store.STORE_ID, "root": str(path.parent / "store"),
             "enabled": True, **overrides}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    return path


def _shared(root: Path, runtime: Path, *, uid: str = "w4storecall",
            round_number: int = 2) -> list[str]:
    """Build inert call arguments; refuse any provider dispatch in fixtures."""
    return [
        "--call-uid", uid, "--audit-id", "test-audit", "--round", str(round_number),
        "--round-dir", str(root), "--step", "codex", "--persona", PERSONA,
        "--origin", "wrapper", "--provider", "openai", "--model", "gpt-5.6-sol",
        "--effort", "medium", "--client-name", "test", "--client-version", "1",
        "--prompt-path", str(root / "prompt.txt"), "--runtime-json", "{}",
        "--runtime-file", str(runtime),
    ]


def _prepare(root: Path, runtime: Path, *, uid: str = "w4storecall",
             round_number: int = 2) -> tuple[Path, dict]:
    """Prepare a real receipt; refuse fabricated runtime snapshot records."""
    register_round(root)
    scope_path = root / "round-scope.json"
    if round_number != 2:
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        scope["round_number"] = round_number
        scope_path.write_text(json.dumps(scope), encoding="utf-8")
    (root / "prompt.txt").write_text("fixture prompt\n", encoding="utf-8")
    assert provenance.main([
        "prepare", *_shared(root, runtime, uid=uid, round_number=round_number),
        "--expected-output-path", str(root / "result.json"),
    ]) == 0
    receipt = next((root / "provenance").glob(f"*.{uid}.preflight.json"))
    return receipt, json.loads(receipt.read_text(encoding="utf-8"))


def _record(root: Path, runtime: Path, preflight: Path) -> Path:
    """Record an inert successful call; refuse synthetic store metadata injection."""
    (root / "response.log").write_text("fixture response\n", encoding="utf-8")
    (root / "result.json").write_text("{}\n", encoding="utf-8")
    assert provenance.main([
        "record", *_shared(root, runtime), "--preflight-path", str(preflight),
        "--invoked-at", "2026-10-01T00:00:00Z", "--duration", "1",
        "--exit-code", "0", "--exit-status", "ok",
        "--response-path", str(root / "response.log"),
        "--output-path", str(root / "result.json"),
        "--usage-json", '{"status":"total-only","total_tokens":1}',
    ]) == 0
    return next((root / "provenance").glob("*.w4storecall.call.json"))


def _index(root: Path) -> list[dict[str, str]]:
    with (root / "index.tsv").open(encoding="utf-8", newline="") as fh:
        assert fh.readline() == "\t".join(store.INDEX_HEADER) + "\n"
        return list(csv.DictReader(fh, fieldnames=store.INDEX_HEADER, delimiter="\t"))


def _assert_prepare_refused(root: Path, runtime: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        provenance.main(["prepare", *_shared(root, runtime),
                         "--expected-output-path", str(root / "result.json")])
    assert exc.value.code == 2
    assert not (root / "provenance").exists()


def _load_copy(path: Path, name: str, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_two_rounds_share_one_readonly_blob_and_one_index_row(
        tmp_path: Path, minsky_store: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"runtime version one\n")
    sha = provenance.sha256_file(runtime)
    first_root, second_root = tmp_path / "round-1", tmp_path / "round-2"
    _, first = _prepare(first_root, runtime, round_number=1)
    _, second = _prepare(second_root, runtime, round_number=2)
    target = store.cas_path(minsky_store, sha)
    expected = {"path": str(target), "bytes": runtime.stat().st_size, "sha256": sha,
                "source_path": str(runtime), "store_ref": {"store_id": store.STORE_ID, "sha256": sha}}
    assert first["runtime_snapshots"] == second["runtime_snapshots"] == [expected]
    assert first["schema_version"] == second["schema_version"] == "1.1"
    checks = first["store"].pop("free_space_checks")
    assert first["store"] == {"status": "enabled", "store_id": store.STORE_ID,
                              "root": str(minsky_store), "root_source": "env:MINSKY_STORE_ROOT",
                              "ingested": [sha]}
    assert [c["incoming_bytes"] for c in checks] == [runtime.stat().st_size]     # recorded, in bytes (v0.2 §4.4)
    assert set(checks[0]) == {"target", "free_bytes", "incoming_bytes", "floor_bytes", "margin_bytes",
                              "required_bytes", "free_after_bytes", "policy_origin", "config_path",
                              "config_sha256", "floor_bytes_min", "production_floor_bytes",
                              "production_floor_source", "production_source_sha256", "production_floor_opt_out"}
    assert second["store"]["ingested"] == [] and second["store"]["free_space_checks"] == []
    assert not (first_root / "provenance" / "runtime-blobs").exists()
    assert not (second_root / "provenance" / "runtime-blobs").exists()
    assert list((minsky_store / "sha256").rglob(sha)) == [target]
    assert stat.S_IMODE(target.stat().st_mode) == 0o444
    rows = _index(minsky_store)
    assert len(rows) == 1
    assert rows[0]["sha256"] == sha and int(rows[0]["bytes"]) == runtime.stat().st_size
    assert rows[0]["first_source_path"] == str(runtime)
    assert rows[0]["first_audit_id"] == "test-audit" and rows[0]["first_round"] == "1"
    assert rows[0]["first_seen_utc"].endswith("Z")


@pytest.mark.parametrize("payload,defect", [
    (None, "No such file"), ("{truncated", "Expecting"), ("[]", "object"),
    ('{"root":"/fixture","enabled":true}', "store_id"),
    ('{"store_id":"mars-minsky-cas-v1","enabled":true}', "root"),
    ('{"store_id":"mars-minsky-cas-v1","root":"/fixture"}', "enabled"),
])
def test_required_store_config_refuses_prepare_before_any_write(
        tmp_path: Path, monkeypatch, capsys, payload: str | None, defect: str) -> None:
    skill = tmp_path / "skill"
    path = skill / "config" / "store.json"
    if payload is not None:
        path.parent.mkdir(parents=True)
        path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(store, "SKILL_DIR", skill)
    with pytest.raises(store.StoreConfigError) as exc:
        store.load_store_config(config_path=path)
    assert str(path) in str(exc.value) and defect in str(exc.value)
    root = tmp_path / "unwritten-round"
    _assert_prepare_refused(root, tmp_path / "absent-runtime")
    assert not root.exists()
    assert str(path) in capsys.readouterr().err


@pytest.mark.parametrize("overrides,defect", [
    ({"enabled": 1}, "enabled"), ({"enabled": "true"}, "enabled"),
    ({"store_id": "foreign"}, "store_id"), ({"root": "relative"}, "absolute"),
    ({"root": 3}, "root"), ({"root": ""}, "root"),
    ({"enabled": False}, "disabled_by"),
    ({"enabled": False, "disabled_by": " "}, "disabled_by"),
    ({"enabled": False, "disabled_by": "host"}, "reason"),
    ({"enabled": False, "disabled_by": "host", "reason": ""}, "reason"),
])
def test_store_config_refuses_malformed_values(
        tmp_path: Path, overrides: dict, defect: str) -> None:
    config = _config(tmp_path / "store.json", **overrides)
    with pytest.raises(store.StoreConfigError, match=defect) as exc:
        store.load_store_config(config, env={})
    assert str(config) in str(exc.value)


@pytest.mark.parametrize("override", ["", "relative"])
def test_store_root_override_must_be_absolute(tmp_path: Path, override: str) -> None:
    config = _config(tmp_path / "store.json")
    with pytest.raises(store.StoreConfigError, match="MINSKY_STORE_ROOT"):
        store.load_store_config(config, env={"MINSKY_STORE_ROOT": override})


def test_disabled_store_requires_actor_and_records_per_round_copy(
        tmp_path: Path, monkeypatch, minsky_store: Path) -> None:
    skill = tmp_path / "skill"
    path = _config(skill / "config" / "store.json", enabled=False, reason="fixture rollback")
    monkeypatch.setattr(store, "SKILL_DIR", skill)
    _assert_prepare_refused(tmp_path / "refused-round", tmp_path / "absent-runtime")
    _config(path, enabled=False, disabled_by="fixture host", reason="fixture rollback")
    cfg = store.load_store_config(path, env={"MINSKY_STORE_ROOT": "relative-is-ignored"})
    assert cfg.status == "disabled"
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"disabled-mode runtime\n")
    root = tmp_path / "round-2"
    preflight, receipt = _prepare(root, runtime)
    assert receipt["store"] == {"status": "disabled", "disabled_by": "fixture host",
                                "reason": "fixture rollback"}
    snapshot = receipt["runtime_snapshots"][0]
    assert snapshot["path"] == str(root / "provenance" / "runtime-blobs" / snapshot["sha256"])
    assert "store_ref" not in snapshot
    assert Path(snapshot["path"]).read_bytes() == runtime.read_bytes()
    manifest = json.loads(_record(root, runtime, preflight).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.1" and manifest["store"] == receipt["store"]
    assert list(minsky_store.iterdir()) == [minsky_store / "STORE_ID"]


def test_config_root_source_and_record_copy_are_preserved(
        tmp_path: Path, monkeypatch, minsky_store: Path) -> None:
    skill = tmp_path / "skill"
    _config(skill / "config" / "store.json", root=str(minsky_store))
    monkeypatch.setattr(store, "SKILL_DIR", skill)
    monkeypatch.delenv("MINSKY_STORE_ROOT")
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"configured runtime\n")
    root = tmp_path / "round-2"
    preflight, receipt = _prepare(root, runtime)
    assert receipt["store"]["root_source"] == "store.json"
    # Record copies the original receipt even if configuration subsequently changes.
    monkeypatch.setenv("MINSKY_STORE_ROOT", str(tmp_path / "missing-root"))
    manifest = json.loads(_record(root, runtime, preflight).read_text(encoding="utf-8"))
    assert manifest["store"] == receipt["store"] and manifest["schema_version"] == "1.1"


@pytest.mark.parametrize("defect", ["missing-root", "missing-marker", "wrong-marker", "unwritable"])
def test_store_validation_refuses_prepare_before_any_write(
        tmp_path: Path, monkeypatch, minsky_store: Path, capsys, defect: str) -> None:
    root = minsky_store
    if defect == "missing-root":
        root = tmp_path / "missing-store"
        monkeypatch.setenv("MINSKY_STORE_ROOT", str(root))
    elif defect == "missing-marker":
        (root / "STORE_ID").unlink()
    elif defect == "wrong-marker":
        (root / "STORE_ID").write_text("foreign-store\n", encoding="utf-8")
    else:
        monkeypatch.setattr(store.os, "access", lambda path, mode: False)
    before = sorted(root.iterdir()) if root.exists() else []
    round_dir = tmp_path / "unwritten-round"
    _assert_prepare_refused(round_dir, tmp_path / "absent-runtime")
    assert not round_dir.exists()
    assert (sorted(root.iterdir()) if root.exists() else []) == before
    assert str(root) in capsys.readouterr().err


@pytest.mark.parametrize("marker", ["mars-minsky-cas-v1", "mars-minsky-cas-v1\n"])
def test_marker_accepts_only_the_optional_single_newline(
        minsky_store: Path, marker: str) -> None:
    (minsky_store / "STORE_ID").write_text(marker, encoding="utf-8")
    store.validate_store(store.load_store_config())


@pytest.mark.parametrize("marker", ["mars-minsky-cas-v1\n\n", "mars-minsky-cas-v1\r\n", " mars-minsky-cas-v1"])
def test_marker_refuses_extra_whitespace(minsky_store: Path, marker: str) -> None:
    (minsky_store / "STORE_ID").write_bytes(marker.encode("utf-8"))
    with pytest.raises(store.StoreConfigError, match="STORE_ID"):
        store.validate_store(store.load_store_config())


def test_preexisting_corrupt_cas_is_refused_without_overwrite(
        tmp_path: Path, minsky_store: Path, capsys) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"correct runtime\n")
    target = store.cas_path(minsky_store, provenance.sha256_file(runtime))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt runtime\n")
    root = tmp_path / "round-2"
    with pytest.raises(SystemExit):
        _prepare(root, runtime)
    assert f"runtime snapshot mismatch: {target}" in capsys.readouterr().err
    assert target.read_bytes() == b"corrupt runtime\n"
    assert not (minsky_store / "index.tsv").exists()
    assert not (root / "provenance").exists()


@pytest.mark.parametrize("headroom", [-1, 0, 1])
def test_free_space_accounts_for_incoming_floor_and_margin(
        tmp_path: Path, monkeypatch, minsky_store: Path, headroom: int, capsys) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"free-space fixture\n")
    policy = free_space.load_policy()
    incoming = runtime.stat().st_size
    free = policy["floor_bytes"] + policy["margin_bytes"] + incoming + headroom
    seen = []
    real_check = free_space.check_free_space

    def check(target: Path, byte_count: int) -> dict:
        seen.append((target, byte_count))
        return real_check(target, byte_count, disk_usage=lambda path: SimpleNamespace(free=free))

    monkeypatch.setattr(free_space, "check_free_space", check)
    root = tmp_path / "round-2"
    if headroom < 0:
        with pytest.raises(SystemExit) as exc:
            _prepare(root, runtime)
        assert exc.value.code == 2
        assert not (root / "provenance").exists()
        assert list(minsky_store.iterdir()) == [minsky_store / "STORE_ID"]
        message = capsys.readouterr().err
        for name, value in (("incoming_bytes", incoming), ("free_bytes", free),
                            ("floor_bytes", policy["floor_bytes"]),
                            ("margin_bytes", policy["margin_bytes"])):
            assert f"{name}={value} bytes" in message
    else:
        _prepare(root, runtime)
        assert store.cas_path(minsky_store, provenance.sha256_file(runtime)).is_file()
    assert seen == [(minsky_store, incoming)]


def test_existing_blob_needs_no_new_free_space_check(
        tmp_path: Path, monkeypatch, minsky_store: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"already stored\n")
    _prepare(tmp_path / "first", runtime)
    monkeypatch.setattr(free_space, "check_free_space",
                        lambda *args: pytest.fail("existing blob must not reserve incoming bytes"))
    _, receipt = _prepare(tmp_path / "second", runtime)
    assert receipt["store"]["ingested"] == [] and len(_index(minsky_store)) == 1


def test_runtime_change_during_copy_refuses_and_removes_temp(
        tmp_path: Path, monkeypatch, minsky_store: Path, capsys) -> None:
    import shutil

    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"original\n")
    monkeypatch.setattr(shutil, "copyfileobj", lambda src, dst: dst.write(b"changed\n"))
    with pytest.raises(SystemExit):
        _prepare(tmp_path / "round-2", runtime)
    assert "runtime changed during snapshot" in capsys.readouterr().err
    assert not (minsky_store / "index.tsv").exists()
    assert not list(minsky_store.rglob("*.tmp"))
    assert not list((minsky_store / "sha256").rglob(provenance.sha256_file(runtime)))


def test_concurrent_processes_publish_one_blob_and_index_row(
        tmp_path: Path, minsky_store: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"race fixture\n")
    program = '''
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import provenance
import store
root, runtime, barrier = map(Path, sys.argv[2:5])
worker = sys.argv[5]
real_link = os.link
def link(source, target):
    (barrier / (worker + ".ready")).write_text("ready")
    deadline = time.monotonic() + 15
    while len(list(barrier.glob("*.ready"))) != 2:
        if time.monotonic() > deadline:
            raise RuntimeError("ingest barrier timeout")
        time.sleep(0.01)
    return real_link(source, target)
provenance.os.link = link
cfg = store.load_store_config()
records = provenance.snapshot_runtime([provenance.file_record(str(runtime))], store=cfg,
                                      audit_id="race-fixture", round_number=1)
print(json.dumps({"records": records, "ingested": sorted(cfg.ingested)}))
'''
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    processes = [subprocess.Popen(
        [sys.executable, "-c", program, str(SCRIPTS), str(minsky_store), str(runtime),
         str(barrier), str(worker)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    ) for worker in range(2)]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    sha = provenance.sha256_file(runtime)
    target = store.cas_path(minsky_store, sha)
    assert results[0]["records"] == results[1]["records"]
    assert sorted(result["ingested"] for result in results) == [[], [sha]]
    assert provenance.sha256_file(target) == sha
    assert stat.S_IMODE(target.stat().st_mode) == 0o444
    assert len(_index(minsky_store)) == 1
    assert list((minsky_store / "sha256").rglob(sha)) == [target]
    assert not list(minsky_store.rglob("*.tmp"))


def test_store_host_cli_initialises_checks_and_refuses_foreign_directory(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "nested" / "new-store"
    assert store.main(["init", "--root", str(root)]) == 0
    assert (root / "STORE_ID").read_bytes() == (store.STORE_ID + "\n").encode("ascii")
    (root / "kept").write_bytes(b"preserve")
    assert store.main(["init", "--root", str(root)]) == 0
    assert (root / "kept").read_bytes() == b"preserve"
    config = _config(tmp_path / "config.json", root=str(root))
    monkeypatch.delenv("MINSKY_STORE_ROOT")
    assert store.main(["check", "--config", str(config)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["validation"] == "ok" and checked["root"] == str(root)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "kept").write_bytes(b"foreign")
    assert store.main(["init", "--root", str(foreign)]) == 1
    assert not (foreign / "STORE_ID").exists()
    capsys.readouterr()
    assert store.main(["check", "--config", str(tmp_path / "missing.json")]) == 1
    result = capsys.readouterr()
    assert json.loads(result.out)["validation"] == "error"
    assert "missing.json" in result.err
    (root / "STORE_ID").write_bytes(b"wrong marker")
    assert store.main(["check", "--config", str(config)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["root"] == str(root) and result["validation"] == "error"


def test_store_ref_relocates_source_preservation_and_canonical(
        tmp_path: Path, monkeypatch, minsky_store: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"relocated runtime\n")
    root = tmp_path / "round-2"
    preflight, receipt = _prepare(root, runtime)
    manifest_path = _record(root, runtime, preflight)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["store"] == receipt["store"]
    moved = tmp_path / "store-B"
    minsky_store.rename(moved)
    monkeypatch.setenv("MINSKY_STORE_ROOT", str(moved))
    snapshot = manifest["runtime_snapshots"][0]
    target = store.cas_path(moved, snapshot["sha256"])
    assert not Path(snapshot["path"]).exists()
    assert LiveSource().resolve(snapshot["path"], snapshot["sha256"], store_ref=snapshot["store_ref"]) == Resolution(
        True, snapshot["sha256"], "store_ref", str(target),
    )
    # T5 (host): after the root move, the canonical gate itself resolves through store_ref and re-hashes (v0.2 §3.2).
    verdict_path = tmp_path / "live-verdict.json"
    assert provenance.main(["verify-round", "--round-dir", str(root), "--mode", "live",
                            "--json-out", str(verdict_path)]) == 0
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict["verdict"] == "ok-live" and verdict["canonical"]["result"] == "ok"
    runtime_objects = [obj for obj in verdict["preservation"]["objects"] if obj["kind"] == "runtime_snapshot"]
    assert len(runtime_objects) == 1
    assert runtime_objects[0]["status"] == "ok" and runtime_objects[0]["resolved_via"] == "store_ref"
    target.chmod(0o644)
    target.write_bytes(b"corrupted relocated runtime\n")
    corrupt_verdict = tmp_path / "corrupt-verdict.json"
    assert provenance.main(["verify-round", "--round-dir", str(root), "--mode", "live",
                            "--json-out", str(corrupt_verdict)]) == 1
    objects = json.loads(corrupt_verdict.read_text(encoding="utf-8"))["preservation"]["objects"]
    assert next(obj for obj in objects if obj["kind"] == "runtime_snapshot")["status"] == "mismatch"


def test_invalid_index_header_refuses_new_receipt(
        tmp_path: Path, minsky_store: Path, capsys) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"index refusal fixture\n")
    index = minsky_store / "index.tsv"
    index.write_bytes(b"wrong\theader\n")
    root = tmp_path / "round-2"
    with pytest.raises(SystemExit):
        _prepare(root, runtime)
    assert f"refused index {index}: header mismatch" in capsys.readouterr().err
    assert index.read_bytes() == b"wrong\theader\n"
    assert not (root / "provenance").exists()


def test_multiple_runtime_names_share_blob_and_sorted_ingested_report(
        tmp_path: Path, minsky_store: Path) -> None:
    runtimes = [tmp_path / name for name in ("runtime-a", "runtime-b", "runtime-c")]
    for path, payload in zip(runtimes, (b"same bytes", b"different bytes", b"same bytes")):
        path.write_bytes(payload)
    cfg = store.load_store_config()
    records = [provenance.file_record(str(path)) for path in runtimes]
    snapshots = provenance.snapshot_runtime(records, store=cfg, audit_id="multi-runtime", round_number=1)
    assert len(snapshots) == 3 and snapshots[0]["path"] == snapshots[2]["path"]
    assert cfg.receipt()["ingested"] == sorted({record["sha256"] for record in records})
    assert len(_index(minsky_store)) == 2
    provenance.snapshot_runtime(records, store=cfg, audit_id="multi-runtime", round_number=2)
    assert cfg.receipt()["ingested"] == [] and len(_index(minsky_store)) == 2


def test_store_ref_never_replaces_existing_live_bytes_and_is_lazy(
        tmp_path: Path, monkeypatch, minsky_store: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"original runtime\n")
    _, receipt = _prepare(tmp_path / "round-2", runtime)
    snapshot = receipt["runtime_snapshots"][0]
    target = Path(snapshot["path"])
    target.chmod(0o644)
    target.write_bytes(b"corrupt live blob\n")
    monkeypatch.setattr(store, "load_store_config", lambda: pytest.fail("must use live path first"))
    result = LiveSource().resolve(str(target), snapshot["sha256"], store_ref=snapshot["store_ref"])
    assert result.resolved_via == "live" and result.sha256 != snapshot["sha256"]
    assert LiveSource().resolve(str(tmp_path / "missing"), None) == Resolution(False, None, "live", "")


@pytest.mark.parametrize("defect,detail", [
    ("config", "store-config-error:"), ("disabled", "store-disabled"),
    ("marker", "store-id-mismatch"), ("ref-id", "store-id-mismatch"),
    ("ref-sha", "store-ref-sha256-mismatch"), ("ref-path", "store-ref-invalid:"),
    ("ref-shape", "store-ref-invalid"),
])
def test_store_ref_refuses_config_marker_and_reference_defects(
        tmp_path: Path, monkeypatch, minsky_store: Path, defect: str, detail: str) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"store ref fixture\n")
    _, receipt = _prepare(tmp_path / "round-2", runtime)
    snapshot = receipt["runtime_snapshots"][0]
    ref = snapshot["store_ref"].copy()
    if defect == "config":
        monkeypatch.setattr(store, "SKILL_DIR", tmp_path / "missing-skill")
    elif defect == "disabled":
        config = _config(tmp_path / "skill" / "config" / "store.json", enabled=False,
                         disabled_by="host", reason="rollback")
        monkeypatch.setattr(store, "SKILL_DIR", config.parents[1])
    elif defect == "marker":
        (minsky_store / "STORE_ID").write_bytes(b"foreign marker")
    elif defect == "ref-id":
        ref["store_id"] = "foreign"
    elif defect == "ref-sha":
        ref["sha256"] = "0" * 64
    elif defect == "ref-path":
        ref["sha256"] = "../../outside"
    else:
        ref = []
    result = LiveSource().resolve(str(tmp_path / "old-store" / snapshot["sha256"]),
                                  snapshot["sha256"], store_ref=ref)
    assert not result.present and result.detail.startswith(detail)


def test_archive_ignores_store_ref_and_live_explicit_root_is_validated(
        tmp_path: Path, minsky_store: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"archive-compatible runtime\n")
    _, receipt = _prepare(tmp_path / "round-2", runtime)
    snapshot = receipt["runtime_snapshots"][0]
    source = ArchiveSource(store_root=minsky_store, pack=None, remaps=[], no_live=True)
    assert source.resolve("/old/path", snapshot["sha256"], store_ref={"store_id": "foreign"}).sha256 == snapshot["sha256"]
    assert LiveSource(minsky_store).resolve("/old/path", snapshot["sha256"],
                                           store_ref=snapshot["store_ref"]).resolved_via == "store_ref"


def test_missing_config_per_round_fallback_mutant_is_killed(
        tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"mutation fixture\n")
    root = tmp_path / "round-2"
    register_round(root)
    (root / "prompt.txt").write_text("fixture prompt\n", encoding="utf-8")
    skill = tmp_path / "missing-skill"
    monkeypatch.setattr(store, "SKILL_DIR", skill)
    _assert_prepare_refused(root, runtime)
    original = (SCRIPTS / "store.py").read_text(encoding="utf-8")
    needle = "    env = os.environ if env is None else env\n"
    assert original.count(needle) == 1
    mutant = original.replace(needle, needle + (
        '    if not path.exists():\n'
        '        return StoreConfig(STORE_ID, Path("/unused"), False,\n'
        '                           disabled_by="mutant", reason="missing config fallback")\n'
    ))
    copy = tmp_path / "store_missing_config_fallback.py"
    copy.write_text(mutant, encoding="utf-8")
    module = _load_copy(copy, "w4_missing_config_mutant", monkeypatch)
    monkeypatch.setattr(module, "SKILL_DIR", skill)
    monkeypatch.setattr(store, "load_store_config", module.load_store_config)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        _assert_prepare_refused(root, runtime)


def _assert_rehash_refusal(source: LiveSource, recorded: str, sha: str, ref: dict) -> None:
    assert source.resolve(recorded, sha, store_ref=ref).sha256 != sha


def test_store_ref_filename_without_rehash_mutant_is_killed(
        tmp_path: Path, monkeypatch, minsky_store: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"mutation runtime\n")
    _, receipt = _prepare(tmp_path / "round-2", runtime)
    snapshot = receipt["runtime_snapshots"][0]
    target = Path(snapshot["path"])
    target.chmod(0o644)
    target.write_bytes(b"wrong bytes\n")
    recorded = str(tmp_path / "missing-old-root" / snapshot["sha256"])
    _assert_rehash_refusal(LiveSource(), recorded, snapshot["sha256"], snapshot["store_ref"])
    original = (SCRIPTS / "byte_sources.py").read_text(encoding="utf-8")
    needle = 'return Resolution(True, provenance.sha256_file(target), "store_ref", str(target))'
    assert original.count(needle) == 1
    copy = tmp_path / "byte_sources_filename_without_rehash.py"
    copy.write_text(original.replace(needle, 'return Resolution(True, sha, "store_ref", str(target))'),
                    encoding="utf-8")
    module = _load_copy(copy, "w4_filename_without_rehash_mutant", monkeypatch)
    with pytest.raises(AssertionError):
        _assert_rehash_refusal(module.LiveSource(), recorded, snapshot["sha256"], snapshot["store_ref"])


def test_autouse_store_is_unique_and_beneath_test_temp(minsky_store: Path) -> None:
    root = Path(os.environ["MINSKY_TEST_TMP"]).resolve()
    assert minsky_store.resolve().is_relative_to(root)
    assert store.load_store_config().root == minsky_store
    assert list(minsky_store.iterdir()) == [minsky_store / "STORE_ID"]


def test_record_without_runtime_uses_schema_11_and_explicit_store(tmp_path: Path) -> None:
    root = tmp_path / "round-2"
    register_round(root)
    output = root / "result.json"
    output.write_bytes(b"{}\n")
    path = record_call(root, uid="w4noruntime", step="codex", model="gpt-5.6-sol",
                       effort="medium", persona=PERSONA, outputs=[output],
                       invoked_at="2026-10-01T00:00:00Z",
                       usage={"status": "total-only", "total_tokens": 1})
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "1.1" and receipt["store"]["ingested"] == []
    assert provenance.reconcile_artifact(root, step="codex", persona=PERSONA,
                                        output_path=output, registered_models=MODELS)[0]


def test_blob_without_index_row_is_indexed_on_the_next_prepare(
        tmp_path: Path, minsky_store: Path) -> None:
    """Host addition (Astra pre-review, W4): a blob published before a failed index append must not stay unindexed."""
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"orphaned blob fixture\n")
    _prepare(tmp_path / "round-2", runtime)
    index = minsky_store / "index.tsv"
    lines = index.read_bytes().splitlines(keepends=True)
    index.write_bytes(lines[0])                              # simulate: blob published, its row never written
    assert _index(minsky_store) == []
    _prepare(tmp_path / "round-3", runtime)                  # the blob already exists; the row must be restored
    rows = _index(minsky_store)
    assert len(rows) == 1 and rows[0]["sha256"] == hashlib.sha256(runtime.read_bytes()).hexdigest()
    _prepare(tmp_path / "round-4", runtime)
    assert len(_index(minsky_store)) == 1                    # idempotent: never a duplicate row
