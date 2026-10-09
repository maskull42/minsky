"""Restore, promote and offload verified evidence; refuse namespace drift and uncommitted removal intents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import io
from contextlib import redirect_stderr
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

import zstandard

import lifecycle_copies as copies
import lifecycle_pack as packs
import store
from byte_sources import iter_pack_members
from free_space import check_free_space
from lifecycle_citations import effective_class, round_inputs
from lifecycle_register import AuditLock, REGISTER_PATH, RegisterError, _append_lock, git_read, read_register, require_gate
from lifecycle_seal import (FREEZES, _class, _directory, _event, _field, _inventory, _layouts, utc_now)
from round_verify import verify_round

RAW_CLASSES = frozenset(("raw-stream", "runtime-blob", "native-state", "dependency-cache"))


def _record_restore_check(packed: dict, result: dict) -> None:
    """Bind helper checks to their locked operation; refuse a missing audit directory or check collector."""
    directory = packed.get("_free_space_audit_dir")
    shas = packed.get("_free_space_checks")
    if not isinstance(directory, Path) or not isinstance(shas, list):
        raise RegisterError(f"refused free-space record for {packed['audit_id']}: locked operation context missing")
    shas.append(packs.record_free_space(directory, result))


def _copy_identity(repo: Path, audit_id: str, packed: dict, copy: str,
                   copy_a_root: Path | None, restic_repo: str | None) -> tuple[str, str | None]:
    if copy == "A":
        root = Path(packed["copy_location"])
        if copy_a_root is not None and copy_a_root.resolve() != root.resolve():
            raise RegisterError(f"refused copy-A root {copy_a_root}: differs from pack location {root}")
        location = copies.normalised_location("A:" + str(root))
    elif copy == "B":
        if restic_repo is None:
            raise RegisterError("refused restore copy B: --restic-repo required")
        location = copies.normalised_location("B:" + restic_repo)
    else:
        raise RegisterError(f"refused restore copy {copy!r}: A or B required")
    register = read_register(repo)
    if location not in copies.counting_locations(audit_id, register):
        raise RegisterError(f"refused restore copy {location}: three current copy verifications required")
    rows = [row for row in register.rows if row["audit_id"] == audit_id and row["event"] == "copy"
            and row["pack_sha256"] == packed["pack_sha256"]
            and copies.normalised_location(row["copy_location"]) == location]
    # Do not use the snapshot of a later, incomplete copy attempt.
    groups: dict[str | None, list[str]] = {}
    complete = []
    _, freeze = copies.freeze_rows(repo, audit_id)
    refs_sha = re.search(r"cas_refs_sha256:([0-9a-f]{64})", packed["note"])
    if refs_sha is None:
        raise RegisterError(f"refused restore copy {location}: pack CAS_REFS.tsv identity missing")
    for row in rows:
        parsed = copies.parse_copy_note(row)
        sequence = groups.setdefault(parsed["snapshot"], [])
        if parsed["kind"] == "pack":
            sequence[:] = ["pack"] if parsed["pack"] == packed["pack_sha256"] else []
        elif (parsed["kind"] == "cas-replica" and sequence == ["pack"]
              and parsed["present"] == parsed["total"] and parsed["total"] > 0
              and parsed["refs"] == refs_sha[1]):
            sequence.append("cas-replica")
        elif (parsed["kind"] == "freeze" and sequence == ["pack", "cas-replica"]
              and parsed["freeze"] == freeze["sha256"]):
            complete.append(parsed["snapshot"])
            sequence.clear()
        else:
            sequence.clear()
    if not complete:
        raise RegisterError(f"refused restore copy {location}: complete copy identity missing")
    return location, complete[-1]


@contextmanager
def verified_material(repo: Path, audit_id: str, packed: dict, resident: list[dict], *,
                      copy: str, scratch_parent: Path, copy_a_root: Path | None = None,
                      restic_repo: str | None = None, restic_password_file: Path | None = None,
                      forget_policy: str | None = None,
                      window_confirmed: str | None = None) -> Iterator[tuple[Path, Path, str]]:
    """Re-hash pack, replica and freeze without live CAS; refuse incomplete copy evidence or unsafe scratch."""
    packs.volume_window(Path(packed["copy_location"]), window_confirmed)
    location, snapshot = _copy_identity(repo, audit_id, packed, copy, copy_a_root, restic_repo)
    _, freeze = copies.freeze_rows(repo, audit_id)
    expected_refs = b""
    if copy == "A":
        root = Path(location[2:])
        packs.volume_window(root, window_confirmed)
        pack = packs.bounded_path(root, root / "packs" / f"{audit_id}.tar.zst")
        packs.checked_file(pack, packed["pack_sha256"], pack.stat().st_size)
        refs = copies.read_cas_refs(pack, expected_refs, packed)
        replica = packs.bounded_path(root, root / "cas")
        for row in refs:
            blob = packs.bounded_path(replica, store.cas_path(replica, row["sha256"]))
            packs.checked_file(blob, row["sha256"], int(row["bytes"]))
        frozen = packs.bounded_path(root / "freezes", Path(freeze["path"]))
        packs.volume_window(frozen, window_confirmed)
        packs.checked_file(frozen, freeze["sha256"], int(freeze["bytes"]))
        yield pack, replica, location
        return
    if restic_password_file is None or forget_policy is None:
        raise RegisterError("refused restore copy B: --restic-password-file and --forget-policy required")
    local = copies.local_repository(restic_repo)
    if local is not None:
        packs.volume_window(local, window_confirmed)
    packs.volume_window(restic_password_file, window_confirmed)
    base = copies._restic_base(restic_repo, restic_password_file)
    snapshots = copies._retention(base, copies._policy(forget_policy))
    matches = [row for row in snapshots if row["id"] == snapshot]
    if (len(matches) != 1 or "minsky-lifecycle" not in matches[0].get("tags", [])
            or "audit:" + audit_id not in matches[0].get("tags", [])):
        raise RegisterError(f"refused restic restore snapshot {snapshot}: lifecycle and audit tags required")
    paths = matches[0].get("paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) and PurePosixPath(path).is_absolute()
                                          and ".." not in PurePosixPath(path).parts for path in paths):
        raise RegisterError(f"refused restic restore snapshot {snapshot}: absolute object paths required")

    def object_path(suffix: str) -> str:
        found = [path for path in paths if path.endswith("/" + suffix)]
        if len(found) != 1:
            raise RegisterError(f"refused restic restore snapshot {snapshot} object {suffix}: unique path required")
        return found[0]

    pack_path = object_path(f"packs/{audit_id}.tar.zst")
    listing = copies._restic(base, "ls", "--json", snapshot, pack_path)
    size = json.loads(listing.splitlines()[-1]).get("size")
    if type(size) is not int or size < 0:
        raise RegisterError(f"refused restic pack size {pack_path}: invalid listing")
    copies._dump_checked(base, snapshot, pack_path, packed["pack_sha256"], size)
    boot, _ = copies.freeze_rows(repo, audit_id)
    freeze_path = object_path(Path(boot["path"]).name)
    copies._dump_checked(base, snapshot, freeze_path, freeze["sha256"], int(freeze["bytes"]))
    listing = copies._restic(base, "ls", "--json", snapshot)
    object_bytes = 0
    for line in listing.splitlines():
        node = json.loads(line)
        if node.get("type") == "file":
            count = node.get("size")
            if type(count) is not int or count < 0:
                raise RegisterError(f"refused restic restore snapshot {snapshot}: invalid object size")
            object_bytes += count
    packs.volume_window(scratch_parent, window_confirmed)
    scratch_parent.mkdir(parents=True, exist_ok=True)
    space = check_free_space(scratch_parent, object_bytes)
    _record_restore_check(packed, space)
    scratch = scratch_parent / f".{audit_id}.copy-B-restore"
    scratch.mkdir()  # Create-only: a prior or foreign tree is never cleaned up.
    try:
        copies._restic(base, "restore", snapshot, "--target", str(scratch))

        def restored(path: str) -> Path:
            return packs.bounded_path(scratch, scratch.joinpath(*PurePosixPath(path).parts[1:]))

        pack = restored(pack_path)
        packs.checked_file(pack, packed["pack_sha256"], size)
        refs = copies.read_cas_refs(pack, expected_refs, packed)
        roots, blob_paths = set(), []
        for row in refs:
            suffix = f"sha256/{row['sha256'][:2]}/{row['sha256'][2:4]}/{row['sha256']}"
            path = object_path(suffix)
            roots.add(path[:-len(suffix)].rstrip("/"))
            blob_paths.append((path, row))
        if len(roots) != 1:
            raise RegisterError(f"refused restic CAS replica snapshot {snapshot}: one nonempty CAS root required")
        if set(paths) != {pack_path, freeze_path, *(path for path, _ in blob_paths)}:
            raise RegisterError(f"refused restic restore snapshot {snapshot}: object set differs from audit dependencies")
        replica = restored(next(iter(roots)))
        for path, row in blob_paths:
            packs.checked_file(restored(path), row["sha256"], int(row["bytes"]))
        packs.checked_file(restored(freeze_path), freeze["sha256"], int(freeze["bytes"]))
        yield pack, replica, location
    finally:
        shutil.rmtree(scratch)


def restore_plan(pack: Path, manifest: list[dict], audit_class: str,
                 resident: list[dict], packed: dict) -> tuple[list[packs.ExpectedMember], list[dict]]:
    """Require exact manifest coverage; refuse traversal, extra members, conflicting metadata and unsafe links."""
    omitted = packs.cas_rows(manifest, audit_class)
    by_path = {row["path"]: row for row in manifest}
    omitted_paths = {row["path"] for row in omitted}
    refs = packs.cas_refs_bytes(copies.read_cas_refs(pack, b"", packed))
    expected = []
    seen = set()
    for scanned in iter_pack_members(pack):
        info = scanned.info
        if info.name in seen:
            raise RegisterError(f"refused restore pack member {info.name}: duplicate name")
        seen.add(info.name)
        if info.name == "CAS_REFS.tsv":
            expected.append(packs.ExpectedMember(info.name, "file", hashlib.sha256(refs).hexdigest(), len(refs), 0o644, 0))
            continue
        row = by_path.get(info.name)
        if row is None or info.name in omitted_paths:
            raise RegisterError(f"refused restore pack member {info.name}: not in expected packed namespace")
        ns = info.pax_headers.get("MINSKY.mtime_ns", "")
        if not re.fullmatch(r"-?\d+", ns):
            raise RegisterError(f"refused restore pack member {info.name}: MINSKY.mtime_ns missing or invalid")
        if row["sha256"]:
            item = packs.ExpectedMember(info.name, "file", row["sha256"], int(row["bytes"]),
                                        int(row["mode"], 8), int(row["mtime_ns"]))
        else:
            item = packs.ExpectedMember(info.name, row["type"], None, 0, info.mode, int(ns), row["symlink_target"])
            if item.type == "symlink":
                target = PurePosixPath(item.symlink_target)
                # Resolve relative components lexically, never at a recorded live path.
                parts = list(PurePosixPath(item.path).parent.parts)
                for part in target.parts:
                    if part == "..":
                        if not parts:
                            raise RegisterError(f"refused restore symlink {item.path}: target escapes destination")
                        parts.pop()
                    elif part != ".":
                        parts.append(part)
                if target.is_absolute() or (parts and PurePosixPath(*parts).as_posix() not in by_path):
                    raise RegisterError(f"refused restore symlink {item.path}: target outside manifest namespace")
        expected.append(item)
    wanted = set(by_path) - omitted_paths | {"CAS_REFS.tsv"}
    if seen != wanted:
        raise RegisterError(f"refused restore pack {pack}: missing members {sorted(wanted - seen)}")
    packs.round_trip(pack, expected)
    return sorted(expected, key=lambda item: item.path), omitted


def _target(root: Path, name: str) -> Path:
    path = root / name
    parent = path.parent
    while parent != root:
        if parent.is_symlink():
            raise RegisterError(f"refused restore member {name}: symlink parent {parent}")
        if parent == parent.parent:
            raise RegisterError(f"refused restore member {name}: parent escapes destination {root}")
        parent = parent.parent
    if root.is_symlink():
        raise RegisterError(f"refused restore destination {root}: symlink root")
    return path


def _present(path: Path, item: packs.ExpectedMember, window_confirmed: str | None = None) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if item.type == "file" and stat.S_ISLNK(info.st_mode):
        # Host fix: an external symlink of the sealed tree is restored as its dereferenced content (H4), so in place a
        # present link whose TARGET matches the manifest identity is present, not "differing".
        target = path.resolve()
        packs.volume_window(target, window_confirmed)
        packs.checked_file(target, item.sha256, item.bytes)
        info = target.stat()
        if stat.S_IMODE(info.st_mode) != item.mode or info.st_mtime_ns != item.mtime_ns:
            raise RegisterError(f"refused present restore link target {target}: mode or mtime_ns differs")
        return True
    if item.type == "file":
        packs.checked_file(path, item.sha256, item.bytes)
        if stat.S_IMODE(info.st_mode) != item.mode or info.st_mtime_ns != item.mtime_ns:
            raise RegisterError(f"refused present restore file {path}: mode or mtime_ns differs")
    elif item.type == "dir":
        if not stat.S_ISDIR(info.st_mode):
            raise RegisterError(f"refused present restore directory {path}: type differs")
    elif not stat.S_ISLNK(info.st_mode) or os.readlink(path) != item.symlink_target:
        raise RegisterError(f"refused present restore symlink {path}: target or type differs")
    return True


def restore_namespace(pack: Path, replica: Path, manifest: list[dict], audit_class: str,
                      resident: list[dict], packed: dict, into: Path, *, in_place: bool,
                      window_confirmed: str | None = None) -> None:
    """Restore exact members except multi-version CAS paths; refuse differing present paths before writes."""
    expected, omitted = restore_plan(pack, manifest, audit_class, resident, packed)
    multi_version = _multi_version_paths(copies.read_cas_refs(pack, b"", packed))
    payloads = [item for item in expected if item.path != "CAS_REFS.tsv" and item.path not in multi_version]
    payloads.extend(packs.ExpectedMember(row["path"], "file", row["sha256"], int(row["bytes"]),
                                        int(row["mode"], 8), int(row["mtime_ns"]))
                    for row in omitted if row["path"] not in multi_version)
    payloads.sort(key=lambda item: item.path)
    packs._expected_members(payloads)
    if into.is_symlink() or not into.parent.resolve().is_dir():
        raise RegisterError(f"refused restore destination {into}: real existing parent required")
    if not in_place and into.exists() and (not into.is_dir() or any(into.iterdir())):
        raise RegisterError(f"refused restore destination {into}: empty or absent directory required")
    present = {item.path for item in payloads if _present(_target(into, item.path), item, window_confirmed)}
    if present and not in_place:
        raise RegisterError(f"refused restore destination {into}: existing manifest paths")
    for item in payloads:
        for parent in PurePosixPath(item.path).parents:
            if parent.as_posix() != "." and not any(candidate.path == parent.as_posix()
                                                   and candidate.type == "dir" for candidate in payloads):
                raise RegisterError(f"refused restore member {item.path}: directory {parent} missing from manifest")
    space = check_free_space(into if into.exists() else into.parent,
                             sum(item.bytes for item in payloads if item.path not in present))
    _record_restore_check(packed, space)
    into.mkdir(exist_ok=True)
    for item in payloads:
        if item.type == "dir" and item.path not in present:
            _target(into, item.path).mkdir()

    def write(item: packs.ExpectedMember, source) -> None:
        target = _target(into, item.path)
        if item.path in present:
            _present(target, item, window_confirmed)
            return
        with target.open("xb") as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        packs.checked_file(target, item.sha256, item.bytes)
        target.chmod(item.mode)
        os.utime(target, ns=(item.mtime_ns, item.mtime_ns))
        _present(target, item)

    members = {item.path: item for item in expected}
    with pack.open("rb") as compressed, zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for info in archive:
                item = members[info.name]
                if item.path == "CAS_REFS.tsv" or item.type == "dir" or item.path in multi_version:
                    continue
                if item.type == "symlink":
                    if item.path not in present:
                        os.symlink(item.symlink_target, _target(into, item.path))
                    _present(_target(into, item.path), item)
                else:
                    with archive.extractfile(info) as source:
                        write(item, source)
    for row in omitted:
        if row["path"] in multi_version:
            continue
        source = packs.bounded_path(replica, store.cas_path(replica, row["sha256"]))
        packs.checked_file(source, row["sha256"], int(row["bytes"]))
        item = next(item for item in payloads if item.path == row["path"])
        with source.open("rb") as payload:
            write(item, payload)
    for item in sorted(payloads, key=lambda item: len(PurePosixPath(item.path).parts), reverse=True):
        target = _target(into, item.path)
        if item.type == "dir" and item.path not in present:
            target.chmod(item.mode)
            os.utime(target, ns=(item.mtime_ns, item.mtime_ns))
        _present(target, item)
    packs.checked_file(pack, packed["pack_sha256"], pack.stat().st_size)


def _multi_version_paths(resident: list[dict]) -> list[str]:
    versions: dict[str, set[str]] = {}
    for row in resident:
        versions.setdefault(row["path"], set()).add(row["sha256"])
    return sorted(path for path, shas in versions.items() if len(shas) > 1)


def rehydrate(repo: Path, *, audit_id: str, copy: str, actor: str, into: Path | None = None,
              in_place: bool = False, copy_a_root: Path | None = None,
              restic_repo: str | None = None, restic_password_file: Path | None = None,
              forget_policy: str | None = None, window_confirmed: str | None = None) -> None:
    """Restore from one fully verified copy under the audit lock; refuse conflicting targets or changed members."""
    repo = repo.resolve()
    _field(actor, "--actor", nonempty=True)
    if (into is None) != in_place:
        raise RegisterError("refused rehydrate target: exactly one of --into and --in-place required")
    packs.volume_window(repo, window_confirmed)
    with AuditLock.acquire(repo, audit_id, verb="rehydrate", session=actor) as lock:
        _, packed, directory, resident = copies._pack_context(repo, audit_id)
        packed = {**packed, "_free_space_audit_dir": directory, "_free_space_checks": []}
        target = directory if in_place else Path(os.path.abspath(into))
        packs.volume_window(target, window_confirmed)
        if not in_place and target.resolve().is_relative_to(directory.resolve()):
            raise RegisterError(f"refused --into {target}: use --in-place for audit directory {directory}")
        seal = next(row for row in reversed(read_register(repo).rows)
                    if row["audit_id"] == audit_id and row["event"] == "seal")
        manifest = packs.sealed_rows(repo, directory, seal)
        with verified_material(repo, audit_id, packed, resident, copy=copy, scratch_parent=target.parent,
                               copy_a_root=copy_a_root, restic_repo=restic_repo,
                               restic_password_file=restic_password_file, forget_policy=forget_policy,
                               window_confirmed=window_confirmed) as (pack, replica, location):
            if not in_place and (pack.resolve().is_relative_to(target.resolve())
                                 or replica.resolve().is_relative_to(target.resolve())):
                raise RegisterError(f"refused restore destination {target}: contains source pack or CAS replica")
            lock.check()
            restore_namespace(pack, replica, manifest, packed["class"], resident, packed, target, in_place=in_place,
                              window_confirmed=window_confirmed)
            multi_version = _multi_version_paths(copies.read_cas_refs(pack, b"", packed))
            lock.check()
            _event(repo, audit_id, packed["audit_dir"], actor, "rehydrate", "rehydrated", lock=lock,
                   **{"class": effective_class(repo, audit_id), "citation_resolvability": packed["citation_resolvability"],
                      "manifest_sha256": packed["manifest_sha256"], "pack_sha256": packed["pack_sha256"],
                      "copy_location": location, "note": packs.free_space_note(
                          f"into:{target}; verified_utc:{utc_now()}" +
                          "".join("; multi-version:" + path for path in multi_version), packed["_free_space_checks"])})


def ingest_promotion(repo: Path, audit_id: str, directory: Path, seal: dict, packed: dict | None,
                     lock: AuditLock, window_confirmed: str | None) -> int:
    """Ingest manifest-bound bytes from the sealed tree or immutable pack; refuse missing or changed members."""
    from lifecycle_seal import STORE_HEADER, STORE_INDEX, _append_table, _read_table

    manifest = packs.sealed_rows(repo, directory, seal)
    selected = {row["path"]: row for row in manifest if row["hash_bound"] == "1" and row["sha256"]}
    cfg = packs.load_cas()
    packs.volume_window(cfg.root, window_confirmed)
    indexed = _read_table(repo, STORE_INDEX, STORE_HEADER)
    records = []

    def ingest(row: dict, source: Path) -> None:
        lock.check()
        packs.checked_file(source, row["sha256"], int(row["bytes"]))
        store.ingest_file(cfg, source, row["sha256"], byte_count=int(row["bytes"]), audit_id=audit_id,
                          round_label=row["path"].split("/", 1)[0], source_path=row["path"])
        packs.checked_file(store.cas_path(cfg.root, row["sha256"]), row["sha256"], int(row["bytes"]))
        record = {"sha256": row["sha256"], "bytes": row["bytes"], "audit_id": audit_id,
                  "audit_dir": directory.relative_to(repo).as_posix(), "use_path": row["path"],
                  "class": row["class"], "sealed_utc": utc_now()}
        if not any(all(old[key] == record[key] for key in STORE_HEADER if key != "sealed_utc") for old in indexed):
            records.append(record)

    missing = {row["sha256"]: int(row["bytes"]) for row in selected.values()
               if not store.cas_path(cfg.root, row["sha256"]).exists()}
    space = check_free_space(cfg.root, sum(missing.values()))
    _record_restore_check(seal, space)
    if packed is None:
        for row in selected.values():
            source, _ = packs._source(directory, row, window_confirmed)
            ingest(row, source)
    else:
        root = Path(packed["copy_location"])
        packs.volume_window(root, window_confirmed)
        pack = packs.bounded_path(root, root / "packs" / f"{audit_id}.tar.zst")
        packs.checked_file(pack, packed["pack_sha256"], pack.stat().st_size)
        _, omitted = restore_plan(pack, manifest, packed["class"], [], packed)
        omitted_paths = {row["path"] for row in omitted}
        for name in sorted(omitted_paths & selected.keys()):
            row = selected[name]
            ingest(row, store.cas_path(cfg.root, row["sha256"]))
        scratch_parent = packs.bounded_path(root, root / "_promotion")
        scratch_parent.mkdir(exist_ok=True)
        space = check_free_space(scratch_parent, max((int(row["bytes"]) for row in selected.values()), default=0))
        _record_restore_check(seal, space)
        scratch = scratch_parent / audit_id
        scratch.mkdir()
        seen = set(omitted_paths & selected.keys())
        try:
            with pack.open("rb") as compressed, zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
                with tarfile.open(fileobj=stream, mode="r|") as archive:
                    for info in archive:
                        row = selected.get(info.name)
                        if row is None:
                            continue
                        if info.name in seen or not info.isfile():
                            raise RegisterError(f"refused promotion member {info.name}: duplicate or nonregular payload")
                        source = scratch / "member"
                        try:
                            with archive.extractfile(info) as payload, source.open("xb") as out:
                                shutil.copyfileobj(payload, out)
                                out.flush()
                                os.fsync(out.fileno())
                            ingest(row, source)
                        finally:
                            source.unlink(missing_ok=True)
                        seen.add(info.name)
            if seen != set(selected):
                raise RegisterError(f"refused promotion {audit_id}: missing hash-bound members {sorted(set(selected) - seen)}")
            packs.checked_file(pack, packed["pack_sha256"], pack.stat().st_size)
        finally:
            shutil.rmtree(scratch)
            if not any(scratch_parent.iterdir()):
                scratch_parent.rmdir()
    _append_table(repo, STORE_INDEX, STORE_HEADER, records, lock)
    return len({row["sha256"] for row in selected.values()})


def _protected(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return (parts[0] == "lifecycle" or "findings" in parts or "claude-synth" in parts
            or parts[-1] in ("pack.xml", "consensus.md", "resolutions.json", "decisions.md")
            or parts[-1].endswith(".json") and any(part in ("codex", "opencode") for part in parts))


def _tracked(repo: Path, directory: Path, name: str) -> bool:
    relative = (directory / name).relative_to(repo).as_posix()
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), "--literal-pathspecs",
                             "ls-files", "--error-unmatch", "--", relative], capture_output=True)
    if result.returncode not in (0, 1):
        raise RegisterError(f"refused tracked-status check {relative}: git exited {result.returncode}")
    return result.returncode == 0


def _committed(repo: Path, path: Path) -> bytes:
    relative = path.relative_to(repo).as_posix()
    packs.bounded_path(repo, path)
    if path.is_symlink() or not path.is_file():
        raise RegisterError(f"refused committed evidence {relative}: regular file required")
    raw = path.read_bytes()
    if (git_read(repo, "--literal-pathspecs", "status", "--porcelain", "--", relative)
            or git_read(repo, "cat-file", "-p", "HEAD:" + relative) != raw):
        raise RegisterError(f"refused committed evidence {relative}: HEAD bytes or status differ")
    return raw


def _removal_set(manifest: list[dict]) -> list[dict]:
    entries = []
    for row in manifest:
        if row["type"] == "dir" or row["git_tracked"] == "1" or row["class"] not in RAW_CLASSES:
            continue
        if _protected(row["path"]):
            raise RegisterError(f"refused offload protected member {row['path']}")
        entry = {"path": row["path"], "type": row["type"], "class": row["class"]}
        if row["type"] == "symlink":
            entry["symlink_target"] = row["symlink_target"]
        if row["sha256"]:
            entry.update(sha256=row["sha256"], bytes=int(row["bytes"]))
        entries.append(entry)
    return entries


def _check_removal(repo: Path, directory: Path, entry: dict, *, resume: bool,
                   window_confirmed: str | None) -> bool:
    name = entry["path"]
    if _protected(name) or _tracked(repo, directory, name):
        raise RegisterError(f"refused offload protected or tracked member {name}")
    path = _target(directory, name)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if resume and (entry.get("sha256") or entry.get("type") == "symlink" and entry.get("symlink_target")):
            return False
        raise RegisterError(f"refused offload missing intent member {name}")
    if entry["type"] == "symlink":
        if not stat.S_ISLNK(info.st_mode) or os.readlink(path) != entry["symlink_target"]:
            raise RegisterError(f"refused offload symlink {name}: target or type changed")
        if "sha256" in entry:
            packs.volume_window(path, window_confirmed)
            packs.checked_file(path.resolve(), entry["sha256"], entry["bytes"])
    elif not stat.S_ISREG(info.st_mode):
        raise RegisterError(f"refused offload member {name}: regular file required")
    else:
        packs.checked_file(path, entry["sha256"], entry["bytes"])
    return True


def _write_record(path: Path, raw: bytes) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise RegisterError(f"refused lifecycle record {path}: existing bytes differ")
        return
    with path.open("xb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    if path.read_bytes() != raw:
        raise RegisterError(f"refused lifecycle record {path}: written bytes differ")


def offload(repo: Path, *, audit_id: str, actor: str, phase: str, audit_dir: Path | None = None,
            copy_a_root: Path | None = None, restic_repo: str | None = None,
            restic_password_file: Path | None = None, forget_policy: str | None = None,
            window_confirmed: str | None = None) -> None:
    """Require committed intent and copy-only rehearsals; refuse unlisted, changed, tracked or protected raw members."""
    _field(actor, "--actor", nonempty=True)
    if phase not in ("prepare", "execute", "resume"):
        raise RegisterError(f"refused offload phase {phase!r}")
    repo = repo.resolve()
    packs.volume_window(repo, window_confirmed)
    with AuditLock.acquire(repo, audit_id, verb="offload", session=actor) as lock:
        register = read_register(repo)
        rows = [row for row in register.rows if row["audit_id"] == audit_id]
        _, packed, directory, resident = copies._pack_context(repo, audit_id)
        packed = {**packed, "_free_space_audit_dir": directory, "_free_space_checks": []}
        if audit_dir is not None and _directory(repo, audit_dir) != directory:
            raise RegisterError(f"refused offload audit directory {audit_dir}: differs from pack")
        if any(row["event"] == "offload" and row["pack_sha256"] == packed["pack_sha256"] for row in rows):
            raise RegisterError(f"refused offload {audit_id}: already completed")
        seal = next(row for row in reversed(rows) if row["event"] == "seal")
        manifest = packs.sealed_rows(repo, directory, seal)
        if packed["citation_resolvability"] != "pass" and not any(
                row["event"] == "ruling" and row["note"].startswith("citation-waiver:")
                for row in rows[rows.index(seal) + 1:]):
            raise RegisterError(f"refused offload {audit_id}: citation_resolvability pass or later citation-waiver required")
        boot, _ = copies.freeze_rows(repo, audit_id)
        require_gate("G2", audit_dir=directory, db_path=Path(boot["path"]))
        root = Path(packed["copy_location"])
        if copy_a_root is not None and root.resolve() != copy_a_root.resolve():
            raise RegisterError(f"refused offload copy-A root {copy_a_root}: differs from pack")
        packs.volume_window(root, window_confirmed)
        locations = sorted(copies.counting_locations(audit_id, register))
        required = 2 if effective_class(repo, audit_id) == "methodology-bearing" else 1
        if len(locations) < required:
            raise RegisterError(f"refused offload {audit_id}: {required} complete copies required")
        selected = locations[:required]
        intent_path = _target(directory, "lifecycle/offload_intent.json")
        removal = _removal_set(manifest)
        orphan_raw = None
        if phase == "prepare":
            if intent_path.exists() or intent_path.is_symlink():
                if intent_path.is_symlink() or not intent_path.is_file():
                    raise RegisterError(f"refused offload intent {intent_path}: regular file required")
                orphan_raw = intent_path.read_bytes()
                sha = hashlib.sha256(orphan_raw).hexdigest()
                if any(row["event"] == "offload-intent" and re.fullmatch(
                        "intent_sha256:" + sha + packs.FREE_SPACE_SUFFIX, row["note"]) for row in rows):
                    raise RegisterError(f"refused offload intent {intent_path}: already exists")
                try:
                    orphan = json.loads(orphan_raw)
                except (ValueError, UnicodeError) as exc:
                    raise RegisterError(f"refused offload intent {intent_path}: malformed JSON") from exc
                if not isinstance(orphan, dict):
                    raise RegisterError(f"refused offload intent {intent_path}: JSON object required")
            classification = next((row for row in reversed(rows) if row["event"] == "classify"), None)
            if classification is None or not classification["note"]:
                raise RegisterError(f"refused offload {audit_id}: classification record path missing")
            for path in (directory / "lifecycle/raw_manifest.tsv", directory / classification["note"],
                         repo / REGISTER_PATH, repo / FREEZES):
                _committed(repo, path)
        else:
            raw_intent = _committed(repo, intent_path)
            intent = json.loads(raw_intent)
            if (not isinstance(intent, dict) or not isinstance(intent.get("copies"), list)
                    or any(not isinstance(item, dict) or not isinstance(item.get("location"), str)
                           for item in intent["copies"])
                    or not isinstance(intent.get("rehearsals"), list)
                    or any(not isinstance(item, dict) or not isinstance(item.get("verdicts"), list)
                           or any(not isinstance(verdict, dict) or not isinstance(verdict.get("round"), str)
                                  or not isinstance(verdict.get("sha256"), str) for verdict in item["verdicts"])
                           for item in intent["rehearsals"])):
                raise RegisterError(f"refused offload intent {intent_path}: malformed copy or rehearsal identities")
            if (intent.get("audit_id") != audit_id or intent.get("audit_dir") != packed["audit_dir"]
                    or intent.get("manifest_sha256") != packed["manifest_sha256"]
                    or intent.get("pack_sha256") != packed["pack_sha256"] or intent.get("removal_set") != removal
                    or [item["location"] for item in intent.get("copies", [])] != selected
                    or len(intent.get("rehearsals", [])) != required):
                raise RegisterError(f"refused offload intent {intent_path}: manifest, pack, removal set or copy identities differ")
            round_names = {info["root"].relative_to(directory).as_posix() for info in round_inputs(directory, audit_id)}
            for rehearsal, location in zip(intent["rehearsals"], selected):
                verdicts = rehearsal.get("verdicts", [])
                if (rehearsal.get("location") != location or not round_names
                        or {item.get("round") for item in verdicts} != round_names
                        or len(verdicts) != len(round_names)
                        or any(not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")) for item in verdicts)):
                    raise RegisterError(f"refused offload intent {intent_path}: incomplete rehearsal for {location}")
            sha = hashlib.sha256(raw_intent).hexdigest()
            if not any(row["event"] == "offload-intent" and re.fullmatch(
                    "intent_sha256:" + sha + packs.FREE_SPACE_SUFFIX, row["note"]) for row in rows):
                raise RegisterError(f"refused offload intent {intent_path}: recorded intent hash missing")
        identities, rehearsals = [], []
        for location in selected:
            copy = location[0]
            with verified_material(repo, audit_id, packed, resident, copy=copy, scratch_parent=root / "_rehearsal",
                                   copy_a_root=root, restic_repo=restic_repo,
                                   restic_password_file=restic_password_file, forget_policy=forget_policy,
                                   window_confirmed=window_confirmed) as (pack, replica, checked_location):
                _, snapshot = _copy_identity(repo, audit_id, packed, copy, root, restic_repo)
                identity = {"location": checked_location, "snapshot": snapshot}
                if phase != "prepare" and identity != intent["copies"][len(identities)]:
                    raise RegisterError(f"refused offload copy {location}: snapshot identity changed")
                identities.append(identity)
                if phase != "prepare":
                    continue
                parent = packs.bounded_path(root, root / "_rehearsal")
                parent.mkdir(exist_ok=True)
                scratch = parent / f"{audit_id}.{copy}.{utc_now().replace('-', '').replace(':', '')}"
                scratch.mkdir()
                try:
                    restored = scratch / "audit"
                    restore_namespace(pack, replica, manifest, packed["class"], resident, packed, restored, in_place=False)
                    actual = {path.relative_to(restored).as_posix() for path in _inventory(restored)}
                    multi_version = _multi_version_paths(copies.read_cas_refs(pack, b"", packed))
                    wanted = {row["path"] for row in manifest} - set(multi_version)
                    if actual != wanted:
                        raise RegisterError(f"refused rehearsal {location}: namespace differs from manifest")
                    _write_record(restored / "lifecycle/raw_manifest.tsv",
                                  _committed(repo, directory / "lifecycle/raw_manifest.tsv"))
                    verdicts = []
                    rounds = round_inputs(restored, audit_id)
                    if not rounds:
                        raise RegisterError(f"refused rehearsal {location}: no verifiable rounds")
                    for info in rounds:
                        relative = info["root"].relative_to(restored).as_posix()
                        out = scratch / f"round-{info['number']}.json"
                        errors = io.StringIO()
                        try:
                            with redirect_stderr(errors):
                                verify_round(info["root"], mode="archive", no_live=True, store=replica, pack=pack,
                                             json_out=out, anchor_repo=repo,
                                             anchor_prefix=packed["audit_dir"] + "/" + relative)
                        except SystemExit as exc:
                            raise RegisterError(f"refused rehearsal {location} round {relative}: {errors.getvalue().strip()}") from exc
                        verdict = json.loads(out.read_bytes())
                        anchors = verdict.get("manifest_anchors", [])
                        if verdict["verdict"] != "ok-archive" or not anchors or any(
                                entry.get("anchor") not in ("git", "register") for entry in anchors):
                            raise RegisterError(f"refused rehearsal {location} round {relative}: verdict or anchors failed")
                        verdicts.append({"round": relative, "sha256": packs._hash_file(out)})
                    rehearsals.append({"location": location, "verdicts": verdicts})
                finally:
                    shutil.rmtree(scratch)
        if phase == "prepare":
            for entry in removal:
                _check_removal(repo, directory, entry, resume=False, window_confirmed=window_confirmed)
            intent = {"audit_id": audit_id, "audit_dir": packed["audit_dir"],
                      "manifest_sha256": packed["manifest_sha256"], "pack_sha256": packed["pack_sha256"],
                      "copies": identities, "rehearsals": rehearsals, "removal_set": removal}
            raw = (json.dumps(intent, indent=2, sort_keys=True) + "\n").encode("utf-8")
            lock.check()
            if orphan_raw is None:
                _write_record(intent_path, raw)
            else:
                for key in sorted(orphan.keys() | intent.keys()):
                    if key != "rehearsals" and (key not in orphan or key not in intent
                            or json.dumps(orphan[key], sort_keys=True) != json.dumps(intent[key], sort_keys=True)):
                        raise RegisterError(f"refused offload intent {intent_path}: {key} differs")
                if intent_path.is_symlink() or not intent_path.is_file() or intent_path.read_bytes() != orphan_raw:
                    raise RegisterError(f"refused offload intent {intent_path}: bytes changed during recovery")
                raw = orphan_raw
            _event(repo, audit_id, packed["audit_dir"], actor, "offload-intent", rows[-1]["state_after"], lock=lock,
                   **{"class": effective_class(repo, audit_id), "manifest_sha256": packed["manifest_sha256"],
                      "pack_sha256": packed["pack_sha256"], "note": packs.free_space_note(
                          "intent_sha256:" + hashlib.sha256(raw).hexdigest(), packed["_free_space_checks"])})
            return
        declarations, _ = _layouts(round_inputs(directory, audit_id), directory)
        by_path = {row["path"]: row for row in manifest}
        allowed = {entry["path"] for entry in removal}
        for path in _inventory(directory):
            name = path.relative_to(directory).as_posix()
            tracked = _tracked(repo, directory, name)
            kind = by_path[name]["class"] if name in by_path else _class(name, tracked, declarations)
            if not stat.S_ISDIR(path.lstat().st_mode) and not tracked and kind in RAW_CLASSES and name not in allowed:
                raise RegisterError(f"refused offload new raw member {name}: absent from committed intent")
        for entry in removal:
            _check_removal(repo, directory, entry, resume=phase == "resume", window_confirmed=window_confirmed)
        for entry in removal:
            lock.check()
            if _check_removal(repo, directory, entry, resume=phase == "resume", window_confirmed=window_confirmed):
                os.unlink(_target(directory, entry["path"]))
        for row in sorted(manifest, key=lambda row: (-len(PurePosixPath(row["path"]).parts), row["path"])):
            if row["type"] != "dir" or row["class"] not in RAW_CLASSES or _protected(row["path"]):
                continue
            path = _target(directory, row["path"])
            if path.exists() and not _tracked(repo, directory, row["path"]):
                if path.is_symlink() or not path.is_dir():
                    raise RegisterError(f"refused offload directory {row['path']}: type changed")
                if not any(path.iterdir()):
                    path.rmdir()
        sha = hashlib.sha256(raw_intent).hexdigest()
        times = [{"location": location, "verified_utc": utc_now()} for location in selected]
        stub = {"pack_sha256": packed["pack_sha256"], "manifest_sha256": packed["manifest_sha256"],
                "copies": times, "rehydrate_command": shlex.join([
                    "minsky-lifecycle.py", "rehydrate", "--repo", str(repo), "--audit", audit_id,
                    "--from", "A", "--in-place", "--actor", "<actor>"])}
        # Preserve a previously written verification time during partial-completion recovery.
        existing_stub = directory / "lifecycle/STUB.json"
        if existing_stub.exists():
            old = json.loads(existing_stub.read_bytes())
            if (old.get("pack_sha256") != stub["pack_sha256"] or old.get("manifest_sha256") != stub["manifest_sha256"]
                    or [item["location"] for item in old.get("copies", [])] != selected
                    or old.get("rehydrate_command") != stub["rehydrate_command"]):
                raise RegisterError(f"refused offload stub {existing_stub}: identity differs")
            stub = old
        _write_record(_target(directory, "lifecycle/STUB.json"), (json.dumps(stub, indent=2, sort_keys=True) + "\n").encode())
        _write_record(_target(directory, "lifecycle/TOMBSTONE.md"),
                      f"# Offloaded audit {audit_id}\n\nIntent sha256: {sha}\nPack sha256: {packed['pack_sha256']}\n".encode())
        ledger = repo / "documentation/cold_storage_ledger.md"
        packs.bounded_path(repo, ledger)
        header = "| Date | Original path (repo-relative) | sha256 | Size | Destination | Reason | Citing manifests/logs |"
        lines = [str(number) for number, row in zip(register.line_numbers, register.rows)
                 if row["audit_id"] == audit_id and row["event"] in ("seal", "pack", "copy", "offload-intent")]
        cells = [stub["copies"][0]["verified_utc"][:10], f"{packed['audit_dir']} (raw classes; {len(removal)} files)",
                 sha, str(sum(entry.get("bytes", 0) for entry in removal)),
                 ", ".join(selected) + "; pack:" + packed["pack_sha256"],
                 "minsky lifecycle offload (plan v0.2 §6.6)", ", ".join(lines)]
        if any("|" in value or "\n" in value or "\r" in value for value in cells):
            raise RegisterError(f"refused cold storage ledger row for {audit_id}: cell delimiter")
        line = ("| " + " | ".join(cells) + " |\n").encode()
        with _append_lock(repo):
            raw_ledger = ledger.read_bytes() if ledger.exists() else b""
            if ledger.is_symlink() or (raw_ledger and (header.encode() not in raw_ledger or not raw_ledger.endswith(b"\n"))):
                raise RegisterError(f"refused cold storage ledger {ledger}: schema or LF mismatch")
            if line not in raw_ledger.splitlines(keepends=True):
                lock.check()
                payload = line if raw_ledger else (header + "\n| --- | --- | --- | --- | --- | --- | --- |\n").encode() + line
                with ledger.open("ab", buffering=0) as out:
                    if os.write(out.fileno(), payload) != len(payload):
                        raise RegisterError(f"refused cold storage ledger {ledger}: partial append")
                    os.fsync(out.fileno())
        _event(repo, audit_id, packed["audit_dir"], actor, "offload", "offloaded", lock=lock,
               **{"class": effective_class(repo, audit_id), "manifest_sha256": packed["manifest_sha256"],
                  "pack_sha256": packed["pack_sha256"], "citation_resolvability": packed["citation_resolvability"],
                  "note": packs.free_space_note("intent_sha256:" + sha, packed["_free_space_checks"])})
