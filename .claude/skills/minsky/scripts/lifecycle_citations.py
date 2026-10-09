"""Resolve audit citations; refuse incomplete joins and unanchored or changed source bytes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath

from byte_sources import RAW_MANIFEST_HEADER
from converge import quoted_line_matches
from lifecycle_register import RegisterError, git_read, read_register
from provenance import stable_finding_uid
from store import cas_path, read_store_id


def read_json(path: Path) -> object:
    """Read a required record; refuse missing, malformed or unreadable JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RegisterError(f"refused JSON record {path}: {exc}") from exc


def round_inputs(audit_dir: Path, audit_id: str) -> list[dict]:
    """Collect every modern round without following directories; refuse conflicting identities."""
    paths = set()
    for directory, names, files in os.walk(audit_dir, followlinks=False):
        names[:] = sorted(name for name in names if not (Path(directory) / name).is_symlink())
        root = Path(directory)
        if "round-scope.json" in files:
            paths.add(root)
        if root.name == "provenance" and any(
                name.endswith((".call.json", ".preflight.json")) for name in files):
            paths.add(root.parent)
    rounds = []
    numbers = set()
    for root in sorted(paths):
        scope = read_json(root / "round-scope.json")
        if (not isinstance(scope, dict) or scope.get("audit_id") != audit_id
                or type(scope.get("round_number")) is not int or scope["round_number"] < 1
                or scope.get("schema_version") not in ("1.0", "1.1")):
            raise RegisterError(f"refused round scope {root / 'round-scope.json'}: audit/round/schema mismatch")
        number = scope["round_number"]
        if number in numbers:
            raise RegisterError(f"refused audit {audit_id}: duplicate round number {number}")
        numbers.add(number)
        receipts = []
        for path in sorted((root / "provenance").glob("*.json")):
            if not path.name.endswith((".call.json", ".preflight.json")):
                continue
            value = read_json(path)
            if not isinstance(value, dict):
                raise RegisterError(f"refused receipt {path}: expected an object")
            if value.get("audit_id") != audit_id or value.get("round_number") != number:
                raise RegisterError(f"refused receipt {path}: audit/round mismatch")
            for field in ("referenced_context", "runtime_snapshots"):
                records = value.get(field, [])
                if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                    raise RegisterError(f"refused receipt {path}: {field} must be an array of objects")
            file_records(value)
            receipts.append({"path": path, "value": value, "call": path.name.endswith(".call.json")})
        rounds.append({"root": root, "number": number, "scope": scope, "receipts": receipts})
    return sorted(rounds, key=lambda item: (item["number"], item["root"].as_posix()))


