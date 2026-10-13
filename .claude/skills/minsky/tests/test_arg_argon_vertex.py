"""ARG regressions: refuse model substitution, credential leakage and invented prices.

Only temporary W10d wrapper copies execute. Provider, identity-probe and legacy DB
boundaries are synthetic; actual provenance prepare/record and usage gates remain.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_hardening import MODELS, SCRIPTS, load_script, provenance
from test_w10d_opencode_credentials import _Wrapper, wrapper as w10d_wrapper


ARGON = "gemini-4-argon"
SYNTHETIC_KEY = "obviously-fake-argon-api-key-not-a-secret"
SYNTHETIC_TOKEN = "obviously-fake-refreshed-token-not-a-secret"
SYNTHETIC_PROJECT = "obviously-fake-argon-project"
TOKEN_METRICS = {
    "input_tokens": 2, "output_tokens": 3, "reasoning_tokens": 1,
    "cache_read_tokens": 4, "cache_write_tokens": 0, "total_tokens": 10,
}


def _event(cost: float) -> dict:
    return {"type": "step_finish", "part": {"type": "step-finish", "tokens": {
        "input": 2, "output": 3, "reasoning": 1,
        "cache": {"read": 4, "write": 0}, "total": 10}, "cost": cost}}


def _rows(path: Path) -> list[dict]:
    """Require a nonempty fake log before making claims about captured dispatch."""
    assert path.is_file(), f"fake boundary was not reached: {path.name}"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows, f"fake boundary log is empty: {path.name}"
    return rows


def _dotenv(wrapper: _Wrapper, *, credential: str | None = "present",
            project: str | None = SYNTHETIC_PROJECT) -> Path:
    """Write synthetic credentials only inside the pytest-owned wrapper copy."""
    path = wrapper.root / "synthetic-service-account.json"
    path.write_text('{"type":"service_account","private_key":"not-a-real-key"}\n', encoding="utf-8")
    lines = [f"GOOGLE_API_KEY={SYNTHETIC_KEY}", "CLOUD_REGION=us-central1", "GOOGLE_CLOUD_REGION=us-central1"]
    if credential is not None:
        value = (f"../{path.name}" if credential == "relative" else
                 path if credential == "present" else path.with_name("missing-account.json"))
        lines.append(f"GOOGLE_APPLICATION_CREDENTIALS={value}")
    if project is not None:
        lines.append(f"GOOGLE_CLOUD_PROJECT={project}")
    (wrapper.root / "repo/.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def wrapper_factory(tmp_path: Path, minsky_store: Path, monkeypatch):
    """Reuse W10d isolation; no real executable, DB helper or identity probe runs."""
    def build(name: str, stamp: str = f"{ARGON}@high") -> _Wrapper:
        root = tmp_path / name
        root.mkdir()
        # Registration's real worktree guard must inspect this isolated copy, not the host checkout.
        with monkeypatch.context() as registration:
            registration.setattr(provenance, "SKILL_DIR", root / "repo/.claude/skills/minsky")
            wrapper = w10d_wrapper.__wrapped__(root, minsky_store, SimpleNamespace(param=stamp))
        # The fake records env outside the run logs, so leakage checks examine the wrapper.
        (wrapper.fakebin / "opencode").write_text('''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

version = sys.argv[1:] == ["--version"]
log = Path(__file__).with_name("opencode.version.argv.jsonl" if version else "opencode.argv.jsonl")
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if version:
    print("opencode-fixture ARG")
    sys.exit(0)
names = ("OPENCODE_CONFIG", "GOOGLE_VERTEX_LOCATION", "GOOGLE_VERTEX_PROJECT",
         "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_API_KEY")
with Path(__file__).with_name("opencode.env.jsonl").open("a", encoding="utf-8") as stream:
    captured = {key: os.environ.get(key) for key in names}
    captured["credential_file_readable"] = Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")).is_file()
    stream.write(json.dumps(captured) + "\\n")
persona = "agentic-context-engineering"
Path(persona + ".json").write_text(json.dumps({"persona": persona, "findings": [],
    "verdict": {"agree": "true", "reasoning": "Synthetic successful review."}}), encoding="utf-8")
print(json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {
    "input": 2, "output": 3, "reasoning": 1, "cache": {"read": 4, "write": 0}, "total": 10},
    "cost": float(os.environ.get("FAKE_OPENCODE_COST", "0"))}}))
''', encoding="utf-8")
        (wrapper.fakebin / "timeout").write_text('''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

with Path(__file__).with_name("timeout.argv.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
os.execv(sys.argv[3], sys.argv[3:])
''', encoding="utf-8")
        # Stub just the legacy index boundary, preserving all actual prepare/record logic.
        (wrapper.skill / "scripts/audit-db.py").write_text('''import json
import sys
from pathlib import Path

log = Path(__file__).resolve().parents[5] / "fakebin/audit-index.jsonl"
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:]}) + "\\n")
''', encoding="utf-8")
        (wrapper.skill / "scripts/vertex-identity-probe.py").write_text('''import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--model", required=True)
parser.add_argument("--location", required=True)
parser.add_argument("--credential-mode", required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
served = os.environ.get("FAKE_SERVED_MODEL", args.model)
record = {"requested_model": args.model, "served_model_version": served, "location": args.location,
          "credential_mode": args.credential_mode, "http_status": 200,
          "checked_at": "2026-10-13T00:00:00Z", "usage": {"totalTokenCount": 1}}
args.out.write_text(json.dumps(record) + "\\n", encoding="utf-8")
log = Path(__file__).resolve().parents[5] / "fakebin/probe.jsonl"
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:], "record": record}) + "\\n")
sys.exit(0 if served == args.model else 1)
''', encoding="utf-8")
        return wrapper
    return build


def test_default_vertex_and_explicit_flash_override(wrapper_factory) -> None:
    """Wrong: default Flash/regional/key dispatch, or breaking the explicit Flash route."""
    wrapper = wrapper_factory("default")
    _dotenv(wrapper)
    result = wrapper.run(CLOUD_REGION="us-central1", GOOGLE_CLOUD_REGION="us-central1", GOOGLE_API_KEY=SYNTHETIC_KEY,
                         GOOGLE_GENERATIVE_AI_API_KEY=SYNTHETIC_KEY)
    assert result.returncode == 0, result.stderr
    calls = wrapper.calls("opencode")
    assert len(calls) == 1
    assert calls[0][calls[0].index("--model") + 1] == f"google-vertex/{ARGON}"
    environments = _rows(wrapper.fakebin / "opencode.env.jsonl")
    assert len(environments) == 1
    captured = environments[0]
    assert captured["OPENCODE_CONFIG"] == str(wrapper.skill / "config/opencode-vertex.json")
    assert captured["GOOGLE_VERTEX_LOCATION"] == "global"
    assert captured["GOOGLE_GENERATIVE_AI_API_KEY"] is None
    assert captured["GOOGLE_API_KEY"] is None
    assert captured["credential_file_readable"] is True

    flash = wrapper_factory("explicit-flash", MODELS[2])
    _dotenv(flash)
    result = flash.run(OPENCODE_MODEL="google/gemini-3.8-flash", FAKE_OPENCODE_COST="0.02")
    assert result.returncode == 0, result.stderr
    calls = flash.calls("opencode")
    assert len(calls) == 1
    assert calls[0][calls[0].index("--model") + 1] == "google/gemini-3.8-flash"
    environments = _rows(flash.fakebin / "opencode.env.jsonl")
    assert len(environments) == 1
    assert environments[0]["GOOGLE_GENERATIVE_AI_API_KEY"] == SYNTHETIC_KEY
    assert environments[0]["OPENCODE_CONFIG"] is None
    assert not (flash.fakebin / "probe.jsonl").exists()


@pytest.mark.parametrize("mode", ["repo-dotenv", "opencode"])
def test_relative_vertex_credentials_refused(wrapper_factory, mode: str) -> None:
    """Wrong: either mode resolves a relative credential or prints its value."""
    wrapper = wrapper_factory("relative-credential")
    relative = "../synthetic-service-account.json"
    if mode == "repo-dotenv":
        _dotenv(wrapper, credential="relative")
        variables = {}
    else:
        (wrapper.root / "synthetic-service-account.json").write_text("{}\n", encoding="utf-8")
        variables = {"GOOGLE_VERTEX_PROJECT": SYNTHETIC_PROJECT,
                     "GOOGLE_APPLICATION_CREDENTIALS": relative}
    result = wrapper.run(MINSKY_OPENCODE_CREDENTIALS=mode, **variables)
    assert result.returncode == 1, result.stderr
    assert "invoke-opencode: GOOGLE_APPLICATION_CREDENTIALS must be absolute" in result.stderr
    assert relative not in result.stdout + result.stderr
    assert wrapper.calls("opencode") == wrapper.calls("opencode.version") == []
    assert not (wrapper.fakebin / "probe.jsonl").exists()


def test_adc_vertex_unsets_api_keys(wrapper_factory) -> None:
    """Wrong: ADC dispatch retains either API key or loses its Vertex settings."""
    adc = wrapper_factory("adc")
    credential = adc.root / "synthetic-adc.json"
    credential.write_text("{}\n", encoding="utf-8")
    result = adc.run(MINSKY_OPENCODE_CREDENTIALS="opencode", GOOGLE_VERTEX_PROJECT=SYNTHETIC_PROJECT,
                     GOOGLE_APPLICATION_CREDENTIALS=str(credential), GOOGLE_CLOUD_REGION="us-central1",
                     GOOGLE_GENERATIVE_AI_API_KEY=SYNTHETIC_KEY, GOOGLE_API_KEY=SYNTHETIC_KEY)
    assert result.returncode == 0, result.stderr
    environments = _rows(adc.fakebin / "opencode.env.jsonl")
    assert len(environments) == 1
    assert environments[0]["GOOGLE_GENERATIVE_AI_API_KEY"] is None
    assert environments[0]["GOOGLE_API_KEY"] is None
    assert environments[0]["credential_file_readable"] is True
    assert environments[0]["GOOGLE_VERTEX_LOCATION"] == "global"
    assert environments[0]["OPENCODE_CONFIG"] == str(adc.skill / "config/opencode-vertex.json")
    manifests = list((adc.round_dir / "provenance").glob("*.call.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["runtime"]["credential_kind"] == "vertex-adc"
    assert not (adc.root / "repo/.env").exists()


def test_vertex_credential_failures_redact_details(wrapper_factory) -> None:
    """Wrong: a missing path/project dispatches, or failure prints synthetic secrets."""
    for name, credential, project, message in (
        ("missing-credential", None, SYNTHETIC_PROJECT, "required provider credential is unavailable"),
        ("nonexistent-credential", "missing", SYNTHETIC_PROJECT,
         "required Vertex credential must be a readable regular file"),
        ("missing-project", "present", None, "required provider credential is unavailable"),
    ):
        wrapper = wrapper_factory(name)
        path = _dotenv(wrapper, credential=credential, project=project)
        result = wrapper.run()
        assert result.returncode == 1, result.stderr
        assert message in result.stderr
        for secret in (str(path), str(path.with_name("missing-account.json")), SYNTHETIC_PROJECT, SYNTHETIC_KEY):
            assert secret not in result.stdout + result.stderr
        assert wrapper.calls("opencode") == []
        assert wrapper.calls("opencode.version") == []
        assert not (wrapper.fakebin / "probe.jsonl").exists()

    adc = wrapper_factory("adc-missing-project")
    credential_path = adc.root / "synthetic-adc.json"
    credential_path.write_text("{}\n", encoding="utf-8")
    result = adc.run(MINSKY_OPENCODE_CREDENTIALS="opencode",
                     GOOGLE_APPLICATION_CREDENTIALS=str(credential_path))
    assert result.returncode == 1, result.stderr
    assert "required Vertex project is unavailable" in result.stderr
    for secret in (str(credential_path), SYNTHETIC_PROJECT, SYNTHETIC_KEY):
        assert secret not in result.stdout + result.stderr
    assert adc.calls("opencode") == adc.calls("opencode.version") == []
    assert not (adc.fakebin / "probe.jsonl").exists()


def test_preset_opencode_config_refuses_before_dispatch(wrapper_factory) -> None:
    """Wrong: silently replacing preset OpenCode configuration reaches the fake CLI."""
    wrapper = wrapper_factory("preset-config")
    _dotenv(wrapper)
    result = wrapper.run(OPENCODE_CONFIG=str(wrapper.root / "caller-config.json"))
    assert result.returncode == 1, result.stderr
    assert "invoke-opencode: OPENCODE_CONFIG is preset; minsky passes its own config" in result.stderr.splitlines()
    assert wrapper.calls("opencode") == wrapper.calls("opencode.version") == []
    assert not (wrapper.fakebin / "probe.jsonl").exists()

    missing = wrapper_factory("missing-skill-config")
    _dotenv(missing)
    config = missing.skill / "config/opencode-vertex.json"
    assert config.is_file()
    config.unlink()
    result = missing.run()
    assert result.returncode == 1, result.stderr
    assert "required minsky Vertex config is missing" in result.stderr
    assert missing.calls("opencode") == missing.calls("opencode.version") == []
    assert not (missing.fakebin / "probe.jsonl").exists()


def test_identity_probe_failure_prevents_dispatch(wrapper_factory) -> None:
    """Wrong: a failed probe dispatches OpenCode or loses its error-call evidence."""
    wrapper = wrapper_factory("wrong-served-model")
    _dotenv(wrapper)
    result = wrapper.run(FAKE_SERVED_MODEL="gemini-3.8-flash")
    assert result.returncode == 1, result.stderr
    probes = _rows(wrapper.fakebin / "probe.jsonl")
    assert len(probes) == 1
    assert probes[0]["record"]["served_model_version"] == "gemini-3.8-flash"
    assert wrapper.calls("opencode") == []
    assert wrapper.calls("opencode.version") == [["--version"]]
    assert (wrapper.round_dir / "round-scope.json").is_file()
    preflights = list((wrapper.round_dir / "provenance").glob("*.preflight.json"))
    assert len(preflights) == 1
    manifests = list((wrapper.round_dir / "provenance").glob("*.call.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    preflight = json.loads(preflights[0].read_text(encoding="utf-8"))
    assert manifest["exit_status"] == "error"
    assert manifest["exit_code"] == 1
    assert manifest["usage"] == {"status": "unavailable", "source": "no step_finish events"}
    assert manifest["preflight_verification"] == {"status": "match", "issues": []}
    assert manifest["runtime"] == preflight["runtime"]
    events = Path(manifest["response"]["path"])
    assert events.is_file() and events.read_bytes() == b""
    identity = Path(probes[0]["argv"][probes[0]["argv"].index("--out") + 1])
    outputs = {record["path"]: record for record in manifest["outputs"]}
    assert outputs[str(identity)]["sha256"] == hashlib.sha256(identity.read_bytes()).hexdigest()
    assert f"invoke-opencode: Vertex identity probe failed (see {identity})" in result.stderr.splitlines()
    assert "invoke-opencode: FAILED (status=error exit=1)" in result.stderr


@pytest.mark.parametrize("payload", [None, "{truncated"], ids=["missing", "invalid"])
def test_prepare_failure_prevents_identity_probe(wrapper_factory, payload: str | None) -> None:
    """Wrong: a refused store still allows the probe's provider request."""
    wrapper = wrapper_factory("invalid-store")
    _dotenv(wrapper)
    config = wrapper.skill / "config/store.json"
    assert config.is_file()
    if payload is None:
        config.unlink()
    else:
        config.write_text(payload, encoding="utf-8")
    result = wrapper.run()
    assert result.returncode != 0, result.stderr
    assert str(config) in result.stderr
    assert "provenance:" in result.stderr
    assert not (wrapper.fakebin / "probe.jsonl").exists()
    assert wrapper.calls("opencode") == []
    assert wrapper.calls("opencode.version") == [["--version"]]
    assert list((wrapper.round_dir / "provenance").glob("*.preflight.json")) == []


