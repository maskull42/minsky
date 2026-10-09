#!/usr/bin/env python3
"""Check release bytes; refuse sensitive names, text and unused exact allowances."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                   r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")
ENV = re.compile(r"^\s*(export\s+)?[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*\s*=\s*\S+")
DATABASE = re.compile(r"(?:\.(?:db|sqlite|sqlite3)|-wal|-shm)$")


class PublishError(RuntimeError):
    """A release tree or allowance input was refused."""


def _walk_error(error: OSError) -> None:
    raise error


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PublishError(f"refused input {path}: {exc}") from exc


def check_tree(tree: Path, *, deny_file: Path | None = None,
               allow_file: Path | None = None) -> dict:
    """Scan all files except .git; refuse symlinks, special files and unused allowances."""
    if tree.is_symlink() or not tree.is_dir():
        raise PublishError(f"refused tree {tree}: expected a directory, not a symlink")
    literals = _lines(deny_file) if deny_file else []
    if any(not value for value in literals):
        raise PublishError(f"refused deny-file {deny_file}: empty literal")
    allowances = []
    for number, line in enumerate(_lines(allow_file) if allow_file else [], 1):
        fields = line.split("\t")
        if len(fields) != 4 or any(not value for value in fields) or not fields[3].strip():
            raise PublishError(f"refused allow-file {allow_file} line {number}: expected four nonempty TSV fields")
        allowances.append(dict(zip(("path", "rule", "matched_text", "reason"), fields)))
    hits = []
    skipped = []
    count = 0

    def hit(rule: str, path: str, line: int, matched_text: str) -> None:
        hits.append({"rule": rule, "path": path, "line": line, "matched_text": matched_text})

    for directory, dirs, files in os.walk(tree, followlinks=False, onerror=_walk_error):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink():
                raise PublishError(f"refused symlink directory {path}")
        for name in sorted(files):
            if name == ".git":
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise PublishError(f"refused nonregular file {path}")
            relative = path.relative_to(tree).as_posix()
            count += 1
            if name == ".env" or name.startswith(".env."):
                hit("env-content", relative, 0, name)
            if DATABASE.search(name):
                hit("database", relative, 0, name)
            if "codex-audits" in path.relative_to(tree).parts or relative == ".minsky/audits.db":
                hit("audit-records", relative, 0, relative)
            payload = path.read_bytes()
            if b"\0" in payload[:8192]:
                skipped.append(relative)
                continue
            for number, line in enumerate(payload.decode("utf-8", errors="replace").splitlines(), 1):
                for match in re.finditer(r"/Users/", line):
                    hit("users-path", relative, number, match.group())
                for match in EMAIL.finditer(line):
                    hit("email", relative, number, match.group())
                match = ENV.match(line)
                if match:
                    hit("env-content", relative, number, match.group())
                for literal in literals:
                    start = 0
                    while (start := line.find(literal, start)) != -1:
                        hit("deny-literal", relative, number, literal)
                        start += len(literal)
    allowed = []
    remaining = []
    unused = list(allowances)
    for item in sorted(hits, key=lambda h: (h["path"], h["line"], h["rule"], h["matched_text"])):
        entry = next((a for a in unused if all(a[key] == item[key]
                                             for key in ("path", "rule", "matched_text"))), None)
        if entry is None:
            remaining.append(item)
        else:
            unused.remove(entry)
            allowed.append({**item, "reason": entry["reason"]})
    errors = [f"unused allow entry: {a['path']}\t{a['rule']}\t{a['matched_text']}" for a in unused]
    return {"schema": "minsky-publish-check/1", "tree": str(tree), "file_count": count,
            "hits": remaining, "allowed": allowed, "skipped_binary": sorted(skipped),
            "errors": errors, "result": "error" if errors else "hits" if remaining else "clean"}


def main(argv: list[str] | None = None) -> int:
    """Write a new report; refuse outputs inside the tree, existing outputs and unreadable inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--deny-file", type=Path)
    parser.add_argument("--allow-file", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.json_out.resolve().is_relative_to(args.tree.resolve()):
            raise PublishError(f"refused json-out {args.json_out}: resolves inside tree {args.tree}")
        if args.json_out.exists() or args.json_out.is_symlink():
            raise PublishError(f"refused json-out {args.json_out}: already exists")
        report = check_tree(args.tree, deny_file=args.deny_file, allow_file=args.allow_file)
        with args.json_out.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        for error in report["errors"]:
            sys.stderr.write(error + "\n")
        return {"clean": 0, "hits": 1, "error": 2}[report["result"]]
    except (OSError, UnicodeError, PublishError) as exc:
        sys.stderr.write(f"publish-check: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
