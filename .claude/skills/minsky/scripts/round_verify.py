"""Verify both round dimensions; refuse unanchored archive acceptance and evidence writes."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import provenance
from byte_sources import (
    RAW_MANIFEST_HEADER, ArchiveSource, ByteSource, LiveSource, Resolution,
    load_symlink_map, parse_remaps,
)


REGISTER_PATH = "documentation/minsky_audit_lifecycle_register.tsv"
REGISTER_HEADER = (
    "utc", "audit_id", "audit_dir", "event", "state_after", "class",
    "citation_resolvability", "manifest_sha256", "pack_sha256", "copy_location",
    "verdict_json", "actor", "note", "lock_id",
)
SCHEMA_PATH = provenance.SKILL_DIR / "schemas" / "verify-round.schema.json"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-optional-locks", "--literal-pathspecs", "-C", str(repo), *args],
        capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


def _load(loader: Callable[..., Any], *args: Any) -> tuple[Any, str | None]:
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            return loader(*args), None
    except SystemExit:
        text = " ".join(stderr.getvalue().splitlines()).strip()
        return None, f"load-error: {text or loader.__name__}"
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return None, f"load-error: {loader.__name__}: {exc}"


def _root(receipt: dict[str, Any]) -> str | None:
    path = (receipt.get("round_scope") or {}).get("path")
    return str(PurePosixPath(path).parent) if path else None


def _records(receipt: dict[str, Any], kind: str) -> list[tuple[str, dict[str, Any]]]:
    records = []
    for label in ("prompt", "response", "round_scope"):
        if label in receipt:
            records.append((label, receipt[label]))
    for field, label in (("outputs", "output"), ("referenced_context", "context"),
                         ("runtime_snapshots", "runtime_snapshot")):
        records.extend((label, record) for record in receipt.get(field) or [])
    if not receipt.get("runtime_snapshots"):
        records.extend(("runtime_file", record) for record in receipt.get("runtime_files") or [])
    if kind == "call" and "preflight" in receipt:
        records.append(("preflight", receipt["preflight"]))
    return records


def _check_receipt(receipt: dict[str, Any], path: Path, kind: str) -> None:
    if not isinstance(receipt, dict):
        raise ValueError(f"invalid {kind} receipt {path}: expected an object")
    for field in ("call_uid", "step"):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise ValueError(f"invalid {kind} receipt {path}: {field} must be a non-empty string")
    if receipt.get("persona") is not None and not isinstance(receipt["persona"], str):
        raise ValueError(f"invalid {kind} receipt {path}: persona must be a string or null")
    if kind == "call" and not isinstance(receipt.get("exit_status"), str):
        raise ValueError(f"invalid {kind} receipt {path}: exit_status must be a string")
    for field in ("outputs", "referenced_context", "runtime_snapshots", "runtime_files"):
        if field in receipt and not isinstance(receipt[field], list):
            raise ValueError(f"invalid {kind} receipt {path}: {field} must be an array")
    for label, record in _records(receipt, kind):
        if not isinstance(record, dict):
            raise ValueError(f"invalid {kind} receipt {path}: {label} must be an object")
        if "path" in record and not isinstance(record["path"], str):
            raise ValueError(f"invalid {kind} receipt {path}: {label}.path must be a string")
        if "sha256" in record and (not isinstance(record["sha256"], str)
                                   or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            raise ValueError(f"invalid {kind} receipt {path}: {label}.sha256 is invalid")
        if "reason" in record and not isinstance(record["reason"], str):
            raise ValueError(f"invalid {kind} receipt {path}: {label}.reason must be a string")


def _table(payload: bytes, header: tuple[str, ...], label: str) -> list[dict[str, str]]:
    text = payload.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0] != "\t".join(header) + "\n":
        raise ValueError(f"{label} header mismatch")
    rows = []
    for number, fields in enumerate(csv.reader(lines[1:], delimiter="\t", quoting=csv.QUOTE_NONE), 2):
        if len(fields) != len(header):
            raise ValueError(f"{label} line {number} has {len(fields)} fields")
        rows.append(dict(zip(header, fields)))
    return rows


def _anchor(path: Path, repo: Path | None, prefix: str | None, round_dir: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    result = {"manifest": path.name, "sha256": digest, "anchor": "none", "commit": None,
              "register_line": None, "reason": ""}
    if repo is None:
        return {**result, "reason": "no anchor repository"}
    rel = PurePosixPath(prefix, "provenance", path.name).as_posix()
    history = _git(repo, "rev-list", "--all", "--", rel)
    if history.returncode:
        return {**result, "reason": f"cannot read git history for {rel}: "
                + history.stderr.decode("utf-8", errors="replace").strip()}
    for commit in history.stdout.decode("ascii").splitlines():
        blob = _git(repo, "cat-file", "-p", f"{commit}:{rel}")
        if not blob.returncode and blob.stdout == payload:
            return {**result, "anchor": "git", "commit": commit}
    register = _git(repo, "cat-file", "-p", f"HEAD:{REGISTER_PATH}")
    if register.returncode:
        return {**result, "reason": f"not in any commit; no committed register {REGISTER_PATH}"}
    try:
        rows = _table(register.stdout, REGISTER_HEADER, "register")
        audit_dir = PurePosixPath(prefix).parent.as_posix()
        seals = [(number, row) for number, row in enumerate(rows, 2)
                 if row["event"] == "seal" and row["audit_dir"] == audit_dir]
        if not seals:
            return {**result, "reason": "no seal row"}
        raw_path = round_dir.parent / "lifecycle" / "raw_manifest.tsv"
        if not raw_path.is_file():
            return {**result, "reason": f"raw manifest is missing: {raw_path}"}
        raw = raw_path.read_bytes()
        raw_rows = _table(raw, RAW_MANIFEST_HEADER, "raw manifest")
        raw_sha = hashlib.sha256(raw).hexdigest()
        matching = [number for number, row in seals if row["manifest_sha256"] == raw_sha]
        if not matching:
            return {**result, "reason": "raw manifest hash differs from seal row"}
        name = f"{round_dir.name}/provenance/{path.name}"
        listed = [row for row in raw_rows if row["path"] == name]
        if not listed:
            return {**result, "reason": "manifest not listed in raw manifest"}
        if len(listed) != 1:
            return {**result, "reason": "manifest listed more than once in raw manifest"}
        if listed[0]["sha256"] != digest:
            return {**result, "reason": "manifest hash differs from raw manifest"}
        return {**result, "anchor": "register", "register_line": matching[0]}
    except (OSError, UnicodeError, ValueError) as exc:
        return {**result, "reason": str(exc)}


def _canonical(round_dir: Path, manifests: list[dict[str, Any]], source: ByteSource,
               models: list[str], errors: list[str], *,
               preflights: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select prepared targets; refuse hiding outputs of wholly uncovered manifests."""
    targets = {}
    excluded = {}
    display_source = source if isinstance(source, ArchiveSource) else ArchiveSource(
        store_root=None, pack=None, remaps=[], no_live=True,
    )
    for manifest in manifests:
        covered = {
            source.output_key(record.get("path", ""), _root(manifest))
            for record in manifest.get("outputs") or []
            if any(receipt["step"] == manifest["step"]
                   and receipt.get("persona") in {manifest.get("persona"), None}
                   and isinstance(receipt.get("expected_outputs", []), list)
                   and all(isinstance(expected, str)
                           for expected in receipt.get("expected_outputs", []))
                   and source.output_key(record.get("path", ""), _root(manifest)) in {
                       source.output_key(expected, _root(receipt))
                       for expected in receipt.get("expected_outputs", [])
                   } for receipt in preflights)
        }
        for record in manifest.get("outputs") or []:
            recorded = record.get("path", "")
            key = source.output_key(recorded, _root(manifest))
            target = (manifest["step"], manifest.get("persona"), key)
            display = display_source.output_key(recorded, _root(manifest))
            if not covered or key in covered:
                targets.setdefault(target, (recorded, display))
            elif target not in excluded or manifest["call_uid"] > excluded[target]["call_uid"]:
                excluded[target] = {
                    "step": manifest["step"], "persona": manifest.get("persona"),
                    "call_uid": manifest["call_uid"], "output": display,
                    "reason": "not an expected output of any prepared attempt (preservation only)",
                }
    outputs = []
    for (step, persona, key), (recorded, display) in sorted(
            targets.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2])):
        trace = []
        latest = None
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                ok, reason, latest = provenance.reconcile_core(
                    round_dir, step=step, persona=persona, target_key=key,
                    output_path=Path(recorded) if isinstance(source, LiveSource) else None,
                    registered_models=models, required_context_paths=None, source=source, trace=trace,
                )
        except SystemExit:
            ok = False
            reason = "load-error: " + " ".join(stderr.getvalue().splitlines()).strip()
            errors.append(reason)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            ok, reason = False, f"verification-error: {exc}"
            errors.append(reason)
        status = "ok" if ok else "fail"
        first_failure = next((guard for guard, state in trace if state == "fail"), None)
        if (not ok and first_failure == "runtime_hash" and latest is not None
                and not latest.get("runtime_snapshots") and latest.get("runtime_files")):
            status = "legacy-runtime-unverifiable"
        reached = dict(trace)
        outputs.append({
            "step": step, "persona": persona, "output": display, "status": status,
            "reason": reason, "call_uid": latest.get("call_uid") if latest is not None else None,
            "trace": [[guard, reached.get(guard, "not-reached")] for guard in provenance.GUARDS],
        })
    attempt_evidence = sorted(
        (item for target, item in excluded.items() if target not in targets),
        key=lambda item: (item["step"], item["persona"] or "", item["output"]),
    )
    return outputs, attempt_evidence


