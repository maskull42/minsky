"""Classify and seal audits; refuse live writers, unmet gates and unverifiable preservation."""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from contextlib import closing, redirect_stderr
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath

import jsonschema
import store
from byte_sources import RAW_MANIFEST_HEADER
from free_space import check_free_space, load_policy
from lifecycle_citations import (
    audit_relative, citation_resolvability, decisions_in, effective_class, file_records,
    findings_in, read_json, round_inputs,
)
from lifecycle_register import (
    AuditLock, COLUMNS, Register, RegisterError, SKILL_DIR, _append_lock,
    append_event, git_read, read_register, require_gate,
)
from progress import progress_path
from provenance import sha256_file
from round_verify import verify_round

EXCLUSIONS = frozenset((
    "lifecycle/raw_manifest.tsv", "lifecycle/digest.json", "lifecycle/DIGEST.md",
    "lifecycle/STUB.json", "lifecycle/TOMBSTONE.md", "lifecycle/offload_intent.json", "CAS_REFS.tsv",
))
STORE_INDEX = Path("documentation/minsky_store_index.tsv")
STORE_HEADER = ("sha256", "bytes", "audit_id", "audit_dir", "use_path", "class", "sealed_utc")
FREEZES = Path("documentation/minsky_audits_db_freezes.tsv")
RESERVED_FREEZE_PREFIX = "freeze."
FREEZE_HEADER = ("utc", "audit_id", "event", "location", "path", "bytes", "sha256", "integrity_check", "note")
CLASSES = ("policy-output", "raw-stream", "runtime-blob", "native-state", "dependency-cache", "lifecycle", "other")


