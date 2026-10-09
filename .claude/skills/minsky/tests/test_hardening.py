from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import provenance


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


converge = load_script("minsky_converge", "converge.py")
pack_build = load_script("minsky_pack_build", "pack-build.py")
claude_synth = load_script("minsky_claude_synth", "claude-synth.py")


PERSONA = "agentic-context-engineering"
MODELS = ["gpt-6-astra@not_exposed", "gpt-5.6-sol@medium", "gemini-3.8-flash@high"]
STEP_MODELS = {
    "claude_self": MODELS[0], "codex": MODELS[1],
    "opencode": MODELS[2], "claude_synth": MODELS[0],
}


def register_round(round_dir: Path, audit_id: str = "test-audit") -> None:
    assert provenance.main([
        "register-round", "--audit-id", audit_id, "--round", "2",
        "--round-dir", str(round_dir), "--personas", json.dumps([PERSONA]),
        "--models", json.dumps(MODELS), "--step-models", json.dumps(STEP_MODELS),
    ]) == 0


def finding_doc(persona: str = PERSONA, findings: list[dict] | None = None) -> dict:
    return {
        "persona": persona,
        "findings": findings or [],
        "verdict": {"agree": "true", "reasoning": "No material issue under this lens."},
    }


def record_call(round_dir: Path, *, uid: str, step: str, model: str, effort: str,
                outputs: list[Path], invoked_at: str, persona: str | None = None,
                status: str = "ok", origin: str = "wrapper", usage: dict | None = None,
                raw_response: bool = True, context_files: list[Path] | None = None) -> Path:
    evidence = round_dir / "test-evidence"
    evidence.mkdir(exist_ok=True)
    prompt = evidence / f"{uid}.prompt.txt"
    prompt.write_text(f"exact prompt {uid}\n", encoding="utf-8")
    provider = "openai" if model != "gemini-3.8-flash" else "google"
    shared = [
        "--call-uid", uid, "--audit-id", "test-audit", "--round", "2",
        "--round-dir", str(round_dir), "--step", step, "--origin", origin,
        "--provider", provider, "--model", model, "--effort", effort,
        "--client-name", "test-client", "--client-version", "1.0",
        "--prompt-path", str(prompt), "--runtime-json", "{}",
    ]
    if persona:
        shared += ["--persona", persona]
    prepare_argv = ["prepare", *shared]
    for output in outputs:
        prepare_argv += ["--expected-output-path", str(output)]
    for context_file in context_files or []:
        prepare_argv += ["--context-file", str(context_file)]
    assert provenance.main(prepare_argv) == 0
    preflight_matches = list((round_dir / "provenance").glob(f"*.{uid}.preflight.json"))
    assert len(preflight_matches) == 1
    argv = [
        "record", *shared, "--preflight-path", str(preflight_matches[0]),
        "--exit-status", status, "--usage-json", json.dumps(
            usage or {"status": "unavailable", "source": "not exposed"}
        ),
    ]
    if origin == "host":
        argv += ["--invoked-at-not-exposed", "--duration-not-exposed", "--exit-code-not-exposed"]
    else:
        argv += ["--invoked-at", invoked_at, "--duration", "1.25",
                 "--exit-code", "0" if status == "ok" else "1"]
    if raw_response:
        response = evidence / f"{uid}.response.log"
        response.write_text(f"raw response {uid}\n", encoding="utf-8")
        argv += ["--response-path", str(response)]
    else:
        argv += ["--response-unavailable-reason", "host transcript is not exposed"]
    for output in outputs:
        argv += ["--output-path", str(output)]
    for context_file in context_files or []:
        argv += ["--context-file", str(context_file)]
    assert provenance.main(argv) == 0
    matches = list((round_dir / "provenance").glob(f"*.{uid}.call.json"))
    assert len(matches) == 1
    return matches[0]


