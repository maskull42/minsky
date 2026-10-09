#!/usr/bin/env python3
"""Lifecycle CLI shell; refuse invalid evidence, unknown census entries and unimplemented verbs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import stat
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from lifecycle_register import (
    AuditLock, COLUMNS, Register, RegisterError, SKILL_DIR, append_event,
    clear_lock, git_read, lock_status, read_register, require_gate,
)

STATUSES = ("deferred", "bespoke", "modern", "legacy-preserved", "unclassifiable")
RD_HEADER = "audit_id\taudit_dir\tlocation\tmanifest_tsv"
EXCLUDED = frozenset((".worktrees", ".git", "node_modules"))


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _external_reason(repo: Path, path: Path, seen: frozenset[Path] = frozenset()) -> str | None:
    """Check link metadata; refuse loops and never inspect an external target."""
    path = _absolute(path)
    if not path.is_relative_to(repo):
        return "audit directory resolves outside repo"
    if path in seen:
        raise RegisterError(f"refused census symlink loop: {path}")
    current = repo
    parts = path.relative_to(repo).parts
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(current)
            destination = _absolute(current.parent / target)
            if destination == Path("/Volumes") or destination.is_relative_to("/Volumes"):
                return "audit directory is a symlink into /Volumes/"
            if not destination.is_relative_to(repo):
                return "audit directory resolves outside repo"
            return _external_reason(repo, destination.joinpath(*parts[index + 1:]), seen | {path})
    return None


def _rd_view(repo: Path, path: Path) -> dict[str, dict[str, str]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise RegisterError(f"refused RD register view {path}: invalid UTF-8") from exc
    lines = text.split("\n")
    if not text.endswith("\n") or "\r" in text or lines[0] != RD_HEADER:
        raise RegisterError(f"refused RD register view {path} line 1: header or LF mismatch")
    entries = {}
    for number, line in enumerate(lines[1:-1], 2):
        fields = line.split("\t")
        if len(fields) != 4 or not fields[0] or not fields[1] or fields[2] not in ("boot", "extended"):
            raise RegisterError(f"refused RD register view {path} line {number}: invalid fields")
        row = dict(zip(RD_HEADER.split("\t"), fields))
        if row["audit_id"] in entries:
            raise RegisterError(f"refused RD register view {path} line {number}: duplicate audit_id {fields[0]}")
        row["audit_dir"] = str(_absolute(repo / row["audit_dir"]))
        entries[row["audit_id"]] = row
    return entries


def _database(path: Path) -> dict[str, str | None]:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            rows = conn.execute("SELECT audit_id, finished_at FROM audits").fetchall()
    except sqlite3.Error as exc:
        raise RegisterError(f"refused audits database {path} opened read-only: {exc}") from exc
    entries = {}
    for audit_id, finished_at in rows:
        if not isinstance(audit_id, str) or not audit_id or audit_id in entries:
            raise RegisterError(f"refused audits database {path}: invalid or duplicate audit_id {audit_id!r}")
        if finished_at is not None and (not isinstance(finished_at, str) or not finished_at):
            raise RegisterError(f"refused audits database {path}: invalid finished_at for {audit_id}")
        entries[audit_id] = finished_at
    return entries


def _walk(root: Path, excluded_paths: set[Path], excluded_names: frozenset[str] = EXCLUDED):
    """Walk without following or stat-ing symlink targets (host fix: os.walk's entry.is_dir() follows links)."""
    stack = [Path(root)]
    while stack:
        path = stack.pop()
        names, files = [], []
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in excluded_names and path / entry.name not in excluded_paths:
                        names.append(entry.name)
                else:
                    files.append(entry.name)
        names.sort()
        yield path, names, sorted(files)
        stack.extend(path / name for name in reversed(names))


def discover_rounds(repo: Path, rd: dict[str, dict[str, str]]) -> dict[str, dict]:
    """Discover marker-bearing rounds; refuse conflicting identities and unreadable scopes."""
    excluded = {Path(row["audit_dir"]) for row in rd.values() if row["location"] == "extended"}
    return _discover(repo, excluded)


def discover_rounds_in(audit_dir: Path) -> dict[str, dict]:
    """Re-discover one audit directory's rounds (used under that audit's lock)."""
    return _discover(audit_dir, set())


def _discover(root: Path, excluded: set[Path]) -> dict[str, dict]:
    rounds = set()
    for path, names, files in _walk(root, excluded):
        if "round-scope.json" in files:
            rounds.add(path)
        if path.name == "provenance" and any(name.endswith(".call.json") for name in files):
            rounds.add(path.parent)
    discovered = {}
    by_directory = {}
    for round_dir in sorted(rounds):
        audit_dir = round_dir.parent
        scope = round_dir / "round-scope.json"
        audit_id = audit_dir.name
        try:
            info = scope.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode):
                raise RegisterError(f"refused census round scope {scope}: not a regular file")
            try:
                value = json.loads(scope.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise RegisterError(f"refused census round scope {scope}: {exc}") from exc
            if not isinstance(value, dict):
                raise RegisterError(f"refused census round scope {scope}: expected an object")
            audit_id = value.get("audit_id", audit_id)
        if not isinstance(audit_id, str) or not audit_id:
            raise RegisterError(f"refused census audit_id in {scope}: {audit_id!r}")
        prior = discovered.get(audit_id)
        if prior is not None and prior["audit_dir"] != audit_dir:
            raise RegisterError(f"refused census audit_id {audit_id}: multiple audit directories")
        if audit_dir in by_directory and by_directory[audit_dir] != audit_id:
            raise RegisterError(f"refused census {audit_dir}: conflicting round audit ids")
        by_directory[audit_dir] = audit_id
        entry = discovered.setdefault(audit_id, {"audit_dir": audit_dir, "rounds": []})
        entry["rounds"].append(round_dir)
    return discovered


def _status(repo: Path, audit_dir: Path, rounds: list[Path], registered_db: bool,
            registered_rd: bool, location: str | None) -> tuple[str, str]:
    external = _external_reason(repo, audit_dir)
    if location == "extended" or external:
        return "deferred", external or "RD register view location is extended"
    try:
        info = audit_dir.lstat()
    except FileNotFoundError:
        return "unclassifiable", "registered audit directory is absent"
    if not stat.S_ISDIR(info.st_mode):
        return "unclassifiable", "audit directory is not a real directory on boot"
    if rounds:
        bespoke = audit_dir.parent != repo / "codex-audits" or not registered_db
        for round_dir in rounds:
            for name in ("native-calls", "round-layout.json", "round-layout.retro.json"):
                try:
                    marker = (round_dir / name).lstat()
                except FileNotFoundError:
                    continue
                if name != "native-calls" or stat.S_ISDIR(marker.st_mode):
                    bespoke = True
        if bespoke:
            return "bespoke", "modern markers with bespoke layout, placement or unregistered id"
        return "modern", "modern markers in a registered standard audit directory"
    if registered_db or registered_rd:
        return "legacy-preserved", "registered real boot directory with no modern markers"
    return "unclassifiable", "no registered identity or modern markers"


def _inventory(repo: Path, audit_dir: Path, tracked: set[str]) -> dict[str, int]:
    count = total = 0
    for path, names, files in _walk(audit_dir, set(), frozenset()):
        # os.walk's directory entries also include symlinks: lstat them without traversal.
        for child in sorted(path.iterdir()):
            info = child.lstat()
            if not stat.S_ISDIR(info.st_mode) and child.relative_to(repo).as_posix() not in tracked:
                count += 1
                total += info.st_size
    return {"untracked_files": count, "untracked_bytes": total}


def closeout_flags(audit_id: str, finished_at: str | None, register: Register,
                   today: date) -> list[str]:
    """Refuse malformed copy evidence; flag overdue audits until complete current copies exist."""
    if finished_at is None:
        from lifecycle_copies import parse_copy_note
        for row in register.rows:
            if row["audit_id"] == audit_id and row["event"] == "copy":
                parse_copy_note(row)
        return []
    try:
        instant = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        finished = (instant.astimezone(UTC) if instant.tzinfo is not None else instant).date()
    except ValueError as exc:
        raise RegisterError(f"refused finished_at for {audit_id}: {finished_at!r}") from exc
    rows = [row for row in register.rows if row["audit_id"] == audit_id]
    from lifecycle_copies import counting_locations
    copies = counting_locations(audit_id, register)
    flags = []
    age = (today - finished).days
    if age > 7 and not copies:
        flags.append("overdue-first-copy")
    audit_class = next((row["class"] for row in reversed(rows) if row["class"]), "")
    if audit_class == "methodology-bearing" and age > 30 and len(copies) < 2:
        flags.append("overdue-second-copy")
    return flags


def census(repo_root: Path, *, audits_db: Path, rd_register_view: Path,
           report_out: Path, record: bool = False, today: date | None = None) -> int:
    """Refuse invalid inputs and internal reports; report every union entry before unknown refusal."""
    repo = repo_root.resolve()
    if report_out.resolve().is_relative_to(repo):
        raise RegisterError(f"refused --report-out {report_out}: resolves inside repo {repo}")
    register = read_register(repo)
    if record:
        require_gate("G3", audit_dir=repo, db_path=audits_db)
    db = _database(audits_db)
    rd = _rd_view(repo, rd_register_view)
    discovered = discover_rounds(repo, rd)
    union = set(db) | set(rd) | set(discovered)
    tracked = set(git_read(repo, "ls-files", "-z").decode("utf-8").split("\0"))
    entries = []
    for audit_id in sorted(union):
        directory = discovered.get(audit_id, {}).get("audit_dir")
        rd_row = rd.get(audit_id)
        if directory is not None and rd_row is not None and directory != Path(rd_row["audit_dir"]):
            raise RegisterError(f"refused census audit_id {audit_id}: RD and discovery directories differ")
        directory = directory or (Path(rd_row["audit_dir"]) if rd_row else repo / "codex-audits" / audit_id)
        rounds = discovered.get(audit_id, {}).get("rounds", [])
        # Census takes each audit's lock across its checks and optional register append.
        with AuditLock.acquire(repo, audit_id, verb="census", session="") as lock:
            register = read_register(repo)
            if (not (rd_row and rd_row["location"] == "extended") and directory.is_dir()
                    and not directory.is_symlink() and not _external_reason(repo, directory)):
                # Re-discover this audit's rounds under its lock (host fix: the preliminary scan may be stale).
                fresh = discover_rounds_in(directory)
                if fresh.get(audit_id, {}).get("audit_dir", directory) != directory:
                    raise RegisterError(f"refused census audit_id {audit_id}: identity changed during census")
                rounds = fresh.get(audit_id, {}).get("rounds", [])
            status, reason = _status(repo, directory, rounds, audit_id in db, audit_id in rd,
                                     rd_row["location"] if rd_row else None)
            entry = {
                "audit_id": audit_id, "audit_dir": str(directory), "status": status, "reason": reason,
                "registered": audit_id in db or audit_id in rd, "finished_at": db.get(audit_id),
                "rounds": [str(path) for path in rounds],
                "flags": closeout_flags(audit_id, db.get(audit_id), register, today or datetime.now(UTC).date()),
            }
            if status == "legacy-preserved":
                entry["inventory"] = _inventory(repo, directory, tracked)
            entries.append(entry)
            if record:
                row = dict.fromkeys(COLUMNS, "")
                row.update(audit_id=audit_id, audit_dir=os.path.relpath(directory, repo),
                           event="census", state_after=status, note=reason)
                lock.check()
                append_event(repo, row, lock=lock)
    counts = Counter(entry["status"] for entry in entries)
    report = {"entries": entries, "totals": {status: counts[status] for status in STATUSES},
              "union_size": len(union)}
    report_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failures = [f"refused census audit {entry['audit_id']}: {entry['reason']}"
                for entry in entries if entry["status"] == "unclassifiable"]
    if sum(report["totals"].values()) != report["union_size"]:
        failures.append("refused census totals: statuses do not reconcile to union_size")
    for failure in failures:
        sys.stderr.write(failure + "\n")
    return 1 if failures else 0


def _today(value: str) -> date:
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"refused --today {value!r}: expected YYYY-MM-DD") from exc
    if value != result.isoformat():
        raise argparse.ArgumentTypeError(f"refused --today {value!r}: expected YYYY-MM-DD")
    return result