def file_records(value: object) -> list[dict]:
    """Collect recorded byte identities recursively; refuse malformed path/hash records."""
    result = []
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            if (not isinstance(value["path"], str) or not value["path"]
                    or not isinstance(value["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
                raise RegisterError(f"refused recorded path/sha256: {value!r}")
            result.append(value)
        for child in value.values():
            result.extend(file_records(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(file_records(child))
    return result


def audit_relative(path: str, audit_dir: Path, round_info: dict | None = None) -> str | None:
    """Map a recorded path to the audit; refuse to invent a root for external paths."""
    candidate = PurePosixPath(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    try:
        return candidate.relative_to(audit_dir.as_posix()).as_posix()
    except ValueError:
        pass
    if round_info:
        roots = set()
        for receipt in round_info["receipts"]:
            recorded = (receipt["value"].get("round_scope") or {}).get("path")
            if isinstance(recorded, str) and PurePosixPath(recorded).is_absolute():
                roots.add(PurePosixPath(recorded).parent.parent)
        matches = set()
        for root in roots:
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            matches.add(relative.as_posix())
        if len(matches) > 1:
            raise RegisterError(f"refused ambiguous recorded round roots for {path}")
        if matches:
            return next(iter(matches))
    return None


def findings_in(round_info: dict) -> list[tuple[str, str, dict]]:
    """Read every findings JSON; refuse invalid persona and findings shapes."""
    result = []
    for step, subdir in (("claude_self", "claude-self/findings"), ("codex", "codex"),
                         ("opencode", "opencode")):
        for path in sorted((round_info["root"] / subdir).glob("*.json")):
            doc = read_json(path)
            if (not isinstance(doc, dict) or not isinstance(doc.get("findings"), list)
                    or doc.get("persona") != path.stem):
                raise RegisterError(f"refused findings {path}: persona/findings mismatch")
            for finding in doc["findings"]:
                if not isinstance(finding, dict):
                    raise RegisterError(f"refused finding in {path}: expected an object")
                result.append((step, path.stem, finding))
    return result


def decisions_in(round_info: dict) -> list[dict]:
    """Read required synthesis decisions; refuse malformed resolution arrays."""
    path = round_info["root"] / "claude-synth/resolutions.json"
    value = read_json(path)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise RegisterError(f"refused resolutions {path}: expected an array of objects")
    return value


PACK_TEXT_PREFIX = "\n      "
PACK_TEXT_SUFFIX = "\n    "


def pack_entries(path: Path) -> list[tuple[str, str]]:
    """Parse embedded text with XML; refuse malformed packs and duplicate text paths."""
    try:
        tree = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise RegisterError(f"refused pack XML {path}: {exc}") from exc
    entries = []
    seen = set()
    for node in tree.iter("file"):
        name = node.get("path")
        if not name:
            raise RegisterError(f"refused pack entry {path}: missing path")
        if node.get("binary") == "true" or node.get("sha256") is not None:
            continue
        if name in seen:
            raise RegisterError(f"refused pack entry {path}: duplicate path {name}")
        seen.add(name)
        text = "".join(node.itertext())
        # pack-build.py:197-199 writes "<file ...>\n      <![CDATA[...]]>\n    </file>": strip exactly that wrapper so the
        # source hash and line map describe the embedded file's own text (host fix, W5 review).
        if text.startswith(PACK_TEXT_PREFIX) and text.endswith(PACK_TEXT_SUFFIX):
            text = text[len(PACK_TEXT_PREFIX):len(text) - len(PACK_TEXT_SUFFIX)]
        else:
            continue   # not pack-build's layout: never used as a source (its exact text cannot be recovered)
        entries.append((name, text))
    return sorted(entries)


def effective_class(repo: Path, audit_id: str) -> str:
    """Read the latest class; refuse to downgrade an outstanding challenge."""
    rows = [row for row in read_register(repo).rows if row["audit_id"] == audit_id]
    last_ruling = max((i for i, row in enumerate(rows) if row["event"] == "ruling"), default=-1)
    if any(row["event"] == "challenge" for row in rows[last_ruling + 1:]):
        return "methodology-bearing"
    return next((row["class"] for row in reversed(rows)
                 if row["event"] in ("classify", "ruling", "promote") and row["class"]), "methodology-bearing")


def _raw_rows(audit_dir: Path) -> dict[str, dict]:
    path = audit_dir / "lifecycle/raw_manifest.tsv"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if not text.endswith("\n") or "\r" in text or lines[0] != "\t".join(RAW_MANIFEST_HEADER):
        raise RegisterError(f"refused raw manifest {path}: header/LF mismatch")
    rows = {}
    for line in lines[1:-1]:
        fields = line.split("\t")
        if len(fields) != len(RAW_MANIFEST_HEADER):
            raise RegisterError(f"refused raw manifest {path}: field count")
        row = dict(zip(RAW_MANIFEST_HEADER, fields))
        rel = PurePosixPath(row["path"])
        if rel.is_absolute() or ".." in rel.parts or row["path"] in rows:
            raise RegisterError(f"refused raw manifest {path}: invalid/duplicate path {row['path']}")
        rows[row["path"]] = row
    return rows


def _repo_path(path: str, repo: Path) -> str | None:
    candidate = PurePosixPath(path)
    if ".." in candidate.parts:
        return None
    if candidate.is_absolute():
        try:
            return candidate.relative_to(repo.as_posix()).as_posix()
        except ValueError:
            return None
    return candidate.as_posix()


def _resolve(ev: dict, info: dict, audit_dir: Path, repo: Path, raw: dict,
             tracked: set[str], store_root: Path | None, methodology: bool) -> tuple:
    path, line, quote = ev.get("file_path"), ev.get("line_number"), ev.get("quoted_line")
    if (not isinstance(path, str) or not path or type(line) is not int or line < 1
            or not isinstance(quote, str) or not quote.strip()):
        return None, None, "invalid citation file_path/line_number/quoted_line"
    rel_repo = _repo_path(path, repo)
    live = repo / rel_repo if rel_repo else Path(path)
    rel = audit_relative(str(live), audit_dir, info)
    details = []

    def matches(payload: bytes) -> bool:
        return quoted_line_matches(payload.decode("utf-8", errors="replace"), line, quote)

    if rel is not None:
        local = audit_dir / rel
        repo_rel = local.relative_to(repo).as_posix()
        if repo_rel in tracked or rel in raw:
            try:
                payload = local.read_bytes()
                sha = hashlib.sha256(payload).hexdigest()
                expected = (hashlib.sha256(git_read(repo, "cat-file", "-p", f"HEAD:{repo_rel}")).hexdigest()
                            if repo_rel in tracked else raw[rel]["sha256"])
                if sha == expected and matches(payload):
                    return "a", sha, f"audit file {rel}"
                details.append(f"audit-file-mismatch:{rel}")
            except (OSError, RegisterError) as exc:
                details.append(str(exc))
    pack = info["root"] / "pack.xml"
    contexts = []
    for receipt in info["receipts"]:
        if receipt["call"]:
            contexts.extend(receipt["value"].get("referenced_context") or [])
    try:
        payload = pack.read_bytes()
        sha = hashlib.sha256(payload).hexdigest()
        anchored = any(record.get("sha256") == sha
                       and audit_relative(record.get("path", ""), audit_dir, info)
                       == pack.relative_to(audit_dir).as_posix() for record in contexts)
        if anchored:
            for entry, content in pack_entries(pack):
                if _repo_path(entry, repo) == rel_repo and rel_repo is not None:
                    encoded = content.encode("utf-8")
                    if matches(encoded):
                        line_map = ",".join(str(n) for n in range(1, len(content.splitlines()) + 1))
                        return "b", hashlib.sha256(encoded).hexdigest(), f"pack entry {entry}; line_map:{line_map}"
        else:
            details.append("pack-unanchored")
    except (OSError, RegisterError) as exc:
        details.append(str(exc))
    if methodology and store_root is not None:
        for record in contexts:
            same = (_repo_path(record.get("path", ""), repo) == rel_repo if rel_repo is not None
                    else record.get("path") == path)
            if not same:
                continue
            try:
                read_store_id(store_root)
                blob = cas_path(store_root, record.get("sha256"))
                if blob.is_symlink():
                    raise RegisterError(f"refused CAS citation symlink {blob}")
                payload = blob.read_bytes()
                sha = hashlib.sha256(payload).hexdigest()
                if sha == record["sha256"] and matches(payload):
                    return "c", sha, "referenced_context CAS snapshot"
                details.append(f"CAS-citation-mismatch:{record['path']}")
            except (OSError, RuntimeError) as exc:
                details.append(str(exc))
    scope = info["scope"]
    if scope.get("schema_version") == "1.1" and scope.get("git_status") == "ok" and rel_repo:
        dirty = scope.get("dirty_paths")
        head = scope.get("git_head")
        if (not isinstance(dirty, list) or any(
                not isinstance(entry, list) or len(entry) not in (2, 3)
                or any(not isinstance(part, str) for part in entry) for entry in dirty)
                or not isinstance(head, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}", head)):
            details.append("invalid round git scope")
        elif not any(rel_repo in entry[1:] for entry in dirty):
            try:
                payload = git_read(repo, "cat-file", "-p", f"{head}:{rel_repo}")
                if matches(payload):
                    return "d", hashlib.sha256(payload).hexdigest(), f"round git_head:{head}"
                details.append("round-git-quote-mismatch")
            except RegisterError as exc:
                details.append(str(exc))
        else:
            details.append(f"round-dirty-path:{rel_repo}")
    return None, None, "; ".join(details) or "no persisted audit-time source"


def citation_resolvability(audit_dir: Path, *, audit_id: str, repo: Path,
                           store_root: Path | None) -> dict:
    """Refuse incomplete finding/decision joins and unresolved verified or adopted citations."""
    audit_dir, repo = audit_dir.resolve(), repo.resolve()
    result = {"result": "pass", "citations": [], "failing": [], "unverified_at_audit": [], "errors": []}
    try:
        if not audit_dir.is_relative_to(repo):
            raise RegisterError(f"refused citation audit directory {audit_dir}: outside repo {repo}")
        raw = _raw_rows(audit_dir)
        tracked = set(git_read(repo, "ls-files", "-z").decode("utf-8").split("\0"))
        methodology = effective_class(repo, audit_id) == "methodology-bearing"
        rounds = round_inputs(audit_dir, audit_id)
        for info in rounds:
            try:
                converge = read_json(info["root"] / "converge.json")
                index = converge.get("finding_index") if isinstance(converge, dict) else None
                if not isinstance(index, list):
                    raise RegisterError(f"refused finding_index round {info['number']}: missing array")
                by_uid = {}
                for entry in index:
                    uid = entry.get("finding_uid") if isinstance(entry, dict) else None
                    if not isinstance(uid, str) or uid in by_uid or type(entry.get("verified")) is not bool:
                        raise RegisterError(f"refused finding_index round {info['number']}: invalid/duplicate uid {uid}")
                    by_uid[uid] = entry
                findings = findings_in(info)
                joined = []
                occurrences = Counter()
                for step, persona, finding in findings:
                    uid = stable_finding_uid(audit_id, info["number"], step, persona, finding)
                    occurrences[uid] += 1
                    if uid not in by_uid:
                        result["errors"].append(f"round {info['number']} finding uid missing from finding_index: {uid}")
                        continue
                    if any(by_uid[uid].get(key) != value for key, value in
                           (("step", step), ("persona", persona), ("round", info["number"]))):
                        result["errors"].append(f"round {info['number']} finding_index identity mismatch: {uid}")
                    joined.append((uid, finding, by_uid[uid]["verified"]))
                for uid in sorted(by_uid):
                    if occurrences[uid] != by_uid[uid].get("source_occurrences", 1):
                        result["errors"].append(f"round {info['number']} finding_index coverage mismatch: {uid}")
                decisions = decisions_in(info)
                adopted = set()
                decision_evidence = []
                seen = set()
                for row in decisions:
                    uid = row.get("finding_uid")
                    if uid not in by_uid or uid in seen:
                        result["errors"].append(f"round {info['number']} resolution missing/duplicate finding_uid join: {uid}")
                        continue
                    seen.add(uid)
                    if row.get("resolution") not in ("retracted", "addressed", "carried_forward", "user_overruled"):
                        result["errors"].append(f"round {info['number']} invalid resolution: {uid}")
                    is_adopted = row.get("resolution") != "retracted"
                    if is_adopted:
                        adopted.add(uid)
                    if "evidence" in row:
                        decision_evidence.append((uid, row["evidence"], is_adopted))
                citations = [(uid, finding.get("evidence"), verified, uid in adopted, "finding")
                             for uid, finding, verified in joined]
                citations.extend((uid, ev, True, is_adopted, "decision-evidence")
                                 for uid, ev, is_adopted in decision_evidence)
                for uid, ev, verified, is_adopted, origin in citations:
                    ev = ev if isinstance(ev, dict) else {}
                    source, sha, detail = _resolve(ev, info, audit_dir, repo, raw, tracked, store_root, methodology)
                    unverified = not verified and not is_adopted
                    entry = {"round": info["number"], "finding_uid": uid, "origin": origin,
                             "file_path": ev.get("file_path"), "line_number": ev.get("line_number"),
                             "verified": verified, "adopted": is_adopted,
                             "status": "unverified-at-audit" if unverified else "resolved" if source else "unresolved",
                             "source": source, "source_sha256": sha,
                             "detail": "unverified-at-audit (leg error); " + detail if unverified else detail}
                    result["citations"].append(entry)
                    if unverified:
                        result["unverified_at_audit"].append(entry)
                    elif source is None:
                        result["failing"].append(entry)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError) as exc:
                result["errors"].append(f"round {info['number']}: {exc}")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result["errors"].append(str(exc))
    for key in ("citations", "failing", "unverified_at_audit"):
        result[key].sort(key=lambda entry: json.dumps(entry, sort_keys=True))
    result["errors"] = sorted(set(result["errors"]))
    if result["errors"] or result["failing"]:
        result["result"] = "fail"
    return result