def test_explicit_persona_and_model_lists_are_strict() -> None:
    assert provenance.parse_string_list(json.dumps([PERSONA]), "p", persona=True) == [PERSONA]
    assert provenance.parse_string_list(json.dumps([MODELS[0]]), "m", model=True) == [MODELS[0]]
    with pytest.raises(SystemExit):
        provenance.parse_string_list("[]", "p", persona=True)
    with pytest.raises(SystemExit):
        provenance.parse_string_list(json.dumps([PERSONA, PERSONA]), "p", persona=True)
    with pytest.raises(SystemExit):
        provenance.parse_string_list('["gpt-5.6-sol"]', "m", model=True)
    with pytest.raises(SystemExit):
        provenance.parse_string_list('["not-a-real-persona"]', "p", persona=True)


def test_opencode_usage_is_complete_or_explicitly_incomplete(tmp_path: Path, capsys) -> None:
    log = tmp_path / "events.ndjson"
    log.write_text("\n".join([
        json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {
            "total": 16, "input": 2, "output": 3, "reasoning": 1,
            "cache": {"read": 10, "write": 0}}, "cost": 0.02}}),
        json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {
            "total": 9, "input": 1, "output": 2, "reasoning": 1,
            "cache": {"read": 4, "write": 1}}, "cost": 0.03}}),
    ]) + "\n", encoding="utf-8")
    provenance.main(["parse-opencode-usage", "--log", str(log)])
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {
        "status": "complete", "input_tokens": 3, "output_tokens": 5,
        "reasoning_tokens": 2, "cache_read_tokens": 14,
        "cache_write_tokens": 1, "total_tokens": 25, "cost_usd": 0.05,
        "step_finish_events": 2,
    }

    log.write_text(json.dumps({"type": "step_finish", "part": {
        "type": "step-finish", "tokens": {"total": 1, "input": 1}, "cost": 0.01,
    }}) + "\n", encoding="utf-8")
    provenance.main(["parse-opencode-usage", "--log", str(log)])
    incomplete = json.loads(capsys.readouterr().out)
    assert incomplete["status"] == "incomplete"
    assert not any(key.endswith("_tokens") for key in incomplete)