def ruling(repo: Path, *, audit_id: str, kind: str, verbatim: str,
           researcher_session: str, actor: str) -> None:
    """Append the researcher's exact words; refuse unknown kinds, missing identity and TSV delimiters."""
    from lifecycle_seal import _event, _field
    for label, value in (("--verbatim", verbatim), ("--researcher-session", researcher_session), ("--actor", actor)):
        _field(value, label, nonempty=True)
    if kind not in ("downgrade", "citation-waiver", "classification-confirm", "challenge-resolved", "legacy-migrate"):
        raise RegisterError(f"refused ruling kind {kind!r}")
    repo = repo.resolve()
    with AuditLock.acquire(repo, audit_id, verb="ruling", session=researcher_session) as lock:
        rows = [row for row in read_register(repo).rows if row["audit_id"] == audit_id]
        if not rows:
            raise RegisterError(f"refused ruling {audit_id}: registered audit required")
        prior = rows[-1]
        values = {key: prior[key] for key in ("class", "citation_resolvability", "manifest_sha256", "pack_sha256")}
        _event(repo, audit_id, prior["audit_dir"], actor, "ruling", prior["state_after"], lock=lock,
               **{**values, "note": f"{kind}: {verbatim}"})


def promote(repo: Path, *, audit_id: str, reason: str, by: str, actor: str,
            copy: str | None = None, copy_a_root: Path | None = None,
            restic_repo: str | None = None, restic_password_file: Path | None = None,
            forget_policy: str | None = None, window_confirmed: str | None = None) -> None:
    """Promote sealed bytes or ingest then copy a pack; refuse other states, expiry, drift and incomplete copies."""
    import lifecycle_copies as copies
    import lifecycle_pack as packs
    from lifecycle_citations import effective_class
    from lifecycle_offload import ingest_promotion
    from lifecycle_seal import _directory, _event, _field
    for label, value in (("--reason", reason), ("--by", by), ("--actor", actor)):
        _field(value, label, nonempty=True)
    repo = repo.resolve()
    packs.volume_window(repo, window_confirmed)
    with AuditLock.acquire(repo, audit_id, verb="promote", session=actor) as lock:
        rows = [row for row in read_register(repo).rows if row["audit_id"] == audit_id]
        if not rows or any(row["event"] == "expire" for row in rows):
            raise RegisterError(f"refused promote {audit_id}: registered unexpired audit required")
        state = rows[-1]["state_after"]
        if state not in ("sealed", "packed", "offloaded") and not re.fullmatch(r"replicated\(\d+\)", state):
            raise RegisterError(f"refused promote {audit_id}: latest state sealed, packed, replicated(n) or offloaded required; found {state}")
        if any(row["event"] == "promote" for row in rows) and effective_class(repo, audit_id) == "methodology-bearing":
            return
        seal = next((row for row in reversed(rows) if row["event"] == "seal"), None)
        if seal is None:
            raise RegisterError(f"refused promote {audit_id}: seal row required")
        directory = _directory(repo, Path(seal["audit_dir"]))
        packed = next((row for row in reversed(rows) if row["event"] == "pack"), None)
        if state == "sealed" and packed is not None:
            raise RegisterError(f"refused promote {audit_id}: sealed state has a pack row")
        if state != "sealed" and packed is None:
            raise RegisterError(f"refused promote {audit_id}: pack row required for state {state}")
        if not any(row["event"] == "promotion-pending" for row in rows):
            _event(repo, audit_id, seal["audit_dir"], actor, "promotion-pending", rows[-1]["state_after"],
                   lock=lock, **{"class": effective_class(repo, audit_id),
                                "manifest_sha256": seal["manifest_sha256"],
                                "pack_sha256": packed["pack_sha256"] if packed else "",
                                "note": f"reason:{reason}; by:{by}"})
        free_space_checks: list[str] = []
        seal = {**seal, "_free_space_audit_dir": directory, "_free_space_checks": free_space_checks}
        ingested = ingest_promotion(repo, audit_id, directory, seal, packed, lock, window_confirmed)
        if state == "sealed":
            _event(repo, audit_id, seal["audit_dir"], actor, "promote", state, lock=lock,
                   **{"class": "methodology-bearing", "manifest_sha256": seal["manifest_sha256"],
                      "citation_resolvability": seal["citation_resolvability"],
                      "note": packs.free_space_note(
                          f"reason:{reason}; by:{by}; ingested:{ingested}", free_space_checks)})
            return
    # Host fix (Astra W7c): create EVERY missing copy (A, then B), not just one, so a promotion from `packed` with no copy
    # yet can complete in one invocation. Each copy_operation takes its own lock; promotion-pending guards the gaps.
    for selected in (("A", "B") if copy is None else (copy,)):
        locations = copies.counting_locations(audit_id, read_register(repo))
        if len(locations) >= 2:
            break
        if any(location.startswith(selected + ":") for location in locations):
            continue
        root_a = copy_a_root if copy_a_root is not None else (Path(packed["copy_location"]) if selected == "A" else None)
        copies.copy_operation(repo, audit_id=audit_id, copy=selected, actor=actor, event="copy",
                              copy_a_root=root_a, restic_repo=restic_repo,
                              restic_password_file=restic_password_file, forget_policy=forget_policy,
                              window_confirmed=window_confirmed)
    with AuditLock.acquire(repo, audit_id, verb="promote", session=actor) as lock:
        register = read_register(repo)
        rows = [row for row in register.rows if row["audit_id"] == audit_id]
        if any(row["event"] == "expire" for row in rows):
            raise RegisterError(f"refused promote {audit_id}: expire row exists")
        state = rows[-1]["state_after"]
        if state not in ("packed", "offloaded") and not re.fullmatch(r"replicated\(\d+\)", state):
            raise RegisterError(f"refused promote {audit_id}: latest state packed, replicated(n) or offloaded required after copying; found {state}")
        if len(copies.counting_locations(audit_id, register)) < 2:
            raise RegisterError(f"refused promote {audit_id}: two complete copies on separate devices required")
        latest_pack = next((row for row in reversed(rows) if row["event"] == "pack"), None)
        latest_seal = next((row for row in reversed(rows) if row["event"] == "seal"), None)
        if (packed is None or latest_pack is None or latest_pack["pack_sha256"] != packed["pack_sha256"]
                or latest_seal is None or latest_seal["manifest_sha256"] != seal["manifest_sha256"]
                or latest_seal["audit_dir"] != seal["audit_dir"]):
            raise RegisterError(f"refused promote {audit_id}: pack identity changed between promotion steps")
        _event(repo, audit_id, seal["audit_dir"], actor, "promote", rows[-1]["state_after"], lock=lock,
               **{"class": "methodology-bearing", "manifest_sha256": seal["manifest_sha256"],
                  "pack_sha256": packed["pack_sha256"], "citation_resolvability": packed["citation_resolvability"],
                  "note": packs.free_space_note(
                      f"reason:{reason}; by:{by}; ingested:{ingested}", free_space_checks)})


