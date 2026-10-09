"""FROZEN legacy reconcile_artifact for the T1 differential (v0.2 §9 T1; v0.1 §3.2).

Extracted VERBATIM (by ast node spans, not retyped) from .claude/skills/minsky/scripts/provenance.py at commit
a96f851929c4ac8f28d0f1c6028c72353cf5b0ca (sha256 of that file: see FROZEN_SOURCE_SHA256). Test fixture only: never imported by production code, never edited.
The only non-verbatim lines are this docstring, the imports and the two path constants below.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[2]
PERSONAS_DIR = SKILL_DIR / "personas"
FROZEN_SOURCE_COMMIT = "a96f851929c4ac8f28d0f1c6028c72353cf5b0ca"
FROZEN_SOURCE_SHA256 = "8e32250426a6d117385b5a21740987fe7fd5626d43108c3f79fdb70e5fff761c"

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*@[A-Za-z0-9][A-Za-z0-9._+-]*$")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

STEP_NAMES = ("claude_self", "codex", "opencode", "claude_synth")

def fail(message: str, code: int = 2) -> "None":
    print(f"provenance: {message}", file=sys.stderr)
    raise SystemExit(code)

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def parse_json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"{label} is not valid JSON: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must be a JSON object")
    return value

def parse_string_list(raw: str, label: str, *, persona: bool = False,
                      model: bool = False) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"{label} must be a JSON array: {exc}")
    if not isinstance(value, list) or not value:
        fail(f"{label} must be a non-empty JSON array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        fail(f"{label} entries must be non-empty strings")
    cleaned = [item.strip() for item in value]
    if len(set(cleaned)) != len(cleaned):
        fail(f"{label} contains duplicate entries")
    if persona:
        for item in cleaned:
            if not SLUG_RE.fullmatch(item):
                fail(f"invalid persona slug in {label}: {item!r}")
            if not (PERSONAS_DIR / f"{item}.md").is_file():
                fail(f"persona in {label} has no definition: {item!r}")
    if model:
        for item in cleaned:
            if not MODEL_RE.fullmatch(item):
                fail(f"model in {label} must include an explicit @effort: {item!r}")
    return cleaned

def parse_step_models(raw: str, models: list[str], label: str = "--step-models") -> dict[str, str]:
    value = parse_json_object(raw, label)
    if set(value) != set(STEP_NAMES):
        fail(f"{label} must map exactly these phases: {', '.join(STEP_NAMES)}")
    for step, model_stamp in value.items():
        if not isinstance(model_stamp, str) or model_stamp not in models:
            fail(f"{label}.{step} must name one of the registered model@effort stamps")
    return {step: value[step] for step in STEP_NAMES}

def validate_usage(usage: dict[str, Any]) -> dict[str, Any]:
    status = usage.get("status")
    metric_keys = (
        "input_tokens", "output_tokens", "reasoning_tokens",
        "cache_read_tokens", "cache_write_tokens", "total_tokens", "cost_usd",
    )
    if status not in {"complete", "total-only", "unavailable", "incomplete"}:
        fail("usage.status must be complete, total-only, unavailable, or incomplete")
    present = {key for key in metric_keys if key in usage}
    for key in present:
        value = usage[key]
        if key == "cost_usd":
            valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
        else:
            valid = not isinstance(value, bool) and isinstance(value, int) and value >= 0
        if not valid:
            kind = "number" if key == "cost_usd" else "integer"
            fail(f"usage.{key} must be a non-negative {kind} when present")
    if status == "complete":
        missing = set(metric_keys) - present
        if missing:
            fail(f"complete usage is missing: {', '.join(sorted(missing))}")
        component_total = sum(
            usage[key] for key in (
                "input_tokens", "output_tokens", "reasoning_tokens",
                "cache_read_tokens", "cache_write_tokens",
            )
        )
        if usage["total_tokens"] != component_total:
            fail(
                "usage.total_tokens does not equal input + output + reasoning + "
                "cache_read + cache_write"
            )
    elif status == "total-only":
        if present != {"total_tokens"}:
            fail("total-only usage must contain total_tokens and no inferred components/cost")
    elif status == "unavailable" and present:
        fail("unavailable usage must not contain numeric metrics (unknown is not zero)")
    elif status == "incomplete" and "issues" not in usage:
        fail("incomplete usage must explain the defect in usage.issues")
    return usage

def external_usage_valid(usage: dict[str, Any]) -> bool:
    """A provider success requires measured positive usage; price may be zero."""
    return (usage.get("status") in {"complete", "total-only"}
            and type(usage.get("total_tokens")) is int and usage["total_tokens"] > 0)

def load_round_scope(round_dir: Path) -> dict[str, Any]:
    path = round_dir.resolve() / "round-scope.json"
    if not path.is_file():
        fail(f"missing explicit round scope: {path}")
    try:
        scope = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"invalid round scope {path}: {exc}")
    if not isinstance(scope, dict):
        fail(f"round scope must be an object: {path}")
    if scope.get("schema_version") != "1.0":
        fail(f"unsupported or missing round-scope schema_version: {path}")
    if not isinstance(scope.get("audit_id"), str) or not scope["audit_id"]:
        fail(f"round scope has invalid audit_id: {path}")
    if isinstance(scope.get("round_number"), bool) or not isinstance(scope.get("round_number"), int) or scope["round_number"] < 1:
        fail(f"round scope has invalid round_number: {path}")
    scope["personas"] = parse_string_list(
        json.dumps(scope.get("personas")), "round-scope personas", persona=True
    )
    scope["models"] = parse_string_list(
        json.dumps(scope.get("models")), "round-scope models", model=True
    )
    scope["step_models"] = parse_step_models(
        json.dumps(scope.get("step_models")), scope["models"], "round-scope step_models"
    )
    return scope

def _load_preflight(path_string: str) -> tuple[Path, dict[str, Any]]:
    path = Path(path_string).resolve()
    if not path.is_file():
        fail(f"preflight receipt is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"preflight receipt is invalid JSON: {path}: {exc}")
    if not isinstance(value, dict) or value.get("receipt_kind") != "pre-dispatch":
        fail(f"not a Minsky pre-dispatch receipt: {path}")
    return path, value

def load_call_manifests(round_dir: Path) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    provenance_dir = round_dir.resolve() / "provenance"
    if not provenance_dir.is_dir():
        return manifests
    for path in sorted(provenance_dir.glob("*.call.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"invalid call manifest {path}: {exc}")
        record["_path"] = str(path)
        manifests.append(record)
    return manifests

def reconcile_artifact(round_dir: Path, *, step: str, persona: str | None,
                       output_path: Path, registered_models: list[str],
                       required_context_paths: list[Path] | None = None,
                       ) -> tuple[bool, str, dict[str, Any] | None]:
    """Require the latest manifest covering an artifact to be terminal-ok and hash-matching."""
    target = str(output_path.resolve())
    candidates = []
    for manifest in load_call_manifests(round_dir):
        if manifest.get("step") != step:
            continue
        manifest_persona = manifest.get("persona")
        if manifest_persona not in {persona, None}:
            continue
        if any(out.get("path") == target for out in manifest.get("outputs") or []):
            candidates.append(manifest)
    if not candidates:
        return False, f"no call manifest covers {target}", None
    latest = sorted(candidates, key=lambda m: (
        str(m.get("recorded_at", "")), str(m.get("call_uid", "")),
    ))[-1]
    prepared = []
    for path in (round_dir.resolve() / "provenance").glob("*.preflight.json"):
        _, receipt = _load_preflight(str(path))
        if (receipt.get("step") == step and receipt.get("persona") in {persona, None}
                and target in receipt.get("expected_outputs", [])):
            prepared.append(receipt)
    if not prepared:
        return False, "no prepared attempt covers output", latest
    newest = max(prepared, key=lambda r: (r.get("prepared_at", ""), r.get("call_uid", "")))
    if newest.get("call_uid") != latest.get("call_uid"):
        return False, "newest prepared attempt has no matching current terminal", latest
    scope = load_round_scope(round_dir)
    if (latest.get("audit_id") != scope.get("audit_id") or
            latest.get("round_number") != scope.get("round_number")):
        return False, f"manifest audit/round mismatch: {latest.get('_path')}", latest
    scope_record = latest.get("round_scope") or {}
    scope_path = Path(str(scope_record.get("path", "")))
    if not scope_path.is_file() or sha256_file(scope_path) != scope_record.get("sha256"):
        return False, f"round-scope missing or hash-mismatched: {latest.get('_path')}", latest
    if latest.get("exit_status") != "ok":
        return False, f"latest call is terminal {latest.get('exit_status')!r}: {latest.get('_path')}", latest
    if latest.get("origin") == "wrapper" and latest.get("exit_code") != 0:
        return False, f"terminal-ok manifest has nonzero exit_code: {latest.get('_path')}", latest
    if latest.get("origin") == "host" and latest.get("exit_code") is None:
        if latest.get("exit_code_status") != "not_exposed":
            return False, f"host exit-code limitation is not explicit: {latest.get('_path')}", latest
    if latest.get("model_stamp") not in registered_models:
        return False, f"unregistered model stamp in {latest.get('_path')}", latest
    if scope["step_models"].get(step) != latest.get("model_stamp"):
        return False, f"wrong model for phase {step!r}: {latest.get('_path')}", latest
    if latest.get("model_stamp") != f"{latest.get('model')}@{latest.get('effort')}":
        return False, f"model/effort stamp is internally inconsistent: {latest.get('_path')}", latest
    try:
        validate_usage(latest.get("usage") or {})
    except SystemExit:
        return False, f"usage record is invalid: {latest.get('_path')}", latest
    if latest.get("origin") == "wrapper" and not external_usage_valid(latest["usage"]):
        return False, "external call lacks positive authoritative usage", latest
    preflight = latest.get("preflight") or {}
    preflight_path = Path(str(preflight.get("path", "")))
    if (not preflight_path.is_file() or
            sha256_file(preflight_path) != preflight.get("sha256")):
        return False, f"preflight receipt missing or hash-mismatched: {latest.get('_path')}", latest
    if (latest.get("preflight_verification") or {}).get("status") != "match":
        return False, f"preflight/postflight drift was detected: {latest.get('_path')}", latest
    prompt = latest.get("prompt") or {}
    prompt_path = Path(str(prompt.get("path", "")))
    if not prompt_path.is_file() or sha256_file(prompt_path) != prompt.get("sha256"):
        return False, f"prompt missing or hash-mismatched in {latest.get('_path')}", latest
    response = latest.get("response") or {}
    if response.get("status") == "unavailable":
        if latest.get("origin") != "host" or not response.get("reason"):
            return False, f"response evidence is unavailable without a valid host limitation: {latest.get('_path')}", latest
    else:
        response_path = Path(str(response.get("path", "")))
        if not response_path.is_file() or sha256_file(response_path) != response.get("sha256"):
            return False, f"response missing or hash-mismatched in {latest.get('_path')}", latest
    context_records = latest.get("referenced_context") or []
    for record in context_records:
        context_path = Path(str(record.get("path", "")))
        if not context_path.is_file() or sha256_file(context_path) != record.get("sha256"):
            return False, f"context missing or hash-mismatched in {latest.get('_path')}: {context_path}", latest
    context_by_path = {record.get("path"): record for record in context_records}
    for required in required_context_paths or []:
        required_path = str(required.resolve())
        if required_path not in context_by_path:
            return False, f"required context is not recorded in {latest.get('_path')}: {required_path}", latest
    if not output_path.is_file():
        return False, f"output is missing: {output_path}", latest
    output_record = next(out for out in latest["outputs"] if out.get("path") == target)
    if output_record.get("missing") or sha256_file(output_path) != output_record.get("sha256"):
        return False, f"output hash mismatch: {output_path}", latest
    for record in latest.get("outputs", []):
        path = Path(record["path"])
        if record.get("missing") or not path.is_file() or sha256_file(path) != record.get("sha256"):
            return False, f"required sibling output missing or changed: {path}", latest
    snapshots = latest.get("runtime_snapshots", [])
    if snapshots:
        expected = {(r["path"], r["sha256"]) for r in latest.get("runtime_files", [])}
        if {(r.get("source_path"), r.get("sha256")) for r in snapshots} != expected:
            return False, "runtime snapshot coverage mismatch", latest
        runtime_records = snapshots
    else:
        # Legacy receipts retain current-path checks, not invented historical snapshots.
        runtime_records = latest.get("runtime_files", [])
    for record in runtime_records:
        path = Path(record["path"])
        if not path.is_file() or sha256_file(path) != record.get("sha256"):
            return False, f"runtime evidence missing or changed: {path}", latest
    return True, "ok", latest
