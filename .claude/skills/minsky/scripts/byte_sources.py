"""Resolve evidence bytes; refuse ambiguous remaps and untrustworthy packs."""

from __future__ import annotations

import csv
import hashlib
import tarfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

import zstandard


RAW_MANIFEST_HEADER = (
    "path", "type", "symlink_target", "bytes", "sha256", "mtime_ns", "mode",
    "git_tracked", "class", "hash_bound", "hardlink_group", "symlink_map",
)


@dataclass(frozen=True)
class Resolution:
    present: bool
    sha256: str | None
    resolved_via: str
    detail: str


class ByteSource(ABC):
    """Refuse to treat a recorded filename as proof of its content."""

    @abstractmethod
    def resolve(self, recorded_path: str, expected_sha256: str | None,
                *, store_ref: dict | None = None) -> Resolution:
        """Resolve and hash bytes; refuse unverified content identities."""

    @abstractmethod
    def output_key(self, recorded_path: str, recorded_round_root: str | None) -> str:
        """Key a recorded output; refuse implicit live-path relocation."""


class LiveSource(ByteSource):
    """Use live paths then explicit store refs; refuse unvalidated store identities."""

    def __init__(self, store_root: Path | None = None) -> None:
        self.store_root = store_root

    def resolve(self, recorded_path: str, expected_sha256: str | None,
                *, store_ref: dict | None = None) -> Resolution:
        """Re-hash live or referenced CAS bytes; refuse foreign or malformed refs."""
        import provenance
        from store import StoreConfigError, cas_path, load_store_config

        path = Path(recorded_path)
        present = path.is_file()
        if present or store_ref is None:
            return Resolution(present, provenance.sha256_file(path) if present else None,
                              "live", str(path) if present else "")
        try:
            cfg = load_store_config()
        except StoreConfigError as exc:
            return Resolution(False, None, "unresolved", f"store-config-error:{exc}")
        if not cfg.enabled:
            return Resolution(False, None, "unresolved", "store-disabled")
        if not isinstance(store_ref, dict):
            return Resolution(False, None, "unresolved", "store-ref-invalid")
        if store_ref.get("store_id") != cfg.store_id:
            return Resolution(False, None, "unresolved", "store-id-mismatch")
        root = self.store_root if self.store_root is not None else cfg.root
        marker = root / "STORE_ID"
        try:
            payload = marker.read_bytes()
        except OSError as exc:
            return Resolution(False, None, "unresolved", f"store-config-error:{marker}: {exc}")
        if payload not in (cfg.store_id.encode("ascii"), (cfg.store_id + "\n").encode("ascii")):
            return Resolution(False, None, "unresolved", "store-id-mismatch")
        sha = store_ref.get("sha256")
        try:
            target = cas_path(root, sha)
        except StoreConfigError as exc:
            return Resolution(False, None, "unresolved", f"store-ref-invalid:{exc}")
        if expected_sha256 is not None and sha != expected_sha256:
            return Resolution(False, None, "unresolved", "store-ref-sha256-mismatch")
        if not target.is_file():
            return Resolution(False, None, "unresolved", str(target))
        return Resolution(True, provenance.sha256_file(target), "store_ref", str(target))

    def output_key(self, recorded_path: str, recorded_round_root: str | None) -> str:
        """Keep absolute-string matching; refuse to remap recorded live outputs."""
        return recorded_path


class PackError(ValueError):
    """Refuse a pack whose payloads or member types cannot be trusted."""


@dataclass(frozen=True)
class ScannedMember:
    """Retain metadata and computed hashes; refuse payload retention."""

    info: tarfile.TarInfo
    sha256: str | None
    bytes: int


def iter_pack_members(pack: Path) -> Iterator[ScannedMember]:
    """Stream and hash all members; refuse false PAX hashes and unsafe member types."""
    try:
        with pack.open("rb") as compressed:
            with zstandard.ZstdDecompressor().stream_reader(compressed) as stream:
                with tarfile.open(fileobj=stream, mode="r|") as archive:
                    for member in archive:
                        if member.type in {
                            tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE,
                        }:
                            raise PackError(f"refused pack member {member.name}: type {member.type!r}")
                        if not member.isfile():
                            yield ScannedMember(member, None, 0)
                            continue
                        payload = archive.extractfile(member)
                        if payload is None:
                            raise PackError(f"refused pack member {member.name}: missing payload")
                        digest = hashlib.sha256()
                        byte_count = 0
                        with payload:
                            for block in iter(lambda: payload.read(1024 * 1024), b""):
                                digest.update(block)
                                byte_count += len(block)
                        computed = digest.hexdigest()
                        if "MINSKY.sha256" in member.pax_headers:
                            declared = member.pax_headers["MINSKY.sha256"]
                            if declared != computed:
                                raise PackError(
                                    f"refused pack member {member.name}: MINSKY.sha256 "
                                    f"{declared!r} differs from computed {computed}"
                                )
                        yield ScannedMember(member, computed, byte_count)
    except (OSError, tarfile.TarError, zstandard.ZstdError) as exc:
        raise PackError(f"refused pack {pack}: {exc}") from exc


