#!/usr/bin/env python3
"""Durable, content-addressed provenance sidecars for Minsky calls.

This module deliberately records only observable evidence.  It cannot recover a
host application's hidden system prompt or transcript, and never claims that it
can.  Exact caller-supplied prompts, raw response logs, output artifacts, model
settings, client/runtime facts, and honestly classified usage are preserved in
``round-N/provenance/*.call.json``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import json
import math
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from byte_sources import ByteSource, LiveSource
from store import StoreConfig


SKILL_DIR = Path(__file__).resolve().parent.parent
PERSONAS_DIR = SKILL_DIR / "personas"
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*@[A-Za-z0-9][A-Za-z0-9._+-]*$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TERMINAL_STATUSES = {
    "ok", "error", "rate-limit", "timeout", "schema-invalid", "usage-invalid",
    "input-drift", "exit-code-mismatch",
}
STEP_NAMES = ("claude_self", "codex", "opencode", "claude_synth")
GUARDS: tuple[str, ...] = (
    "covering_manifest", "prepared_attempt", "newest_prepared_matches_latest",
    "round_scope_loads", "audit_round_match", "round_scope_hash", "exit_status_ok",
    "wrapper_exit_code_zero", "host_exit_code_explicit", "model_registered",
    "step_model_bound", "stamp_consistent", "usage_valid", "wrapper_usage_positive",
    "preflight_hash", "preflight_verification_match", "prompt_hash",
    "response_limitation_valid", "response_hash", "context_hash",
    "required_context_recorded", "output_present", "output_hash", "sibling_outputs",
    "snapshot_coverage", "runtime_hash",
)


def fail(message: str, code: int = 2) -> "None":
    print(f"provenance: {message}", file=sys.stderr)
    raise SystemExit(code)


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json_once(path: Path, value: dict[str, Any]) -> None:
    """Atomically publish a JSON record without permitting replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    payload = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    try:
        with temp.open("xb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            fail(f"refusing to overwrite immutable record: {path}")
    finally:
        temp.unlink(missing_ok=True)


def file_record(path_string: str, *, required: bool = True) -> dict[str, Any] | None:
    path = Path(path_string).resolve()
    if not path.is_file():
        if required:
            fail(f"required evidence file is missing: {path}")
        return None
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


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


def snapshot_runtime(records: list[dict[str, Any]], root: Path | None = None,
                     *, store: StoreConfig | None = None, audit_id: str | None = None,
                     round_number: int | None = None) -> list[dict[str, Any]]:
    """Preserve pre-call bytes; refuse corrupt snapshots and insufficient CAS headroom."""
    from store import StoreConfigError, cas_path, ingest_file, validate_store

    enabled = store is not None and store.enabled
    if enabled:
        store.ingested.clear()
        store.free_space_checks.clear()
        try:
            validate_store(store)
        except StoreConfigError as exc:
            fail(str(exc))
        if (not isinstance(audit_id, str) or not audit_id or
                any(char in audit_id for char in "\t\r\n\0") or
                type(round_number) is not int or round_number < 1):
            fail("CAS runtime snapshot requires audit_id and positive round_number")
    elif root is None:
        fail("per-round runtime snapshot requires root")
    snapshots = []
    for record in records:
        if enabled:
            try:
                target = cas_path(store.root, record["sha256"])
            except StoreConfigError as exc:
                fail(str(exc))
            if (type(record["bytes"]) is not int or record["bytes"] < 0 or
                    any(char in record["path"] for char in "\t\r\n\0")):
                fail(f"refused CAS runtime record: {record['path']}")
            try:
                created = ingest_file(store, Path(record["path"]), record["sha256"],
                                      byte_count=record["bytes"], audit_id=audit_id,
                                      round_label=round_number, source_path=record["path"])
            except StoreConfigError as exc:
                fail(str(exc))
            if created:
                store.ingested.add(record["sha256"])
            snapshots.append({
                "path": str(target), "bytes": record["bytes"], "sha256": record["sha256"],
                "source_path": record["path"],
                "store_ref": {"store_id": store.store_id, "sha256": record["sha256"]},
            })
            continue
        else:
            target = root / "runtime-blobs" / record["sha256"]
            target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.parent / f".{uuid.uuid4().hex}.tmp"
            try:
                with Path(record["path"]).open("rb") as src, temp.open("xb") as dst:
                    import shutil
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                if sha256_file(temp) != record["sha256"]:
                    fail("runtime changed during snapshot")
                try:
                    os.link(temp, target)
                except FileExistsError:
                    pass
            finally:
                temp.unlink(missing_ok=True)
        if sha256_file(target) != record["sha256"]:
            fail(f"runtime snapshot mismatch: {target}")
        snapshots.append({**file_record(str(target)), "source_path": record["path"]})
    return snapshots


def validate_dispatch(args: argparse.Namespace, runtime: dict[str, Any]) -> None:
    if args.origin != "wrapper" or args.client_name not in {"codex-cli", "opencode-cli"}:
        return
    argv = runtime.get("argv", [])
    expected = f"{args.provider}/{args.model}" if args.step == "opencode" else args.model
    if argv.count("--model") != 1 or argv[argv.index("--model") + 1:][:1] != [expected]:
        fail("actual runtime argv model differs from phase registration")
    if args.step == "opencode":
        if argv.count("--variant") != 1 or argv[argv.index("--variant") + 1:][:1] != [args.effort]:
            fail("actual runtime variant differs from registered effort")
    elif argv.count(f"model_reasoning_effort={args.effort}") != 1:
        fail("actual Codex runtime effort differs from registration")


def stable_finding_uid(audit_id: str, round_number: int, step: str, persona: str,
                       finding: dict[str, Any]) -> str:
    """Identity for one exact source finding; independent of array ordering."""
    source = {
        "audit_id": audit_id,
        "round_number": round_number,
        "step": step,
        "persona": persona,
        "finding": {k: v for k, v in finding.items() if not k.startswith("_")},
    }
    return "mf_" + hashlib.sha256(canonical_json(source).encode("utf-8")).hexdigest()[:24]


def load_round_scope(round_dir: Path) -> dict[str, Any]:
    """Refuse missing scopes, unsupported schemas and malformed registration fields."""
    path = round_dir.resolve() / "round-scope.json"
    if not path.is_file():
        fail(f"missing explicit round scope: {path}")
    try:
        scope = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"invalid round scope {path}: {exc}")
    if not isinstance(scope, dict):
        fail(f"round scope must be an object: {path}")
    if scope.get("schema_version") not in ("1.0", "1.1"):
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
    if scope["schema_version"] == "1.1":
        if scope.get("git_status") not in ("ok", "not-a-git-work-tree"):
            fail(f"round scope has invalid git_status: {path}")
        head = scope.get("git_head")
        dirty = scope.get("dirty_paths")
        if "git_head" not in scope or (
            scope["git_status"] == "ok" and
            (not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head))
        ) or (scope["git_status"] != "ok" and head is not None):
            fail(f"round scope has invalid git_head: {path}")
        if "dirty_paths" not in scope or (
            scope["git_status"] == "ok" and
            (not isinstance(dirty, list) or any(
                not isinstance(entry, list) or len(entry) not in (2, 3) or
                any(not isinstance(part, str) for part in entry) for entry in dirty
            ))
        ) or (scope["git_status"] != "ok" and dirty is not None):
            fail(f"round scope has invalid dirty_paths: {path}")
        pack = scope.get("pack_sha256")
        if "pack_sha256" not in scope or (pack is not None and
                (not isinstance(pack, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", pack))):
            fail(f"round scope has invalid pack_sha256: {path}")
    return scope


def git_state(repo: Path) -> dict:
    """Refuse unreadable HEAD or tracked status; scratch directories have null state."""
    import subprocess

    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True)
        except OSError as exc:
            fail(f"refused git_status for {repo}: {exc}")

    inside = git("rev-parse", "--is-inside-work-tree")
    if (inside.returncode and b"not a git repository" in inside.stderr) or (
            not inside.returncode and inside.stdout.strip() == b"false"):
        return {"git_status": "not-a-git-work-tree", "git_head": None, "dirty_paths": None}
    if inside.returncode or inside.stdout.strip() != b"true":
        fail(f"refused git_status for {repo}: {os.fsdecode(inside.stderr).strip()}")
    head = git("rev-parse", "--verify", "HEAD")
    sha = os.fsdecode(head.stdout).strip()
    if head.returncode or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        fail(f"refused git_head HEAD for {repo}: {os.fsdecode(head.stderr).strip() or sha}")
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=no")
    if status.returncode:
        fail(f"refused dirty_paths for {repo}: {os.fsdecode(status.stderr).strip()}")
    dirty_paths = []
    fields = status.stdout.split(b"\0")
    if fields.pop() != b"":
        fail(f"refused malformed dirty_paths for {repo}: missing NUL terminator")
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2:3] != b" ":
            fail(f"refused malformed dirty_paths entry for {repo}: {field!r}")
        entry = [os.fsdecode(field[:2]), os.fsdecode(field[3:])]
        if "R" in entry[0] or "C" in entry[0]:
            if index == len(fields) or not fields[index]:
                fail(f"refused dirty_paths rename/copy without orig_path for {repo}: {entry[1]}")
            entry.append(os.fsdecode(fields[index]))
            index += 1
        dirty_paths.append(entry)
    return {"git_status": "ok", "git_head": sha, "dirty_paths": sorted(dirty_paths)}


def cmd_register_round(args: argparse.Namespace) -> int:
    """Refuse conflicting registration, unsafe worktrees and missing pack bytes."""
    from worktree_guard import WorktreeRefused, check_not_linked_worktree

    repo = SKILL_DIR.parents[2]
    try:
        worktree = check_not_linked_worktree(repo)
    except WorktreeRefused as exc:
        fail(str(exc), code=1)
    round_dir = Path(args.round_dir).resolve()
    personas = parse_string_list(args.personas, "--personas", persona=True)
    models = parse_string_list(args.models, "--models", model=True)
    step_models = parse_step_models(args.step_models, models)
    record = {
        "schema_version": "1.1",
        "audit_id": args.audit_id,
        "round_number": args.round,
        "personas": personas,
        "models": models,
        "step_models": step_models,
        "registered_at": now_utc(),
        **git_state(repo),
        "pack_sha256": None,
    }
    if args.pack is not None:
        pack = Path(args.pack).resolve()
        if not pack.is_file():
            fail(f"--pack file is missing: {pack}")
        try:
            record["pack_sha256"] = sha256_file(pack)
        except OSError as exc:
            fail(f"refused --pack bytes at {pack}: {exc}")
    if worktree["status"] == "linked-worktree-override":
        record["worktree"] = worktree
    path = round_dir / "round-scope.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        comparable = {k: old.get(k) for k in (
            "schema_version", "audit_id", "round_number", "personas", "models", "step_models"
        )}
        expected = {k: record[k] for k in comparable}
        if comparable != expected:
            fail(f"refusing to overwrite conflicting round scope: {path}")
        print(path)
        return 0
    write_json_once(path, record)
    print(path)
    return 0


