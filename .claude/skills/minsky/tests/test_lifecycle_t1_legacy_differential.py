"""T1 (host-owned): the legacy differential and the per-guard isolation fixtures (v0.2 §9 T1; §3.2; LC-F12, LC-F13).

Written line by line by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 1 Oct 2026. Not delegated.

What it proves:
- For schema-1.0 live inputs, `reconcile_artifact` (now `reconcile_core` + `LiveSource`) returns exactly what the FROZEN copy of
  today's function returns — the same `(ok, reason, manifest)` tuple, or the same `SystemExit` code and stderr text.
- One isolated fixture per retained guard: in fixture `g`, every guard before `g` passes and `g` is the FIRST to fail. So deleting
  any single guard from the core changes that fixture's outcome (the per-guard kill matrix, run by the W9 harness, relies on this).
- The core's trace names exactly that guard as the first `fail`, with every earlier guard `pass`.

Fixtures are written as schema-1.0 JSON by hand (not through `prepare`/`record`), so later receipt-format changes (1.1, store_ref)
cannot leak into the legacy contract under test.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS / "fixtures"))
from test_hardening import MODELS, PERSONA, STEP_MODELS, provenance  # noqa: E402
import legacy_reconcile_frozen as frozen  # noqa: E402
from byte_sources import LiveSource  # noqa: E402

AUDIT = "t1-audit"
ROUND = 2
CODEX_MODEL, CODEX_EFFORT = MODELS[1].split("@")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rec(path: Path) -> dict[str, Any]:
    path = path.resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha(path)}


def dump(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class Round:
    """A complete, passing schema-1.0 round with one wrapper Codex call; mutate it to isolate one guard."""

    def __init__(self, root: Path) -> None:
        self.dir = root / f"round-{ROUND}"
        ev = self.dir / "test-evidence"
        ev.mkdir(parents=True)
        self.scope_path = self.dir / "round-scope.json"
        dump(self.scope_path, {
            "schema_version": "1.0", "audit_id": AUDIT, "round_number": ROUND,
            "personas": [PERSONA], "models": MODELS, "step_models": STEP_MODELS,
            "registered_at": "2026-10-01T00:00:00.000000Z",
        })
        self.prompt = ev / "call.prompt.txt"
        self.prompt.write_text("exact prompt\n", encoding="utf-8")
        self.response = ev / "call.response.log"
        self.response.write_text("raw response\n", encoding="utf-8")
        self.context = ev / "context.txt"
        self.context.write_text("context bytes\n", encoding="utf-8")
        self.output = self.dir / "codex" / f"{PERSONA}.json"
        self.output.parent.mkdir()
        self.output.write_text("{}\n", encoding="utf-8")
        self.sibling = self.dir / "codex" / "sibling.stderr.log"
        self.sibling.write_text("stderr\n", encoding="utf-8")
        self.runtime = root / "fake-runtime" / "codex.js"
        self.runtime.parent.mkdir()
        self.runtime.write_text("runtime v1\n", encoding="utf-8")
        prov = self.dir / "provenance"
        blob = prov / "runtime-blobs" / sha(self.runtime)
        blob.parent.mkdir(parents=True)
        blob.write_bytes(self.runtime.read_bytes())
        self.blob = blob
        self.uid = "callaaaa1111"
        self.preflight_path = prov / f"codex.{PERSONA}.{self.uid}.preflight.json"
        self.manifest_path = prov / f"codex.{PERSONA}.{self.uid}.call.json"
        identity = {
            "call_uid": self.uid, "audit_id": AUDIT, "round_number": ROUND, "step": "codex",
            "persona": PERSONA, "origin": "wrapper", "provider": "openai",
            "model": CODEX_MODEL, "effort": CODEX_EFFORT, "model_stamp": MODELS[1],
        }
        self.preflight = {
            "schema_version": "1.0", "receipt_kind": "pre-dispatch", **identity,
            "prepared_at": "2026-10-01T00:00:01.000000Z",
            "client": {"name": "test-client", "version": "1.0"},
            "prompt": rec(self.prompt),
            "expected_outputs": [str(self.output.resolve()), str(self.sibling.resolve())],
            "runtime": {}, "runtime_files": [rec(self.runtime)],
            "runtime_snapshots": [{**rec(blob), "source_path": str(self.runtime.resolve())}],
            "referenced_context": [rec(self.context)], "round_scope": rec(self.scope_path),
        }
        self.write_preflight()
        self.manifest = {
            "schema_version": "1.0", **identity,
            "invoked_at": "2026-10-01T00:00:02Z", "invoked_at_status": "observed",
            "recorded_at": "2026-10-01T00:00:03.000000Z",
            "duration_seconds": 1.0, "duration_status": "observed",
            "exit_code": 0, "exit_code_status": "observed",
            "reported_exit_status": "ok", "exit_status": "ok", "terminal_status_issues": [],
            "context_status": "wrapper-visible", "client": {"name": "test-client", "version": "1.0"},
            "prompt": rec(self.prompt), "response": rec(self.response),
            "outputs": [rec(self.output), rec(self.sibling)],
            "usage": {"status": "total-only", "total_tokens": 7},
            "runtime": {}, "runtime_files": [rec(self.runtime)],
            "runtime_snapshots": copy.deepcopy(self.preflight["runtime_snapshots"]),
            "referenced_context": [rec(self.context)],
            "preflight": rec(self.preflight_path),
            "preflight_verification": {"status": "match", "issues": []},
            "round_scope": rec(self.scope_path),
            "manifest_path": str(self.manifest_path.resolve()),
        }
        self.write_manifest()

    def write_preflight(self) -> None:
        dump(self.preflight_path, self.preflight)

    def write_manifest(self) -> None:
        dump(self.manifest_path, self.manifest)

    def rebind_preflight(self) -> None:
        """After editing the preflight, re-record its hash in the manifest (so only the intended guard can fail)."""
        self.write_preflight()
        self.manifest["preflight"] = rec(self.preflight_path)
        self.write_manifest()


# Each mutation isolates ONE guard: every guard before it still passes. Returns call kwargs overrides (or {}).
Mutation = Callable[[Round, Path], dict[str, Any]]


def m_covering_manifest(r: Round, tmp: Path) -> dict[str, Any]:
    other = r.dir / "codex" / "uncovered.json"
    other.write_text("{}\n", encoding="utf-8")
    return {"output_path": other}


def m_prepared_attempt(r: Round, tmp: Path) -> dict[str, Any]:
    r.preflight["expected_outputs"] = [str(r.sibling.resolve())]
    r.rebind_preflight()
    return {}


def m_newest_prepared(r: Round, tmp: Path) -> dict[str, Any]:
    newer = copy.deepcopy(r.preflight)
    newer.update(call_uid="interrupted9999", prepared_at="2099-01-01T00:00:00.000000Z")
    dump(r.preflight_path.with_name(f"codex.{PERSONA}.interrupted9999.preflight.json"), newer)
    return {}


def m_round_scope_loads(r: Round, tmp: Path) -> dict[str, Any]:
    r.scope_path.write_text("{not json", encoding="utf-8")
    return {}


def m_audit_round_match(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["audit_id"] = "another-audit"
    r.write_manifest()
    return {}


def m_round_scope_hash(r: Round, tmp: Path) -> dict[str, Any]:
    r.scope_path.write_text(r.scope_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    return {}


def m_exit_status_ok(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["exit_status"] = "error"
    r.write_manifest()
    return {}


def m_wrapper_exit_code(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["exit_code"] = 1
    r.write_manifest()
    return {}


def m_host_exit_code(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest.update(origin="host", exit_code=None, exit_code_status="observed")
    r.write_manifest()
    return {}


def m_model_registered(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["model_stamp"] = "unregistered-model@high"
    r.write_manifest()
    return {}


def m_step_model_bound(r: Round, tmp: Path) -> dict[str, Any]:
    model, effort = MODELS[2].split("@")
    r.manifest.update(model_stamp=MODELS[2], model=model, effort=effort)
    r.write_manifest()
    return {}


def m_stamp_consistent(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["effort"] = "low"
    r.write_manifest()
    return {}


def m_usage_valid(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["usage"] = {"status": "bogus"}
    r.write_manifest()
    return {}


def m_wrapper_usage(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["usage"] = {"status": "unavailable"}
    r.write_manifest()
    return {}


def m_preflight_hash(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["preflight"] = {**r.manifest["preflight"], "sha256": "0" * 64}
    r.write_manifest()
    return {}


def m_preflight_verification(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["preflight_verification"] = {"status": "drift", "issues": ["x"]}
    r.write_manifest()
    return {}


def m_prompt_hash(r: Round, tmp: Path) -> dict[str, Any]:
    r.prompt.write_text("changed prompt\n", encoding="utf-8")
    return {}


def m_response_limitation(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["response"] = {"status": "unavailable", "reason": "not exposed"}
    r.write_manifest()
    return {}


def m_response_hash(r: Round, tmp: Path) -> dict[str, Any]:
    r.response.write_text("changed response\n", encoding="utf-8")
    return {}


def m_context_hash(r: Round, tmp: Path) -> dict[str, Any]:
    r.context.write_text("changed context\n", encoding="utf-8")
    return {}


def m_required_context(r: Round, tmp: Path) -> dict[str, Any]:
    return {"required_context_paths": [r.dir / "pack.xml"]}


def m_output_present(r: Round, tmp: Path) -> dict[str, Any]:
    r.output.unlink()
    return {}


def m_output_hash(r: Round, tmp: Path) -> dict[str, Any]:
    r.output.write_text('{"changed": true}\n', encoding="utf-8")
    return {}


def m_sibling_outputs(r: Round, tmp: Path) -> dict[str, Any]:
    r.sibling.write_text("changed stderr\n", encoding="utf-8")
    return {}


def m_snapshot_coverage(r: Round, tmp: Path) -> dict[str, Any]:
    r.manifest["runtime_snapshots"][0]["source_path"] = str(tmp / "elsewhere" / "codex.js")
    r.write_manifest()
    return {}


def m_runtime_hash_snapshot(r: Round, tmp: Path) -> dict[str, Any]:
    r.blob.write_bytes(b"corrupted blob\n")
    return {}


def m_runtime_hash_legacy(r: Round, tmp: Path) -> dict[str, Any]:
    """A pre-snapshot (legacy) receipt whose live runtime changed: the current-path check fails."""
    r.manifest["runtime_snapshots"] = []
    r.write_manifest()
    r.runtime.write_text("runtime v2\n", encoding="utf-8")
    return {}


CASES: list[tuple[str, Mutation]] = [
    ("covering_manifest", m_covering_manifest),
    ("prepared_attempt", m_prepared_attempt),
    ("newest_prepared_matches_latest", m_newest_prepared),
    ("round_scope_loads", m_round_scope_loads),
    ("audit_round_match", m_audit_round_match),
    ("round_scope_hash", m_round_scope_hash),
    ("exit_status_ok", m_exit_status_ok),
    ("wrapper_exit_code_zero", m_wrapper_exit_code),
    ("host_exit_code_explicit", m_host_exit_code),
    ("model_registered", m_model_registered),
    ("step_model_bound", m_step_model_bound),
    ("stamp_consistent", m_stamp_consistent),
    ("usage_valid", m_usage_valid),
    ("wrapper_usage_positive", m_wrapper_usage),
    ("preflight_hash", m_preflight_hash),
    ("preflight_verification_match", m_preflight_verification),
    ("prompt_hash", m_prompt_hash),
    ("response_limitation_valid", m_response_limitation),
    ("response_hash", m_response_hash),
    ("context_hash", m_context_hash),
    ("required_context_recorded", m_required_context),
    ("output_present", m_output_present),
    ("output_hash", m_output_hash),
    ("sibling_outputs", m_sibling_outputs),
    ("snapshot_coverage", m_snapshot_coverage),
    ("runtime_hash", m_runtime_hash_snapshot),
    ("runtime_hash", m_runtime_hash_legacy),
]


def run(fn: Callable[..., Any], **kwargs: Any) -> tuple[str, Any]:
    """Call fn; return ("return", value) or ("exit", (code, stderr text))."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return "return", fn(**kwargs)
    except SystemExit as exc:
        return "exit", (exc.code, err.getvalue())