def _genuinely_absent(resolution: Resolution) -> bool:
    """Recognize no candidate bytes anywhere via not-present and empty detail.

    Refuse corrupt identities, store or ref errors, and resolution exceptions.
    """
    return not resolution.present and resolution.detail == ""


def _preservation(receipts: list[tuple[str, Path, dict[str, Any]]],
                  all_receipts: list[tuple[str, Path, dict[str, Any]]],
                  source: ByteSource, errors: list[str]) -> list[dict[str, Any]]:
    """Check every recorded object; refuse unverified substitutes for absent bytes."""
    by_key: dict[str, dict[str, set[str]]] = {}
    for kind, path, receipt in all_receipts:
        for _, record in _records(receipt, kind):
            if record.get("path") and record.get("sha256"):
                key = source.output_key(record["path"], _root(receipt))
                by_key.setdefault(key, {}).setdefault(record["sha256"], set()).add(str(path))
    objects = []
    for kind, path, receipt in receipts:
        for label, record in _records(receipt, kind):
            recorded = record.get("path", "")
            sha = record.get("sha256", "")
            obj = {"receipt": kind, "call_uid": receipt["call_uid"],
                   "exit_status": receipt.get("exit_status", "") if kind == "call" else "prepared-only",
                   "kind": label, "recorded_path": recorded, "sha256": sha,
                   "status": "", "resolved_via": "", "detail": ""}
            if ((label == "output" and record.get("missing") is True)
                    or (label == "response" and record.get("status") == "unavailable")):
                obj.update(status="recorded-absent", detail=record.get("reason", ""))
            else:
                try:
                    if label == "runtime_snapshot":
                        resolved = source.resolve(recorded, sha or None,
                                                  store_ref=record.get("store_ref"))
                    else:
                        resolved = source.resolve(recorded, sha or None)
                except (OSError, ValueError) as exc:
                    detail = f"resolution-error: {label} {recorded}: {exc}"
                    errors.append(detail)
                    resolved = Resolution(False, None, "unresolved", detail)
                if resolved.present and sha and resolved.sha256 == sha:
                    status = "ok"
                elif label == "runtime_file":
                    status = "legacy-runtime-unverifiable"
                elif resolved.present:
                    key = source.output_key(recorded, _root(receipt))
                    others = by_key.get(key, {}).get(resolved.sha256, set()) - {str(path)}
                    status = "superseded-unrecoverable" if others else "mismatch"
                else:
                    status = "missing"
                    if _genuinely_absent(resolved):
                        key = source.output_key(recorded, _root(receipt))
                        for other_sha, holders in sorted(by_key.get(key, {}).items()):
                            if other_sha == sha or not holders - {str(path)}:
                                continue
                            try:
                                later = source.resolve(recorded, other_sha)
                            except (OSError, ValueError) as exc:
                                errors.append(f"resolution-error: {label} {recorded}: {exc}")
                                break
                            if later.present and later.sha256 == other_sha:
                                status = "superseded-unrecoverable"
                                resolved = Resolution(
                                    True, other_sha, later.resolved_via,
                                    f"superseded: later recorded version {other_sha} resolves",
                                )
                                break
                obj.update(status=status, resolved_via=resolved.resolved_via, detail=resolved.detail)
            objects.append(obj)
    return sorted(objects, key=lambda obj: (
        obj["receipt"], obj["call_uid"], obj["kind"], obj["recorded_path"], obj["sha256"],
    ))


