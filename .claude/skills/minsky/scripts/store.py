"""Runtime CAS configuration and index; refuse implicit opt-outs and foreign stores."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


SKILL_DIR = Path(__file__).resolve().parent.parent
STORE_ID = "mars-minsky-cas-v1"
INDEX_HEADER = (
    "sha256", "bytes", "first_source_path", "first_seen_utc", "first_audit_id", "first_round",
)


class StoreConfigError(RuntimeError):
    """Refuse absent, malformed, inaccessible or foreign store configuration."""


@dataclass
class StoreConfig:
    """Resolved configuration; refuse to treat absence as disabled storage."""

    store_id: str
    root: Path
    enabled: bool
    root_source: str = "store.json"
    disabled_by: str | None = None
    reason: str | None = None
    # A fresh configuration belongs to one prepare; only its created blobs are reported.
    ingested: set[str] = field(default_factory=set)
    # Every free-space check of this prepare, in bytes (v0.2 §4.4 "All units are bytes, logged").
    free_space_checks: list[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Report the explicit opt-out; refuse inferred enabled/disabled state."""
        return "enabled" if self.enabled else "disabled"

    def receipt(self) -> dict:
        """Describe resolved storage; refuse to omit the actor or override source."""
        if not self.enabled:
            return {"status": "disabled", "disabled_by": self.disabled_by, "reason": self.reason}
        return {
            "status": "enabled", "store_id": self.store_id, "root": str(self.root),
            "root_source": self.root_source, "ingested": sorted(self.ingested),
            "free_space_checks": list(self.free_space_checks),
        }