@dataclass(frozen=True)
class PackIndex:
    """Keep content hashes and member names only; refuse payload retention."""

    members: dict[str, str]
    pax_sha256: dict[str, str]

    @classmethod
    def build(cls, pack: Path) -> PackIndex:
        """Stream and hash a pack; refuse false PAX hashes and unsafe member types."""
        members = {}
        pax_sha256 = {}
        for scanned in iter_pack_members(pack):
            member = scanned.info
            if scanned.sha256 is None:
                continue
            if "MINSKY.sha256" in member.pax_headers:
                pax_sha256[member.name] = member.pax_headers["MINSKY.sha256"]
            members[scanned.sha256] = member.name
        return cls(members, pax_sha256)


def _relative_to(path: str, prefix: str) -> PurePosixPath | None:
    try:
        return PurePosixPath(path).relative_to(PurePosixPath(prefix))
    except ValueError:
        return None


def parse_remaps(values: list[str]) -> list[tuple[str, str]]:
    """Parse explicit absolute remaps; refuse overlapping OLD component prefixes."""
    remaps = []
    for value in values:
        if value.count("=") != 1:
            raise ValueError(f"refused remap {value!r}: requires exactly one =")
        old, new = (part.rstrip("/") for part in value.split("="))
        if not old or not new or not PurePosixPath(old).is_absolute() or not PurePosixPath(new).is_absolute():
            raise ValueError(f"refused remap {value!r}: both prefixes must be absolute and non-empty")
        for previous, _ in remaps:
            if _relative_to(old, previous) is not None or _relative_to(previous, old) is not None:
                raise ValueError(f"overlapping remap prefixes: {previous!r} and {old!r}")
        remaps.append((old, new))
    return remaps


def load_symlink_map(audit_dir: Path) -> list[tuple[str, str]]:
    """Read seal-time link mappings; refuse a raw manifest with the wrong header."""
    manifest = audit_dir / "lifecycle" / "raw_manifest.tsv"
    if not manifest.exists():
        return []
    with manifest.open(encoding="utf-8", newline="") as fh:
        if fh.readline() != "\t".join(RAW_MANIFEST_HEADER) + "\n":
            raise ValueError(f"refused raw manifest {manifest}: wrong header")
        mappings = []
        for line_number, row in enumerate(csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE), 2):
            if len(row) != len(RAW_MANIFEST_HEADER):
                raise ValueError(f"refused raw manifest {manifest}: line {line_number} has {len(row)} fields")
            fields = dict(zip(RAW_MANIFEST_HEADER, row))
            if fields["type"] == "symlink" and fields["symlink_map"]:
                mappings.append((fields["symlink_map"], fields["path"]))
    return sorted(mappings, key=lambda entry: len(entry[0]), reverse=True)


class ArchiveSource(ByteSource):
    """Resolve by content then explicit remaps; refuse all live access under no_live."""

    def __init__(self, *, store_root: Path | None, pack: Path | None,
                 remaps: list[tuple[str, str]], no_live: bool,
                 symlink_map: list[tuple[str, str]] | None = None) -> None:
        self.store_root = store_root
        self.pack = pack
        self.remaps = parse_remaps([f"{old}={new}" for old, new in remaps])
        self.no_live = no_live
        self.symlink_map = sorted(symlink_map or [], key=lambda entry: len(entry[0]), reverse=True)
        self.remap_log: list[dict[str, str | list[str]]] = []
        self._pack_index: PackIndex | None = None

    def resolve(self, recorded_path: str, expected_sha256: str | None,
                *, store_ref: dict | None = None) -> Resolution:
        """Re-hash resolved bytes; refuse corrupt CAS identities and implicit live reads."""
        import provenance

        notes = []
        if self.store_root is not None and expected_sha256:
            path = (self.store_root / "sha256" / expected_sha256[:2]
                    / expected_sha256[2:4] / expected_sha256)
            if path.is_file():
                computed = provenance.sha256_file(path)
                if computed == expected_sha256:
                    return Resolution(True, computed, "cas", str(path))
                notes.append(f"cas-corrupt:{path}")
        if self.pack is not None and expected_sha256:
            if self._pack_index is None:
                self._pack_index = PackIndex.build(self.pack)
            if expected_sha256 in self._pack_index.members:
                return Resolution(True, expected_sha256, "pack", self._pack_index.members[expected_sha256])
        if not self.no_live:
            for old, new in self.remaps:
                rest = _relative_to(recorded_path, old)
                if rest is None:
                    continue
                path = Path(new).joinpath(*rest.parts)
                self.remap_log.append({"recorded": recorded_path, "opened": str(path), "remap": [old, new]})
                if path.is_file():
                    return Resolution(True, provenance.sha256_file(path), "remap", str(path))
                break
        return Resolution(False, None, "unresolved", "; ".join(notes))

    def output_key(self, recorded_path: str, recorded_round_root: str | None) -> str:
        """Map recorded components to round-relative keys; refuse outside-path matches."""
        if not isinstance(recorded_path, str):
            return f"invalid:{recorded_path!r}"
        if recorded_round_root is not None and PurePosixPath(recorded_path).is_absolute():
            for realpath, link in self.symlink_map:
                rest = _relative_to(recorded_path, realpath)
                if rest is not None:
                    return PurePosixPath(link).joinpath(*rest.parts).as_posix()
            rest = _relative_to(recorded_path, recorded_round_root)
            if rest is not None:
                return rest.as_posix()
        return "abs:" + recorded_path