def test_host_manifest_is_honest_and_content_addressed(tmp_path: Path) -> None:
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    pack = round_dir / "pack.xml"
    pack.write_text("<pack/>\n", encoding="utf-8")
    output = round_dir / "claude-self" / "report.md"
    output.parent.mkdir(parents=True)
    output.write_text("observable host output\n", encoding="utf-8")
    manifest_path = record_call(
        round_dir, uid="hostcall01", step="claude_self", model="gpt-6-astra",
        effort="not_exposed", outputs=[output], invoked_at="2026-09-05T10:00:00Z",
        origin="host", raw_response=False, context_files=[pack],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["response"]["status"] == "unavailable"
    assert manifest["usage"]["status"] == "unavailable"
    assert manifest["exit_code"] is None and manifest["exit_code_status"] == "not_exposed"
    assert manifest["invoked_at"] is None and manifest["duration_seconds"] is None
    assert "hidden-host-context" in manifest["context_status"]
    assert manifest["outputs"][0]["sha256"] == provenance.sha256_file(output)


def test_preflight_detects_drift_exit_mismatch_and_wrong_phase_model(tmp_path: Path) -> None:
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    context = round_dir / "pack.xml"
    context.write_text("before\n", encoding="utf-8")
    prompt = round_dir / "prompt.txt"
    prompt.write_text("exact\n", encoding="utf-8")
    output = round_dir / "codex" / f"{PERSONA}.json"
    output.parent.mkdir()
    output.write_text(json.dumps(finding_doc()), encoding="utf-8")
    shared = [
        "--call-uid", "driftcall1", "--audit-id", "test-audit", "--round", "2",
        "--round-dir", str(round_dir), "--step", "codex", "--persona", PERSONA,
        "--origin", "wrapper", "--provider", "openai", "--model", "gpt-5.6-sol",
        "--effort", "medium", "--client-name", "test", "--client-version", "1",
        "--prompt-path", str(prompt), "--runtime-json", '{"timeout_seconds":1800}',
        "--context-file", str(context),
    ]
    provenance.main(["prepare", *shared, "--expected-output-path", str(output)])
    preflight = next((round_dir / "provenance").glob("*.driftcall1.preflight.json"))
    context.write_text("after\n", encoding="utf-8")
    response = round_dir / "response.log"
    response.write_text("raw\n", encoding="utf-8")
    provenance.main([
        "record", *shared, "--preflight-path", str(preflight),
        "--invoked-at", "2026-09-05T10:00:00Z", "--duration", "1",
        "--exit-code", "0", "--exit-status", "ok", "--response-path", str(response),
        "--output-path", str(output),
        "--usage-json", '{"status":"total-only","total_tokens":10}',
    ])
    manifest = json.loads(next((round_dir / "provenance").glob("*.driftcall1.call.json")).read_text())
    assert manifest["exit_status"] == "input-drift"
    assert manifest["preflight_verification"]["status"] == "drift"

    with pytest.raises(SystemExit):
        provenance.main([
            "prepare", "--call-uid", "wrongmodel", "--audit-id", "test-audit",
            "--round", "2", "--round-dir", str(round_dir), "--step", "opencode",
            "--persona", PERSONA, "--origin", "wrapper", "--provider", "openai",
            "--model", "gpt-5.6-sol", "--effort", "medium", "--client-name", "test",
            "--client-version", "1", "--prompt-path", str(prompt),
            "--expected-output-path", str(output), "--runtime-json", "{}",
        ])

    context.write_text("stable\n", encoding="utf-8")
    shared2 = [value if value != "driftcall1" else "exitcall1" for value in shared]
    provenance.main(["prepare", *shared2, "--expected-output-path", str(output)])
    preflight2 = next((round_dir / "provenance").glob("*.exitcall1.preflight.json"))
    provenance.main([
        "record", *shared2, "--preflight-path", str(preflight2),
        "--invoked-at", "2026-09-05T10:01:00Z", "--duration", "1",
        "--exit-code", "7", "--exit-status", "ok", "--response-path", str(response),
        "--output-path", str(output),
        "--usage-json", '{"status":"total-only","total_tokens":10}',
    ])
    mismatch = json.loads(next((round_dir / "provenance").glob("*.exitcall1.call.json")).read_text())
    assert mismatch["exit_status"] == "exit-code-mismatch"


def test_latest_terminal_manifest_and_hash_gate_convergence(tmp_path: Path) -> None:
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    pack = round_dir / "pack.xml"
    pack.write_text("<pack/>\n", encoding="utf-8")
    self_dir = round_dir / "claude-self"
    (self_dir / "findings").mkdir(parents=True)
    report = self_dir / "report.md"
    report.write_text("Section A\n" + "x" * 220 + "\nSection B\n", encoding="utf-8")
    self_json = self_dir / "findings" / f"{PERSONA}.json"
    self_json.write_text(json.dumps(finding_doc()), encoding="utf-8")
    codex_json = round_dir / "codex" / f"{PERSONA}.json"
    codex_json.parent.mkdir()
    codex_json.write_text(json.dumps(finding_doc()), encoding="utf-8")
    gemini_json = round_dir / "opencode" / f"{PERSONA}.json"
    gemini_json.parent.mkdir()
    gemini_json.write_text(json.dumps(finding_doc()), encoding="utf-8")

    record_call(
        round_dir, uid="hostcall02", step="claude_self", model="gpt-6-astra",
        effort="not_exposed", outputs=[report, self_json], invoked_at="2026-09-05T10:00:00Z",
        origin="host", raw_response=False, context_files=[pack],
    )
    record_call(
        round_dir, uid="codexcall1", step="codex", model="gpt-5.6-sol", effort="medium",
        persona=PERSONA, outputs=[codex_json], invoked_at="2026-09-05T10:01:00Z",
        usage={"status": "total-only", "total_tokens": 123},
        context_files=[pack, report, self_json],
    )
    record_call(
        round_dir, uid="geminicall1", step="opencode", model="gemini-3.8-flash", effort="high",
        persona=PERSONA, outputs=[gemini_json], invoked_at="2026-09-05T10:02:00Z",
        usage={"status": "complete", "input_tokens": 2, "output_tokens": 3,
               "reasoning_tokens": 1, "cache_read_tokens": 4, "cache_write_tokens": 0,
               "total_tokens": 10, "cost_usd": 0.01},
        context_files=[pack, report, self_json, codex_json],
    )
    assert converge.main([
        "--audit-id", "test-audit", "--round", "2", "--round-dir", str(round_dir),
        "--skip-db",
    ]) == 0
    verdict = json.loads((round_dir / "converge.json").read_text(encoding="utf-8"))
    assert verdict["provenance_reconciliation"]["status"] == "ok"

    record_call(
        round_dir, uid="geminicall2", step="opencode", model="gemini-3.8-flash", effort="high",
        persona=PERSONA, outputs=[gemini_json], invoked_at="2026-09-05T10:03:00Z",
        status="error", context_files=[pack, report, self_json, codex_json],
    )
    assert converge.main([
        "--audit-id", "test-audit", "--round", "2", "--round-dir", str(round_dir),
        "--skip-db",
    ]) == 2
    verdict = json.loads((round_dir / "converge.json").read_text(encoding="utf-8"))
    assert verdict["decision"] == "incomplete"
    assert verdict["provenance_reconciliation"]["status"] == "incomplete"


def test_stable_finding_identity_and_exact_deduplication() -> None:
    finding = {
        "severity": "medium", "category": "provenance", "claim": "Exact claim",
        "evidence": {"file_path": "x", "line_number": 1, "quoted_line": "q" * 30},
        "suggestion": "Fix it",
    }
    outputs = {"codex": {PERSONA: finding_doc(findings=[finding.copy(), finding.copy()])}}
    first = converge.build_finding_index("audit", 2, outputs)
    second = converge.build_finding_index("audit", 2, outputs)
    assert first == second
    assert len(first) == 1
    assert first[0]["source_occurrences"] == 2
    assert first[0]["finding_uid"].startswith("mf_")


def test_doc_drift_absence_is_explicit(tmp_path: Path, monkeypatch) -> None:
    drift = tmp_path / "DOCUMENTATION_DRIFT_REGISTER.md"
    monkeypatch.setattr(pack_build, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pack_build, "DOC_DRIFT_PATH", drift)
    missing = pack_build.build_doc_drift(1)
    assert 'status="missing"' in missing
    assert "not evidence that documentation is current" in missing
    drift.write_text("", encoding="utf-8")
    assert 'status="empty"' in pack_build.build_doc_drift(1)
    drift.write_text("known stale doc\n", encoding="utf-8")
    assert 'status="active"' in pack_build.build_doc_drift(1)


def test_audit_db_requires_explicit_lists_and_deduplicates_exact_rows(tmp_path: Path) -> None:
    import os

    db = tmp_path / "audit.db"
    helper = SCRIPTS / "audit-db.py"
    progress_root = tmp_path / "progress-events"
    env = {**os.environ, "MINSKY_PROGRESS_ROOT": str(progress_root)}

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(helper), "--db", str(db), *args],
            text=True, capture_output=True, env=env,
        )

    assert run("initdb").returncode == 0
    missing = run(
        "open", "--audit-id", "a", "--branch", "b", "--commit", "0" * 40,
        "--mode", "audit", "--scope", "explicit",
    )
    assert missing.returncode != 0
    opened = run(
        "open", "--audit-id", "a", "--branch", "b", "--commit", "0" * 40,
        "--mode", "audit", "--scope", "explicit", "--files", '["artifact.md"]',
        "--personas", json.dumps([PERSONA]), "--models", json.dumps([MODELS[1]]),
    )
    assert opened.returncode == 0, opened.stderr
    progress_file = progress_root / "a" / "progress.ndjson"
    assert progress_file.is_file()
    assert json.loads(progress_file.read_text(encoding="utf-8"))["event"] == "audit_open"
    round_dir = tmp_path / "round-2"
    register_round(round_dir, audit_id="a")
    host_provenance = run(
        "insert-provenance", "--audit-id", "a", "--round", "2",
        "--step", "claude_self", "--model", MODELS[0],
        "--invoked-at", "2026-09-05T10:00:00Z", "--duration", "1",
        "--output-path", str(round_dir / "host.call.json"), "--exit-status", "ok",
        "--round-scope", str(round_dir / "round-scope.json"),
    )
    assert host_provenance.returncode == 0, host_provenance.stderr
    finding_args = (
        "insert-finding", "--audit-id", "a", "--round", "2", "--step", "codex",
        "--persona", PERSONA, "--severity", "medium", "--category", "provenance",
        "--claim", "same exact claim",
    )
    assert run(*finding_args).returncode == 0
    duplicate = run(*finding_args)
    assert duplicate.returncode == 0 and duplicate.stdout.startswith("duplicate:")
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from findings").fetchone()[0] == 1
        assert conn.execute("select count(*) from provenance").fetchone()[0] == 1
        totals = conn.execute(
            "select total_input_tokens,total_output_tokens from audits where audit_id='a'"
        ).fetchone()
        assert totals == (None, None)


