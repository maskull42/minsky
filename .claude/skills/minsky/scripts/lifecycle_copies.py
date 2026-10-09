"""Replicate and verify lifecycle objects; refuse incomplete evidence and unsafe restic retention."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import threading
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import zstandard

import store
from free_space import check_free_space
from lifecycle_pack import (
    CAS_HEADER, FREE_SPACE_SUFFIX, bounded_path, cas_refs_bytes, checked_file, free_space_note,
    load_cas, record_free_space, sealed_rows, volume_window,
)
from lifecycle_register import AuditLock, COLUMNS, Register, RegisterError, append_event, read_register, require_gate
from lifecycle_seal import FREEZES, FREEZE_HEADER, _directory, _field, _read_table

SHA = r"[0-9a-f]{64}"
Z_TIME = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
COPY_NOTE = re.compile(
    rf"copy-row:(pack|cas-replica|freeze); (pack:({SHA})(?:; device:(\d+))?|cas_replica:(\d+)/(\d+); "
    rf"cas_refs_sha256:({SHA})|freeze:({SHA})); verified_utc:({Z_TIME})(?:; snapshot:({SHA}))?"
    rf"(?:; window-confirmed:[^\t\r\n;]+)?"
    + FREE_SPACE_SUFFIX
)


def _copy_window(path: Path, window_confirmed: str | None) -> None:
    volume_window(path, window_confirmed)


def local_repository(repository: str) -> Path | None:
    """Resolve local repository identities; refuse empty strings and TSV delimiters."""
    _field(repository, "restic repository identity", nonempty=True)
    # restic's `local:<path>` backend is a local filesystem repository (host fix, Astra W6c): normalise it like a bare path
    # so the same-device, window and alias rules apply.
    if repository.startswith("local:"):
        repository = repository[len("local:"):]
        if not repository:
            raise RegisterError("refused restic repository 'local:' without a path")
    return None if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", repository) else Path(repository).resolve()


def normalised_location(location: str) -> str:
    """Normalise copy locations; refuse an invalid label or a relative copy-A root."""
    if not location.startswith(("A:", "B:")) or not location[2:]:
        raise RegisterError(f"refused copy_location: {location!r}")
    root = location[2:]
    if location.startswith("A:"):
        if not Path(root).is_absolute():
            raise RegisterError(f"refused copy_location {location!r}: absolute copy-A root required")
        return "A:" + str(Path(root).resolve())
    local = local_repository(root)
    return "B:" + (str(local) if local is not None else root)


def parse_copy_note(row: dict) -> dict:
    """Refuse every malformed copy note, including invalid times and mismatched row labels."""
    match = COPY_NOTE.fullmatch(row["note"])
    if match is None:
        raise RegisterError(f"refused W6 copy note format for {row['audit_id']}: unresolved or malformed note {row['note']!r}")
    kind, value, pack_sha, device, present, total, refs_sha, freeze_sha, utc, snapshot = match.groups()
    if (kind == "pack" and pack_sha is None or kind == "cas-replica" and present is None
            or kind == "freeze" and freeze_sha is None):
        raise RegisterError(f"refused copy row for {row['audit_id']}: label differs from verification {value}")
    try:
        datetime.fromisoformat(utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegisterError(f"refused copy verified_utc for {row['audit_id']}: {utc}") from exc
    location = row["copy_location"]
    if not location.startswith(("A:", "B:")) or not location[2:] or (location.startswith("B:") != bool(snapshot)):
        raise RegisterError(f"refused copy_location for {row['audit_id']}: {location!r} or snapshot identity")
    return {"kind": kind, "pack": pack_sha, "present": int(present) if present is not None else None,
            "total": int(total) if total is not None else None, "refs": refs_sha, "freeze": freeze_sha,
            "snapshot": snapshot, "device": int(device) if device is not None else None}


def freeze_rows(repo: Path, audit_id: str) -> tuple[dict, dict]:
    """Refuse missing freeze coverage or inconsistent boot/copy identities in the authoritative TSV."""
    rows = [row for row in _read_table(repo, FREEZES, FREEZE_HEADER) if row["audit_id"] == audit_id]
    copy = next((row for row in reversed(rows) if row["event"] == "freeze-copy"), None)
    if copy is None:
        raise RegisterError(f"refused freeze-copy for {audit_id}: missing row in {repo / FREEZES}")
    boot = next((row for row in reversed(rows) if row["event"] == "freeze" and row["sha256"] == copy["sha256"]), None)
    for row in (copy, boot):
        if (row is None or not re.fullmatch(SHA, row["sha256"]) or not re.fullmatch(r"\d+", row["bytes"])
                or not Path(row["path"]).is_absolute() or row["integrity_check"] != "ok"):
            raise RegisterError(f"refused freeze identity for {audit_id}: missing or invalid freeze/freeze-copy row")
    if boot["bytes"] != copy["bytes"]:
        raise RegisterError(f"refused freeze bytes for {audit_id}: boot and freeze-copy differ")
    return boot, copy


def counting_locations(audit_id: str, register: Register, *, freeze_sha: str | None = None) -> set[str]:
    """Refuse malformed copy rows; count only ordered, complete copies of the current pack and freeze."""
    rows = [row for row in register.rows if row["audit_id"] == audit_id]
    copies = [(row, parse_copy_note(row)) for row in rows if row["event"] == "copy"]
    if not copies:
        return set()
    latest = next((row for row in reversed(rows) if row["event"] == "pack"), None)
    if latest is None or not re.fullmatch(SHA, latest["pack_sha256"]):
        raise RegisterError(f"refused copy accounting for {audit_id}: latest pack row missing or invalid")
    refs = re.search(rf"(?:^|; )cas_refs_sha256:({SHA})(?:;|$)", latest["note"])
    if refs is None:
        raise RegisterError(f"refused copy accounting for {audit_id}: pack CAS_REFS.tsv identity missing")
    if freeze_sha is None:
        if register.repo_root is None:
            raise RegisterError(f"refused copy accounting for {audit_id}: register repository is unavailable")
        _, freeze = freeze_rows(register.repo_root, audit_id)
        freeze_sha = freeze["sha256"]
    groups, devices, complete = {}, {}, {}
    for row, parsed in copies:
        if row["pack_sha256"] != latest["pack_sha256"]:
            continue
        location = normalised_location(row["copy_location"])
        key = (location, parsed["snapshot"])
        sequence = groups.setdefault(key, [])
        if parsed["kind"] == "pack":
            sequence.clear()
            devices[key] = parsed["device"]
            if parsed["pack"] == latest["pack_sha256"]:
                sequence.append("pack")
        elif parsed["kind"] == "cas-replica":
            if (sequence == ["pack"] and parsed["present"] == parsed["total"] and parsed["total"] > 0
                    and parsed["refs"] == refs[1]):
                sequence.append("cas-replica")
            else:
                sequence.clear()
        elif sequence == ["pack", "cas-replica"] and parsed["freeze"] == freeze_sha:
            complete[location] = devices[key]
            sequence.clear()
        else:
            sequence.clear()
    distinct, seen_devices = set(), set()
    for location, device in sorted(complete.items()):
        if device is not None and device in seen_devices:
            continue
        distinct.add(location)
        if device is not None:
            seen_devices.add(device)
    return distinct


def _pack_context(repo: Path, audit_id: str) -> tuple[dict, dict, Path, list[dict]]:
    """Read immutable pack identity; refuse absent seals and defer byte resolution to the selected copy."""
    rows = [row for row in read_register(repo).rows if row["audit_id"] == audit_id]
    if not rows or not (rows[-1]["state_after"] in ("packed", "offloaded", "rehydrated")
                        or re.fullmatch(r"replicated\(\d+\)", rows[-1]["state_after"])):
        raise RegisterError(f"refused copy operation {audit_id}: latest state packed or replicated(n) required")
    packed = next((row for row in reversed(rows) if row["event"] == "pack"), None)
    seal = next((row for row in reversed(rows) if row["event"] == "seal"), None)
    if packed is None or seal is None or not re.fullmatch(SHA, packed["pack_sha256"]):
        raise RegisterError(f"refused copy operation {audit_id}: pack and seal rows required")
    directory = _directory(repo, Path(packed["audit_dir"]))
    if packed["audit_dir"] != seal["audit_dir"] or packed["manifest_sha256"] != seal["manifest_sha256"]:
        raise RegisterError(f"refused pack identity {audit_id}: seal differs from pack row")
    sealed_rows(repo, directory, seal)
    return rows[-1], packed, directory, []


def read_cas_refs(pack: Path, expected: bytes, packed: dict) -> list[dict]:
    """Read the pack's authoritative refs; refuse duplicate, unbound or malformed CAS_REFS.tsv."""
    from contextlib import nullcontext

    note = re.fullmatch(rf"pack_verified:1; members:\d+; cas_refs_sha256:({SHA})(?:; window-confirmed:[^\t\r\n;]+)?"
                       + FREE_SPACE_SUFFIX, packed["note"])
    if note is None:
        raise RegisterError(f"refused pack {pack}: CAS_REFS.tsv differs from recorded manifest selection")
    wanted = note[1]
    found = []
    try:
        with (pack.open("rb") if isinstance(pack, Path) else nullcontext(pack)) as source, zstandard.ZstdDecompressor().stream_reader(source) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for info in archive:
                    if info.name != "CAS_REFS.tsv":
                        continue
                    if not info.isfile():
                        raise RegisterError(f"refused CAS_REFS.tsv in {pack}: wrong type or bytes")
                    with archive.extractfile(info) as payload:
                        raw = payload.read()
                    if (len(raw) != info.size or hashlib.sha256(raw).hexdigest() != wanted
                            or info.pax_headers.get("MINSKY.sha256") != wanted
                            or info.pax_headers.get("MINSKY.mtime_ns") != "0" or info.mode != 0o644 or info.mtime != 0):
                        raise RegisterError(f"refused CAS_REFS.tsv in {pack}: sha256 or metadata mismatch")
                    found.append(raw)
    except (tarfile.TarError, zstandard.ZstdError) as exc:
        raise RegisterError(f"refused CAS_REFS.tsv in {pack}: {exc}") from exc
    if len(found) != 1:
        raise RegisterError(f"refused CAS_REFS.tsv in {pack}: expected one member, found {len(found)}")
    try:
        text = found[0].decode("utf-8")
    except UnicodeError as exc:
        raise RegisterError(f"refused CAS_REFS.tsv in {pack}: invalid UTF-8") from exc
    lines = text.split("\n")
    if not text.endswith("\n") or any(char in text for char in "\r\0") or lines[0] != "\t".join(CAS_HEADER):
        raise RegisterError(f"refused CAS_REFS.tsv in {pack}: header/LF mismatch")
    rows, shas = [], set()
    for line in lines[1:-1]:
        fields = line.split("\t")
        if (len(fields) != 3 or not re.fullmatch(SHA, fields[0]) or not re.fullmatch(r"\d+", fields[1])
                or not fields[2] or fields[0] in shas
                or ".." in PurePosixPath(fields[2]).parts or PurePosixPath(fields[2]).as_posix() != fields[2]
                or fields[2] == "."):
            raise RegisterError(f"refused CAS_REFS.tsv in {pack}: invalid or duplicate dependency {line!r}")
        shas.add(fields[0])
        rows.append(dict(zip(CAS_HEADER, fields)))
    if cas_refs_bytes(rows) != found[0]:
        raise RegisterError(f"refused CAS_REFS.tsv in {pack}: noncanonical row order")
    return rows


