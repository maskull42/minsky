"""Append-only lifecycle records; refuse invalid registers, unheld locks and unmet gates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REGISTER_PATH = Path("documentation/minsky_audit_lifecycle_register.tsv")
COLUMNS = (
    "utc", "audit_id", "audit_dir", "event", "state_after", "class",
    "citation_resolvability", "manifest_sha256", "pack_sha256", "copy_location",
    "verdict_json", "actor", "note", "lock_id",
)
HEADER = "\t".join(COLUMNS) + "\n"
EVENTS = frozenset((
    "census", "classify", "challenge", "ruling", "promote", "promotion-pending",
    "seal", "pack", "copy", "verify", "offload-intent", "offload", "rehydrate",
    "expire", "legacy-register", "correction",
))


class RegisterError(RuntimeError):
    """A register, lock or gate operation was refused."""


class LockHeld(RegisterError):
    """An O_EXCL lock already exists; its content is reported, never auto-cleared."""


@dataclass(frozen=True)
class Register:
    rows: list[dict[str, str]]
    line_numbers: list[int]
    acknowledged_lines: tuple[int, ...] = ()
    repo_root: Path | None = None


def git_read(repo_root: Path, *args: str) -> bytes:
    """Refuse a failed Git read; never replace failures with empty results."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(key, None)
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo_root), *args],
        env=env, capture_output=True,
    )
    if result.returncode:
        raise RegisterError(f"refused git {' '.join(args)} at {repo_root}: "
                            f"{result.stderr.decode('utf-8', errors='replace').strip()}")
    return result.stdout