def cmd_new_uid(_args: argparse.Namespace) -> int:
    print(uuid.uuid4().hex)
    return 0


def _validate_call_registration(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    model_stamp = f"{args.model}@{args.effort}"
    if not MODEL_RE.fullmatch(model_stamp):
        fail(f"invalid explicit model/effort stamp: {model_stamp!r}")
    scope = load_round_scope(Path(args.round_dir))
    if scope.get("audit_id") != args.audit_id or scope.get("round_number") != args.round:
        fail("call metadata does not match round-scope audit_id/round_number")
    if model_stamp not in scope["models"]:
        fail(f"call model {model_stamp!r} is not registered for this round")
    if scope["step_models"].get(args.step) != model_stamp:
        fail(
            f"call model {model_stamp!r} is not bound to phase {args.step!r}; "
            f"expected {scope['step_models'].get(args.step)!r}"
        )
    if args.persona and args.persona not in scope["personas"]:
        fail(f"call persona {args.persona!r} is not registered for this round")
    if args.step in {"codex", "opencode"} and not args.persona:
        fail(f"--persona is required for {args.step} calls")
    if not args.provider.strip() or not args.client_name.strip() or not args.client_version.strip():
        fail("provider, client-name, and client-version must be non-empty")
    if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", args.call_uid):
        fail("--call-uid must be an 8-128 character safe identifier")
    return scope, model_stamp


def cmd_prepare(args: argparse.Namespace) -> int:
    """Publish pre-dispatch evidence; refuse invalid stores before writing any file."""
    from store import StoreConfigError, load_store_config, validate_store

    try:
        cfg = load_store_config()
        if cfg.enabled:
            validate_store(cfg)
    except StoreConfigError as exc:
        fail(str(exc))
    _scope, model_stamp = _validate_call_registration(args)
    runtime = parse_json_object(args.runtime_json, "--runtime-json")
    validate_dispatch(args, runtime)
    prompt = file_record(args.prompt_path)
    runtime_files = [file_record(path) for path in args.runtime_file]
    context_files = [file_record(path) for path in args.context_file]
    expected_outputs = [str(Path(path).resolve()) for path in args.expected_output_path]
    round_scope_record = file_record(str(Path(args.round_dir).resolve() / "round-scope.json"))
    for label, paths in (
        ("--runtime-file", [record["path"] for record in runtime_files if record]),
        ("--context-file", [record["path"] for record in context_files if record]),
        ("--expected-output-path", expected_outputs),
    ):
        if len(set(paths)) != len(paths):
            fail(f"{label} contains duplicate paths")
    preflight_dir = Path(args.round_dir).resolve() / "provenance"
    runtime_snapshots = snapshot_runtime(
        runtime_files, preflight_dir if not cfg.enabled else None,
        store=cfg, audit_id=args.audit_id, round_number=args.round,
    )
    persona_part = args.persona or "host"
    path = preflight_dir / f"{args.step}.{persona_part}.{args.call_uid}.preflight.json"
    record = {
        "schema_version": "1.1",
        "store": cfg.receipt(),
        "receipt_kind": "pre-dispatch",
        "call_uid": args.call_uid,
        "audit_id": args.audit_id,
        "round_number": args.round,
        "step": args.step,
        "persona": args.persona,
        "origin": args.origin,
        "provider": args.provider,
        "model": args.model,
        "effort": args.effort,
        "model_stamp": model_stamp,
        "prepared_at": now_utc(),
        "client": {"name": args.client_name, "version": args.client_version},
        "prompt": prompt,
        "expected_outputs": expected_outputs,
        "runtime": runtime,
        "runtime_files": runtime_files,
        "runtime_snapshots": runtime_snapshots,
        "referenced_context": context_files,
        "round_scope": round_scope_record,
    }
    write_json_once(path, record)
    print(path)
    return 0


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


def _compare_file_records(before: list[dict[str, Any]], current_paths: list[str],
                          label: str) -> tuple[list[dict[str, Any]], list[str]]:
    after = [
        file_record(path, required=False) or {
            "path": str(Path(path).resolve()), "missing": True,
        }
        for path in current_paths
    ]
    issues: list[str] = []
    before_by_path = {record.get("path"): record for record in before}
    after_by_path = {record.get("path"): record for record in after}
    if set(before_by_path) != set(after_by_path):
        issues.append(f"{label} path set changed")
    for path in sorted(set(before_by_path) & set(after_by_path)):
        if after_by_path[path].get("missing"):
            issues.append(f"{label} missing after dispatch: {path}")
        elif before_by_path[path].get("sha256") != after_by_path[path].get("sha256"):
            issues.append(f"{label} hash changed: {path}")
    return after, issues


def _audit_db_insert(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    if not args.record_db:
        return
    db = Path(args.db).resolve() if args.db else None
    helper = SKILL_DIR / "scripts" / "audit-db.py"
    command = [sys.executable, str(helper)]
    if db:
        command += ["--db", str(db)]
    if manifest.get("invoked_at") is None:
        fail("cannot index a host call with unexposed invoked_at in the legacy DB; keep its sidecar authoritative")
    command += [
        "insert-provenance", "--audit-id", args.audit_id, "--round", str(args.round),
        "--step", args.step, "--model", manifest["model_stamp"],
        "--invoked-at", manifest["invoked_at"],
        "--output-path", manifest["manifest_path"], "--exit-status", manifest["exit_status"],
        "--round-scope", str(Path(args.round_dir).resolve() / "round-scope.json"),
    ]
    if manifest.get("duration_seconds") is not None:
        command += ["--duration", str(manifest["duration_seconds"])]
    if args.persona:
        command += ["--persona", args.persona]
    usage = manifest["usage"]
    if usage["status"] == "complete":
        command += [
            "--input-tokens", str(usage["input_tokens"]),
            "--output-tokens", str(usage["output_tokens"]),
        ]
    import subprocess
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        fail(f"audit DB provenance insert failed: {result.stderr.strip() or result.stdout.strip()}")


def cmd_record(args: argparse.Namespace) -> int:
    if args.exit_status not in TERMINAL_STATUSES:
        fail(f"invalid terminal exit status: {args.exit_status}")
    if args.response_unavailable_reason and args.origin != "host":
        fail("--response-unavailable-reason is permitted only for --origin host")
    if args.duration is not None and args.duration < 0:
        fail("--duration must be non-negative")
    if args.duration_not_exposed and args.origin != "host":
        fail("--duration-not-exposed is permitted only for --origin host")
    if args.invoked_at_not_exposed and args.origin != "host":
        fail("--invoked-at-not-exposed is permitted only for --origin host")
    if args.invoked_at is not None and not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[^ ]+Z", args.invoked_at):
        fail("--invoked-at must be an ISO-8601 UTC timestamp ending in Z")
    _scope, model_stamp = _validate_call_registration(args)

    usage = validate_usage(parse_json_object(args.usage_json, "--usage-json"))
    runtime = parse_json_object(args.runtime_json, "--runtime-json")
    validate_dispatch(args, runtime)
    prompt = file_record(args.prompt_path)
    preflight_path, preflight = _load_preflight(args.preflight_path)
    identity_fields = (
        "call_uid", "audit_id", "round_number", "step", "persona", "origin",
        "provider", "model", "effort", "model_stamp",
    )
    current_identity = {
        "call_uid": args.call_uid, "audit_id": args.audit_id, "round_number": args.round,
        "step": args.step, "persona": args.persona, "origin": args.origin,
        "provider": args.provider, "model": args.model, "effort": args.effort,
        "model_stamp": model_stamp,
    }
    drift_issues = [
        f"preflight metadata changed: {field}"
        for field in identity_fields if preflight.get(field) != current_identity[field]
    ]
    if preflight.get("client") != {"name": args.client_name, "version": args.client_version}:
        drift_issues.append("preflight client identity changed")
    if preflight.get("runtime") != runtime:
        drift_issues.append("preflight runtime/command metadata changed")
    before_prompt = preflight.get("prompt") or {}
    if (before_prompt.get("path") != prompt.get("path") or
            before_prompt.get("sha256") != prompt.get("sha256")):
        drift_issues.append("prompt path or hash changed after pre-dispatch receipt")
    round_scope_record = file_record(str(Path(args.round_dir).resolve() / "round-scope.json"))
    if (preflight.get("round_scope") or {}).get("sha256") != round_scope_record.get("sha256"):
        drift_issues.append("round-scope hash changed after pre-dispatch receipt")

    runtime_files, runtime_issues = _compare_file_records(
        preflight.get("runtime_files") or [], args.runtime_file, "runtime file"
    )
    context_files, context_issues = _compare_file_records(
        preflight.get("referenced_context") or [], args.context_file, "context file"
    )
    drift_issues.extend(runtime_issues)
    drift_issues.extend(context_issues)
    if args.response_path:
        response: dict[str, Any] = file_record(args.response_path) or {}
    else:
        response = {
            "status": "unavailable",
            "reason": args.response_unavailable_reason,
        }
    outputs = [file_record(p, required=False) or {"path": str(Path(p).resolve()), "missing": True}
               for p in args.output_path]
    if len({record["path"] for record in outputs}) != len(outputs):
        fail("--output-path contains duplicates")
    expected_outputs = set(preflight.get("expected_outputs") or [])
    actual_outputs = {record["path"] for record in outputs}
    if not expected_outputs.issubset(actual_outputs):
        drift_issues.append("declared output set omits a preflight expected output")
    if args.exit_status == "ok" and any(
            record.get("missing") and record["path"] in expected_outputs for record in outputs):
        drift_issues.append("terminal-ok call is missing a preflight expected output")
    call_uid = args.call_uid
    final_status = args.exit_status
    terminal_status_issues: list[str] = []
    exit_code = None if args.exit_code_not_exposed else args.exit_code
    if args.exit_code_not_exposed and args.origin != "host":
        fail("--exit-code-not-exposed is permitted only for --origin host")
    if args.exit_status == "ok" and exit_code is not None and exit_code != 0:
        final_status = "exit-code-mismatch"
        terminal_status_issues.append("exit_status=ok conflicts with nonzero exit_code")
    elif drift_issues:
        final_status = "input-drift"
    elif args.exit_status == "ok" and args.origin == "wrapper" and not external_usage_valid(usage):
        final_status = "usage-invalid"
        terminal_status_issues.append("external success requires authoritative positive total usage")
    provenance_dir = Path(args.round_dir).resolve() / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)
    persona_part = args.persona or "host"
    manifest_path = provenance_dir / f"{args.step}.{persona_part}.{call_uid}.call.json"
    if manifest_path.exists():
        fail(f"refusing to overwrite call manifest: {manifest_path}")
    manifest: dict[str, Any] = {
        "schema_version": "1.1",
        "store": preflight.get("store"),
        "call_uid": call_uid,
        "audit_id": args.audit_id,
        "round_number": args.round,
        "step": args.step,
        "persona": args.persona,
        "origin": args.origin,
        "provider": args.provider,
        "model": args.model,
        "effort": args.effort,
        "model_stamp": model_stamp,
        "invoked_at": args.invoked_at,
        "invoked_at_status": "not_exposed" if args.invoked_at is None else "observed",
        "recorded_at": now_utc(),
        "duration_seconds": args.duration,
        "duration_status": "not_exposed" if args.duration is None else "observed",
        "exit_code": exit_code,
        "exit_code_status": "not_exposed" if exit_code is None else "observed",
        "reported_exit_status": args.exit_status,
        "exit_status": final_status,
        "terminal_status_issues": terminal_status_issues,
        "context_status": (
            "caller-visible-prompt-and-artifacts; hidden-host-context-and-provider-request-not-available"
            if args.origin == "host"
            else "wrapper-visible-prompt/response/declared-context exact; hidden/global client state not claimed"
        ),
        "client": {"name": args.client_name, "version": args.client_version},
        "prompt": prompt,
        "response": response,
        "outputs": outputs,
        "usage": usage,
        "runtime": runtime,
        "runtime_files": runtime_files,
        "runtime_snapshots": preflight.get("runtime_snapshots", []),
        "referenced_context": context_files,
        "preflight": {
            "path": str(preflight_path),
            "bytes": preflight_path.stat().st_size,
            "sha256": sha256_file(preflight_path),
        },
        "preflight_verification": {
            "status": "match" if not drift_issues else "drift",
            "issues": drift_issues,
        },
        "round_scope": round_scope_record,
    }
    manifest["manifest_path"] = str(manifest_path)
    write_json_once(manifest_path, manifest)
    _audit_db_insert(args, manifest)
    print(manifest_path)
    return 0


def cmd_parse_opencode_usage(args: argparse.Namespace) -> int:
    path = Path(args.log).resolve()
    if not path.is_file():
        fail(f"log does not exist: {path}")
    sums = {k: 0 for k in (
        "input_tokens", "output_tokens", "reasoning_tokens",
        "cache_read_tokens", "cache_write_tokens", "total_tokens",
    )}
    cost = decimal.Decimal("0")
    seen = 0
    issues: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                issues.append(f"line {line_number}: malformed nonblank JSON record")
                continue
            if not isinstance(record, dict) or not isinstance(record.get("part", {}), dict):
                issues.append(f"line {line_number}: event and part must be objects")
                continue
            part = record.get("part") or {}
            if record.get("type") != "step_finish" and part.get("type") != "step-finish":
                continue
            seen += 1
            tokens = part.get("tokens")
            cache = tokens.get("cache") if isinstance(tokens, dict) else None
            values = {
                "input_tokens": tokens.get("input") if isinstance(tokens, dict) else None,
                "output_tokens": tokens.get("output") if isinstance(tokens, dict) else None,
                "reasoning_tokens": tokens.get("reasoning") if isinstance(tokens, dict) else None,
                "cache_read_tokens": cache.get("read") if isinstance(cache, dict) else None,
                "cache_write_tokens": cache.get("write") if isinstance(cache, dict) else None,
                "total_tokens": tokens.get("total") if isinstance(tokens, dict) else None,
            }
            bad = [key for key, value in values.items()
                   if isinstance(value, bool) or not isinstance(value, int) or value < 0]
            raw_cost = part.get("cost")
            if isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float)) or not math.isfinite(raw_cost) or raw_cost < 0:
                bad.append("cost_usd")
            if bad:
                issues.append(f"line {line_number}: missing/invalid {', '.join(bad)}")
                continue
            component_total = sum(values[k] for k in values if k != "total_tokens")
            if values["total_tokens"] != component_total:
                issues.append(f"line {line_number}: total token count does not equal components")
                continue
            for key, value in values.items():
                sums[key] += value
            cost += decimal.Decimal(str(raw_cost))
    if issues:
        result = {"status": "incomplete", "step_finish_events": seen, "issues": issues}
    elif not seen:
        result: dict[str, Any] = {"status": "unavailable", "source": "no step_finish events"}
    else:
        result = {"status": "complete", **sums, "cost_usd": float(cost),
                  "step_finish_events": seen}
    print(json.dumps(result, separators=(",", ":")))
    return 0


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
    """Refuse live artifacts without a terminal-ok, hash-matching latest manifest."""
    return reconcile_core(
        round_dir, step=step, persona=persona, target_key=str(output_path.resolve()),
        output_path=output_path, registered_models=registered_models,
        required_context_paths=required_context_paths, source=LiveSource(),
    )