def load_store_config(config_path: Path | None = None,
                      env: Mapping[str, str] | None = None) -> StoreConfig:
    """Load required store.json; refuse malformed fields and unexplained opt-outs."""
    path = config_path if config_path is not None else SKILL_DIR / "config" / "store.json"
    env = os.environ if env is None else env
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise StoreConfigError(f"refused store config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StoreConfigError(f"refused store config {path}: expected an object")
    for name in ("store_id", "root", "enabled"):
        if name not in value:
            raise StoreConfigError(f"refused store config {path}: missing {name}")
    if value["store_id"] != STORE_ID:
        raise StoreConfigError(f"refused store config {path}: store_id must be {STORE_ID}")
    if type(value["enabled"]) is not bool:
        raise StoreConfigError(f"refused store config {path}: enabled must be a bool")
    root = value["root"]
    if not isinstance(root, str) or not root or "\0" in root:
        raise StoreConfigError(f"refused store config {path}: root must be a non-empty path string")
    if not value["enabled"]:
        for name in ("disabled_by", "reason"):
            if not isinstance(value.get(name), str) or not value[name].strip():
                raise StoreConfigError(f"refused store config {path}: disabled storage requires {name}")
        return StoreConfig(STORE_ID, Path(root), False,
                           disabled_by=value["disabled_by"], reason=value["reason"])
    if not Path(root).is_absolute():
        raise StoreConfigError(f"refused store config {path}: root must be absolute: {root!r}")
    root_source = "store.json"
    if "MINSKY_STORE_ROOT" in env:
        root = env["MINSKY_STORE_ROOT"]
        if not isinstance(root, str) or not root or "\0" in root or not Path(root).is_absolute():
            raise StoreConfigError(f"refused store config {path}: MINSKY_STORE_ROOT must be absolute: {root!r}")
        root_source = "env:MINSKY_STORE_ROOT"
    return StoreConfig(STORE_ID, Path(root), True, root_source)


def read_store_id(root: Path) -> str:
    """Read the exact marker; refuse missing markers, extra whitespace and foreign IDs."""
    marker = root / "STORE_ID"
    try:
        payload = marker.read_bytes()
    except OSError as exc:
        raise StoreConfigError(f"refused STORE_ID check {marker}: {exc}") from exc
    if payload not in (STORE_ID.encode("ascii"), (STORE_ID + "\n").encode("ascii")):
        raise StoreConfigError(f"refused STORE_ID check {marker}: expected {STORE_ID}")
    return STORE_ID


def validate_store(cfg: StoreConfig) -> None:
    """Validate enabled roots; refuse missing, unwritable or incorrectly marked stores."""
    if not cfg.enabled:
        return
    if not cfg.root.is_dir():
        raise StoreConfigError(f"refused store root directory check: {cfg.root}")
    if not os.access(cfg.root, os.W_OK):
        raise StoreConfigError(f"refused store root writable check: {cfg.root}")
    read_store_id(cfg.root)


def cas_path(root: Path, sha: str) -> Path:
    """Address a sha256 blob; refuse malformed identities and path traversal."""
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise StoreConfigError(f"refused CAS sha256 at {root}: {sha!r}")
    return root / "sha256" / sha[:2] / sha[2:4] / sha


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ingest_file(cfg: StoreConfig, src: Path, sha: str, *, byte_count: int,
                audit_id: str, round_label: str | int, source_path: str) -> bool:
    """Publish and index CAS bytes; refuse disabled stores, corrupt blobs or changed sources.

    Returns True only when this call created the blob. Existing blobs are re-hashed and indexed on every call.
    """
    from free_space import FreeSpaceError, check_free_space

    if not cfg.enabled:
        raise StoreConfigError(f"refused CAS ingest {src}: store disabled by {cfg.disabled_by}: {cfg.reason}")
    validate_store(cfg)
    target = cas_path(cfg.root, sha)
    if (type(byte_count) is not int or byte_count < 0 or
            any(char in source_path for char in "\t\r\n\0")):
        raise StoreConfigError(f"refused CAS runtime record: {source_path}")
    created = False
    try:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise StoreConfigError(f"runtime snapshot mismatch: {target}")
        if not target.exists():
            try:
                cfg.free_space_checks.append(check_free_space(cfg.root, byte_count))
            except FreeSpaceError as exc:
                raise StoreConfigError(str(exc)) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.parent / f".{uuid.uuid4().hex}.tmp"
            try:
                with src.open("rb") as source, temp.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                if _sha256_file(temp) != sha:
                    raise StoreConfigError(f"runtime changed during snapshot: {src}")
                try:
                    os.link(temp, target)
                    created = True
                except FileExistsError:
                    pass
            finally:
                temp.unlink(missing_ok=True)
        if created:
            target.chmod(0o444)
        if target.is_symlink() or not target.is_file() or _sha256_file(target) != sha:
            raise StoreConfigError(f"runtime snapshot mismatch: {target}")
        ensure_index_row(cfg.root, sha, byte_count, source_path, audit_id, round_label)
    except OSError as exc:
        raise StoreConfigError(f"refused CAS ingest {src} to {target}: {exc}") from exc
    return created


def ensure_index_row(root: Path, sha: str, byte_count: int, source_path: str,
                     audit_id: str, round_number: int) -> bool:
    """Ensure one row per blob under flock; refuse corrupt indexes or partial writes.

    Called for every enabled snapshot (not only by the creating process), so a blob whose first append failed gets its row
    on the next prepare instead of silently staying unindexed. Returns True when this call appended the row.
    """
    path = root / "index.tsv"
    utc = dt.datetime.now(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    fields = (sha, str(byte_count), source_path, utc, audit_id, str(round_number))
    if any(any(char in value for char in "\t\r\n\0") for value in fields):
        raise StoreConfigError(f"refused index row for {path}: TSV field contains a separator")
    header = ("\t".join(INDEX_HEADER) + "\n").encode("utf-8")
    row = ("\t".join(fields) + "\n").encode("utf-8")
    try:
        with path.open("a+b", buffering=0) as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                existing = fh.read()
                if existing and not existing.startswith(header):
                    raise StoreConfigError(f"refused index {path}: header mismatch")
                if existing and not existing.endswith(b"\n"):
                    raise StoreConfigError(f"refused index {path}: partial last line")
                for number, line in enumerate(existing.splitlines()[1:], 2):
                    parts = line.split(b"\t")
                    if len(parts) != len(INDEX_HEADER):
                        raise StoreConfigError(f"refused index {path}: line {number} has {len(parts)} fields")
                    if parts[0].decode("ascii", errors="replace") == sha:
                        return False
                payload = row if existing else header + row
                if os.write(fh.fileno(), payload) != len(payload):
                    raise StoreConfigError(f"refused index {path}: partial append")
                os.fsync(fh.fileno())
                return True
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise StoreConfigError(f"refused index append {path}: {exc}") from exc


def init_store(root: Path) -> None:
    """Initialise an explicitly selected root; refuse nonempty unmarked directories."""
    if not root.is_absolute():
        raise StoreConfigError(f"refused init root {root}: must be absolute")
    try:
        if root.exists():
            if not root.is_dir():
                raise StoreConfigError(f"refused init root {root}: not a directory")
            if any(root.iterdir()):
                read_store_id(root)
                return
        else:
            root.mkdir(parents=True)
        marker = root / "STORE_ID"
        try:
            with marker.open("xb") as fh:
                fh.write((STORE_ID + "\n").encode("ascii"))
                fh.flush()
                os.fsync(fh.fileno())
        except FileExistsError:
            read_store_id(root)
    except OSError as exc:
        raise StoreConfigError(f"refused init root {root}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """Expose explicit init/check; refuse failed configuration or store validation."""
    parser = argparse.ArgumentParser(description="Minsky runtime CAS host tools")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--root", type=Path, required=True)
    check = commands.add_parser("check")
    check.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    cfg = None
    try:
        if args.command == "init":
            init_store(args.root)
        else:
            cfg = load_store_config(args.config)
            validate_store(cfg)
            print(json.dumps({**cfg.receipt(), "validation": "ok" if cfg.enabled else "disabled"}))
        return 0
    except StoreConfigError as exc:
        resolved = cfg.receipt() if cfg is not None else {}
        print(json.dumps({**resolved, "validation": "error", "error": str(exc)}))
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