def lock_directory(repo_root: Path) -> Path:
    """Resolve the shared lock directory; refuse invalid Git common-directory output."""
    output = git_read(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    value = output.decode("utf-8").rstrip("\n")
    if not value or "\n" in value or not Path(value).is_absolute():
        raise RegisterError(f"refused git-common-dir at {repo_root}: {value!r}")
    return Path(value) / "minsky-lifecycle-locks"


def _audit_id(audit_id: str) -> None:
    if (not isinstance(audit_id, str) or not audit_id or audit_id in (".", "..", "register.append")
            or any(char in audit_id for char in "/\\\t\r\n\0")):
        raise RegisterError(f"refused audit_id for lock filename: {audit_id!r}")


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLock:
    """Hold one exclusive lifecycle lock; refuse contention or replacement on release."""

    def __init__(self, repo_root: Path, path: Path, content: bytes, identity: tuple[int, int]) -> None:
        self.repo_root = repo_root.resolve()
        self.path = path
        self.content = content
        self.identity = identity
        self.lock_id = json.loads(content)["lock_id"]
        self.audit_id = json.loads(content)["audit_id"]
        self.held = True

    @classmethod
    def acquire(cls, repo_root: Path, audit_id: str, *, verb: str, session: str) -> AuditLock:
        """Refuse an existing per-audit lock, including stale locks."""
        _audit_id(audit_id)
        return cls._acquire(repo_root, audit_id + ".lock", audit_id, verb, session)

    @classmethod
    def _acquire(cls, repo_root: Path, name: str, audit_id: str,
                 verb: str, session: str) -> AuditLock:
        directory = lock_directory(repo_root)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        content = (json.dumps({
            "lock_id": uuid.uuid4().hex, "audit_id": audit_id, "verb": verb,
            "pid": os.getpid(), "host": socket.gethostname(), "session": session, "utc": _utc(),
        }, sort_keys=True) + "\n").encode("utf-8")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            try:
                existing = path.read_bytes()
            except FileNotFoundError:
                raise LockHeld(f"refused held lock {path}: disappeared while inspecting existing content") from exc
            raise LockHeld(f"refused held lock {path}: "
                           f"{existing.decode('utf-8', errors='replace').rstrip()}") from exc
        try:
            info = os.fstat(fd)
            if os.write(fd, content) != len(content):
                raise RegisterError(f"refused short write of lock {path}")
            os.fsync(fd)
        finally:
            os.close(fd)
        return cls(repo_root, path, content, (info.st_dev, info.st_ino))

    def check(self) -> None:
        """Refuse a released, missing or replaced lock before an operation."""
        if not self.held:
            raise RegisterError(f"refused released lock {self.path}")
        info = self.path.lstat()
        if (info.st_dev, info.st_ino) != self.identity or self.path.read_bytes() != self.content:
            raise RegisterError(f"refused replaced lock {self.path}")

    def release(self) -> None:
        """Refuse to unlink a lock whose content or inode has changed."""
        self.check()
        self.path.unlink()
        self.held = False

    def __enter__(self) -> AuditLock:
        self.check()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def _append_lock(repo_root: Path) -> AuditLock:
    # Bounded contention waits never break or replace a stale append lock.
    deadline = time.monotonic() + 10
    while True:
        try:
            return AuditLock._acquire(repo_root, "register.append.lock", "", "append", "")
        except LockHeld:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _row_defect(row: dict[str, str]) -> str | None:
    if row["event"] not in EVENTS:
        return f"unknown event {row['event']!r}"
    value = row["utc"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value):
        return f"utc is not ISO-8601 Z: {value!r}"
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return f"utc is not ISO-8601 Z: {value!r}"
    return None


def _parse(raw: bytes, path: Path, *, validate: bool) -> Register:
    lines = raw.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    defects = {}
    parsed = []
    numbers = []
    if not lines or lines[0] != HEADER.rstrip("\n").encode("utf-8"):
        defects[1] = "header mismatch"
    for number, line in enumerate(lines[1:], 2):
        try:
            text = line.decode("utf-8")
        except UnicodeError:
            defects[number] = "invalid UTF-8"
            continue
        fields = text.split("\t")
        if len(fields) != len(COLUMNS) or "\r" in text:
            defects[number] = "expected exactly 14 fields, UTF-8, LF"
            continue
        row = dict(zip(COLUMNS, fields))
        defect = _row_defect(row)
        if defect:
            defects[number] = defect
            continue
        parsed.append(row)
        numbers.append(number)
    if not raw.endswith(b"\n"):
        defects[max(1, len(lines))] = "file does not end with LF"
    acknowledged = set()
    for row, number in zip(parsed, numbers):
        if row["event"] == "correction":
            match = re.match(r"malformed-line:([1-9]\d*);", row["note"])
            if match and int(match[1]) < number:
                acknowledged.add(int(match[1]))
    # The header remains exact even when a correction acknowledges line 1.
    remaining = {n: reason for n, reason in defects.items() if n == 1 or n not in acknowledged}
    if validate and remaining:
        raise RegisterError("; ".join(f"refused register {path} line {n}: {reason}"
                                      for n, reason in sorted(remaining.items())))
    return Register(parsed, numbers, tuple(sorted(acknowledged)))


def read_register(repo_root: Path) -> Register:
    """Validate the entire register; refuse every unacknowledged malformed line."""
    path = repo_root / REGISTER_PATH
    if not path.resolve().is_relative_to(repo_root.resolve()):
        raise RegisterError(f"refused register {path}: resolves outside repo {repo_root}")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Register([], [], repo_root=repo_root.resolve())
    parsed = _parse(raw, path, validate=True)
    return Register(parsed.rows, parsed.line_numbers, parsed.acknowledged_lines, repo_root.resolve())


def _prepare_row(repo_root: Path, row: dict, lock: AuditLock | None) -> bytes:
    if set(row) != set(COLUMNS):
        raise RegisterError(f"refused register row keys: missing {sorted(set(COLUMNS) - set(row))}, "
                            f"extra {sorted(set(row) - set(COLUMNS))}")
    values = dict(row)
    for name, value in values.items():
        if not isinstance(value, str) or any(char in value for char in "\t\r\n\0"):
            raise RegisterError(f"refused register {name}: {value!r} contains a delimiter or is not text")
    values["utc"] = _utc()
    if lock is None:
        if values["event"] not in ("census", "correction"):
            raise RegisterError(f"refused {values['event']} for {values['audit_id']}: per-audit lock required")
        if values["lock_id"]:
            raise RegisterError(f"refused {values['event']} lock_id without a held lock")
    else:
        lock.check()
        if lock.repo_root != repo_root.resolve() or lock.audit_id != values["audit_id"]:
            raise RegisterError(f"refused register row for {values['audit_id']}: lock belongs to another audit/repo")
        if values["event"] in ("census", "correction"):
            if values["lock_id"]:
                raise RegisterError(f"refused {values['event']} lock_id: must be empty")
        else:
            if values["lock_id"] not in ("", lock.lock_id):
                raise RegisterError(f"refused register lock_id {values['lock_id']}: does not match held lock")
            values["lock_id"] = lock.lock_id
    defect = _row_defect(values)
    if defect:
        raise RegisterError(f"refused register row: {defect}")
    return ("\t".join(values[name] for name in COLUMNS) + "\n").encode("utf-8")


def _write_row(repo_root: Path, line: bytes, *, correction: bool = False) -> int:
    path = repo_root / REGISTER_PATH
    if not path.resolve().is_relative_to(repo_root.resolve()):
        raise RegisterError(f"refused register {path}: resolves outside repo {repo_root}")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
        absent = True
    else:
        absent = False
    if not absent and not correction:
        _parse(raw, path, validate=True)
    prefix = HEADER.encode("utf-8") if absent else (b"\n" if raw and not raw.endswith(b"\n") else b"")
    payload = prefix + line
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        if os.write(fd, payload) != len(payload):
            raise RegisterError(f"refused short register write {path}; append a correction")
        os.fsync(fd)
    finally:
        os.close(fd)
    return 2 if absent else raw.count(b"\n") + (1 if prefix else 0) + 1


def append_event(repo_root: Path, row: dict, *, lock: AuditLock | None) -> int:
    """Refuse invalid rows or registers; append one complete fsynced line under O_EXCL."""
    line = _prepare_row(repo_root, row, lock)
    with _append_lock(repo_root):
        if lock is not None:
            lock.check()
        return _write_row(repo_root, line)


def append_correction(repo_root: Path, *, malformed_line: int | None, reason: str, by: str) -> int:
    """Append without editing history; refuse missing actor/reason or invalid line numbers."""
    if not reason.strip() or not by.strip():
        raise RegisterError("refused correction: reason and by are required")
    if malformed_line is not None and (type(malformed_line) is not int or malformed_line < 1):
        raise RegisterError(f"refused malformed_line {malformed_line!r}")
    row = dict.fromkeys(COLUMNS, "")
    prefix = f"malformed-line:{malformed_line};" if malformed_line is not None else "correction;"
    row.update(event="correction", actor=by, note=f"{prefix} {reason}")
    line = _prepare_row(repo_root, row, None)
    with _append_lock(repo_root):
        return _write_row(repo_root, line, correction=True)


def current_state(register: Register, audit_id: str) -> dict | None:
    """Return the latest validated row; malformed files must first be refused by read_register."""
    return next((dict(row) for row in reversed(register.rows) if row["audit_id"] == audit_id), None)


def lock_status(repo_root: Path, audit_id: str | None = None) -> list[dict[str, str]]:
    """List lock bytes as text; refuse invalid audit ids or unreadable lock files."""
    read_register(repo_root)
    directory = lock_directory(repo_root)
    if audit_id is not None:
        _audit_id(audit_id)
        paths = [directory / (audit_id + ".lock")]
    else:
        paths = sorted(directory.glob("*.lock"))
    result = []
    for path in paths:
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            if audit_id is not None:
                continue
            raise RegisterError(f"refused lock-status: lock disappeared {path}")
        result.append({"path": str(path), "content": content.decode("utf-8", errors="replace")})
    return result


def clear_lock(repo_root: Path, audit_id: str, *, by: str, reason: str) -> int:
    """Clear one named lock and record its hash; refuse absent flags or changed lock bytes."""
    _audit_id(audit_id)
    if not by.strip() or not reason.strip():
        raise RegisterError(f"refused lock-clear {audit_id}: --by and --reason are required")
    path = lock_directory(repo_root) / (audit_id + ".lock")
    with _append_lock(repo_root):
        read_register(repo_root)
        content = path.read_bytes()
        info = path.lstat()
        row = dict.fromkeys(COLUMNS, "")
        row.update(audit_id=audit_id, event="correction", actor=by,
                   note=f"lock-cleared:{hashlib.sha256(content).hexdigest()}; {reason}")
        line = _prepare_row(repo_root, row, None)
        now = path.lstat()
        if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino) or path.read_bytes() != content:
            raise RegisterError(f"refused lock-clear: replaced lock {path}")
        path.unlink()
        return _write_row(repo_root, line)


