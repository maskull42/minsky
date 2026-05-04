#!/usr/bin/env python3
"""
validate-findings.py — strict schema validation for one minsky findings JSON.

This is used by invoke wrappers before a persona walk is recorded as successful.
It intentionally fails loud if jsonschema is unavailable; schema validation is a
runtime integrity requirement for the audit chain, not an optional nicety.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import jsonschema
except Exception as exc:  # pragma: no cover - environment failure path
    print(
        "validate-findings: jsonschema is required for strict validation "
        f"but could not be imported: {exc}",
        file=sys.stderr,
    )
    sys.exit(2)


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_SCHEMA = SKILL_DIR / "schemas" / "findings.schema.json"


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in {path}: {exc}") from None


def validate(path: Path, schema_path: Path, persona: str | None) -> list[str]:
    data = load_json(path)
    schema = load_json(schema_path)

    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(data), key=lambda e: e.path):
        where = "/".join(str(part) for part in err.absolute_path) or "(root)"
        errors.append(f"{where}: {err.message}")

    if persona is not None and isinstance(data, dict):
        got = data.get("persona")
        if got != persona:
            errors.append(f"persona: expected {persona!r}, got {got!r}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate one minsky findings JSON against findings.schema.json"
    )
    parser.add_argument("path", help="Path to findings JSON")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA), help="Schema path")
    parser.add_argument("--persona", help="Expected persona slug")
    args = parser.parse_args(argv)

    try:
        errors = validate(Path(args.path), Path(args.schema), args.persona)
    except RuntimeError as exc:
        print(f"validate-findings: ERROR — {exc}", file=sys.stderr)
        return 1

    if errors:
        print(f"validate-findings: ERROR — {args.path} failed schema validation", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"validate-findings: OK — {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