def test_probe_identity_requires_matching_served_model(tmp_path: Path, monkeypatch, capsys) -> None:
    """Wrong: mismatch/error succeeds, unrefreshed auth is used, or error details leak."""
    assert (SCRIPTS / "vertex-identity-probe.py").is_file()
    probe = load_script("argon_identity_test", "vertex-identity-probe.py")
    credential_path = tmp_path / "synthetic-service-account.json"
    credential_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credential_path))
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", SYNTHETIC_PROJECT)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", SYNTHETIC_PROJECT)
    refreshed: list[object] = []
    loaded: list[tuple[str, object]] = []
    requests: list[tuple[str, dict]] = []

    class Credentials:
        token = "stale-synthetic-token"

        def refresh(self, request):
            refreshed.append(request)
            self.token = SYNTHETIC_TOKEN

    def service_account(path, *, scopes):
        loaded.append(("repo-dotenv", path))
        assert path == str(credential_path)
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        return Credentials()

    def adc(*, scopes):
        loaded.append(("opencode", None))
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        return Credentials(), SYNTHETIC_PROJECT

    monkeypatch.setattr(probe.service_account.Credentials,
                        "from_service_account_file", service_account)
    monkeypatch.setattr(probe.google.auth, "default", adc)
    request_transport = object()
    monkeypatch.setattr(probe, "Request", lambda: request_transport)
    for mode in ("repo-dotenv", "opencode"):
        for name, status, model, raises, expected in (
            ("match", 200, ARGON, False, 0),
            ("mismatch", 200, "gemini-3.8-flash", False, 1),
            ("missing-identity", 200, None, False, 1),
            ("forbidden", 403, ARGON, False, 1),
            ("exception", None, None, True, 1),
        ):
            def post(url, **kwargs):
                requests.append((url, kwargs))
                assert kwargs["timeout"] == 120
                assert kwargs["headers"]["Authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
                assert "/locations/global/" in url and f"/models/{ARGON}:generateContent" in url
                assert kwargs["json"] == {
                    "contents": [{"role": "user", "parts": [{"text": "Reply with exactly one word: ok"}]}],
                    "generationConfig": {"maxOutputTokens": 64, "thinkingConfig": {"thinkingLevel": "low"}},
                }
                if raises:
                    raise RuntimeError(f"{SYNTHETIC_TOKEN} {credential_path} {SYNTHETIC_PROJECT}")
                return SimpleNamespace(status_code=status, json=lambda: {
                    "modelVersion": model, "usageMetadata": {"totalTokenCount": 5}, "error": {
                        "message": f"{SYNTHETIC_TOKEN} {credential_path} {SYNTHETIC_PROJECT}"}})

            monkeypatch.setattr(probe.requests, "post", post)
            out = tmp_path / f"{mode}-{name}.json"
            before = len(requests)
            assert probe.main(["--model", ARGON, "--location", "global",
                               "--credential-mode", mode, "--out", str(out)]) == expected
            assert len(requests) == before + 1
            assert len(refreshed) == len(requests) == len(loaded)
            assert refreshed[-1] is request_transport
            assert loaded[-1][0] == mode
            evidence = ""
            if not raises:
                evidence = out.read_text(encoding="utf-8")
                record = json.loads(evidence)
                assert set(record) == {"requested_model", "served_model_version", "http_status",
                                       "usage", "checked_at", "credential_mode", "location"}
                assert record["requested_model"] == ARGON
                assert record["location"] == "global"
                assert record["http_status"] == status
                assert record["credential_mode"] == mode
                assert record["usage"] == {"totalTokenCount": 5}
                assert record["checked_at"].endswith("Z")
                assert dt.datetime.fromisoformat(record["checked_at"]).utcoffset() == dt.timedelta(0)
                if status == 200:
                    assert record["served_model_version"] == model
            else:
                assert not out.exists()
            captured = capsys.readouterr()
            for secret in (SYNTHETIC_TOKEN, str(credential_path), SYNTHETIC_PROJECT):
                assert secret not in evidence + captured.out + captured.err


def test_usage_distinguishes_unknown_prices_and_enforces_totals(tmp_path: Path, capsys) -> None:
    """Wrong: zero-cost steps invent a known price, mixed pricing passes, or bad totals pass."""
    log = tmp_path / "events.ndjson"

    def parse(costs):
        log.write_text("".join(json.dumps(_event(cost)) + "\n" for cost in costs), encoding="utf-8")
        assert provenance.main(["parse-opencode-usage", "--log", str(log)]) == 0
        return json.loads(capsys.readouterr().out)

    unpriced = parse([0, 0])
    assert unpriced == {"status": "complete-unpriced", "price": "unknown",
                       **{key: value * 2 for key, value in TOKEN_METRICS.items()}, "step_finish_events": 2}
    mixed = parse([0, 0.02])
    assert mixed["status"] == "incomplete"
    assert mixed["issues"] == ["mixed priced and unpriced steps"]
    assert not any(key.endswith("_tokens") for key in mixed)
    priced = parse([0.02, 0.03])
    assert priced == {"status": "complete", **{key: value * 2 for key, value in TOKEN_METRICS.items()},
                      "cost_usd": 0.05, "step_finish_events": 2}

    valid = {"status": "complete-unpriced", "price": "unknown", **TOKEN_METRICS}
    assert provenance.validate_usage(valid) == valid
    assert provenance.external_usage_valid(valid)
    invalid = [
        {key: value for key, value in valid.items() if key != "price"},
        {**valid, "price": "known"}, {**valid, "cost_usd": 0},
        {**valid, "total_tokens": valid["total_tokens"] + 1},
    ]
    for metric in TOKEN_METRICS:
        invalid.append({key: value for key, value in valid.items() if key != metric})
        invalid.extend({**valid, metric: value} for value in (-1, True, 1.5))
    for bad in invalid:
        with pytest.raises(SystemExit):
            provenance.validate_usage(bad)
    zeros = {**valid, **dict.fromkeys(TOKEN_METRICS, 0)}
    assert provenance.validate_usage(zeros) == zeros
    assert not provenance.external_usage_valid(zeros)


def test_unpriced_wrapper_success_preserves_identity_without_credential_path(wrapper_factory) -> None:
    """Wrong: exact unpriced tokens fail the wrapper gate, or provenance/logs leak the key path."""
    wrapper = wrapper_factory("successful-unpriced")
    credential_path = _dotenv(wrapper)
    result = wrapper.run()
    assert result.returncode == 0, result.stderr
    manifests = list((wrapper.round_dir / "provenance").glob("*.call.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["exit_status"] == "ok"
    assert manifest["usage"] == {"status": "complete-unpriced", "price": "unknown",
                                  **TOKEN_METRICS, "step_finish_events": 1}
    assert manifest["runtime"]["credential_kind"] == "vertex-service-account"
    assert "served_model_version" not in manifest["runtime"]
    assert "identity_probe_usage" not in manifest["runtime"]
    preflight = json.loads(Path(manifest["preflight"]["path"]).read_text(encoding="utf-8"))
    assert manifest["runtime"] == preflight["runtime"]
    assert manifest["preflight_verification"] == {"status": "match", "issues": []}
    identities = list((wrapper.round_dir / "opencode/_opencode_logs").glob("*.identity.json"))
    assert len(identities) == 1
    runtime_files = {record["path"]: record for record in manifest["runtime_files"]}
    config = wrapper.skill / "config/opencode-vertex.json"
    assert runtime_files[str(config)]["sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert str(identities[0]) not in runtime_files
    outputs = {record["path"]: record for record in manifest["outputs"]}
    identity_bytes = identities[0].read_bytes()
    assert outputs[str(identities[0])]["sha256"] == hashlib.sha256(identity_bytes).hexdigest()
    identity = json.loads(identity_bytes)
    assert identity["served_model_version"] == ARGON
    assert identity["usage"] == {"totalTokenCount": 1}
    assert str(credential_path) not in runtime_files
    indexed = _rows(wrapper.fakebin / "audit-index.jsonl")
    assert len(indexed) == 1
    assert indexed[0]["argv"][indexed[0]["argv"].index("--input-tokens") + 1] == "2"
    assert indexed[0]["argv"][indexed[0]["argv"].index("--output-tokens") + 1] == "3"
    logs = [path for path in (wrapper.round_dir / "opencode/_opencode_logs").rglob("*") if path.is_file()]
    assert logs, "no run logs were checked for leaks"
    for path in logs:
        contents = path.read_bytes()
        assert str(credential_path).encode() not in contents, path.name
        assert SYNTHETIC_KEY.encode() not in contents, path.name


@pytest.mark.parametrize("neutral_first", [False, True])
def test_zero_token_step_is_neutral(tmp_path: Path, capsys, neutral_first: bool) -> None:
    """Wrong: a zero-token step inherits earlier tokens and invalidates a priced log."""
    neutral = {"type": "step_finish", "part": {"type": "step-finish", "tokens": {
        "input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}, "total": 0},
        "cost": 0}}
    events = [neutral, _event(0.02)] if neutral_first else [_event(0.02), neutral]
    log = tmp_path / "neutral.ndjson"
    log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    assert provenance.main(["parse-opencode-usage", "--log", str(log)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "complete", **TOKEN_METRICS, "cost_usd": 0.02, "step_finish_events": 2,
    }


@pytest.mark.parametrize("requirement", ["google-auth>=2.40", "requests>=2.32"])
def test_probe_dependencies_declared(requirement: str) -> None:
    """Wrong: installing the declared requirements leaves a probe import unsupported."""
    lines = (SCRIPTS.parent / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert requirement in lines


@pytest.mark.parametrize("document", ["SKILL.md", "README.md"])
def test_vertex_docs_explain_probe_contract(document: str) -> None:
    """Wrong: setup omits the PATH requirement or promises an unrecorded pre-prepare probe."""
    contents = " ".join((SCRIPTS.parent / document).read_text(encoding="utf-8").split())
    for required in ("project `.venv/bin` must be first on `PATH`",
                     "After `provenance.py prepare`", "`error` call with no OpenCode dispatch"):
        assert required in contents


def test_no_test_reads_the_live_gate_config() -> None:
    """Refuse gate-sensitive tests without a preceding, same-function config patch.

    Wrong: a require_gate call or gate-refusal expectation can use the live config.
    All direct require_gate calls require isolation, even fixture-looking paths;
    comments, docstrings and patches inside nested/conditional blocks do not count.
    """
    sources = sorted(Path(__file__).parent.glob("*.py"))
    assert sources, "refused gate-config scan: 0 Python source files"
    checked = 0
    sensitive = 0
    offenders = []

    def local_nodes(node: ast.AST):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from local_nodes(child)

    for source in sources:
        module = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        registers = {alias.asname or alias.name for node in module.body if isinstance(node, ast.Import)
                     for alias in node.names if alias.name == "lifecycle_register"}
        gate_names = {"require_gate"} | {
            alias.asname or alias.name for node in module.body if isinstance(node, ast.ImportFrom)
            and node.module == "lifecycle_register" for alias in node.names if alias.name == "require_gate"
        }
        for function in ast.walk(module):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or not function.name.startswith("test_"):
                continue
            if source == Path(__file__) and function.name == "test_no_test_reads_the_live_gate_config":
                continue
            checked += 1
            patched = False
            needs_patch = False
            missing_patch = False
            for statement in function.body:
                if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                        and isinstance(statement.value.value, str)):
                    continue
                for node in local_nodes(statement):
                    gate_call = (isinstance(node, ast.Call)
                                 and ((isinstance(node.func, ast.Attribute) and node.func.attr == "require_gate")
                                      or (isinstance(node.func, ast.Name) and node.func.id in gate_names)))
                    gate_refusal = (isinstance(node, ast.Constant) and isinstance(node.value, str)
                                    and any(f"gate {gate}" in node.value for gate in ("G1", "G2", "G3")))
                    if gate_call or gate_refusal:
                        needs_patch = True
                        missing_patch = missing_patch or not patched
                call = statement.value if isinstance(statement, ast.Expr) else None
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "monkeypatch"
                        and call.func.attr == "setattr" and len(call.args) == 3
                        and isinstance(call.args[0], ast.Name) and call.args[0].id in registers
                        and isinstance(call.args[1], ast.Constant) and call.args[1].value == "SKILL_DIR"):
                    patched = True
            sensitive += needs_patch
            if missing_patch:
                offenders.append(f"{source.name}::{function.name}")
    assert checked, f"refused gate-config scan: 0 test functions in {len(sources)} Python source files"
    assert sensitive, f"refused gate-config scan: 0 gate-sensitive functions among {checked} test functions"
    assert not offenders, (
        f"{len(offenders)} of {sensitive} gate-sensitive test functions lack a preceding SKILL_DIR monkeypatch "
        f"({checked} test functions in {len(sources)} Python source files checked): " + "; ".join(offenders)
    )