def _structural_check(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "null": value is None,
        "boolean": type(value) is bool, "integer": type(value) is int,
    }
    if types and not any(matches.get(kind, False) for kind in types):
        raise ValueError(f"verdict {path} has invalid type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"verdict {path} has invalid enum {value!r}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"verdict {path} must equal {schema['const']!r}")
    if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        raise ValueError(f"verdict {path} has invalid string {value!r}")
    if "minimum" in schema and value is not None and value < schema["minimum"]:
        raise ValueError(f"verdict {path} is below its minimum")
    if isinstance(value, dict) and "properties" in schema:
        if set(schema.get("required", [])) - value.keys():
            raise ValueError(f"verdict {path} is missing required keys")
        if schema.get("additionalProperties") is False and value.keys() - schema["properties"].keys():
            raise ValueError(f"verdict {path} has unexpected keys")
        for key, item in value.items():
            if key in schema["properties"]:
                _structural_check(item, schema["properties"][key], f"{path}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise ValueError(f"verdict {path} has invalid array length")
        for index, item in enumerate(value):
            rule = (schema["prefixItems"][index] if "prefixItems" in schema
                    and index < len(schema["prefixItems"]) else schema.get("items", {}))
            if isinstance(rule, dict):
                _structural_check(item, rule, f"{path}[{index}]")
            elif rule is False:
                raise ValueError(f"verdict {path}[{index}] is an unexpected array item")


def validate_verdict(verdict: dict[str, Any]) -> None:
    """Refuse invalid verdict structures; use draft 2020-12 validation when available."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        import jsonschema
    except ImportError:
        _structural_check(verdict, schema)
    else:
        jsonschema.Draft202012Validator(schema).validate(verdict)


def verify_round(round_dir: Path, *, mode: str, json_out: Path, no_live: bool = False,
                 store: Path | None = None, pack: Path | None = None,
                 remaps: list[str] | None = None, anchor_repo: Path | None = None,
                 anchor_prefix: str | None = None) -> int:
    """Write a create-only verdict; refuse invalid flags and any write inside the round."""
    if mode not in {"live", "archive"}:
        provenance.fail(f"--mode must be live or archive: {mode!r}")
    if mode == "live":
        for flag, used in (("--no-live", no_live), ("--store", store is not None),
                           ("--pack", pack is not None), ("--remap", bool(remaps))):
            if used:
                provenance.fail(f"{flag} is valid only with --mode archive")
    if (anchor_repo is None) != (anchor_prefix is None):
        provenance.fail("--anchor-repo and --anchor-prefix must be given together")
    round_dir = round_dir.resolve()
    if json_out.exists() or json_out.is_symlink():
        provenance.fail(f"refusing to overwrite immutable record: {json_out}")
    json_out = json_out.resolve()
    if json_out.is_relative_to(round_dir):
        provenance.fail("refusing to write a verdict inside the round directory")
    try:
        pairs = parse_remaps(remaps or [])
    except ValueError as exc:
        provenance.fail(str(exc))
    if anchor_repo is not None:
        anchor_repo = anchor_repo.resolve()
        prefix = PurePosixPath(anchor_prefix)
        if prefix.is_absolute() or ".." in prefix.parts or not anchor_prefix:
            provenance.fail(f"--anchor-prefix must be repository-relative: {anchor_prefix!r}")
        anchor_prefix = prefix.as_posix()
    else:
        found = _git(round_dir, "rev-parse", "--show-toplevel")
        if not found.returncode:
            anchor_repo = Path(os.fsdecode(found.stdout.strip())).resolve()
            anchor_prefix = Path(os.path.relpath(round_dir, anchor_repo)).as_posix()
    store = store.resolve() if store is not None else None
    pack = pack.resolve() if pack is not None else None
    verdict = {
        "schema": "minsky-verify-round/1.1", "round_dir": str(round_dir), "mode": mode,
        "no_live": no_live, "store": str(store) if store is not None else None,
        "pack": str(pack) if pack is not None else None, "remaps": [list(pair) for pair in pairs],
        "anchor_repo": str(anchor_repo) if anchor_repo is not None else None,
        "anchor_prefix": anchor_prefix, "verdict": "",
        "canonical": {"result": "ok", "outputs": [], "attempt_evidence": []},
        "preservation": {"result": "ok", "objects": []}, "manifest_anchors": [],
        "no_runtime_recorded": [], "context_drift": "not-checked" if no_live else "none",
        "context_drift_paths": [], "remap_log": [], "errors": [],
    }
    errors = verdict["errors"]
    manifests, error = _load(provenance.load_call_manifests, round_dir)
    manifests = manifests or []
    receipt_error = error is not None
    if error:
        errors.append(error)
    if not error and not manifests:
        errors.append("no call manifests in round")
    verdict["no_runtime_recorded"] = sorted({
        m["call_uid"] for m in manifests if isinstance(m.get("call_uid"), str)
        and not m.get("runtime_files") and not m.get("runtime_snapshots")
    })
    all_receipts = [("call", Path(m["_path"]), m) for m in manifests]
    for path in sorted((round_dir / "provenance").glob("*.preflight.json")):
        loaded, error = _load(provenance._load_preflight, str(path))
        if error:
            errors.append(error)
            receipt_error = True
        else:
            all_receipts.append(("preflight", path, loaded[1]))
    for kind, path, receipt in all_receipts:
        _, error = _load(_check_receipt, receipt, path, kind)
        if error:
            errors.append(error)
            receipt_error = True
    scope, error = _load(provenance.load_round_scope, round_dir)
    if error:
        errors.append(error)
    source: ByteSource | None = LiveSource()
    if mode == "archive":
        links, error = _load(load_symlink_map, round_dir.parent)
        if error:
            errors.append(error)
            source = None
        else:
            prefix = round_dir.name + "/"
            source = ArchiveSource(
                store_root=store, pack=pack, remaps=pairs, no_live=no_live,
                symlink_map=[(realpath, link[len(prefix):]) for realpath, link in links
                             if link.startswith(prefix)],
            )
    load_error = errors[0] if errors else None
    if not receipt_error and source is not None:
        outputs, attempt_evidence = _canonical(
            round_dir, manifests, source, scope["models"] if scope else [], errors,
            preflights=[receipt for kind, _, receipt in all_receipts if kind == "preflight"],
        )
        verdict["canonical"]["outputs"] = outputs
        verdict["canonical"]["attempt_evidence"] = attempt_evidence
        call_uids = {manifest["call_uid"] for manifest in manifests}
        receipts = [(kind, path, receipt) for kind, path, receipt in all_receipts
                    if kind == "call" or receipt["call_uid"] not in call_uids]
        objects = _preservation(receipts, all_receipts, source, errors)
        verdict["preservation"]["objects"] = objects
        statuses = {obj["status"] for obj in objects}
        verdict["preservation"]["result"] = (
            "fail" if statuses & {"mismatch", "missing"} else "ok-with-losses"
            if statuses & {"superseded-unrecoverable", "legacy-runtime-unverifiable"} else "ok"
        )
        if mode == "archive" and not no_live:
            drift = set()
            for _, _, receipt in receipts:
                for record in receipt.get("referenced_context") or []:
                    path = Path(record.get("path", ""))
                    if path.is_file() and provenance.sha256_file(path) != record.get("sha256"):
                        drift.add(str(path))
            verdict["context_drift_paths"] = sorted(drift)
            verdict["context_drift"] = "drifted" if drift else "none"
    else:
        verdict["preservation"]["result"] = "fail"
    anchor_error = None
    for path in sorted((round_dir / "provenance").glob("*.call.json")):
        try:
            verdict["manifest_anchors"].append(_anchor(path, anchor_repo, anchor_prefix, round_dir))
        except OSError as exc:
            anchor_error = f"load-error: manifest anchor {path}: {exc}"
            errors.append(anchor_error)
    outputs = verdict["canonical"]["outputs"]
    failed = next((out for out in outputs if out["status"] == "fail"), None)
    legacy = any(out["status"] == "legacy-runtime-unverifiable" for out in outputs)
    if failed is not None or load_error or anchor_error:
        verdict["canonical"]["result"] = "fail"
        verdict["verdict"] = "fail:" + (load_error or (failed["reason"] if failed else anchor_error))
    elif verdict["preservation"]["result"] == "fail":
        damaged = next((obj for obj in verdict["preservation"]["objects"]
                        if obj["status"] in {"mismatch", "missing"}), None)
        verdict["verdict"] = (
            f"fail:preservation {damaged['status']} {damaged['kind']} {damaged['recorded_path']}"
            if damaged is not None else "fail:preservation not verified"
        )
    elif legacy:
        verdict["canonical"]["result"] = "legacy-runtime-unverifiable"
        verdict["verdict"] = "legacy-runtime-unverifiable"
    elif mode == "archive" and any(anchor["anchor"] == "none" for anchor in verdict["manifest_anchors"]):
        verdict["verdict"] = "consistent-unanchored"
    elif mode == "archive" and verdict["context_drift"] == "drifted":
        verdict["verdict"] = "ok-archive-context-drifted"
    else:
        verdict["verdict"] = f"ok-{mode}"
    if isinstance(source, ArchiveSource):
        verdict["remap_log"] = sorted(source.remap_log, key=lambda item: (
            item["recorded"], item["opened"], item["remap"],
        ))
    verdict["errors"] = sorted(set(errors))
    validate_verdict(verdict)
    provenance.write_json_once(json_out, verdict)
    return 0 if verdict["verdict"].startswith("ok-") else 1