def _copy_blob(source: Path, destination: Path, sha: str, byte_count: int) -> None:
    if destination.is_symlink() or destination.exists():
        checked_file(destination, sha, byte_count)
        return
    checked_file(source, sha, byte_count)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(destination) + ".partial")
    created = False
    try:
        with source.open("rb") as src, partial.open("xb") as dst:
            created = True
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        checked_file(partial, sha, byte_count)
        partial.chmod(0o444)
        try:
            os.link(partial, destination)
        except FileExistsError:
            checked_file(destination, sha, byte_count)
        checked_file(destination, sha, byte_count)
    finally:
        if created:
            partial.unlink(missing_ok=True)


def _restic_base(repository: str, password_file: Path) -> list[str]:
    _field(repository, "--restic-repo", nonempty=True)
    if any(char.isspace() for char in repository):
        raise RegisterError(f"refused --restic-repo {repository!r}: contains whitespace")
    if not password_file.is_file() or password_file.is_symlink():
        raise RegisterError(f"refused restic password file {password_file}: regular file required")
    return ["restic", "--repo", repository, "--password-file", str(password_file), "--no-cache"]


def _restic_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in (
        "RESTIC_PASSWORD", "RESTIC_PASSWORD_COMMAND", "RESTIC_PASSWORD_FILE", "RESTIC_REPOSITORY",
        "RESTIC_REPOSITORY_FILE", "RESTIC_HOST", "RESTIC_CACHE_DIR",
    )}