def call_kwargs(r: Round, overrides: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "round_dir": r.dir, "step": "codex", "persona": PERSONA, "output_path": r.output,
        "registered_models": MODELS, "required_context_paths": [r.context],
    }
    kwargs.update(overrides)
    return kwargs


def test_guard_names_are_the_ratified_list() -> None:
    assert provenance.GUARDS == tuple(dict.fromkeys(name for name, _ in CASES))
    assert len(provenance.GUARDS) == 26


def test_clean_round_passes_identically(tmp_path: Path) -> None:
    r = Round(tmp_path)
    kwargs = call_kwargs(r, {})
    new = run(lambda **k: provenance.reconcile_artifact(k.pop("round_dir"), **k), **kwargs)
    old = run(lambda **k: frozen.reconcile_artifact(k.pop("round_dir"), **k), **kwargs)
    assert new == old
    assert new[0] == "return" and new[1][0] is True and new[1][1] == "ok"


@pytest.mark.parametrize("guard,mutate", CASES, ids=[f"{g}-{m.__name__}" for g, m in CASES])
def test_each_guard_is_first_failure_and_legacy_identical(tmp_path: Path, guard: str, mutate: Mutation) -> None:
    r = Round(tmp_path)
    overrides = mutate(r, tmp_path)
    kwargs = call_kwargs(r, overrides)

    old = run(lambda **k: frozen.reconcile_artifact(k.pop("round_dir"), **k), **kwargs)
    new = run(lambda **k: provenance.reconcile_artifact(k.pop("round_dir"), **k), **kwargs)
    assert new == old, f"legacy contract broken at guard {guard}"
    if old[0] == "return":
        assert old[1][0] is False, f"fixture for {guard} did not fail at all"

    trace: list[tuple[str, str]] = []
    core_kwargs = dict(kwargs)
    output_path = core_kwargs.pop("output_path")
    round_dir = core_kwargs.pop("round_dir")
    core = run(lambda **k: provenance.reconcile_core(
        round_dir, target_key=str(output_path.resolve()), output_path=output_path,
        source=LiveSource(), trace=trace, **k), **core_kwargs)
    assert core == new
    statuses = dict(trace)
    assert [name for name, status in trace if status == "fail"] == [guard], trace
    order = provenance.GUARDS
    alternatives = {"response_limitation_valid", "response_hash"}
    for earlier in order[:order.index(guard)]:
        if earlier in alternatives and earlier not in statuses:
            continue  # exactly one of the two response guards is evaluated per call
        assert statuses.get(earlier) == "pass", (earlier, trace)
    assert not (alternatives <= set(statuses)), trace
