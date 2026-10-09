"""Write and check sealed packs; refuse member drift, incomplete streams and failed independent checks."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import zstandard

import store
from byte_sources import PackError, RAW_MANIFEST_HEADER, iter_pack_members
from free_space import check_free_space
from lifecycle_citations import effective_class
from lifecycle_register import AuditLock, COLUMNS, RegisterError, append_event, read_register
from lifecycle_seal import CLASSES, STORE_HEADER, STORE_INDEX, _directory, _field, _read_table, _volume_window

CAS_HEADER = ("sha256", "bytes", "path")
FREE_SPACE_SUFFIX = r"(?:; free_space_sha256:[0-9a-f]{64}(?:,[0-9a-f]{64})*)?"


def free_space_sidecar_preflight(directory: Path, result: dict) -> tuple[str, bytes]:
    """Check canonical bytes without writing; refuse linked parents or conflicting sidecar paths."""
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    sha = hashlib.sha256(raw).hexdigest()
    path = directory / "lifecycle/free_space_checks" / (sha + ".json")
    for parent in (path.parent.parent, path.parent):
        if parent.is_symlink():
            raise RegisterError(f"refused free-space sidecar parent {parent}: symlink")
        if parent.exists() and not parent.is_dir():
            raise RegisterError(f"refused free-space sidecar parent {parent}: not a directory")
    bounded_path(directory, path)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise RegisterError(f"refused free-space sidecar {path}: existing bytes differ")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
            existing = source.read()
        if existing != raw or hashlib.sha256(existing).hexdigest() != sha:
            raise RegisterError(f"refused free-space sidecar {path}: existing bytes differ")
    return sha, raw


def record_free_space(directory: Path, result: dict) -> str:
    """Persist canonical check bytes create-only; refuse linked parents or differing existing sidecars."""
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    sha = hashlib.sha256(raw).hexdigest()
    path = bounded_path(directory, directory / "lifecycle/free_space_checks" / (sha + ".json"))
    for parent in (path.parent.parent, path.parent):
        if parent.is_symlink():
            raise RegisterError(f"refused free-space sidecar parent {parent}: symlink")
        parent.mkdir(exist_ok=True)
    try:
        with path.open("xb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise RegisterError(f"refused free-space sidecar {path}: existing bytes differ")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
            existing = source.read()
        if existing != raw or hashlib.sha256(existing).hexdigest() != sha:
            raise RegisterError(f"refused free-space sidecar {path}: existing bytes differ")
        return sha
    checked_file(path, sha, len(raw))
    return sha


def free_space_note(note: str, shas: list[str]) -> str:
    """Append ordered check identities; refuse malformed sha256 references."""
    if any(not re.fullmatch(r"[0-9a-f]{64}", sha) for sha in shas):
        raise RegisterError(f"refused free-space note references {shas!r}: invalid sha256")
    return note + (("; " if note else "") + "free_space_sha256:" + ",".join(shas) if shas else "")


@dataclass(frozen=True)
class ExpectedMember:
    """Require explicit expected metadata; refuse inferred defaults for missing seal fields."""

    path: str
    type: str
    sha256: str | None
    bytes: int
    mode: int
    mtime_ns: int
    symlink_target: str = ""


def _expected_members(expected: list[ExpectedMember]) -> dict[str, ExpectedMember]:
    members = {}
    for item in expected:
        path = PurePosixPath(item.path)
        if (not item.path or item.path == "." or path.is_absolute() or ".." in path.parts
                or path.as_posix() != item.path or any(char in item.path for char in "\t\r\n\0")):
            raise PackError(f"refused expected pack member {item.path!r}: unsafe relative path")
        if item.path in members:
            raise PackError(f"refused expected pack member {item.path}: duplicate name")
        if (item.type not in ("file", "dir", "symlink") or type(item.bytes) is not int or item.bytes < 0
                or type(item.mode) is not int or not 0 <= item.mode <= 0o7777 or type(item.mtime_ns) is not int):
            raise PackError(f"refused expected pack member {item.path}: invalid type, bytes, mode or mtime_ns")
        if item.type == "file":
            if not isinstance(item.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", item.sha256):
                raise PackError(f"refused expected pack member {item.path}: invalid sha256")
        elif item.sha256 is not None or item.bytes != 0:
            raise PackError(f"refused expected pack member {item.path}: non-regular payload")
        if (not isinstance(item.symlink_target, str) or "\0" in item.symlink_target
                or (item.type == "symlink") != bool(item.symlink_target)):
            raise PackError(f"refused expected pack member {item.path}: invalid symlink target")
        members[item.path] = item
    return members


def _independent_check(pack: Path, command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run([*command, str(pack)], capture_output=True, text=True)
    except (OSError, UnicodeError) as exc:
        raise PackError(f"refused pack {pack}: {' '.join(command)}: {exc}") from exc
    if result.returncode:
        raise PackError(f"refused pack {pack}: {' '.join(command)} exited {result.returncode}: "
                        f"{result.stderr.strip()}")
    return result


def round_trip(pack: Path, expected: list[ExpectedMember]) -> None:
    """Refuse missing, extra or changed members; re-hash every payload without extracting to disk."""
    members = _expected_members(expected)
    seen = set()
    for scanned in iter_pack_members(pack):
        info = scanned.info
        if info.name not in members:
            raise PackError(f"refused pack {pack}: extra member {info.name}")
        if info.name in seen:
            raise PackError(f"refused pack {pack}: duplicate member {info.name}")
        seen.add(info.name)
        item = members[info.name]
        actual_type = {tarfile.REGTYPE: "file", tarfile.AREGTYPE: "file",
                       tarfile.DIRTYPE: "dir", tarfile.SYMTYPE: "symlink"}.get(info.type)
        if actual_type != item.type:
            raise PackError(f"refused pack member {info.name}: type {info.type!r} differs from {item.type}")
        if info.size != item.bytes or scanned.bytes != item.bytes or scanned.sha256 != item.sha256:
            raise PackError(f"refused pack member {info.name}: sha256 or bytes differ from expected member")
        if item.type == "file" and info.pax_headers.get("MINSKY.sha256") != item.sha256:
            raise PackError(f"refused pack member {info.name}: MINSKY.sha256 differs from expected member")
        if info.mode != item.mode:
            raise PackError(f"refused pack member {info.name}: mode differs from expected member")
        recorded_ns = info.pax_headers.get("MINSKY.mtime_ns", "")
        if not re.fullmatch(r"-?\d+", recorded_ns) or int(recorded_ns) != item.mtime_ns:
            raise PackError(f"refused pack member {info.name}: MINSKY.mtime_ns differs from expected member")
        if item.type != "file" and (info.mtime != int(recorded_ns) // 10**9
                                    or "MINSKY.sha256" in info.pax_headers):
            raise PackError(f"refused pack member {info.name}: inconsistent non-regular metadata")
        if item.type == "symlink" and info.linkname != item.symlink_target:
            raise PackError(f"refused pack member {info.name}: symlink target differs from expected member")
    if seen != set(members):
        raise PackError(f"refused pack {pack}: missing members {sorted(set(members) - seen)}")
    _independent_check(pack, ["zstd", "-t"])
    listing = _independent_check(pack, ["bsdtar", "-tf"]).stdout.splitlines()
    if len(listing) != len(members):
        raise PackError(f"refused pack {pack}: bsdtar listed {len(listing)} entries; expected {len(members)}")


def volume_window(path: Path, window_confirmed: str | None) -> None:
    """Refuse volume access without a recorded production window, including through parent links."""
    if (path.is_relative_to("/Volumes") or path.resolve().is_relative_to("/Volumes")) and not window_confirmed:
        raise RegisterError(f"refused path through /Volumes/ {path}: --window-confirmed required")
    if window_confirmed is not None:
        _field(window_confirmed, "--window-confirmed", nonempty=True)
        if ";" in window_confirmed:
            raise RegisterError("refused --window-confirmed: contains a copy-note separator")


def bounded_path(root: Path, path: Path) -> Path:
    """Refuse a member or destination whose parent links escape its declared root."""
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise RegisterError(f"refused path {path}: resolves outside declared root {root}")
    return path


def sealed_rows(repo: Path, directory: Path, seal: dict) -> list[dict]:
    """Refuse manifest drift, unsafe paths and invalid H4 rows before resolving their bytes."""
    path = bounded_path(repo, directory / "lifecycle/raw_manifest.tsv")
    if path.is_symlink() or not path.is_file():
        raise RegisterError(f"refused manifest {path}: the manifest changed after seal")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != seal["manifest_sha256"]:
        raise RegisterError(f"refused manifest {path}: the manifest changed after seal")
    text = raw.decode("utf-8")
    lines = text.split("\n")
    if not text.endswith("\n") or "\r" in text or "\0" in text or lines[0] != "\t".join(RAW_MANIFEST_HEADER):
        raise RegisterError(f"refused manifest {path}: header/LF mismatch")
    rows = []
    for number, line in enumerate(lines[1:-1], 2):
        fields = line.split("\t")
        if len(fields) != len(RAW_MANIFEST_HEADER):
            raise RegisterError(f"refused manifest {path} line {number}: field count")
        rows.append(dict(zip(RAW_MANIFEST_HEADER, fields)))
    seen = []
    for row in rows:
        name = row["path"]
        relative = PurePosixPath(name)
        if (not name or name == "." or relative.is_absolute() or ".." in relative.parts
                or relative.as_posix() != name or any(c in name for c in "\t\r\n\0")
                or name == "CAS_REFS.tsv" or name in seen):
            raise RegisterError(f"refused raw manifest {path}: unsafe or duplicate member {name!r}")
        seen.append(name)
        if (row["type"] not in ("file", "dir", "symlink") or row["class"] not in CLASSES
                or row["hash_bound"] not in ("0", "1") or row["git_tracked"] not in ("0", "1")
                or (row["type"] == "symlink") != bool(row["symlink_target"])):
            raise RegisterError(f"refused raw manifest member {name}: invalid type, class or flags")
        fields = [row[key] for key in ("bytes", "sha256", "mtime_ns", "mode")]
        if row["type"] == "file" or row["type"] == "symlink" and any(fields):
            if (not re.fullmatch(r"\d+", row["bytes"]) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                    or not re.fullmatch(r"-?\d+", row["mtime_ns"])
                    or not re.fullmatch(r"0[0-7]{3,4}", row["mode"]) or int(row["mode"], 8) > 0o7777):
                raise RegisterError(f"refused raw manifest member {name}: incomplete content identity")
        elif any(fields):
            raise RegisterError(f"refused raw manifest member {name}: non-content fields must be blank")
    if seen != sorted(seen):
        raise RegisterError(f"refused raw manifest {path}: rows are not sorted by path")
    return rows


def cas_rows(rows: list[dict], audit_class: str) -> list[dict]:
    """Select only content-bearing CAS rows; refuse an unknown audit class."""
    if audit_class not in ("code-only", "methodology-bearing"):
        raise RegisterError(f"refused CAS selection class {audit_class!r}")
    return [row for row in rows if row["sha256"] and (row["class"] == "runtime-blob"
            or audit_class == "methodology-bearing" and row["hash_bound"] == "1")]


def cas_refs_bytes(rows: list[dict]) -> bytes:
    """Generate deterministic CAS_REFS.tsv; refuse delimiter-bearing fields."""
    for row in rows:
        for key in CAS_HEADER:
            _field(row[key], f"CAS_REFS.tsv {key}", nonempty=True)
    return ("\t".join(CAS_HEADER) + "\n" + "".join(
        "\t".join(row[key] for key in CAS_HEADER) + "\n" for row in sorted(rows, key=lambda row: row["path"])
    )).encode("utf-8")


def audit_cas_rows(repo: Path, audit_id: str, rows: list[dict], audit_class: str) -> list[dict]:
    """Union sealed CAS dependencies by sha; refuse malformed index identities or conflicting byte counts."""
    candidates = [{key: row[key] for key in CAS_HEADER} for row in cas_rows(rows, audit_class)]
    # Host fix (Astra W6c): the seal row records how many distinct CAS objects seal ingested/used ("ingested:<n>"). A missing
    # or short store index must refuse, never silently shrink the dependency set to the raw-manifest rows.
    seals = [row for row in read_register(repo).rows if row["audit_id"] == audit_id and row["event"] == "seal"]
    if not seals:
        raise RegisterError(f"refused CAS dependencies {audit_id}: no seal row")
    match = re.search(r"(?:^|; )ingested:(\d+)(?:;|$)", seals[-1]["note"])
    if match is None:
        raise RegisterError(f"refused CAS dependencies {audit_id}: seal row note lacks ingested:<n>")
    expected = int(match[1])
    if expected and not (repo / STORE_INDEX).is_file():
        raise RegisterError(f"refused CAS dependencies {audit_id}: store index {STORE_INDEX} missing; seal recorded {expected}")
    indexed = set()
    for row in _read_table(repo, STORE_INDEX, STORE_HEADER):
        if row["audit_id"] == audit_id:
            indexed.add(row["sha256"])
            candidates.append({"sha256": row["sha256"], "bytes": row["bytes"], "path": row["use_path"]})
    if len(indexed) < expected:
        raise RegisterError(f"refused CAS dependencies {audit_id}: store index has {len(indexed)} of {expected} sealed objects")
    distinct = {}
    for row in sorted(candidates, key=lambda item: (item["path"], item["sha256"])):
        for key in CAS_HEADER:
            _field(row[key], f"CAS dependency {audit_id} {key}", nonempty=True)
        sha = row["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or not re.fullmatch(r"\d+", row["bytes"]):
            raise RegisterError(f"refused CAS dependency {audit_id} {row['path']}: invalid sha256 or bytes")
        previous = distinct.setdefault(sha, row)
        if int(previous["bytes"]) != int(row["bytes"]):
            raise RegisterError(f"refused CAS dependency {audit_id} sha256 {sha}: conflicting bytes")
    return sorted(distinct.values(), key=lambda row: row["path"])


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_file(path: Path, sha: str, byte_count: int) -> None:
    """Re-hash a resolved regular file; refuse a corrupt identity or a symlink leaf."""
    if (path.is_symlink() or not path.is_file() or path.stat().st_size != byte_count
            or _hash_file(path) != sha):
        raise RegisterError(f"refused file sha256/bytes {path}: expected {sha}, {byte_count} bytes")


def load_cas() -> store.StoreConfig:
    """Refuse disabled or foreign configured stores before any CAS resolution."""
    cfg = store.load_store_config()
    if not cfg.enabled:
        raise RegisterError(f"refused CAS: store disabled by {cfg.disabled_by}: {cfg.reason}")
    store.validate_store(cfg)
    return cfg


def _source(directory: Path, row: dict, window_confirmed: str | None) -> tuple[Path, os.stat_result]:
    path = directory / row["path"]
    if not path.parent.resolve().is_relative_to(directory.resolve()):
        raise PackError(f"refused pack member {row['path']}: parent changed since seal")
    volume_window(path, window_confirmed)
    info = path.lstat()
    kind = "dir" if stat.S_ISDIR(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "special"
    if kind != row["type"] or kind == "symlink" and os.readlink(path) != row["symlink_target"]:
        raise PackError(f"refused pack member {row['path']}: changed since seal (type or symlink target)")
    if kind == "symlink":
        outside = not path.resolve().is_relative_to(directory.resolve())
        if outside != bool(row["sha256"]):
            raise PackError(f"refused pack member {row['path']}: changed since seal (symlink resolution)")
        if outside:
            path = path.resolve()
            info = path.lstat()
    if row["sha256"] and (not stat.S_ISREG(info.st_mode) or info.st_size != int(row["bytes"])
            or info.st_mtime_ns != int(row["mtime_ns"]) or stat.S_IMODE(info.st_mode) != int(row["mode"], 8)):
        raise PackError(f"refused pack member {row['path']}: changed since seal (bytes, mode or mtime_ns)")
    return path, info


def _plan(directory: Path, rows: list[dict], resident: list[dict], refs: bytes,
          window_confirmed: str | None) -> tuple[list[ExpectedMember], dict[str, Path]]:
    omitted = {row["path"] for row in resident}
    expected, sources = [], {}
    for row in rows:
        if row["path"] in omitted:
            continue
        path, info = _source(directory, row, window_confirmed)
        if row["sha256"]:
            item = ExpectedMember(row["path"], "file", row["sha256"], int(row["bytes"]),
                                  int(row["mode"], 8), int(row["mtime_ns"]))
        else:
            item = ExpectedMember(row["path"], row["type"], None, 0,
                                  0o777 if row["type"] == "symlink" else stat.S_IMODE(info.st_mode),
                                  info.st_mtime_ns, row["symlink_target"])
        expected.append(item)
        sources[item.path] = path
    expected.append(ExpectedMember("CAS_REFS.tsv", "file", hashlib.sha256(refs).hexdigest(), len(refs), 0o644, 0))
    return sorted(expected, key=lambda item: item.path), sources


class _HashingReader:
    def __init__(self, source) -> None:
        self.source = source
        self.digest = hashlib.sha256()
        self.bytes = 0

    def read(self, size: int = -1) -> bytes:
        block = self.source.read(size)
        self.digest.update(block)
        self.bytes += len(block)
        return block


def write_pack(destination: Path, expected: list[ExpectedMember], sources: dict[str, Path], refs: bytes) -> None:
    """Stream independent regular payloads; refuse bytes changed since seal and reused partial paths."""
    _expected_members(expected)
    created = False
    try:
        with destination.open("xb") as compressed:
            created = True
            with zstandard.ZstdCompressor(level=19).stream_writer(compressed, closefd=False) as stream:
                with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    for item in expected:
                        if item.path != "CAS_REFS.tsv":
                            current = sources[item.path].lstat()
                            actual_type = "file" if stat.S_ISREG(current.st_mode) else "dir" if stat.S_ISDIR(current.st_mode) else "symlink" if stat.S_ISLNK(current.st_mode) else "special"
                            if actual_type != item.type:
                                raise PackError(f"refused pack member {item.path}: changed since seal (type)")
                        info = tarfile.TarInfo(item.path)
                        info.type = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE}[item.type]
                        info.size, info.mode, info.mtime = item.bytes, item.mode, item.mtime_ns // 10**9
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.linkname = item.symlink_target
                        info.pax_headers = {"MINSKY.mtime_ns": str(item.mtime_ns)}
                        if item.type != "file":
                            if item.type == "symlink" and os.readlink(sources[item.path]) != item.symlink_target:
                                raise PackError(f"refused pack member {item.path}: changed since seal")
                            archive.addfile(info)
                            continue
                        info.pax_headers["MINSKY.sha256"] = item.sha256
                        with (io.BytesIO(refs) if item.path == "CAS_REFS.tsv" else
                              os.fdopen(os.open(sources[item.path], os.O_RDONLY | os.O_NOFOLLOW), "rb")) as payload:
                            reader = _HashingReader(payload)
                            try:
                                archive.addfile(info, reader)
                            except OSError as exc:
                                raise PackError(f"refused pack member {item.path}: changed since seal: {exc}") from exc
                            if reader.bytes != item.bytes or reader.digest.hexdigest() != item.sha256 or payload.read(1):
                                raise PackError(f"refused pack member {item.path}: changed since seal")
                            if item.path != "CAS_REFS.tsv":
                                after = os.fstat(payload.fileno())
                                if (after.st_size != item.bytes or stat.S_IMODE(after.st_mode) != item.mode
                                        or after.st_mtime_ns != item.mtime_ns):
                                    raise PackError(f"refused pack member {item.path}: changed since seal")
            compressed.flush()
            os.fsync(compressed.fileno())
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def pack(repo: Path, *, audit_id: str, audit_dir: Path, copy_a_root: Path,
         actor: str, window_confirmed: str | None = None) -> None:
    """Refuse an unsealed audit, citation failure, seal drift, missing CAS or reused final pack."""
    repo = repo.resolve()
    _field(actor, "--actor", nonempty=True)
    with AuditLock.acquire(repo, audit_id, verb="pack", session=actor) as lock:
        directory = _directory(repo, audit_dir)
        _volume_window(directory, window_confirmed)
        volume_window(copy_a_root, window_confirmed)
        root = copy_a_root.resolve()
        if root.is_relative_to(directory):
            raise RegisterError(f"refused copy-A root {root}: inside audit {directory}")
        rows = [row for row in read_register(repo).rows if row["audit_id"] == audit_id]
        seal = next((row for row in reversed(rows) if row["event"] == "seal"), None)
        if not rows or rows[-1]["state_after"] != "sealed" or seal is None:
            raise RegisterError(f"refused pack {audit_id}: latest state sealed required")
        if seal["audit_dir"] != directory.relative_to(repo).as_posix():
            raise RegisterError(f"refused pack {audit_id}: audit directory differs from seal")
        seal_index = rows.index(seal)
        if seal["citation_resolvability"] != "pass" and not any(
                row["event"] == "ruling" and row["note"].startswith("citation-waiver:") for row in rows[seal_index + 1:]):
            raise RegisterError(f"refused pack {audit_id}: citation_resolvability fail; later citation-waiver: ruling required")
        manifest = sealed_rows(repo, directory, seal)
        audit_class = effective_class(repo, audit_id)
        resident = audit_cas_rows(repo, audit_id, manifest, audit_class)
        cfg = load_cas()
        volume_window(cfg.root, window_confirmed)
        for row in resident:
            source = bounded_path(cfg.root, store.cas_path(cfg.root, row["sha256"]))
            volume_window(source, window_confirmed)
            checked_file(source, row["sha256"], int(row["bytes"]))
        refs = cas_refs_bytes(resident)
        omitted = {row["path"] for row in cas_rows(manifest, audit_class)}
        incoming = len(refs) + sum(int(row["bytes"]) for row in manifest if row["sha256"] and row["path"] not in omitted)
        target = root / "packs" / f"{audit_id}.tar.zst"
        bounded_path(root, target)
        for path in (target, Path(str(target) + ".partial"), Path(str(target) + ".failed")):
            if path.exists() or path.is_symlink():
                raise RegisterError(f"refused existing pack destination {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        space = check_free_space(target.parent, incoming)
        free_space_sidecar_preflight(directory, space)
        expected, sources = _plan(directory, manifest, cas_rows(manifest, audit_class), refs, window_confirmed)
        partial = Path(str(target) + ".partial")
        write_pack(partial, expected, sources, refs)
        try:
            # A publication lock makes the check-and-rename create-only across audits/repos using this target.
            publication = Path(str(target) + ".publish-lock")
            fd = os.open(publication, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                if target.exists() or target.is_symlink():
                    raise RegisterError(f"refused existing pack destination {target}")
                os.rename(partial, target)
            finally:
                os.close(fd)
                publication.unlink()
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        try:
            pack_sha = _hash_file(target)
            round_trip(target, expected)
            if _hash_file(target) != pack_sha:
                raise PackError(f"refused pack {target}: changed during round trip")
        except BaseException:
            os.rename(target, Path(str(target) + ".failed"))
            raise
        lock.check()
        space_sha = record_free_space(directory, space)
        row = dict.fromkeys(COLUMNS, "")
        row.update(audit_id=audit_id, audit_dir=seal["audit_dir"], actor=actor, event="pack", state_after="packed",
                   manifest_sha256=seal["manifest_sha256"], pack_sha256=pack_sha, copy_location=str(root),
                   citation_resolvability=seal["citation_resolvability"], **{"class": audit_class},
                   note=f"pack_verified:1; members:{len(expected)}; cas_refs_sha256:{hashlib.sha256(refs).hexdigest()}" +
                   (f"; window-confirmed:{window_confirmed}" if window_confirmed else ""))
        row["note"] = free_space_note(row["note"], [space_sha])
        append_event(repo, row, lock=lock)