def archive(repo: Path, *, audit_id: str, actor: str, audit_dir: Path | None = None,
            audits_db: Path | None = None, freeze_dir: Path | None = None, freeze_copy: Path | None = None,
            copy_a_root: Path | None = None, restic_repo: str | None = None,
            restic_password_file: Path | None = None, forget_policy: str | None = None,
            window_confirmed: str | None = None, legacy_registration: str | None = None,
            audit_close_override: str | None = None, untracked_other_override: str | None = None,
            narrative_file: Path | None = None) -> None:
    """Call public archive verbs with their own locks; refuse the first failed step without classifying."""
    from lifecycle_citations import effective_class
    from lifecycle_copies import copy_operation
    from lifecycle_pack import pack
    from lifecycle_seal import _field, seal
    _field(actor, "--actor", nonempty=True)
    repo = repo.resolve()
    def step(name: str, operation, **kwargs) -> None:
        try:
            operation(repo, audit_id=audit_id, actor=actor, **kwargs)
        except (RegisterError, OSError, UnicodeError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise RegisterError(f"archive step {name} refused for {audit_id}: {exc}") from exc

    if any(value is None for value in (audit_dir, audits_db, freeze_dir, freeze_copy)):
        raise RegisterError(f"archive step seal refused for {audit_id}: --audit-dir, --audits-db, --freeze-dir and --freeze-copy required")
    step("seal", seal, audit_dir=audit_dir, audits_db=audits_db, freeze_dir=freeze_dir, freeze_copy=freeze_copy,
         window_confirmed=window_confirmed, legacy_registration=legacy_registration,
         audit_close_override=audit_close_override, untracked_other_override=untracked_other_override,
         narrative_file=narrative_file)
    if copy_a_root is None:
        raise RegisterError(f"archive step pack refused for {audit_id}: --copy-a-root required")
    step("pack", pack, audit_dir=audit_dir, copy_a_root=copy_a_root, window_confirmed=window_confirmed)
    step("replicate(A)", copy_operation, copy="A", event="copy", copy_a_root=copy_a_root,
         window_confirmed=window_confirmed)
    if effective_class(repo, audit_id) == "methodology-bearing":
        step("replicate(B)", copy_operation, copy="B", event="copy", restic_repo=restic_repo,
             restic_password_file=restic_password_file, forget_policy=forget_policy,
             window_confirmed=window_confirmed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    for name in ("census", "lock-status", "lock-clear", "classify", "challenge", "ruling", "promote", "seal",
                 "pack", "replicate", "verify", "offload", "rehydrate", "expire", "archive", "freeze", "freeze-restore-check"):
        verb = verbs.add_parser(name)
        verb.add_argument("--repo", type=Path, default=SKILL_DIR.parents[2])
        if name == "census":
            verb.add_argument("--audits-db", type=Path, required=True)
            verb.add_argument("--rd-register-view", type=Path, required=True)
            verb.add_argument("--report-out", type=Path, required=True)
            verb.add_argument("--record", action="store_true")
            verb.add_argument("--today", type=_today)
        elif name == "freeze":
            verb.add_argument("--reason", choices=("ad-b", "campaign-close"))
            verb.add_argument("--label")
            verb.add_argument("--audits-db", type=Path)
            verb.add_argument("--freeze-dir", type=Path,
                              default=Path.home() / "Library/Application Support/MARS/frozen/minsky_audits_db")
            verb.add_argument("--freeze-copy", type=Path)
            verb.add_argument("--actor")
            verb.add_argument("--window-confirmed")
        else:
            verb.add_argument("--audit", required=name == "lock-clear")
            if name == "lock-clear":
                verb.add_argument("--by", required=True)
                verb.add_argument("--reason", required=True)
            if name in ("classify", "challenge", "seal", "freeze-restore-check", "pack", "replicate", "verify",
                        "ruling", "promote", "offload", "rehydrate", "expire", "archive"):
                verb.add_argument("--actor")
            if name in ("classify", "seal", "pack", "offload", "archive"):
                verb.add_argument("--audit-dir", type=Path)
            if name in ("classify", "seal", "expire", "archive"):
                verb.add_argument("--audits-db", type=Path)
            if name in ("pack", "replicate", "verify", "promote", "offload", "rehydrate", "expire", "archive"):
                verb.add_argument("--copy-a-root", type=Path)
                verb.add_argument("--window-confirmed")
            if name in ("replicate", "verify", "promote", "offload", "rehydrate", "archive"):
                if name != "rehydrate":
                    verb.add_argument("--copy", choices=("A", "B"))
                verb.add_argument("--restic-repo")
                verb.add_argument("--restic-password-file", type=Path)
                verb.add_argument("--forget-policy")
            if name == "ruling":
                verb.add_argument("--kind", choices=("downgrade", "citation-waiver", "classification-confirm",
                                                    "challenge-resolved", "legacy-migrate"))
                verb.add_argument("--verbatim")
                verb.add_argument("--researcher-session")
            if name == "promote":
                verb.add_argument("--by")
                verb.add_argument("--reason")
            if name == "offload":
                phases = verb.add_mutually_exclusive_group()
                for phase in ("prepare", "execute", "resume"):
                    phases.add_argument("--" + phase, dest="phase", action="store_const", const=phase)
            if name == "rehydrate":
                verb.add_argument("--from", dest="source_copy", choices=("A", "B"))
                targets = verb.add_mutually_exclusive_group()
                targets.add_argument("--into", type=Path)
                targets.add_argument("--in-place", action="store_true")
            if name == "expire":
                verb.add_argument("--execute", action="store_true")
            if name == "classify":
                verb.add_argument("--class", dest="requested_class", choices=("methodology-bearing", "code-only"))
                verb.add_argument("--statement-file", type=Path)
                verb.add_argument("--window-confirmed")
            if name == "challenge":
                verb.add_argument("--by")
                verb.add_argument("--reason")
            if name in ("seal", "archive"):
                verb.add_argument("--freeze-dir", type=Path,
                                  default=Path.home() / "Library/Application Support/MARS/frozen/minsky_audits_db")
                verb.add_argument("--freeze-copy", type=Path)
                for option in ("window-confirmed", "legacy-registration", "audit-close-override", "untracked-other-override"):
                    if name != "archive" or option != "window-confirmed":
                        verb.add_argument("--" + option)
                verb.add_argument("--narrative-file", type=Path)
            if name == "freeze-restore-check":
                verb.add_argument("--freeze-row", type=int)
                verb.add_argument("--scratch", type=Path)
                verb.add_argument("--window-confirmed")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Refuse unknown usage with 2 and lifecycle failures with one-line reasons and 1."""
    logger = logging.getLogger("free_space")
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        return _main(argv)
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(level)
        logger.propagate = propagate


def _main(argv: list[str] | None) -> int:
    """Dispatch lifecycle verbs; refuse invalid usage or any failed operation."""
    parser = _parser()
    args, extra = parser.parse_known_args(argv)
    implemented = ("classify", "challenge", "seal", "freeze", "freeze-restore-check", "pack", "replicate", "verify",
                   "ruling", "promote", "offload", "rehydrate", "expire", "archive")
    if args.verb not in ("census", "lock-status", "lock-clear", *implemented):
        sys.stderr.write(f"{args.verb}: not implemented in W8\n")
        return 2
    # Retain W8's placeholder refusal for calls with no audit identity and unknown future flags.
    if args.verb in ("classify", "challenge", "seal", "pack", "replicate", "verify", "promote",
                     "offload", "rehydrate", "expire", "archive") and not args.audit and extra:
        sys.stderr.write(f"{args.verb}: not implemented in W8\n")
        return 2
    if extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    required = {
        "classify": ("audit", "audit_dir", "audits_db", "actor"),
        "challenge": ("audit", "by", "reason", "actor"),
        "seal": ("audit", "audit_dir", "audits_db", "actor", "freeze_copy"),
        "freeze": ("reason", "audits_db", "freeze_copy", "actor"),
        "freeze-restore-check": ("freeze_row", "scratch", "actor"),
        "pack": ("audit", "audit_dir", "copy_a_root", "actor"),
        "replicate": ("audit", "copy", "actor"),
        "verify": ("audit", "copy", "actor"),
        "ruling": ("audit", "kind", "verbatim", "researcher_session", "actor"),
        "promote": ("audit", "reason", "by", "actor"),
        "offload": ("audit", "phase", "actor"),
        "rehydrate": ("audit", "source_copy", "actor"),
        "expire": ("audit", "actor"),
        "archive": ("audit", "actor"),
    }
    if args.verb in ("replicate", "verify") and args.copy:
        required[args.verb] += (("copy_a_root",) if args.copy == "A" else
                                ("restic_repo", "restic_password_file", "forget_policy"))
    option_names = {"source_copy": "--from", "phase": "one of --prepare, --execute, --resume"}
    missing = [option_names.get(key, "--" + key.replace("_", "-"))
               for key in required.get(args.verb, ()) if getattr(args, key) is None]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))
    if args.verb == "rehydrate" and args.into is None and not args.in_place:
        parser.error("exactly one of --into and --in-place is required")
    try:
        if args.verb == "ruling":
            ruling(args.repo, audit_id=args.audit, kind=args.kind, verbatim=args.verbatim,
                   researcher_session=args.researcher_session, actor=args.actor)
            return 0
        if args.verb == "promote":
            promote(args.repo, audit_id=args.audit, reason=args.reason, by=args.by, actor=args.actor,
                    copy=args.copy, copy_a_root=args.copy_a_root, restic_repo=args.restic_repo,
                    restic_password_file=args.restic_password_file, forget_policy=args.forget_policy,
                    window_confirmed=args.window_confirmed)
            return 0
        if args.verb == "archive":
            archive(args.repo, audit_id=args.audit, actor=args.actor, audit_dir=args.audit_dir,
                    audits_db=args.audits_db, freeze_dir=args.freeze_dir, freeze_copy=args.freeze_copy,
                    copy_a_root=args.copy_a_root, restic_repo=args.restic_repo,
                    restic_password_file=args.restic_password_file, forget_policy=args.forget_policy,
                    window_confirmed=args.window_confirmed, legacy_registration=args.legacy_registration,
                    audit_close_override=args.audit_close_override,
                    untracked_other_override=args.untracked_other_override, narrative_file=args.narrative_file)
            return 0
        if args.verb == "offload":
            from lifecycle_offload import offload
            offload(args.repo, audit_id=args.audit, actor=args.actor, phase=args.phase,
                    audit_dir=args.audit_dir, copy_a_root=args.copy_a_root, restic_repo=args.restic_repo,
                    restic_password_file=args.restic_password_file, forget_policy=args.forget_policy,
                    window_confirmed=args.window_confirmed)
            return 0
        if args.verb == "rehydrate":
            from lifecycle_offload import rehydrate
            rehydrate(args.repo, audit_id=args.audit, copy=args.source_copy, actor=args.actor,
                      into=args.into, in_place=args.in_place, copy_a_root=args.copy_a_root,
                      restic_repo=args.restic_repo, restic_password_file=args.restic_password_file,
                      forget_policy=args.forget_policy, window_confirmed=args.window_confirmed)
            return 0
        if args.verb == "expire":
            from lifecycle_expire import expire
            report = expire(args.repo, audit_id=args.audit, actor=args.actor, execute=args.execute,
                            audits_db=args.audits_db, window_confirmed=args.window_confirmed)
            sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
            for reason in report["reasons"]:
                sys.stderr.write(f"refused expire {args.audit}: {reason}\n")
            return 0 if report["eligible"] else 1
        if args.verb == "pack":
            from lifecycle_pack import pack
            pack(args.repo, audit_id=args.audit, audit_dir=args.audit_dir, copy_a_root=args.copy_a_root,
                 actor=args.actor, window_confirmed=args.window_confirmed)
            return 0
        if args.verb in ("replicate", "verify"):
            from lifecycle_copies import copy_operation
            copy_operation(args.repo, audit_id=args.audit, copy=args.copy, actor=args.actor,
                           event="copy" if args.verb == "replicate" else "verify", copy_a_root=args.copy_a_root,
                           restic_repo=args.restic_repo, restic_password_file=args.restic_password_file,
                           forget_policy=args.forget_policy, window_confirmed=args.window_confirmed)
            return 0
        if args.verb in implemented:
            from lifecycle_seal import challenge, classify, freeze, freeze_restore_check, seal
            if args.verb == "classify":
                classify(args.repo, audit_id=args.audit, audit_dir=args.audit_dir, audits_db=args.audits_db,
                         actor=args.actor, requested_class=args.requested_class, statement_file=args.statement_file,
                         window_confirmed=args.window_confirmed)
            elif args.verb == "challenge":
                challenge(args.repo, audit_id=args.audit, by=args.by, reason=args.reason, actor=args.actor)
            elif args.verb == "freeze":
                result = freeze(args.repo, reason=args.reason, label=args.label, audits_db=args.audits_db,
                                freeze_dir=args.freeze_dir, freeze_copy=args.freeze_copy, actor=args.actor,
                                window_confirmed=args.window_confirmed)
                sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
            elif args.verb == "freeze-restore-check":
                freeze_restore_check(args.repo, freeze_row=args.freeze_row, scratch=args.scratch, actor=args.actor,
                                     window_confirmed=args.window_confirmed)
            else:
                seal(args.repo, audit_id=args.audit, audit_dir=args.audit_dir, audits_db=args.audits_db,
                     actor=args.actor, freeze_dir=args.freeze_dir, freeze_copy=args.freeze_copy,
                     window_confirmed=args.window_confirmed, legacy_registration=args.legacy_registration,
                     audit_close_override=args.audit_close_override,
                     untracked_other_override=args.untracked_other_override, narrative_file=args.narrative_file)
            return 0
        if args.verb == "census":
            return census(args.repo, audits_db=args.audits_db, rd_register_view=args.rd_register_view,
                          report_out=args.report_out, record=args.record, today=args.today)
        if args.verb == "lock-status":
            sys.stdout.write(json.dumps(lock_status(args.repo, args.audit), indent=2, sort_keys=True) + "\n")
        else:
            clear_lock(args.repo, args.audit, by=args.by, reason=args.reason)
        return 0
    except (RegisterError, OSError, UnicodeError, ValueError, RuntimeError, sqlite3.Error) as exc:
        for line in str(exc).splitlines():
            sys.stderr.write(line + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