def reconcile_core(round_dir: Path, *, step: str, persona: str | None,
                   target_key: str, output_path: Path | None,
                   registered_models: list[str], required_context_paths: list[Path] | None,
                   source: ByteSource, trace: list[tuple[str, str]] | None = None,
                   ) -> tuple[bool, str, dict[str, Any] | None]:
    """Refuse a latest artifact failing any provenance guard, using re-hashed source bytes."""
    def rejected(guard: str, condition: bool = False) -> bool:
        if trace is not None:
            trace.append((guard, "fail" if condition else "pass"))
        return condition

    def recorded_root(record: dict[str, Any]) -> str | None:
        if isinstance(source, LiveSource):
            return None
        path = (record.get("round_scope") or {}).get("path")
        return str(Path(path).parent) if path else None

    def hash_matches(path: Path, record: dict[str, Any], *, runtime: bool = False) -> bool:
        # Only runtime snapshot records carry store_ref (schema 1.1); 1.0 records pass None (T1 legacy contract).
        store_ref = record.get("store_ref") if runtime else None
        resolution = source.resolve(str(path), record.get("sha256"), store_ref=store_ref)
        return resolution.present and resolution.sha256 == record.get("sha256")

    candidates = []
    try:
        for manifest in load_call_manifests(round_dir):
            if manifest.get("step") != step:
                continue
            manifest_persona = manifest.get("persona")
            if manifest_persona not in {persona, None}:
                continue
            root = recorded_root(manifest)
            if any(source.output_key(out.get("path"), root) == target_key
                   for out in manifest.get("outputs") or []):
                candidates.append(manifest)
    except SystemExit:
        rejected("covering_manifest", True)
        raise
    if rejected("covering_manifest", not candidates):
        return False, f"no call manifest covers {target_key}", None
    latest = sorted(candidates, key=lambda m: (
        str(m.get("recorded_at", "")), str(m.get("call_uid", "")),
    ))[-1]
    prepared = []
    try:
        for path in (round_dir.resolve() / "provenance").glob("*.preflight.json"):
            _, receipt = _load_preflight(str(path))
            if (receipt.get("step") == step and receipt.get("persona") in {persona, None}
                    and any(source.output_key(path, recorded_root(receipt)) == target_key
                            for path in receipt.get("expected_outputs", []))):
                prepared.append(receipt)
    except SystemExit:
        rejected("prepared_attempt", True)
        raise
    if rejected("prepared_attempt", not prepared):
        return False, "no prepared attempt covers output", latest
    newest = max(prepared, key=lambda r: (r.get("prepared_at", ""), r.get("call_uid", "")))
    if rejected("newest_prepared_matches_latest", newest.get("call_uid") != latest.get("call_uid")):
        return False, "newest prepared attempt has no matching current terminal", latest
    try:
        scope = load_round_scope(round_dir)
    except SystemExit:
        rejected("round_scope_loads", True)
        raise
    rejected("round_scope_loads")
    if rejected("audit_round_match", latest.get("audit_id") != scope.get("audit_id") or
                latest.get("round_number") != scope.get("round_number")):
        return False, f"manifest audit/round mismatch: {latest.get('_path')}", latest
    scope_record = latest.get("round_scope") or {}
    scope_path = Path(str(scope_record.get("path", "")))
    if rejected("round_scope_hash", not hash_matches(scope_path, scope_record)):
        return False, f"round-scope missing or hash-mismatched: {latest.get('_path')}", latest
    if rejected("exit_status_ok", latest.get("exit_status") != "ok"):
        return False, f"latest call is terminal {latest.get('exit_status')!r}: {latest.get('_path')}", latest
    if rejected("wrapper_exit_code_zero", latest.get("origin") == "wrapper" and latest.get("exit_code") != 0):
        return False, f"terminal-ok manifest has nonzero exit_code: {latest.get('_path')}", latest
    if latest.get("origin") == "host" and latest.get("exit_code") is None:
        if latest.get("exit_code_status") != "not_exposed":
            rejected("host_exit_code_explicit", True)
            return False, f"host exit-code limitation is not explicit: {latest.get('_path')}", latest
    rejected("host_exit_code_explicit")
    if rejected("model_registered", latest.get("model_stamp") not in registered_models):
        return False, f"unregistered model stamp in {latest.get('_path')}", latest
    if rejected("step_model_bound", scope["step_models"].get(step) != latest.get("model_stamp")):
        return False, f"wrong model for phase {step!r}: {latest.get('_path')}", latest
    if rejected("stamp_consistent", latest.get("model_stamp") != f"{latest.get('model')}@{latest.get('effort')}"):
        return False, f"model/effort stamp is internally inconsistent: {latest.get('_path')}", latest
    try:
        validate_usage(latest.get("usage") or {})
    except SystemExit:
        rejected("usage_valid", True)
        return False, f"usage record is invalid: {latest.get('_path')}", latest
    rejected("usage_valid")
    if rejected("wrapper_usage_positive", latest.get("origin") == "wrapper" and not external_usage_valid(latest["usage"])):
        return False, "external call lacks positive authoritative usage", latest
    preflight = latest.get("preflight") or {}
    preflight_path = Path(str(preflight.get("path", "")))
    if rejected("preflight_hash", not hash_matches(preflight_path, preflight)):
        return False, f"preflight receipt missing or hash-mismatched: {latest.get('_path')}", latest
    if rejected("preflight_verification_match", (latest.get("preflight_verification") or {}).get("status") != "match"):
        return False, f"preflight/postflight drift was detected: {latest.get('_path')}", latest
    prompt = latest.get("prompt") or {}
    prompt_path = Path(str(prompt.get("path", "")))
    if rejected("prompt_hash", not hash_matches(prompt_path, prompt)):
        return False, f"prompt missing or hash-mismatched in {latest.get('_path')}", latest
    response = latest.get("response") or {}
    if response.get("status") == "unavailable":
        if rejected("response_limitation_valid", latest.get("origin") != "host" or not response.get("reason")):
            return False, f"response evidence is unavailable without a valid host limitation: {latest.get('_path')}", latest
    else:
        response_path = Path(str(response.get("path", "")))
        if rejected("response_hash", not hash_matches(response_path, response)):
            return False, f"response missing or hash-mismatched in {latest.get('_path')}", latest
    context_records = latest.get("referenced_context") or []
    for record in context_records:
        context_path = Path(str(record.get("path", "")))
        if not hash_matches(context_path, record):
            rejected("context_hash", True)
            return False, f"context missing or hash-mismatched in {latest.get('_path')}: {context_path}", latest
    rejected("context_hash")
    root = recorded_root(latest)
    if isinstance(source, LiveSource):
        context_by_path = {record.get("path"): record for record in context_records}
    else:
        context_by_path = {source.output_key(record.get("path"), root): record for record in context_records}
    for required in required_context_paths or []:
        required_path = str(required.resolve()) if isinstance(source, LiveSource) else str(required)
        if required_path not in context_by_path:
            rejected("required_context_recorded", True)
            return False, f"required context is not recorded in {latest.get('_path')}: {required_path}", latest
    rejected("required_context_recorded")
    if isinstance(source, LiveSource):
        if rejected("output_present", not output_path.is_file()):
            return False, f"output is missing: {output_path}", latest
        output_record = next(out for out in latest["outputs"]
                             if source.output_key(out.get("path"), root) == target_key)
        if rejected("output_hash", output_record.get("missing") or
                    source.resolve(str(output_path), output_record.get("sha256")).sha256 != output_record.get("sha256")):
            return False, f"output hash mismatch: {output_path}", latest
    else:
        output_record = next(out for out in latest["outputs"]
                             if source.output_key(out.get("path"), root) == target_key)
        output = source.resolve(output_record["path"], output_record.get("sha256"))
        if rejected("output_present", not output.present):
            return False, f"output is missing: {target_key}", latest
        if rejected("output_hash", output_record.get("missing") or output.sha256 != output_record.get("sha256")):
            return False, f"output hash mismatch: {target_key}", latest
    for record in latest.get("outputs", []):
        path = Path(record["path"])
        if record.get("missing") or not hash_matches(path, record):
            rejected("sibling_outputs", True)
            return False, f"required sibling output missing or changed: {path}", latest
    rejected("sibling_outputs")
    snapshots = latest.get("runtime_snapshots", [])
    if snapshots:
        expected = {(r["path"], r["sha256"]) for r in latest.get("runtime_files", [])}
        if {(r.get("source_path"), r.get("sha256")) for r in snapshots} != expected:
            rejected("snapshot_coverage", True)
            return False, "runtime snapshot coverage mismatch", latest
        runtime_records = snapshots
    else:
        # Legacy receipts retain current-path checks, not invented historical snapshots.
        runtime_records = latest.get("runtime_files", [])
    rejected("snapshot_coverage")
    for record in runtime_records:
        path = Path(record["path"])
        if not hash_matches(path, record, runtime=True):
            rejected("runtime_hash", True)
            return False, f"runtime evidence missing or changed: {path}", latest
    rejected("runtime_hash")
    return True, "ok", latest