def _restic(base: list[str], *args: str) -> str:
    try:
        result = subprocess.run([*base, *args], env=_restic_env(), capture_output=True, text=True)
    except (OSError, UnicodeError) as exc:
        raise RegisterError(f"refused restic {args[0]} at {base[2]}: {exc}") from exc
    if result.returncode:
        raise RegisterError(f"refused restic {args[0]} at {base[2]}: exit {result.returncode}; {result.stderr.strip()}")
    return result.stdout


def _dump_checked(base: list[str], snapshot: str, path: str, sha: str, byte_count: int) -> None:
    digest, total = hashlib.sha256(), 0
    with subprocess.Popen([*base, "dump", snapshot, path], env=_restic_env(),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        errors = []

        def drain_error() -> None:
            for block in iter(lambda: process.stderr.read(8192), b""):
                errors.append(block)
                del errors[:-8]

        error_reader = threading.Thread(target=drain_error, daemon=True)
        try:
            error_reader.start()
            for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
                digest.update(block)
                total += len(block)
            status = process.wait()
            error_reader.join()
            error = b"".join(errors).decode("utf-8", errors="replace")
        except BaseException:
            process.kill()
            process.wait()
            if error_reader.ident is not None:
                error_reader.join()
            raise
    if status or total != byte_count or digest.hexdigest() != sha:
        raise RegisterError(f"refused restic dump snapshot {snapshot} object {path}: sha256/bytes or exit {status}; {error.strip()}")


def _policy(value: str) -> list[str]:
    flags = shlex.split(value)
    allowed = {"--keep-last", "--keep-hourly", "--keep-daily", "--keep-weekly", "--keep-monthly", "--keep-yearly",
               "--keep-within", "--keep-within-hourly", "--keep-within-daily", "--keep-within-weekly",
               "--keep-within-monthly", "--keep-within-yearly", "--keep-tag", "--group-by"}
    pairs, index = [], 0
    while index < len(flags):
        flag, separator, argument = flags[index].partition("=")
        if flag not in allowed:
            raise RegisterError(f"refused --forget-policy flag {flags[index]!r}: retention flags only; no pruning")
        if not separator:
            index += 1
            if index == len(flags):
                raise RegisterError(f"refused --forget-policy flag {flag}: value required")
            argument = flags[index]
        if argument.startswith("-"):
            raise RegisterError(f"refused --forget-policy flag {flag}: invalid value {argument!r}")
        pairs.append((flag, argument))
        index += 1
    if ("--keep-tag", "minsky-lifecycle") not in pairs:
        raise RegisterError("refused --forget-policy: --keep-tag minsky-lifecycle required")
    return [item for pair in pairs for item in pair]


def _retention(base: list[str], policy: list[str]) -> list[dict]:
    try:
        snapshots = json.loads(_restic(base, "snapshots", "--json"))
    except ValueError as exc:
        raise RegisterError(f"refused restic snapshots at {base[2]}: invalid JSON") from exc
    output = _restic(base, "forget", "--dry-run", "--json", *policy)
    # Restic 0.19 emits no groups for an empty repository; this is accepted only when the snapshot census is empty.
    if snapshots == [] and output == "":
        keep_list = []
    else:
        try:
            keep_list = json.loads(output)
        except ValueError as exc:
            raise RegisterError(f"refused restic keep-list at {base[2]}: invalid or missing JSON") from exc
    if not isinstance(snapshots, list) or not isinstance(keep_list, list):
        raise RegisterError(f"refused restic retention at {base[2]}: snapshots/keep-list must be arrays")
    lifecycle = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str) or not re.fullmatch(SHA, snapshot["id"]):
            raise RegisterError(f"refused restic snapshot identity at {base[2]}")
        tags = snapshot.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise RegisterError(f"refused restic snapshot tags at {base[2]}")
        if "minsky-lifecycle" in tags:
            lifecycle.add(snapshot["id"])
    kept = set()
    for group in keep_list:
        if not isinstance(group, dict) or not isinstance(group.get("keep"), list):
            raise RegisterError(f"refused restic keep-list at {base[2]}: missing keep array")
        for snapshot in group["keep"]:
            if not isinstance(snapshot, dict) or not re.fullmatch(SHA, str(snapshot.get("id", ""))):
                raise RegisterError(f"refused restic keep-list snapshot at {base[2]}")
            kept.add(snapshot["id"])
    if not lifecycle <= kept:
        raise RegisterError(f"refused restic keep-list at {base[2]}: lifecycle snapshots not kept {sorted(lifecycle - kept)}")
    _restic(base, "check")
    return snapshots


