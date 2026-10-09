"""Report fresh expiry evidence; refuse deletion without a sanctioned, complete reverse-citation census."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import lifecycle_copies as copies
import lifecycle_pack as packs
from lifecycle_citations import effective_class
from lifecycle_pack import bounded_path, sealed_rows, volume_window
from lifecycle_register import AuditLock, REGISTER_PATH, RegisterError, _append_lock, git_read, read_register
from lifecycle_seal import (_directory, _event, _field, _work_log, audit_record, utc_now)

RETENTION = ("raw pack deleted; runtime CAS blobs retained and verifiable; this code-only audit's prompts, "
             "responses, outputs and other non-CAS raw members are no longer recoverable")


def _cli():
    spec = importlib.util.spec_from_file_location("minsky_expiry_cli", Path(__file__).with_name("minsky-lifecycle.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expire(repo: Path, *, audit_id: str, actor: str, execute: bool = False,
           audits_db: Path | None = None, window_confirmed: str | None = None,
           titan_reader: Callable[[str], list[dict]] | None = None) -> dict:
    """Census fresh citations under the audit lock; refuse unknown sources, dependencies and unsafe pack deletion."""
    repo = repo.resolve()
    _field(actor, "--actor", nonempty=True)
    volume_window(repo, window_confirmed)
    with AuditLock.acquire(repo, audit_id, verb="expire", session=actor) as lock:
        register = read_register(repo)
        rows = [row for row in register.rows if row["audit_id"] == audit_id]
        if not rows or not rows[-1]["audit_dir"]:
            raise RegisterError(f"refused expire {audit_id}: registered audit directory required")
        directory = _directory(repo, Path(rows[-1]["audit_dir"]))
        report = {"audit_id": audit_id, "audit_dir": directory.relative_to(repo).as_posix(),
                  "utc": utc_now(), "eligible": False, "execute": execute, "deletion_list": [],
                  "census_complete": False, "reasons": [], "census_scope": [],
                  "source_versions": [], "dependencies": [], "census_audits": []}
        reasons, dependencies = report["reasons"], report["dependencies"]
        if effective_class(repo, audit_id) != "code-only":
            reasons.append("class is not code-only")
        last_ruling = max((i for i, row in enumerate(rows) if row["event"] == "ruling"), default=-1)
        if any(row["event"] == "challenge" for row in rows[last_ruling + 1:]):
            reasons.append("challenge without a later ruling")
        last_promote = max((i for i, row in enumerate(rows) if row["event"] == "promote"), default=-1)
        if any(row["event"] == "promotion-pending" for row in rows[last_promote + 1:]):
            reasons.append("promotion-pending without a later promote")
        if last_promote >= 0:
            reasons.append("promote row exists")
        if rows[-1]["state_after"] != "offloaded":
            reasons.append("state is not offloaded")
        seal = next((row for row in reversed(rows) if row["event"] == "seal"), None)
        if seal is None:
            reasons.append("seal row missing")
        else:
            sealed = datetime.fromisoformat(seal["utc"].replace("Z", "+00:00"))
            if datetime.now(UTC) - sealed < timedelta(days=90):
                reasons.append("90-day seal grace period has not elapsed")
            if seal["citation_resolvability"] != "pass":
                reasons.append("citation_resolvability is not pass")

        def read_source(path: Path) -> bytes | None:
            label = str(path)
            report["census_scope"].append(label)
            try:
                volume_window(path, window_confirmed)
                bounded_path(repo, path)
                if path.is_symlink() or not path.is_file():
                    raise RegisterError(f"refused expiry source {path}: regular file required")
                raw = path.read_bytes()
                report["source_versions"].append({"path": label, "sha256": hashlib.sha256(raw).hexdigest()})
                raw.decode("utf-8")
                return raw
            except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                reasons.append(f"source unavailable {path}: {exc}")
                return None

        read_source(repo / REGISTER_PATH)
        controls = [directory / "lifecycle/digest.json", directory / "lifecycle/DIGEST.md",
                    directory / "lifecycle/raw_manifest.tsv"]
        classification = next((row for row in reversed(rows) if row["event"] == "classify"), None)
        if classification is None or not classification["note"]:
            reasons.append("classification record path unavailable")
        else:
            controls.append(directory / classification["note"])
        for path in controls:
            raw = read_source(path)
            if raw is None:
                continue
            try:
                relative = path.relative_to(repo).as_posix()
                if git_read(repo, "--literal-pathspecs", "status", "--porcelain", "--", relative):
                    reasons.append(f"expiry control is not committed {relative}")
                if git_read(repo, "cat-file", "-p", "HEAD:" + relative) != raw:
                    reasons.append(f"expiry control differs from HEAD {relative}")
            except (OSError, ValueError, RuntimeError) as exc:
                reasons.append(f"expiry control commitment unavailable {path}: {exc}")

        def search(path: Path, *, work_log: bool = False) -> None:
            raw = read_source(path)
            if raw is None:
                return
            needles = (audit_id, report["audit_dir"])
            if work_log:
                # Use W5's category/entry rule, then record the same bytes' version again below.
                before = hashlib.sha256(raw).hexdigest()
                for needle in needles:
                    fired, status = _work_log(path, needle)
                    if status == "unknown":
                        reasons.append(f"work-log source incomplete {path}")
                    dependencies.extend({"path": str(path), "entry": entry} for entry in fired)
                try:
                    if hashlib.sha256(path.read_bytes()).hexdigest() != before:
                        reasons.append(f"work-log source changed during census {path}")
                except OSError as exc:
                    reasons.append(f"work-log source unavailable {path}: {exc}")
            elif any(needle in raw.decode("utf-8") for needle in needles):
                dependencies.append({"path": str(path), "entry": "audit id or audit directory referenced"})

        search(repo / "documentation/phd_work_log.md", work_log=True)
        if audits_db is None:
            reasons.append("audits.work_log_entry_path source unavailable (--audits-db required)")
        else:
            volume_window(audits_db, window_confirmed)
            try:
                record = audit_record(audits_db, audit_id)
                report["census_scope"].append(str(audits_db) + ":audits.work_log_entry_path")
                report["source_versions"].append({"path": str(audits_db) + ":audits.work_log_entry_path",
                    "sha256": hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()})
                extra = record.get("work_log_entry_path")
                if extra:
                    search(repo / extra, work_log=True)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                reasons.append(f"audits.work_log_entry_path source unavailable {audits_db}: {exc}")
        cli = _cli()
        try:
            discovered = cli.discover_rounds(repo, {})
        except (OSError, ValueError, RuntimeError) as exc:
            reasons.append(f"reverse-citation discovery unavailable {repo}: {exc}")
            discovered = {}
        known = {row["audit_id"] for row in register.rows if row["audit_id"]} | set(discovered)
        report["discovered_audits"] = {key: {"audit_dir": value["audit_dir"].relative_to(repo).as_posix(),
            "rounds": [path.relative_to(repo).as_posix() for path in value["rounds"]]}
            for key, value in sorted(discovered.items())}
        for other in sorted(known - {audit_id}):
            audit_class = effective_class(repo, other)
            if audit_class == "code-only":
                continue
            other_rows = [row for row in register.rows if row["audit_id"] == other]
            entry = {"audit_id": other, "class": audit_class, "audit_dir": "", "files": []}
            report["census_audits"].append(entry)
            try:
                recorded = {row["audit_dir"] for row in other_rows if row["audit_dir"]}
                found = discovered.get(other)
                if found:
                    recorded.add(found["audit_dir"].relative_to(repo).as_posix())
                if len(recorded) != 1:
                    raise RegisterError(f"refused citation audit {other}: unique audit directory required")
                entry["audit_dir"] = next(iter(recorded))
                root = _directory(repo, Path(entry["audit_dir"]))
                inventory = list(cli._walk(root, set(), frozenset()))
            except (OSError, ValueError, RuntimeError) as exc:
                reasons.append(f"methodology-bearing source unavailable {other}: {exc}")
                continue
            sources = {root / "lifecycle/digest.json", root / "lifecycle/DIGEST.md"}
            rounds = set(found["rounds"] if found else [])
            for path, names, files in inventory:
                if path.name.startswith("round-") or "round-scope.json" in files:
                    rounds.add(path)
                for name in files:
                    member = path / name
                    parts = member.relative_to(root).parts
                    if (name in ("pack.xml", "resolutions.json")
                            or name.endswith(".json") and any(part in ("findings", "codex", "opencode") for part in parts)):
                        sources.add(member)
                for name in files:
                    member = path / name
                    if member.is_symlink():
                        reasons.append(f"methodology-bearing source unavailable {other}: unscanned symlink {member}")
            if not rounds:
                reasons.append(f"methodology-bearing source incomplete {other}: round inventory unavailable")
            for path in rounds:
                sources.update((path / "pack.xml", path / "claude-synth/resolutions.json"))
                scope_raw = read_source(path / "round-scope.json")
                if scope_raw is not None:
                    entry["files"].append(report["source_versions"][-1])
                    try:
                        scope = json.loads(scope_raw)
                        personas = scope.get("personas") if isinstance(scope, dict) else None
                        if (not isinstance(personas, list) or not personas
                                or any(not isinstance(persona, str) or not persona or "/" in persona
                                       or persona in (".", "..") for persona in personas)):
                            raise ValueError("declared personas unavailable")
                        sources.update(path / folder / (persona + ".json")
                                       for folder in ("claude-self/findings", "codex", "opencode") for persona in personas)
                    except ValueError as exc:
                        reasons.append(f"methodology-bearing source incomplete {other}: {path / 'round-scope.json'}: {exc}")
            for path in sorted(sources):
                before = len(report["source_versions"])
                search(path)
                if len(report["source_versions"]) > before:
                    entry["files"].append(report["source_versions"][-1])
        report["census_scope"].append("TITAN")
        if titan_reader is None:
            reasons.append("titan source unavailable (no sanctioned read)")
        else:
            try:
                entries = titan_reader(audit_id)
                if not isinstance(entries, list) or any(not isinstance(entry, dict)
                        or not isinstance(entry.get("category"), str) for entry in entries):
                    raise ValueError("entries require a category string")
                raw = json.dumps(entries, sort_keys=True).encode()
                report["source_versions"].append({"path": "TITAN", "sha256": hashlib.sha256(raw).hexdigest()})
                dependencies.extend({"path": "TITAN", "entry": json.dumps(entry, sort_keys=True)}
                                    for entry in entries if entry["category"] in ("Methodology", "Theoretical Framework"))
            except Exception as exc:
                reasons.append(f"titan source unavailable: {exc}")
        if dependencies:
            reasons.append("reverse-citation dependency found; promotion required")
        report["census_complete"] = not any("source unavailable" in reason or "source incomplete" in reason
            or "discovery unavailable" in reason or reason.startswith("titan source") for reason in reasons)
        packed = next((row for row in reversed(rows) if row["event"] == "pack"), None)
        manifest, resident = [], []
        pack = None
        if seal is not None and packed is not None:
            try:
                manifest = sealed_rows(repo, directory, seal)
                root = Path(packed["copy_location"])
                volume_window(root, window_confirmed)
                pack = bounded_path(root, root / "packs" / f"{audit_id}.tar.zst")
                packs.checked_file(pack, packed["pack_sha256"], pack.stat().st_size)
                resident = copies.read_cas_refs(pack, b"", packed)
                if len(copies.counting_locations(audit_id, register)) != 1 or not any(
                        location.startswith("A:") for location in copies.counting_locations(audit_id, register)):
                    reasons.append("single complete copy-A required for expiry")
                from lifecycle_offload import verified_material
                with verified_material(repo, audit_id, packed, resident, copy="A", scratch_parent=root,
                                       copy_a_root=root, window_confirmed=window_confirmed):
                    pass
                report["deletion_list"] = [str(pack)]
            except (OSError, ValueError, RuntimeError) as exc:
                reasons.append(f"expiry pack source unavailable: {exc}")
        else:
            reasons.append("expiry pack source unavailable: pack and seal required")
        report["eligible"] = not reasons
        reasons[:] = sorted(set(reasons))
        report["census_scope"] = sorted(set(report["census_scope"]))
        report["source_versions"].sort(key=lambda source: source["path"])
        dependencies.sort(key=lambda source: (source["path"], source["entry"]))
        # An ineligible census is not an expire event and never claims a pack was deleted.
        lock.check()
        census_note = "expire census: " + json.dumps(report, sort_keys=True)
        _event(repo, audit_id, report["audit_dir"], actor, "census", rows[-1]["state_after"], lock=lock,
               **{"class": effective_class(repo, audit_id), "note": census_note})
        if report["eligible"] and execute:
            from lifecycle_offload import _write_record
            cas_paths = {row["path"] for row in packs.cas_rows(manifest, packed["class"])}
            cas_shas = {row["sha256"] for row in resident}
            lost = [row for row in manifest if row["git_tracked"] == "0" and row["path"] not in cas_paths
                    and row["sha256"] not in cas_shas]
            lost_bytes = ("\t".join(packs.RAW_MANIFEST_HEADER) + "\n" + "".join(
                "\t".join(row[key] for key in packs.RAW_MANIFEST_HEADER) + "\n" for row in lost)).encode()
            report["retained_cas_refs"] = len(resident)
            report["lost_members_sha256"] = hashlib.sha256(lost_bytes).hexdigest()
            target = bounded_path(repo, directory / "lifecycle/EXPIRED.tsv")
            _write_record(target, lost_bytes)
            latest_discovered = cli.discover_rounds(repo, {})
            fresh = {key: {"audit_dir": value["audit_dir"].relative_to(repo).as_posix(),
                "rounds": [path.relative_to(repo).as_posix() for path in value["rounds"]]}
                for key, value in sorted(latest_discovered.items())}
            if fresh != report["discovered_audits"]:
                raise RegisterError(f"refused expire {audit_id}: discovery scope changed during census")
            # The fresh source hashes must still match immediately before the unlink.
            for source in report["source_versions"]:
                if source["path"] in ("TITAN", str(repo / REGISTER_PATH)) or ":audits.work_log_entry_path" in source["path"]:
                    continue
                if hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() != source["sha256"]:
                    raise RegisterError(f"refused expire changed census source {source['path']}")
            with _append_lock(repo):
                current = read_register(repo)
                if (len(current.rows) != len(register.rows) + 1 or current.rows[:-1] != register.rows
                        or current.rows[-1]["audit_id"] != audit_id or current.rows[-1]["event"] != "census"
                        # census rows carry an empty lock_id by the register's rule (W8); identify our own row exactly
                        # by its note and actor instead (host fix: the lock_id comparison could never match).
                        or current.rows[-1]["note"] != census_note or current.rows[-1]["actor"] != actor):
                    raise RegisterError(f"refused expire {audit_id}: register scope changed during census")
                packs.checked_file(pack, packed["pack_sha256"], pack.stat().st_size)
                lock.check()
                pack.unlink()
            note = (RETENTION + f"; retained_cas_refs:{len(resident)}; lost_members_sha256:{report['lost_members_sha256']}"
                    + "; census_scope:" + json.dumps(report, sort_keys=True))
            _event(repo, audit_id, report["audit_dir"], actor, "expire", "expired", lock=lock,
                   **{"class": "code-only", "citation_resolvability": "pass", "manifest_sha256": seal["manifest_sha256"],
                      "pack_sha256": packed["pack_sha256"], "copy_location": "A:" + str(pack.parent.parent), "note": note})
    if dependencies:
        try:
            cli.promote(repo, audit_id=audit_id, reason="reverse-citation dependency found", by=actor, actor=actor,
                        window_confirmed=window_confirmed)
        except (OSError, ValueError, RuntimeError) as exc:
            report["reasons"].append(f"dependency promotion incomplete: {exc}")
    return report