def cmd_verify_round(args: argparse.Namespace) -> int:
    """Verify a round read-only; refuse verdict writes inside its evidence tree."""
    from round_verify import verify_round

    return verify_round(
        Path(args.round_dir), mode=args.mode, no_live=args.no_live,
        store=Path(args.store) if args.store is not None else None,
        pack=Path(args.pack) if args.pack is not None else None,
        remaps=args.remap, anchor_repo=Path(args.anchor_repo) if args.anchor_repo is not None else None,
        anchor_prefix=args.anchor_prefix, json_out=Path(args.json_out),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minsky content-addressed call provenance")
    sub = parser.add_subparsers(dest="command", required=True)

    p_uid = sub.add_parser("new-uid")
    p_uid.set_defaults(handler=cmd_new_uid)

    p_scope = sub.add_parser("register-round")
    p_scope.add_argument("--audit-id", required=True)
    p_scope.add_argument("--round", type=int, required=True)
    p_scope.add_argument("--round-dir", required=True)
    p_scope.add_argument("--pack", help="Hash an already-built audit pack into the round scope")
    p_scope.add_argument("--personas", required=True, help="Explicit JSON array")
    p_scope.add_argument("--models", required=True, help="Explicit JSON array with @effort")
    p_scope.add_argument(
        "--step-models", required=True,
        help="JSON object binding each of the four phases to one registered model@effort",
    )
    p_scope.set_defaults(handler=cmd_register_round)

    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--call-uid", required=True)
    p_prepare.add_argument("--audit-id", required=True)
    p_prepare.add_argument("--round", type=int, required=True)
    p_prepare.add_argument("--round-dir", required=True)
    p_prepare.add_argument("--step", required=True, choices=list(STEP_NAMES))
    p_prepare.add_argument("--persona")
    p_prepare.add_argument("--origin", required=True, choices=["wrapper", "host"])
    p_prepare.add_argument("--provider", required=True)
    p_prepare.add_argument("--model", required=True)
    p_prepare.add_argument("--effort", required=True)
    p_prepare.add_argument("--client-name", required=True)
    p_prepare.add_argument("--client-version", required=True)
    p_prepare.add_argument("--prompt-path", required=True)
    p_prepare.add_argument("--expected-output-path", action="append", required=True)
    p_prepare.add_argument("--runtime-file", action="append", default=[])
    p_prepare.add_argument("--context-file", action="append", default=[])
    p_prepare.add_argument("--runtime-json", required=True)
    p_prepare.set_defaults(handler=cmd_prepare)

    p_record = sub.add_parser("record")
    p_record.add_argument("--call-uid", required=True)
    p_record.add_argument("--audit-id", required=True)
    p_record.add_argument("--round", type=int, required=True)
    p_record.add_argument("--round-dir", required=True)
    p_record.add_argument("--step", required=True, choices=list(STEP_NAMES))
    p_record.add_argument("--persona")
    p_record.add_argument("--origin", required=True, choices=["wrapper", "host"])
    p_record.add_argument("--provider", required=True)
    p_record.add_argument("--model", required=True, help="Base model without @effort")
    p_record.add_argument("--effort", required=True)
    invoked_group = p_record.add_mutually_exclusive_group(required=True)
    invoked_group.add_argument("--invoked-at")
    invoked_group.add_argument("--invoked-at-not-exposed", action="store_true")
    duration_group = p_record.add_mutually_exclusive_group(required=True)
    duration_group.add_argument("--duration", type=float)
    duration_group.add_argument("--duration-not-exposed", action="store_true")
    exit_code_group = p_record.add_mutually_exclusive_group(required=True)
    exit_code_group.add_argument("--exit-code", type=int)
    exit_code_group.add_argument("--exit-code-not-exposed", action="store_true")
    p_record.add_argument("--exit-status", required=True)
    p_record.add_argument("--client-name", required=True)
    p_record.add_argument("--client-version", required=True)
    p_record.add_argument("--prompt-path", required=True)
    p_record.add_argument("--preflight-path", required=True)
    response_group = p_record.add_mutually_exclusive_group(required=True)
    response_group.add_argument("--response-path")
    response_group.add_argument(
        "--response-unavailable-reason",
        help="Host-only: explain why no raw host/provider transcript is observable",
    )
    p_record.add_argument("--output-path", action="append", required=True)
    p_record.add_argument("--runtime-file", action="append", default=[])
    p_record.add_argument(
        "--context-file", action="append", default=[],
        help="Caller-visible context artifact to hash (pack and prior-step outputs)",
    )
    p_record.add_argument("--runtime-json", required=True)
    p_record.add_argument("--usage-json", required=True)
    p_record.add_argument("--record-db", action="store_true")
    p_record.add_argument("--db")
    p_record.set_defaults(handler=cmd_record)

    p_usage = sub.add_parser("parse-opencode-usage")
    p_usage.add_argument("--log", required=True)
    p_usage.set_defaults(handler=cmd_parse_opencode_usage)

    p_verify = sub.add_parser("verify-round")
    p_verify.add_argument("--round-dir", required=True)
    p_verify.add_argument("--mode", required=True, choices=["live", "archive"])
    p_verify.add_argument("--no-live", action="store_true")
    p_verify.add_argument("--store")
    p_verify.add_argument("--pack")
    p_verify.add_argument("--remap", action="append", default=[])
    p_verify.add_argument("--anchor-repo")
    p_verify.add_argument("--anchor-prefix")
    p_verify.add_argument("--json-out", required=True)
    p_verify.set_defaults(handler=cmd_verify_round)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