def copy_operation(repo: Path, *, audit_id: str, copy: str, actor: str, event: str,
                   copy_a_root: Path | None = None, restic_repo: str | None = None,
                   restic_password_file: Path | None = None, forget_policy: str | None = None,
                   window_confirmed: str | None = None) -> None:
    """Refuse predecessor, fixity or retention failures; append each copy row only after its own check."""
    from lifecycle_citations import effective_class

    repo = repo.resolve()
    _field(actor, "--actor", nonempty=True)
    if copy not in ("A", "B") or event not in ("copy", "verify"):
        raise RegisterError(f"refused copy operation {copy!r}/{event!r}")
    with AuditLock.acquire(repo, audit_id, verb="replicate" if event == "copy" else "verify", session=actor) as lock:
        before, packed, directory, resident = _pack_context(repo, audit_id)
        audit_rows = [row for row in read_register(repo).rows if row["audit_id"] == audit_id]
        last_promote = max((i for i, row in enumerate(audit_rows) if row["event"] == "promote"), default=-1)
        promoting = any(row["event"] == "promotion-pending" for row in audit_rows[last_promote + 1:])
        if event == "copy" and not (before["state_after"] == "packed"
                                   or re.fullmatch(r"replicated\(\d+\)", before["state_after"])
                                   or promoting and before["state_after"] in ("offloaded", "rehydrated")):
            raise RegisterError(f"refused copy operation {audit_id}: latest state packed or replicated(n) required")
        boot, freeze = freeze_rows(repo, audit_id)
        if event == "copy":
            require_gate("G2", audit_dir=directory, db_path=Path(boot["path"]))
        _copy_window(Path(packed["copy_location"]), window_confirmed)
        original = Path(packed["copy_location"]) / "packs" / f"{audit_id}.tar.zst"
        if not Path(packed["copy_location"]).is_absolute():
            raise RegisterError(f"refused pack copy-A location {packed['copy_location']}: absolute path required")
        expected_refs = b""  # The historical selection is bound inside the immutable pack.
        note = re.fullmatch(rf"pack_verified:1; members:\d+; cas_refs_sha256:({SHA})(?:; window-confirmed:[^\t\r\n;]+)?"
                           + FREE_SPACE_SUFFIX, packed["note"])
        if note is None:
            raise RegisterError(f"refused pack CAS_REFS.tsv identity for {audit_id}: differs from manifest selection")
        refs_sha = note[1]
        snapshot, device = None, None
        if copy == "A":
            if copy_a_root is None:
                raise RegisterError("refused copy A: --copy-a-root required")
            _copy_window(copy_a_root, window_confirmed)
            root = copy_a_root.resolve()
            if root != original.parent.parent.resolve():
                raise RegisterError(f"refused copy-A root {root}: differs from pack location {original.parent.parent}")
            location = "A:" + str(root)
            device = os.stat(root).st_dev
            target = bounded_path(root, root / "packs" / original.name)
        else:
            if restic_repo is None or restic_password_file is None or forget_policy is None:
                raise RegisterError("refused copy B: --restic-repo, --restic-password-file and --forget-policy required")
            _field(restic_repo, "--restic-repo", nonempty=True)
            if any(char.isspace() for char in restic_repo):
                raise RegisterError(f"refused --restic-repo {restic_repo!r}: contains whitespace")
            policy = _policy(forget_policy)
            local = local_repository(restic_repo)
            if local is not None:
                _copy_window(local, window_confirmed)
                device = os.stat(local).st_dev
                if event == "copy" and device == os.stat(Path(packed["copy_location"])).st_dev:
                    raise RegisterError(f"refused copy B repository {local}: same device as copy A {packed['copy_location']}")
            _copy_window(restic_password_file, window_confirmed)
            base = _restic_base(restic_repo, restic_password_file)
            location = "B:" + (str(local) if local is not None else restic_repo)
            snapshots = _retention(base, policy)

        space_shas = []

        def record(kind: str, verification: str) -> None:
            lock.check()
            row = dict.fromkeys(COLUMNS, "")
            row.update(audit_id=audit_id, audit_dir=packed["audit_dir"], event=event,
                       actor=actor, state_after=before["state_after"], copy_location=location,
                       manifest_sha256=packed["manifest_sha256"], pack_sha256=packed["pack_sha256"],
                       citation_resolvability=packed["citation_resolvability"], **{"class": effective_class(repo, audit_id)},
                       note=f"copy-row:{kind}; {verification}; verified_utc:{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}" +
                       (f"; snapshot:{snapshot}" if snapshot else ""), lock_id=lock.lock_id)
            if window_confirmed is not None:
                row["note"] += f"; window-confirmed:{window_confirmed}"
            row["note"] = free_space_note(row["note"], space_shas)
            # An offloaded or rehydrated audit keeps its state; a copy made during its promotion never regresses it (host fix).
            if event == "copy" and kind == "freeze" and before["state_after"] not in ("offloaded", "rehydrated"):
                current = read_register(repo)
                candidate = Register([*current.rows, row], [*current.line_numbers, 0], repo_root=repo)
                row["state_after"] = f"replicated({len(counting_locations(audit_id, candidate, freeze_sha=freeze['sha256']))})"
            append_event(repo, row, lock=lock)

        if copy == "A":
            checked_file(target, packed["pack_sha256"], target.stat().st_size)
            refs = read_cas_refs(target, expected_refs, packed)
            cfg = load_cas() if event == "copy" else None
            if cfg is not None:
                _copy_window(cfg.root, window_confirmed)
                root.mkdir(parents=True, exist_ok=True)
                missing = {row["sha256"]: int(row["bytes"]) for row in refs
                           if not store.cas_path(root / "cas", row["sha256"]).exists()}
                space = check_free_space(root, sum(missing.values()))
                space_shas.append(record_free_space(directory, space))
            record("pack", f"pack:{packed['pack_sha256']}; device:{device}")
            for row in refs:
                destination = bounded_path(root, store.cas_path(root / "cas", row["sha256"]))
                if cfg is not None:
                    source = bounded_path(cfg.root, store.cas_path(cfg.root, row["sha256"]))
                    _copy_window(source, window_confirmed)
                    _copy_blob(source, destination, row["sha256"], int(row["bytes"]))
                checked_file(destination, row["sha256"], int(row["bytes"]))
            record("cas-replica", f"cas_replica:{len(refs)}/{len(refs)}; cas_refs_sha256:{refs_sha}")
            frozen = Path(freeze["path"])
            _copy_window(frozen, window_confirmed)
            if not frozen.resolve().is_relative_to(root / "freezes"):
                raise RegisterError(f"refused freeze-copy {frozen}: must be under {root / 'freezes'}")
            checked_file(frozen, freeze["sha256"], int(freeze["bytes"]))
            record("freeze", f"freeze:{freeze['sha256']}")
            return

        if event == "copy":
            checked_file(original, packed["pack_sha256"], original.stat().st_size)
            refs = read_cas_refs(original, expected_refs, packed)
            cfg = load_cas()
            _copy_window(cfg.root, window_confirmed)
            frozen = Path(boot["path"])
            _copy_window(frozen, window_confirmed)
            checked_file(frozen, boot["sha256"], int(boot["bytes"]))
            blobs = [bounded_path(cfg.root, store.cas_path(cfg.root, row["sha256"])) for row in refs]
            for row, path in zip(refs, blobs):
                _copy_window(path, window_confirmed)
                checked_file(path, row["sha256"], int(row["bytes"]))
            objects = list(dict.fromkeys([str(original.resolve()), *(str(path.resolve()) for path in blobs), str(frozen.resolve())]))
            output = _restic(base, "backup", "--tag", "minsky-lifecycle", "--tag", "audit:" + audit_id, "--json", "--", *objects)
            summaries = [json.loads(line) for line in output.splitlines() if line]
            ids = [row.get("snapshot_id") for row in summaries if row.get("message_type") == "summary"]
            if len(ids) != 1 or not isinstance(ids[0], str) or not re.fullmatch(SHA, ids[0]):
                raise RegisterError(f"refused restic backup {audit_id}: one full snapshot id required")
            snapshot = ids[0]
            snapshots = _retention(base, policy)
        else:
            copies = [row for row in read_register(repo).rows if row["audit_id"] == audit_id and row["event"] == "copy"
                      and normalised_location(row["copy_location"]) == location
                      and row["pack_sha256"] == packed["pack_sha256"]]
            parsed = [parse_copy_note(row) for row in copies]
            snapshot = next((row["snapshot"] for row in reversed(parsed) if row["kind"] == "pack"), None)
            if snapshot is None:
                raise RegisterError(f"refused verify copy B {audit_id}: recorded snapshot required")
        entries = [row for row in snapshots if row["id"] == snapshot]
        if len(entries) != 1 or "minsky-lifecycle" not in entries[0].get("tags", []) or "audit:" + audit_id not in entries[0].get("tags", []):
            raise RegisterError(f"refused restic snapshot {snapshot}: lifecycle and audit tags required")
        paths = entries[0].get("paths")
        if not isinstance(paths, list) or not all(isinstance(path, str) and PurePosixPath(path).is_absolute() for path in paths):
            raise RegisterError(f"refused restic snapshot {snapshot}: absolute object paths required")

        def object_path(suffix: str) -> str:
            matches = [path for path in paths if path.endswith("/" + suffix)]
            if len(matches) != 1:
                raise RegisterError(f"refused restic snapshot {snapshot} object {suffix}: unique path required")
            return matches[0]

        pack_path = object_path("packs/" + original.name)
        # A streamed dump is the fixity proof, even when the local pack or boot CAS is unavailable.
        listing = json.loads(_restic(base, "ls", "--json", snapshot, pack_path).splitlines()[-1])
        size = listing.get("size")
        if type(size) is not int or size < 0:
            raise RegisterError(f"refused restic pack size {pack_path}: invalid listing")
        _dump_checked(base, snapshot, pack_path, packed["pack_sha256"], size)
        record("pack", f"pack:{packed['pack_sha256']}" + (f"; device:{device}" if device is not None else ""))
        # Parse the remote pack itself even when copy A and the boot CAS are absent.
        with subprocess.Popen([*base, "dump", snapshot, pack_path], env=_restic_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            errors = []

            def drain() -> None:
                for block in iter(lambda: process.stderr.read(8192), b""):
                    errors.append(block)
                    del errors[:-8]

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                refs = read_cas_refs(process.stdout, expected_refs, packed)
                status = process.wait()
                reader.join()
                if status:
                    raise RegisterError(f"refused restic dump snapshot {snapshot} object {pack_path}: exit {status}")
            except BaseException:
                process.kill()
                process.wait()
                reader.join()
                raise
        for row in refs:
            suffix = f"sha256/{row['sha256'][:2]}/{row['sha256'][2:4]}/{row['sha256']}"
            _dump_checked(base, snapshot, object_path(suffix), row["sha256"], int(row["bytes"]))
        record("cas-replica", f"cas_replica:{len(refs)}/{len(refs)}; cas_refs_sha256:{refs_sha}")
        _dump_checked(base, snapshot, object_path(Path(boot["path"]).name), freeze["sha256"], int(freeze["bytes"]))
        record("freeze", f"freeze:{freeze['sha256']}")