def utc_now() -> str:
    """Return the record time; refuse use as generated digest content."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def glob_match(pattern: str, path: str) -> bool:
    """Match POSIX segments; refuse absolute or traversing paths and slash-crossing '*'."""
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    patterns, parts = pattern.split("/"), candidate.parts

    @lru_cache(None)
    def match(i: int, j: int) -> bool:
        if i == len(patterns):
            return j == len(parts)
        if patterns[i] == "**":
            return match(i + 1, j) or (j < len(parts) and match(i, j + 1))
        return j < len(parts) and fnmatch.fnmatchcase(parts[j], patterns[i]) and match(i + 1, j + 1)

    return match(0, 0)


def _field(value: str, label: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or any(char in value for char in "\t\r\n\0") or (nonempty and not value.strip()):
        raise RegisterError(f"refused {label}: empty or contains a TSV delimiter")
    return value


def _directory(repo: Path, directory: Path) -> Path:
    path = Path(os.path.abspath(repo / directory))
    if not path.is_relative_to(repo) or not path.is_dir() or not path.resolve().is_relative_to(repo):
        raise RegisterError(f"refused audit directory {path}: must be an existing directory inside {repo}")
    return path


def audit_record(db_path: Path, audit_id: str) -> dict:
    """Read one audit through SQLite mode=ro; refuse missing or duplicate identity."""
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM audits WHERE audit_id = ?", (audit_id,)).fetchall()
        if len(rows) != 1:
            raise RegisterError(f"refused audits database {db_path}: expected one audit {audit_id}")
        return dict(rows[0])
    except sqlite3.Error as exc:
        raise RegisterError(f"refused audits database {db_path} opened read-only: {exc}") from exc


def _audit_rows(register: Register, audit_id: str) -> list[tuple[int, dict]]:
    return [(number, row) for number, row in zip(register.line_numbers, register.rows) if row["audit_id"] == audit_id]


def _event(repo: Path, audit_id: str, directory: str, actor: str, event: str,
           state: str, *, lock: AuditLock, **values: str) -> None:
    row = dict.fromkeys(COLUMNS, "")
    row.update(audit_id=audit_id, audit_dir=directory, actor=actor, event=event, state_after=state, **values)
    append_event(repo, row, lock=lock)


def _normalize(path: str, repo: Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.relative_to(repo).as_posix()
        except ValueError:
            return path
    return PurePosixPath(path).as_posix()


def _work_log(path: Path, audit_id: str) -> tuple[list[str], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"trigger work-log:unknown {path}: {exc}"], "unknown"
    entries = re.split(r"(?m)(?=^### )", text)
    fired = []
    for entry in entries:
        if not entry.startswith("### "):
            continue
        lines = entry.splitlines()
        if audit_id in entry and any(
                line.startswith(("**Categories", "Categories"))
                and ("Methodology" in line or "Theoretical Framework" in line) for line in lines):
            fired.append(f"trigger work-log:{lines[0] if lines else ''}")
    return fired, "fired" if fired else "none"


def _volume_window(directory: Path, window_confirmed: str | None) -> None:
    """Refuse every volume link before audit reads; inspect only link and directory metadata."""
    links = []

    def walk(root: Path) -> None:
        for path in sorted(root.iterdir()):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = Path(os.path.realpath(path.parent / os.readlink(path)))
                if target.is_relative_to("/Volumes"):
                    links.append(path)
            elif stat.S_ISDIR(info.st_mode):
                walk(path)

    try:
        walk(directory)
    except OSError as exc:
        raise RegisterError(f"refused audit symlink inventory {directory}: {exc}") from exc
    if links and not window_confirmed:
        raise RegisterError("\n".join(f"refused symlink read through /Volumes/: {path}; --window-confirmed required"
                                      for path in sorted(links)))


def _classification_roots(directory: Path) -> list[Path]:
    roots = set()
    for path in _inventory(directory):
        if (path.parent == directory and fnmatch.fnmatchcase(path.name, "round-*")
                and stat.S_ISDIR(path.lstat().st_mode)):
            roots.add(path)
        if path.name in ("pack.xml", "claude-self", "codex", "opencode", "claude-synth", "round-scope.json"):
            roots.add(path.parent)
        if path.parent.name == "provenance" and path.name.endswith((".call.json", ".preflight.json")):
            roots.add(path.parent.parent)
    return sorted(roots)


def classification_inputs(repo: Path, directory: Path, audit: dict) -> list[dict]:
    """Collect modern and legacy triggers; refuse code-only by recording unparseable inputs as unknown."""
    inputs = []

    def add(origin: str, value: object) -> None:
        if not isinstance(value, str) or not value:
            raise RegisterError(f"refused classification input {origin}: expected a path")
        inputs.append({"origin": origin, "path": _normalize(value, repo)})

    def unknown(origin: str, exc: Exception) -> None:
        inputs.append({"origin": origin, "path": origin, "unknown": str(exc)})

    try:
        targets = json.loads(audit.get("files_audited") or "[]")
    except ValueError as exc:
        raise RegisterError(f"refused files_audited for {audit['audit_id']}: {exc}") from exc
    if not isinstance(targets, list):
        raise RegisterError(f"refused files_audited for {audit['audit_id']}: expected a list")
    for target in targets:
        add("audit-target", target)
    try:
        rounds = round_inputs(directory, audit["audit_id"])
    except RegisterError as exc:
        unknown("round-records", exc)
        rounds = []
    for info in rounds:
        for receipt in info["receipts"]:
            contexts = receipt["value"].get("referenced_context") or []
            if not isinstance(contexts, list):
                raise RegisterError(f"refused referenced_context {receipt['path']}: expected a list")
            for record in contexts:
                add(receipt["path"].relative_to(directory).as_posix(), record.get("path"))
    for root in sorted(set(_classification_roots(directory)) | {info["root"] for info in rounds}):
        info = {"root": root}
        label = root.relative_to(directory).as_posix()
        pack = root / "pack.xml"
        if pack.exists() or pack.is_symlink():
            # Binary placeholders remain trigger inputs, even though they cannot resolve quotations.
            try:
                for node in ET.fromstring(pack.read_bytes()).iter("file"):
                    add(label + "/pack.xml", node.get("path"))
            except (OSError, ET.ParseError, RegisterError) as exc:
                unknown(label + "/pack.xml", exc)
        for step, subdir in (("claude_self", "claude-self/findings"), ("codex", "codex"), ("opencode", "opencode")):
            for path in sorted((root / subdir).glob("*.json")):
                try:
                    doc = read_json(path)
                    if (not isinstance(doc, dict) or not isinstance(doc.get("findings"), list)
                            or doc.get("persona") != path.stem):
                        raise RegisterError(f"refused findings {path}: persona/findings mismatch")
                    for finding in doc["findings"]:
                        if not isinstance(finding, dict):
                            raise RegisterError(f"refused finding in {path}: expected an object")
                        ev = finding.get("evidence") or {}
                        if not isinstance(ev, dict):
                            raise RegisterError(f"refused finding evidence {label}/{step}/{path.stem}: expected an object")
                        if ev.get("file_path"):
                            add(f"{label}/{step}/{path.stem}", ev["file_path"])
                except RegisterError as exc:
                    unknown(path.relative_to(directory).as_posix(), exc)
        resolutions = root / "claude-synth/resolutions.json"
        if resolutions.exists() or resolutions.is_symlink():
            try:
                for row in decisions_in(info):
                    ev = row.get("evidence") or {}
                    if not isinstance(ev, dict):
                        raise RegisterError(f"refused decision evidence {resolutions}: expected an object")
                    if ev.get("file_path"):
                        add(label + "/decision-evidence", ev["file_path"])
            except RegisterError as exc:
                unknown(label + "/claude-synth/resolutions.json", exc)
    return sorted(inputs, key=lambda item: (item["path"], item["origin"]))


def _write_once(path: Path, payload: bytes, created: list[Path]) -> None:
    if path.exists() or path.is_symlink():
        raise RegisterError(f"refused existing seal output {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as fh:
        created.append(path)
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())


def classify(repo: Path, *, audit_id: str, audit_dir: Path, audits_db: Path, actor: str,
             requested_class: str | None = None, statement_file: Path | None = None,
             window_confirmed: str | None = None) -> None:
    """Refuse volume links, post-seal classification and code-only without a statement or required ruling."""
    repo = repo.resolve()
    _field(actor, "--actor", nonempty=True)
    with AuditLock.acquire(repo, audit_id, verb="classify", session=actor) as lock:
        directory = _directory(repo, audit_dir)
        if window_confirmed is not None:
            _field(window_confirmed, "--window-confirmed", nonempty=True)
        _volume_window(directory, window_confirmed)   # host fix: classify needs the same G4 option as seal
        register = read_register(repo)
        rows = _audit_rows(register, audit_id)
        if any(row["event"] == "seal" for _, row in rows):
            raise RegisterError(f"refused post-seal classification {audit_id}: use promote for "
                                "methodology-bearing upgrades; CAS ingest and second copy required first")
        audit = audit_record(audits_db, audit_id)
        patterns_path = SKILL_DIR / "config/methodology_triggers.txt"
        raw = patterns_path.read_bytes()
        patterns = [line.strip() for line in raw.decode("utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        inputs = classification_inputs(repo, directory, audit)
        triggers = []
        for item in inputs:
            item["globs"] = [pattern for pattern in patterns if glob_match(pattern, item["path"])]
            triggers.extend(f"trigger glob:{pattern} {item['path']}" for pattern in item["globs"])
            if "unknown" in item:
                triggers.append(f"trigger classification:unknown {item['origin']} {item['unknown']}")
        work_logs = []
        paths = [repo / "documentation/phd_work_log.md"]
        if audit.get("work_log_entry_path"):
            extra = repo / audit["work_log_entry_path"]
            if extra not in paths:
                paths.append(extra)
        for path in paths:
            fired, status = _work_log(path, audit_id)
            triggers.extend(fired)
            work_logs.append({"path": _normalize(str(path), repo), "result": status, "triggers": fired})
        last_ruling = max((i for i, (_, row) in enumerate(rows) if row["event"] == "ruling"), default=-1)
        triggers.extend(f"trigger challenge:{number}" for number, row in rows[last_ruling + 1:]
                        if row["event"] == "challenge")
        downgrade = next(((number, row) for number, row in reversed(rows)
                          if row["event"] == "ruling" and row["note"].startswith("downgrade:")), None)
        # A newer challenge still requires a later ruling.
        permitted = downgrade is not None and not any(row["event"] == "challenge"
                                                     for number, row in rows if number > downgrade[0])
        chosen = requested_class or "methodology-bearing"
        if chosen not in ("methodology-bearing", "code-only"):
            raise RegisterError(f"refused audit class {chosen}")
        prior_class = next((row["class"] for _, row in reversed(rows) if row["class"]), "")
        if chosen == "code-only":
            if triggers and not permitted:
                raise RegisterError("\n".join(sorted(set(triggers))))
            if prior_class == "methodology-bearing" and not permitted:
                raise RegisterError(f"refused code-only downgrade for {audit_id}: downgrade: ruling required")
            if statement_file is None:
                raise RegisterError(f"refused code-only classification {audit_id}: --statement-file required")
            statement = statement_file.read_bytes()
            if not statement.decode("utf-8").strip():
                raise RegisterError(f"refused code-only statement {statement_file}: empty")
        else:
            statement = None
        lifecycle = directory / "lifecycle"
        if lifecycle.is_symlink() or not lifecycle.resolve().is_relative_to(directory.resolve()):
            raise RegisterError(f"refused classification directory symlink {lifecycle}")
        path = lifecycle / "classification.md"
        if path.exists() or path.is_symlink():
            if chosen != "methodology-bearing" and not permitted:
                raise RegisterError(f"refused existing classification {path}: code-only reclassification requires a downgrade: ruling")
            n = 2
            while (lifecycle / f"classification.{n}.md").exists() or (lifecycle / f"classification.{n}.md").is_symlink():
                n += 1
            path = lifecycle / f"classification.{n}.md"
        utc = utc_now()
        text = (f"# Audit classification: {audit_id}\n\nclass: {chosen}\n"
                f"class_source: {'default (host did not assert code-only)' if requested_class is None else 'explicit'}\n"
                f"triggers_sha256: {hashlib.sha256(raw).hexdigest()}\nactor: {actor}\nutc: {utc}\n\n"
                "## Trigger inputs\n\n" + json.dumps(inputs, sort_keys=True, indent=2) + "\n\n"
                "## Fired triggers\n\n" + json.dumps(sorted(set(triggers)), indent=2) + "\n\n"
                "## Work log\n\n" + json.dumps(work_logs, sort_keys=True, indent=2) + "\n\n"
                f"titan: unknown-no-sanctioned-read\ntitan_log_id: {audit.get('titan_log_id') or ''}\n")
        if permitted and chosen == "code-only":
            text += "\n## Researcher ruling (verbatim register row)\n\n> " + "\t".join(downgrade[1][k] for k in COLUMNS) + "\n"
        if statement is not None:
            text += f"\nstatement_sha256: {hashlib.sha256(statement).hexdigest()}\n\n## Host statement (verbatim)\n\n"
            text += statement.decode("utf-8")
        created = []
        try:
            _write_once(path, text.encode("utf-8"), created)
            state = rows[-1][1]["state_after"] if rows else "finished" if audit.get("finished_at") else "open"
            _event(repo, audit_id, directory.relative_to(repo).as_posix(), actor, "classify", state,
                   lock=lock, **{"class": chosen, "note": path.relative_to(directory).as_posix()})
        except BaseException:
            for item in reversed(created):
                item.unlink()
            raise


def challenge(repo: Path, *, audit_id: str, by: str, reason: str, actor: str) -> None:
    """Append a challenge under the audit lock; refuse missing challenger, reason or actor."""
    repo = repo.resolve()
    for label, value in (("--by", by), ("--reason", reason), ("--actor", actor)):
        _field(value, label, nonempty=True)
    with AuditLock.acquire(repo, audit_id, verb="challenge", session=actor) as lock:
        rows = _audit_rows(read_register(repo), audit_id)
        prior = rows[-1][1] if rows else {}
        _event(repo, audit_id, prior.get("audit_dir", ""), actor, "challenge", prior.get("state_after", "open"),
               lock=lock, **{"class": "methodology-bearing", "note": f"by:{by}; reason:{reason}"})


def _inventory(directory: Path) -> list[Path]:
    paths = []

    def walk(root: Path) -> None:
        for path in sorted(root.iterdir()):
            relative = path.relative_to(directory).as_posix()
            if relative in EXCLUSIONS:
                continue
            paths.append(path)
            if stat.S_ISDIR(path.lstat().st_mode):
                walk(path)

    walk(directory)
    return sorted(paths, key=lambda path: path.relative_to(directory).as_posix())


def _layouts(rounds: list[dict], directory: Path) -> tuple[list[tuple[str, str]], list[str]]:
    declarations, used = [], []
    schema = read_json(SKILL_DIR / "schemas/round-layout.schema.json")
    for info in rounds:
        paths = [info["root"] / name for name in ("round-layout.json", "round-layout.retro.json")]
        present = [path for path in paths if path.exists() or path.is_symlink()]
        if len(present) > 1:
            raise RegisterError(f"refused ambiguous round layouts: {present}")
        if not present:
            continue
        path = present[0]
        value = read_json(path)
        try:
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)
        except jsonschema.ValidationError as exc:
            raise RegisterError(f"refused round layout {path}: {exc.message}") from exc
        if value["retrospective"] != (path.name == "round-layout.retro.json"):
            raise RegisterError(f"refused round layout {path}: retrospective/filename mismatch")
        used.append(path.relative_to(directory).as_posix())
        seen = set()
        for row in value["raw_directories"]:
            prefix = (info["root"].relative_to(directory) / row["path"]).as_posix().rstrip("/")
            if any(prefix == prior or prefix.startswith(prior + "/") or prior.startswith(prefix + "/") for prior in seen):
                raise RegisterError(f"refused round layout {path}: overlapping raw directory {row['path']}")
            seen.add(prefix)
            declarations.append((prefix, row["class"]))
    return declarations, sorted(used)


def _class(path: str, tracked: bool, declarations: list[tuple[str, str]]) -> str:
    parts = PurePosixPath(path).parts
    if parts[0] == "lifecycle":
        return "lifecycle"
    for prefix, declared in declarations:
        if path == prefix or path.startswith(prefix + "/"):
            return declared
    if any(parts[i:i + 2] == ("provenance", "runtime-blobs") for i in range(len(parts) - 1)):
        return "runtime-blob"
    name = parts[-1]
    if (any(part in ("_codex_logs", "_opencode_logs", "_chain_logs") for part in parts)
            or fnmatch.fnmatchcase(name, "chain*.log") or name.endswith((".stdout.log", ".stderr.log"))
            or name == "progress.ndjson"):
        return "raw-stream"
    if "native-calls" in parts:
        return "native-state"
    if "node_modules" in parts:
        return "dependency-cache"
    return "policy-output" if tracked else "other"


def raw_manifest(repo: Path, audit_dir: Path, *, audit_id: str,
                 window_confirmed: str | None = None) -> tuple[list[dict], list[str]]:
    """Freeze and hash the input inventory; refuse special files and unconfirmed volume reads."""
    repo = repo.resolve()
    directory = _directory(repo, audit_dir)
    _volume_window(directory, window_confirmed)
    inventory = _inventory(directory)
    rounds = round_inputs(directory, audit_id)
    declarations, layouts = _layouts(rounds, directory)
    tracked = set(git_read(repo, "--literal-pathspecs", "ls-files", "-z", "--",
                           directory.relative_to(repo).as_posix()).decode("utf-8").split("\0"))
    bound, recorded_paths = set(), set()
    for info in rounds:
        for receipt in info["receipts"]:
            for record in file_records(receipt["value"]):
                recorded_paths.add(record["path"])
                relative = audit_relative(record["path"], directory, info)
                if relative:
                    bound.add(relative)
    for path in inventory:
        if path.is_symlink():
            resolved = PurePosixPath(str(path.resolve()))
            for recorded in recorded_paths:
                try:
                    suffix = PurePosixPath(recorded).relative_to(resolved)
                except ValueError:
                    continue
                bound.add((path.relative_to(directory) / suffix).as_posix())
    rows, hardlinks = [], defaultdict(list)
    for path in inventory:
        relative = path.relative_to(directory).as_posix()
        info = path.lstat()
        row = dict.fromkeys(RAW_MANIFEST_HEADER, "")
        tracked_file = path.relative_to(repo).as_posix() in tracked
        row.update(path=relative, git_tracked=str(int(tracked_file)),
                   hash_bound=str(int(relative in bound)), **{"class": _class(relative, tracked_file, declarations)})
        content = None
        if stat.S_ISDIR(info.st_mode):
            row["type"] = "dir"
        elif stat.S_ISREG(info.st_mode):
            row["type"] = "file"
            content = path
            if info.st_nlink > 1:
                hardlinks[(info.st_dev, info.st_ino)].append(row)
        elif stat.S_ISLNK(info.st_mode):
            row.update(type="symlink", symlink_target=os.readlink(path), symlink_map=str(path.resolve()))
            if not path.resolve().is_relative_to(directory.resolve()):
                content = path.resolve()
                if content.is_relative_to("/Volumes") and not window_confirmed:
                    raise RegisterError(f"refused symlink hash through /Volumes/: {path}; --window-confirmed required")
                if not content.is_file():
                    raise RegisterError(f"refused external symlink content {path}: not a regular file {content}")
        else:
            raise RegisterError(f"refused raw manifest special file {path}")
        if content is not None:
            before = content.stat()
            sha = sha256_file(content)
            after = content.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) != (
                    after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev):
                raise RegisterError(f"refused raw manifest file changed while hashing {content}")
            row.update(bytes=str(after.st_size), sha256=sha, mtime_ns=str(after.st_mtime_ns),
                       mode=f"{stat.S_IMODE(after.st_mode):04o}")
        for key, value in row.items():
            _field(value, f"raw manifest {relative} {key}")
        rows.append(row)
    groups = sorted((group for group in hardlinks.values() if len(group) > 1), key=lambda group: group[0]["path"])
    for n, group in enumerate(groups, 1):
        for row in group:
            row["hardlink_group"] = f"h{n}"
    return rows, layouts


def _tsv(header: tuple[str, ...], rows: list[dict]) -> bytes:
    for row in rows:
        for key in header:
            _field(row[key], key)
    return ("\t".join(header) + "\n" + "".join("\t".join(row[key] for key in header) + "\n" for row in rows)).encode("utf-8")


def _read_table(repo: Path, relative: Path, header: tuple[str, ...]) -> list[dict]:
    path = repo / relative
    if not path.resolve().is_relative_to(repo):
        raise RegisterError(f"refused TSV {path}: resolves outside repo")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    text = raw.decode("utf-8")
    lines = text.split("\n")
    if not text.endswith("\n") or "\r" in text or lines[0] != "\t".join(header):
        raise RegisterError(f"refused TSV {path}: header/LF mismatch")
    rows = []
    for number, line in enumerate(lines[1:-1], 2):
        fields = line.split("\t")
        if len(fields) != len(header):
            raise RegisterError(f"refused TSV {path} line {number}: field count")
        rows.append(dict(zip(header, fields)))
    return rows


def _append_table(repo: Path, relative: Path, header: tuple[str, ...], rows: list[dict], lock: AuditLock) -> None:
    if not rows:
        return
    with _append_lock(repo):
        lock.check()
        _read_table(repo, relative, header)
        path = repo / relative
        payload = _tsv(header, rows)
        if path.exists():
            payload = payload.split(b"\n", 1)[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            if os.write(fd, payload) != len(payload):
                raise RegisterError(f"refused partial TSV append {path}")
            os.fsync(fd)
        finally:
            os.close(fd)


def _lsof_holders(output: str, target: str) -> dict[int, list[dict[str, str]]]:
    """Parse process/fd fields; refuse empty, orphaned, duplicate or malformed records."""
    holders = {}
    pid, fd = None, None
    for line in output.splitlines():
        field, value = line[:1], line[1:]
        if field == "p" and re.fullmatch(r"[1-9][0-9]*", value):
            pid, fd = int(value), None
            if pid in holders:
                raise RegisterError(f"refused lsof {target}: duplicate pid record {line!r}")
            holders[pid] = []
        elif field == "f" and pid is not None and re.fullmatch(r"[A-Za-z0-9]+", value):
            if any(item["f"] == value for item in holders[pid]):
                raise RegisterError(f"refused lsof {target}: duplicate fd record {line!r}")
            fd = {"f": value}
            holders[pid].append(fd)
        elif field in ("a", "n") and fd is not None and field not in fd:
            if field == "a" and value not in ("r", "w", "u", "", " "):
                raise RegisterError(f"refused lsof {target}: unparseable access record {line!r}")
            fd[field] = value
        else:
            raise RegisterError(f"refused lsof {target}: unparseable record {line!r}")
    if not holders:
        raise RegisterError(f"refused lsof {target}: no process records")
    return holders


def _lsof_executable(pid: int) -> str | None:
    """Read only the first txt name; refuse to confirm failed or absent executable evidence."""
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "txt", "-F", "n"],
                                capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode not in (0, 1) or result.stderr:
        return None
    return next((line[1:] or None for line in result.stdout.splitlines() if line.startswith("n")), None)


def _lsof(args: list[str], *, allow_self: bool = False) -> None:
    """Refuse other holders and malformed evidence; bound retries for unconfirmed Spotlight holders."""
    logger = logging.getLogger("lifecycle_seal")
    spotlight = (
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/Metadata.framework/Versions/A/Support/mdworker_shared",
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/Metadata.framework/Versions/A/Support/mds",
    )
    target = ' '.join(args)
    for attempt in range(4):
        try:
            result = subprocess.run(["lsof", "-F", "pan", *args], capture_output=True, text=True)
        except OSError as exc:
            raise RegisterError(f"refused lsof {target}: {exc}") from exc
        if result.returncode not in (0, 1) or result.stderr:
            raise RegisterError(f"refused lsof {target}: status {result.returncode}; {result.stderr.strip()}")
        if result.returncode == 1 and not result.stdout:
            return
        holders = _lsof_holders(result.stdout, target)
        pids = list(holders)
        refusal = f"refused live writer: lsof {target} lists processes {pids}"
        readonly, unconfirmed = [], []
        for pid, fds in holders.items():
            if allow_self and pid == os.getpid():
                continue
            executable = _lsof_executable(pid)
            if executable is not None and executable not in spotlight:
                raise RegisterError(refusal)
            if executable in spotlight and fds and all(fd.get("a") == "r" for fd in fds):
                readonly.append((pid, executable, fds))
            else:
                unconfirmed.append(pid)
        if not unconfirmed:
            for pid, executable, fds in readonly:
                logger.warning("Spotlight read-only holder pid=%s executable=%s fds=%s target=%s",
                               pid, executable, fds, target)
            return
        if attempt == 3:
            raise RegisterError(refusal + " (Spotlight holder not confirmed read-only after 3 re-checks)")
        logger.warning("Spotlight holder re-check %s/3 pid=%s target=%s", attempt + 1, unconfirmed, target)
        time.sleep(0.5)


def _seal_preconditions(repo: Path, directory: Path, audit: dict, db_path: Path, register: Register,
                        *, audit_close_override: str | None, legacy_registration: str | None) -> None:
    rows = _audit_rows(register, audit["audit_id"])
    if not any(row["event"] == "classify" for _, row in rows):
        raise RegisterError(f"refused seal {audit['audit_id']}: classify row required")
    if not audit.get("finished_at") and not legacy_registration:
        raise RegisterError(f"refused seal {audit['audit_id']}: finished_at or --legacy-registration required")
    census = next(((number, row) for number, row in reversed(rows) if row["event"] == "census"), None)
    if census and census[1]["state_after"] in ("legacy-preserved", "deferred") and not any(
            number > census[0] and row["event"] == "ruling" and row["note"].startswith("legacy-migrate:")
            for number, row in rows):
        raise RegisterError(f"refused seal {audit['audit_id']}: census {census[1]['state_after']} requires later legacy-migrate: ruling")
    for path in _inventory(directory):
        if fnmatch.fnmatchcase(path.name, "chain*.pid"):
            try:
                pid = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError) as exc:
                raise RegisterError(f"refused chain pid file {path}: {exc}") from exc
            if pid < 1:
                raise RegisterError(f"refused chain pid file {path}: pid must be positive")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            raise RegisterError(f"refused live chain writer {path}: pid {pid}")
    _lsof(["+D", str(directory)])
    # Same rule as audit-db.py (W3a): env var > default DB uses the default root (codex-audits/) > --db-derived root.
    if "MINSKY_PROGRESS_ROOT" in os.environ:
        root = Path(os.environ["MINSKY_PROGRESS_ROOT"])
    elif db_path.resolve() == (repo / ".minsky" / "audits.db").resolve():
        root = None   # host fix: a real seal reads codex-audits/<id>/progress.ndjson, where real audits write it
    else:
        root = db_path.resolve().parent / "progress"
    progress = progress_path(audit["audit_id"], root=root)
    try:
        lines = [line for line in progress.read_text(encoding="utf-8").splitlines() if line.strip()]
        closed = bool(lines) and json.loads(lines[-1]).get("event") == "audit_close"
    except (OSError, ValueError, AttributeError) as exc:
        if not audit_close_override:
            raise RegisterError(f"refused last progress audit_close {progress}: {exc}") from exc
        closed = False
    if not closed and not audit_close_override:
        raise RegisterError(f"refused last progress event {progress}: audit_close required")
    status = git_read(repo, "--literal-pathspecs", "status", "--porcelain=v1", "-z", "--",
                      directory.relative_to(repo).as_posix()).decode("utf-8")
    if any(item and not item.startswith("?? ") for item in status.split("\0")):
        raise RegisterError(f"refused uncommitted tracked audit file {directory}: {status.replace(chr(0), '; ')}")
    require_gate("G1", audit_dir=directory, db_path=db_path)
    lifecycle = directory / "lifecycle"
    outputs = [lifecycle / name for name in ("raw_manifest.tsv", "digest.json", "DIGEST.md", "CONTROLS.tsv", "verdicts")]
    for path in outputs:
        if path.exists() or path.is_symlink():
            raise RegisterError(f"refused existing seal output {path}")
    if lifecycle.is_symlink():
        raise RegisterError(f"refused lifecycle output directory symlink {lifecycle}")


def _copy_checked(source: Path, destination: Path, sha: str) -> None:
    if destination.exists() or destination.is_symlink():
        raise RegisterError(f"refused existing copy destination {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            created = True
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if sha256_file(destination) != sha:
            raise RegisterError(f"refused copy hash mismatch {destination}")
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def _integrity(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise RegisterError(f"refused freeze integrity_check {path}: {exc}") from exc
    if result != [("ok",)]:
        raise RegisterError(f"refused freeze integrity_check {path}: {result}")


def _device(path: Path) -> int:
    current = path.resolve()
    while not current.exists():
        current = current.parent
    if not current.is_dir():
        raise RegisterError(f"refused freeze destination parent {current}: not a directory")
    return current.stat().st_dev


def freeze_database(repo: Path, db_path: Path, *, audit_id: str, freeze_dir: Path,
                    freeze_copy: Path, utc: str, window_confirmed: str | None, lock: AuditLock,
                    kind: str = "seal") -> dict:
    """Back up and replicate SQLite; refuse busy databases, reused paths or changed copies."""
    if kind not in ("seal", "freeze"):
        raise RegisterError(f"refused freeze kind {kind!r}")
    _lsof([str(db_path.resolve())], allow_self=True)
    if freeze_copy.resolve().is_relative_to("/Volumes") and not window_confirmed:
        raise RegisterError(f"refused freeze copy under /Volumes/ {freeze_copy}: --window-confirmed required")
    test_root = Path(os.environ.get("MINSKY_TEST_TMP", str(Path.home() / "Library/Application Support/MARS/minsky-test-tmp")))
    fixture = (test_root.is_absolute() and test_root.resolve() != Path(test_root.anchor)
               and not Path.home().resolve().is_relative_to(test_root.resolve())
               and not SKILL_DIR.parents[2].resolve().is_relative_to(test_root.resolve())
               and db_path.resolve().is_relative_to(test_root.resolve()))
    if not fixture:
        if _device(freeze_dir) != Path.home().stat().st_dev:
            raise RegisterError(f"refused boot freeze destination {freeze_dir}: not on the boot data device")
        if _device(freeze_copy) == _device(freeze_dir):
            raise RegisterError(f"refused off-boot freeze-copy {freeze_copy}: same device as {freeze_dir}")
    compact = utc.replace("-", "").replace(":", "")
    name = f"{compact}_{audit_id}_{kind}.db"
    boot, copy = freeze_dir / name, freeze_copy / name
    if boot.resolve() == copy.resolve():
        raise RegisterError(f"refused freeze-copy {copy}: same path as boot freeze")
    for path in (boot, copy):
        if path.exists() or path.is_symlink():
            raise RegisterError(f"refused existing database freeze {path}")
    _read_table(repo, FREEZES, FREEZE_HEADER)
    freeze_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(boot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(boot)) as dst:
                src.backup(dst)
        with boot.open("rb") as fh:
            os.fsync(fh.fileno())
        _integrity(boot)
        sha, byte_count = sha256_file(boot), boot.stat().st_size
        _copy_checked(boot, copy, sha)
        _integrity(copy)
        rows = [{"utc": utc, "audit_id": audit_id, "event": event, "location": location,
                 "path": str(path.resolve()), "bytes": str(byte_count), "sha256": sha,
                 "integrity_check": "ok", "note": f"window-confirmed:{window_confirmed}" if window_confirmed else ""}
                for event, location, path in (("freeze", "boot", boot), ("freeze-copy", "destination", copy))]
        _append_table(repo, FREEZES, FREEZE_HEADER, rows, lock)
        if not any(row["event"] == "freeze-copy" and row["audit_id"] == audit_id and row["sha256"] == sha
                   for row in _read_table(repo, FREEZES, FREEZE_HEADER)):
            raise RegisterError(f"refused seal {audit_id}: freeze-copy coverage row missing")
        return {"sha256": sha, "bytes": byte_count}
    except BaseException:
        # A durable freezes row is append-only evidence; never delete a file it records.
        recorded = {row["path"] for row in _read_table(repo, FREEZES, FREEZE_HEADER)}
        for path in (boot, copy):
            if str(path.resolve()) not in recorded:
                path.unlink(missing_ok=True)
        raise


def freeze(repo: Path, *, reason: str, label: str | None, audits_db: Path, freeze_dir: Path,
           freeze_copy: Path, actor: str, window_confirmed: str | None) -> dict:
    """Refuse invalid identities, hot journals, audit collisions and ambiguous freeze ledger rows."""
    repo = repo.resolve()
    _field(actor, "--actor", nonempty=True)
    if reason == "ad-b":
        if label is not None:
            raise RegisterError(f"refused --label {label!r} for --reason {reason!r}")
        freeze_id = RESERVED_FREEZE_PREFIX + "AD-B"
    elif reason == "campaign-close":
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", label) or label in (".", ".."):
            raise RegisterError(f"refused --label {label!r} for --reason {reason!r}")
        freeze_id = RESERVED_FREEZE_PREFIX + f"campaign-close.{label}"
    else:
        raise RegisterError(f"refused --reason {reason!r}")
    for base in dict.fromkeys((str(audits_db), str(audits_db.resolve()))):
        journal, wal = Path(base + "-journal"), Path(base + "-wal")
        if journal.exists():
            raise RegisterError(f"refused hot journal {journal}")
        if wal.exists() and wal.stat().st_size > 0:
            raise RegisterError(f"refused hot journal {wal}")
    try:
        with closing(sqlite3.connect(audits_db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            collision = conn.execute("SELECT 1 FROM audits WHERE audit_id = ?", (freeze_id,)).fetchone()
    except sqlite3.Error as exc:
        raise RegisterError(f"refused audits database {audits_db} opened read-only: {exc}") from exc
    if collision is not None:
        raise RegisterError(f"refused freeze audit_id collision {freeze_id!r} in audits database {audits_db}")
    if any(row["audit_id"] == freeze_id for row in read_register(repo).rows):
        raise RegisterError(f"refused freeze audit_id collision {freeze_id!r} in lifecycle register")
    with AuditLock.acquire(repo, freeze_id, verb="freeze", session=actor) as lock:
        # An unchanged database backs up to identical bytes, so a repeated freeze shares its sha256; utc identifies the rows.
        utc = utc_now()
        result = freeze_database(repo, audits_db, audit_id=freeze_id, freeze_dir=freeze_dir,
                                 freeze_copy=freeze_copy, utc=utc, window_confirmed=window_confirmed,
                                 lock=lock, kind="freeze")
        rows = _read_table(repo, FREEZES, FREEZE_HEADER)
        numbers = {}
        for event, key in (("freeze", "freeze_row"), ("freeze-copy", "freeze_copy_row")):
            matches = [number for number, row in enumerate(rows, 2)
                       if row["audit_id"] == freeze_id and row["event"] == event
                       and row["sha256"] == result["sha256"] and row["utc"] == utc]
            if len(matches) != 1:
                raise RegisterError(f"refused freeze ledger {repo / FREEZES}: {freeze_id!r} {event} utc:{utc} "
                                    f"sha256:{result['sha256']} expected one row, found {len(matches)}")
            numbers[key] = matches[0]
        return {"audit_id": freeze_id, "sha256": result["sha256"], "bytes": result["bytes"], **numbers}


def freeze_restore_check(repo: Path, *, freeze_row: int, scratch: Path, actor: str,
                         window_confirmed: str | None = None) -> None:
    """Refuse invalid freezes, changed bytes or unconfirmed /Volumes/ restore reads and writes."""
    repo = repo.resolve()
    _field(actor, "--actor", nonempty=True)
    if window_confirmed is not None:
        _field(window_confirmed, "--window-confirmed", nonempty=True)
    rows = _read_table(repo, FREEZES, FREEZE_HEADER)
    if freeze_row < 2 or freeze_row > len(rows) + 1:
        raise RegisterError(f"refused --freeze-row {freeze_row}: not a data line in {repo / FREEZES}")
    audit_id = rows[freeze_row - 2]["audit_id"]
    with AuditLock.acquire(repo, audit_id, verb="freeze-restore-check", session=actor) as lock:
        row = _read_table(repo, FREEZES, FREEZE_HEADER)[freeze_row - 2]
        if row["event"] not in ("freeze", "freeze-copy"):
            raise RegisterError(f"refused --freeze-row {freeze_row}: event {row['event']}")
        source = Path(row["path"])
        if not source.is_absolute():
            raise RegisterError(f"refused freeze source {source}: absolute regular file required")
        if (source.is_relative_to("/Volumes") or source.resolve().is_relative_to("/Volumes")) and not window_confirmed:
            raise RegisterError(f"refused freeze restore source through /Volumes/ {source}: G4 window confirmation "
                                "--window-confirmed required")
        if (scratch.is_relative_to("/Volumes") or scratch.resolve().is_relative_to("/Volumes")) and not window_confirmed:
            raise RegisterError(f"refused freeze restore scratch through /Volumes/ {scratch}: --window-confirmed required")
        if source.is_symlink():
            raise RegisterError(f"refused freeze source {source}: absolute regular file required")
        if sha256_file(source) != row["sha256"] or str(source.stat().st_size) != row["bytes"]:
            raise RegisterError(f"refused freeze source hash/bytes {source}")
        destination = scratch / source.name
        _copy_checked(source, destination, row["sha256"])
        try:
            _integrity(destination)
            _append_table(repo, FREEZES, FREEZE_HEADER, [{**row, "utc": utc_now(), "event": "restore-demo",
                          "location": "scratch", "path": str(destination.resolve()),
                          "note": f"freeze-row:{freeze_row}; actor:{actor}" +
                          (f"; window:{window_confirmed}" if window_confirmed else "")}], lock)
            if not audit_id.startswith(RESERVED_FREEZE_PREFIX):
                register_rows = _audit_rows(read_register(repo), audit_id)
                prior = register_rows[-1][1] if register_rows else {}
                _event(repo, audit_id, prior.get("audit_dir", ""), actor, "verify", prior.get("state_after", ""),
                       lock=lock, note=f"freeze-restore-check:{freeze_row}; sha256:{row['sha256']}")
        finally:
            destination.unlink()


def _ingest_plan(directory: Path, rows: list[dict], rounds: list[dict], audit_class: str,
                 cfg: store.StoreConfig, window_confirmed: str | None) -> tuple[list[dict], list[dict]]:
    used, drifted = {}, []

    def add(src: Path, sha: str, use_path: str, blob_class: str, round_label: str) -> None:
        target = store.cas_path(cfg.root, sha)
        if src.resolve().is_relative_to("/Volumes") and not window_confirmed:
            raise RegisterError(f"refused CAS ingest source through /Volumes/ {src}: --window-confirmed required")
        if target.is_symlink():
            raise RegisterError(f"refused CAS blob symlink {target}")
        if target.exists():
            if not target.is_file() or sha256_file(target) != sha:
                raise RegisterError(f"refused CAS blob hash {target}")
            byte_count = target.stat().st_size
        else:
            if not src.is_file() or sha256_file(src) != sha:
                raise RegisterError(f"refused CAS ingest source hash {src}: expected {sha}")
            byte_count = src.stat().st_size
        key = (sha, use_path, blob_class)
        used[key] = {"src": src, "sha256": sha, "bytes": byte_count, "use_path": use_path,
                     "class": blob_class, "round_label": round_label, "missing": not target.exists()}

    for row in rows:
        if row["sha256"] and (row["class"] == "runtime-blob"
                               or audit_class == "methodology-bearing" and row["hash_bound"] == "1"):
            add(directory / row["path"], row["sha256"], row["path"], row["class"], row["path"].split("/", 1)[0])
    for info in rounds:
        label = info["root"].relative_to(directory).as_posix()
        for receipt in info["receipts"]:
            value = receipt["value"]
            for record in value.get("runtime_snapshots") or []:
                sha = record.get("sha256")
                source_path = record.get("path")
                if not isinstance(source_path, str) or not source_path:
                    raise RegisterError(f"refused runtime_snapshots path {receipt['path']}")
                relative = audit_relative(source_path, directory, info)
                source = directory / relative if relative else Path(source_path)
                add(source, sha, relative or f"{label}/provenance/runtime-blobs/{sha}", "runtime-blob", label)
            if audit_class != "methodology-bearing":
                continue
            for record in value.get("referenced_context") or []:
                path = record.get("path")
                sha = record.get("sha256")
                if not isinstance(path, str) or not path:
                    raise RegisterError(f"refused referenced_context path {receipt['path']}")
                if audit_relative(path, directory, info):
                    continue
                source = Path(path)
                if not source.is_absolute():
                    raise RegisterError(f"refused external referenced_context {path}: absolute recorded path required")
                if source.resolve().is_relative_to("/Volumes") and not window_confirmed:
                    raise RegisterError(f"refused external context through /Volumes/ {path}: --window-confirmed required")
                target = store.cas_path(cfg.root, sha)
                if source.is_file() and sha256_file(source) == sha:
                    add(source, sha, path, "other", label)
                else:
                    drifted.append({"round": label, "path": path, "sha256": sha,
                                    "status": "context-drifted-not-ingested"})
                    if target.exists():
                        add(source, sha, path, "other", label)
    return [used[key] for key in sorted(used)], sorted(drifted, key=lambda item: (item["round"], item["path"], item["sha256"]))


def _ingest(repo: Path, directory: Path, audit_id: str, used: list[dict], cfg: store.StoreConfig,
            utc: str, lock: AuditLock) -> int:
    incoming = {}
    for item in used:
        if item["missing"]:
            incoming[item["sha256"]] = item["bytes"]
    policy = load_policy()   # the skill's own policy and production floor, as `prepare` uses (host fix)
    cfg.free_space_checks.append(check_free_space(cfg.root, sum(incoming.values()), policy))
    for item in used:
        lock.check()
        store.ingest_file(cfg, item["src"], item["sha256"], byte_count=item["bytes"], audit_id=audit_id,
                          round_label=item["round_label"], source_path=item["use_path"])
        target = store.cas_path(cfg.root, item["sha256"])
        if target.is_symlink() or sha256_file(target) != item["sha256"] or target.stat().st_size != item["bytes"]:
            raise RegisterError(f"refused CAS blob after ingest {target}")
    distinct = {}
    for item in used:
        distinct.setdefault(item["sha256"], item)
    index = [{"sha256": item["sha256"], "bytes": str(item["bytes"]), "audit_id": audit_id,
              "audit_dir": directory.relative_to(repo).as_posix(), "use_path": item["use_path"],
              "class": item["class"], "sealed_utc": utc} for item in distinct.values()]
    _append_table(repo, STORE_INDEX, STORE_HEADER, index, lock)
    return len({item["sha256"] for item in used})


def _portable(value: object, repo: Path, directory: Path, rounds: list[dict]) -> object:
    prefixes = {str(repo): "", str(directory): ""}
    for info in rounds:
        for receipt in info["receipts"]:
            path = (receipt["value"].get("round_scope") or {}).get("path")
            if isinstance(path, str) and Path(path).is_absolute():
                prefixes[str(Path(path).parent.parent)] = ""
    ordered = sorted(prefixes, key=len, reverse=True)

    def convert(item: object) -> object:
        if isinstance(item, str):
            for prefix in ordered:
                item = item.replace(prefix + "/", "").replace(prefix, ".")
            return item
        if isinstance(item, dict):
            return {key: convert(child) for key, child in sorted(item.items())}
        if isinstance(item, list):
            converted = [convert(child) for child in item]
            return sorted(converted, key=lambda child: json.dumps(child, sort_keys=True))
        return item

    return convert(value)


def build_digest(repo: Path, directory: Path, audit: dict, rounds: list[dict], rows: list[dict],
                 *, audit_class: str, citations: dict, verdicts: list[dict], layouts: list[str],
                 drifted: list[dict], overrides: dict) -> dict:
    """Generate only from recorded inputs; refuse wall-clock or machine-root additions."""
    targets = json.loads(audit.get("files_audited") or "[]")
    records = [record for info in rounds for receipt in info["receipts"] for record in file_records(receipt["value"])]
    identities = []
    for target in targets:
        shas = sorted({record["sha256"] for record in records
                       if _normalize(record["path"], repo) == _normalize(target, repo)})
        identities.append({"path": _normalize(target, repo), "sha256": shas or None})
    commits = git_read(repo, "log", "--all", "--format=%H%x09%s", f"--grep={audit['audit_id']}",
                       "--fixed-strings").decode("utf-8").splitlines()
    implemented = sorted({line.split("\t", 1)[0] for line in commits})
    severities, personas, legs, resolutions = Counter(), Counter(), Counter(), Counter()
    round_records, calls, became = [], [], []
    for info in rounds:
        label = info["root"].relative_to(directory).as_posix()
        convergence = read_json(info["root"] / "converge.json")
        if not isinstance(convergence, dict):
            raise RegisterError(f"refused converge.json {info['root']}: expected an object")
        for step, persona, finding in findings_in(info):
            severities[str(finding.get("severity"))] += 1
            personas[persona] += 1
            legs[step] += 1
        for decision in decisions_in(info):
            resolutions[str(decision.get("resolution"))] += 1
            if decision.get("resolution") != "retracted":
                became.append({"round": label, "decision": decision, "commits": implemented})
        scope = info["scope"]
        round_records.append({"round": label, "number": info["number"], "models": scope.get("models"),
                              "step_models": scope.get("step_models"), "personas": scope.get("personas"),
                              "agreement_matrix": convergence.get("agreement_matrix"),
                              "decision": convergence.get("decision")})
        for receipt in info["receipts"]:
            if not receipt["call"]:
                continue
            value = receipt["value"]
            calls.append({"round": label, "call_uid": value.get("call_uid"), "step": value.get("step"),
                          "persona": value.get("persona"), "model": value.get("model"),
                          "effort": value.get("effort"), "usage": value.get("usage"),
                          "exit_status": value.get("exit_status")})
    totals = {name: {"files": 0, "bytes": 0} for name in CLASSES}
    for row in rows:
        if row["type"] == "file" or row["type"] == "symlink" and row["sha256"]:
            totals[row["class"]]["files"] += 1
            totals[row["class"]]["bytes"] += int(row["bytes"])
    digest = {"identity": {"audit_id": audit["audit_id"], "targets": identities, "mode": audit.get("mode"),
                           "started_at": audit.get("started_at"), "finished_at": audit.get("finished_at")},
              "rounds": round_records, "calls": calls,
              "findings": {"by_severity": dict(severities), "by_persona": dict(personas), "by_leg": dict(legs)},
              "decisions_by_resolution": dict(resolutions), "what_the_system_became": became,
              "live_verdicts": verdicts, "raw_manifest_totals": totals, "class": audit_class,
              "citation_resolvability": citations, "round_layouts": layouts,
              "context_drifted_not_ingested": drifted, "overrides": overrides}
    portable = _portable(digest, repo, directory, rounds)
    # Overrides are host records, and the legacy route requires their text verbatim.
    portable["overrides"] = dict(sorted(overrides.items()))
    return portable


def seal(repo: Path, *, audit_id: str, audit_dir: Path, audits_db: Path, actor: str,
         freeze_dir: Path, freeze_copy: Path, window_confirmed: str | None = None,
         legacy_registration: str | None = None, audit_close_override: str | None = None,
         untracked_other_override: str | None = None, narrative_file: Path | None = None) -> None:
    """Refuse any unmet seal precondition; roll back newly written lifecycle controls on failure."""
    repo = repo.resolve()
    _field(actor, "--actor", nonempty=True)
    overrides = {name: value for name, value in (("window-confirmed", window_confirmed),
                 ("legacy-registration", legacy_registration), ("audit-close-override", audit_close_override),
                 ("untracked-other-override", untracked_other_override)) if value is not None}
    for name, value in overrides.items():
        _field(value, f"--{name}", nonempty=True)
    with AuditLock.acquire(repo, audit_id, verb="seal", session=actor) as lock:
        directory = _directory(repo, audit_dir)
        _volume_window(directory, window_confirmed)
        audit = audit_record(audits_db, audit_id)
        register = read_register(repo)
        _seal_preconditions(repo, directory, audit, audits_db, register,
                            audit_close_override=audit_close_override, legacy_registration=legacy_registration)
        for destination in (freeze_dir, freeze_copy):
            if destination.resolve().is_relative_to(directory.resolve()):
                raise RegisterError(f"refused freeze destination {destination}: inside raw-manifest inventory {directory}")
        rows, layouts = raw_manifest(repo, directory, audit_id=audit_id, window_confirmed=window_confirmed)
        other_bytes = sum(int(row["bytes"]) for row in rows
                          if row["class"] == "other" and row["git_tracked"] == "0" and row["bytes"])
        if other_bytes > 2**20 and not untracked_other_override:
            raise RegisterError(f"refused untracked other files {directory}: {other_bytes} bytes > 1048576; --untracked-other-override required")
        lifecycle = directory / "lifecycle"
        controls_classifications = sorted({"lifecycle/classification.md", *(
            path.relative_to(directory).as_posix() for path in lifecycle.glob("classification.*.md")
            if re.fullmatch(r"classification\.[1-9]\d*\.md", path.name))})
        for path in controls_classifications:
            relative = PurePosixPath(path)
            if (relative.is_absolute() or ".." in relative.parts or not (directory / relative).is_file()
                    or (directory / relative).is_symlink()):
                raise RegisterError(f"refused classification control {path}: absent or outside audit")
        rounds = round_inputs(directory, audit_id)
        names = [info["root"].name for info in rounds]
        if len(names) != len(set(names)):
            raise RegisterError(f"refused ambiguous live-verdict filenames for rounds {names}")
        audit_class = effective_class(repo, audit_id)
        cfg = store.load_store_config()   # the skill's own store.json, exactly as `prepare` reads it (host fix)
        if not cfg.enabled:
            raise RegisterError(f"refused seal CAS ingest {audit_id}: store disabled by {cfg.disabled_by}: {cfg.reason}")
        store.validate_store(cfg)
        used, drifted = _ingest_plan(directory, rows, rounds, audit_class, cfg, window_confirmed)
        _read_table(repo, STORE_INDEX, STORE_HEADER)
        _read_table(repo, FREEZES, FREEZE_HEADER)
        narrative = narrative_file.read_text(encoding="utf-8") if narrative_file is not None else None
        utc = utc_now()
        created = []
        try:
            ingested = _ingest(repo, directory, audit_id, used, cfg, utc, lock)
            _write_once(lifecycle / "raw_manifest.tsv", _tsv(RAW_MANIFEST_HEADER, rows), created)
            citations = citation_resolvability(directory, audit_id=audit_id, repo=repo, store_root=cfg.root)
            verdicts = []
            for info in rounds:
                target = lifecycle / "verdicts" / f"{info['root'].name}.live.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                errors = io.StringIO()
                try:
                    with redirect_stderr(errors):
                        verify_round(info["root"], mode="live", json_out=target)
                except SystemExit as exc:
                    raise RegisterError(f"refused live verdict {info['root']}: {errors.getvalue().strip()}") from exc
                finally:
                    if target.exists():
                        created.append(target)
                verdicts.append({"round": info["root"].relative_to(directory).as_posix(),
                                 "verdict": read_json(target)["verdict"]})
            digest = build_digest(repo, directory, audit, rounds, rows, audit_class=audit_class,
                                  citations=citations, verdicts=verdicts, layouts=layouts,
                                  drifted=drifted, overrides=overrides)
            encoded = (json.dumps(digest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
            _write_once(lifecycle / "digest.json", encoded, created)
            markdown = f"# Audit digest: {audit_id}\n\n```json\n{encoded.decode('utf-8')}```\n"
            if narrative is not None:
                markdown += f"\n## Host narrative (not generated; {actor}, {utc})\n\n{narrative}"
            _write_once(lifecycle / "DIGEST.md", markdown.encode("utf-8"), created)
            controls = ["lifecycle/digest.json", "lifecycle/DIGEST.md", "lifecycle/raw_manifest.tsv",
                        *controls_classifications, *(path.relative_to(directory).as_posix() for path in created
                                                    if path.parent.name == "verdicts")]
            _write_once(lifecycle / "CONTROLS.tsv", _tsv(("path", "sha256"),
                        [{"path": path, "sha256": sha256_file(directory / path)} for path in sorted(set(controls))]), created)
            freeze = freeze_database(repo, audits_db, audit_id=audit_id, freeze_dir=freeze_dir,
                                     freeze_copy=freeze_copy, utc=utc, window_confirmed=window_confirmed, lock=lock)
            note = f"controls_sha256:{sha256_file(lifecycle / 'CONTROLS.tsv')}; freeze:{freeze['sha256']}; ingested:{ingested}"
            note += "".join(f"; {key}:{value}" for key, value in sorted(overrides.items()))
            _event(repo, audit_id, directory.relative_to(repo).as_posix(), actor, "seal", "sealed", lock=lock,
                   **{"class": audit_class, "citation_resolvability": citations["result"],
                      "manifest_sha256": sha256_file(lifecycle / "raw_manifest.tsv"),
                      "verdict_json": "lifecycle/verdicts/", "note": note})
        except BaseException:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            verdict_dir = lifecycle / "verdicts"
            if verdict_dir.is_dir() and not any(verdict_dir.iterdir()):
                verdict_dir.rmdir()
            raise