def test_synthesis_requires_exact_uid_coverage(tmp_path: Path) -> None:
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    synth_dir = round_dir / "claude-synth"
    synth_dir.mkdir(parents=True)
    uid = "mf_" + "a" * 24
    source = {
        "finding_uid": uid, "round": 2, "step": "codex", "persona": PERSONA,
        "severity": "medium", "category": "provenance", "claim": "Exact claim",
        "verified": True, "source_occurrences": 1,
    }
    converge_path = round_dir / "converge.json"
    converge_path.write_text(
        json.dumps({"finding_index": [source]}), encoding="utf-8"
    )
    consensus_path = round_dir / "consensus.md"
    consensus_path.write_text("consensus\n", encoding="utf-8")
    self_dir = round_dir / "claude-self"
    (self_dir / "findings").mkdir(parents=True)
    report = self_dir / "report.md"
    report.write_text("host report\n", encoding="utf-8")
    self_json = self_dir / "findings" / f"{PERSONA}.json"
    self_json.write_text(json.dumps(finding_doc()), encoding="utf-8")
    codex_json = round_dir / "codex" / f"{PERSONA}.json"
    codex_json.parent.mkdir()
    codex_json.write_text(json.dumps(finding_doc()), encoding="utf-8")
    opencode_json = round_dir / "opencode" / f"{PERSONA}.json"
    opencode_json.parent.mkdir()
    opencode_json.write_text(json.dumps(finding_doc()), encoding="utf-8")
    (synth_dir / "decisions.md").write_text("decision\n" + "x" * 120, encoding="utf-8")
    resolution = {
        "finding_uid": uid, "round": 2, "step": "codex", "persona": PERSONA,
        "claim": "Exact claim", "resolution": "addressed", "rationale": "Fixed.",
    }
    resolutions_path = synth_dir / "resolutions.json"
    resolutions_path.write_text(json.dumps([resolution]), encoding="utf-8")
    record_call(
        round_dir, uid="hostsynth1", step="claude_synth", model="gpt-6-astra",
        effort="not_exposed", outputs=[synth_dir / "decisions.md", resolutions_path],
        invoked_at="2026-09-05T11:00:00Z", origin="host", raw_response=False,
        context_files=[converge_path, consensus_path, report, self_json, codex_json, opencode_json],
    )
    assert claude_synth.main([
        "validate", "--audit-id", "test-audit", "--round", "2",
        "--round-dir", str(round_dir),
    ]) == 0
    resolution.pop("finding_uid")
    resolutions_path.write_text(json.dumps([resolution]), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        claude_synth.main([
            "validate", "--audit-id", "test-audit", "--round", "2",
            "--round-dir", str(round_dir),
        ])
    assert exc.value.code == 2
