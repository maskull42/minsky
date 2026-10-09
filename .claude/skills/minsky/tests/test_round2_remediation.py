"""Synthetic-only regressions for Astra's accepted round-2 recorder findings."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_hardening import (PERSONA, MODELS, provenance, pack_build, record_call,
                            register_round, load_script)


def reconcile(root, output):
    return provenance.reconcile_artifact(root, step="codex", persona=PERSONA,
                                        output_path=output, registered_models=MODELS)


def fixture_call(root, usage=None, outputs=None):
    register_round(root)
    output = root / "result.json"
    output.write_text("{}")
    path = record_call(root, uid="firstcall", step="codex", model="gpt-5.6-sol",
                       effort="medium", outputs=outputs or [output], persona=PERSONA,
                       invoked_at="2026-09-05T10:00:00Z", usage=usage)
    return output, path


@pytest.mark.parametrize("usage", [{"status": "unavailable"},
                                    {"status": "total-only", "total_tokens": 0},
                                    {"status": "incomplete", "issues": ["missing"]}])
def test_external_unknown_zero_or_incomplete_usage_cannot_pass(tmp_path, usage):
    output, path = fixture_call(tmp_path / "round-2", usage)
    assert json.loads(path.read_text())["exit_status"] == "usage-invalid"
    assert not reconcile(tmp_path / "round-2", output)[0]


def test_newer_prepare_only_blocks_old_success(tmp_path):
    root = tmp_path / "round-2"
    output, _ = fixture_call(root, {"status": "total-only", "total_tokens": 1})
    assert reconcile(root, output)[0]
    receipt = json.loads(next((root / "provenance").glob("*.preflight.json")).read_text())
    receipt.update(call_uid="interrupted", prepared_at="2099-01-01T00:00:00Z")
    (root / "provenance" / "codex.interrupted.preflight.json").write_text(json.dumps(receipt))
    assert "newest prepared" in reconcile(root, output)[1]


def test_missing_sibling_output_cannot_pass(tmp_path):
    root = tmp_path / "round-2"
    root.mkdir()
    sibling = root / "snapshot.json"
    sibling.write_text("{}")
    output, _ = fixture_call(root, {"status": "total-only", "total_tokens": 1},
                             [root / "result.json", sibling])
    sibling.unlink()
    assert "sibling" in reconcile(root, output)[1]


@pytest.mark.parametrize("bad", ["{truncated", "[]", '{"part":[]}'])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_every_malformed_nonblank_record_invalidates_accounting(tmp_path, capsys, bad, position):
    valid = json.dumps({"type": "step_finish", "part": {"tokens": {
        "total": 2, "input": 1, "output": 1, "reasoning": 0,
        "cache": {"read": 0, "write": 0}}, "cost": 0}})
    lines = [valid, valid]
    lines.insert(position, bad)
    log = tmp_path / "events"
    log.write_text("\n".join(lines))
    provenance.main(["parse-opencode-usage", "--log", str(log)])
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"


def test_runtime_snapshot_survives_upgrade_but_not_corruption(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"before")
    snapshots = provenance.snapshot_runtime([provenance.file_record(str(runtime))], tmp_path / "p")
    runtime.write_bytes(b"after")
    preserved = Path(snapshots[0]["path"])
    assert preserved.read_bytes() == b"before"
    preserved.write_bytes(b"corrupt")
    runtime.write_bytes(b"before")
    with pytest.raises(SystemExit):
        provenance.snapshot_runtime([provenance.file_record(str(runtime))], tmp_path / "p")


def test_credential_import_ignores_configuration_and_shell_code(tmp_path, monkeypatch):
    credentials = load_script("credentials_test", "credential-value.py")
    for key in ("GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENCODE_MODEL", "google/gemini-3.8-flash")
    path = tmp_path / ".env.synthetic"
    path.write_text('GOOGLE_API_KEY=synthetic-not-a-secret\nOPENCODE_MODEL=wrong\n'
                    'OPENCODE_VARIANT=low\nOPENCODE_TIMEOUT_SECONDS=999999\n'
                    'IGNORED=$(touch SHOULD_NOT_EXIST)\n')
    assert credentials.credential_value(str(path), "GOOGLE_GENERATIVE_AI_API_KEY") == "synthetic-not-a-secret"
    import os
    assert os.environ["OPENCODE_MODEL"] == "google/gemini-3.8-flash"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    with pytest.raises(ValueError):
        credentials.credential_value(str(path), "OPENCODE_MODEL")


def test_absent_optional_persona_memory_is_explicit(tmp_path):
    assert 'status="absent"' in pack_build.build_persona_memory(tmp_path / "round-1", 2)


def test_dispatch_model_drift_rejected():
    from argparse import Namespace
    args = Namespace(origin="wrapper", client_name="opencode-cli", step="opencode",
                     provider="google", model="gemini-3.8-flash", effort="high")
    provenance.validate_dispatch(args, {"argv": ["--model", "google/gemini-3.8-flash", "--variant", "high"]})
    with pytest.raises(SystemExit):
        provenance.validate_dispatch(args, {"argv": ["--model", "google/gemini-3.7-flash", "--variant", "high"]})