def require_gate(name: str, *, audit_dir: Path, db_path: Path) -> None:
    """Refuse an unmet real-data gate; fixtures require both paths inside MINSKY_TEST_TMP."""
    if name not in ("G1", "G2", "G3"):
        raise RegisterError(f"refused unknown real-data gate {name!r}")
    value = os.environ.get("MINSKY_TEST_TMP")
    root = Path(value) if value is not None else Path.home() / "Library/Application Support/MARS/minsky-test-tmp"
    if not root.is_absolute() or not str(root) or value == "":
        raise RegisterError(f"refused {name}: MINSKY_TEST_TMP must be absolute: {value!r}")
    # A test root that contains the repository, the home directory or "/" would turn every real operation into a "fixture"
    # and silently bypass the gates (host fix, W8 review).
    resolved = root.resolve()
    if (SKILL_DIR.parents[2].resolve().is_relative_to(resolved) or Path.home().resolve().is_relative_to(resolved)
            or resolved == Path(resolved.anchor)):
        raise RegisterError(f"refused {name}: MINSKY_TEST_TMP {resolved} contains the repository or home directory")
    if (audit_dir.resolve().is_relative_to(root.resolve())
            and db_path.resolve().is_relative_to(root.resolve())):
        return
    path = SKILL_DIR / "config" / "real_data_gates.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RegisterError(f"refused gate {name} config {path}: {exc}") from exc
    gate = config.get(name) if isinstance(config, dict) else None
    if (not isinstance(config, dict) or config.get("schema") != "minsky-real-data-gates/1"
            or not isinstance(gate, dict) or gate.get("met") is not True
            or not isinstance(gate.get("evidence"), str) or not gate["evidence"].strip()):
        raise RegisterError(f"refused gate {name} for audit_dir {audit_dir} and db_path {db_path}: "
                            f"met:true and non-empty evidence required in {path}")
