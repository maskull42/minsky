#!/usr/bin/env python3
"""Refuse mismatched provider bindings and existing receipts before detaching a chain."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from provenance import parse_step_models, parse_string_list


SKILL_DIR = Path(__file__).resolve().parent.parent
ENV_KEYS = (
    "CODEX_MODEL", "CODEX_EFFORT", "CODEX_TIMEOUT_SECONDS",
    "OPENCODE_MODEL", "OPENCODE_VARIANT", "OPENCODE_TIMEOUT_SECONDS",
)


class Launch(NamedTuple):
    """Validated argv, environment, detached wrapper and five create-only receipts."""

    argv: list[str]
    env: dict[str, str]
    wrapper: str
    paths: dict[str, Path]


def _refuse_existing(paths: dict[str, Path]) -> None:
    for path in paths.values():
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        raise ValueError(f"refusing existing chain target {path}; use --run-label")


def _shell_wrapper(argv: list[str], paths: dict[str, Path]) -> str:
    lines = ["set -o noclobber", "{"]
    for key in ENV_KEYS:
        lines += [
            f'  if [ "${{{key}+x}}" = x ]; then',
            f'    printf \'%s=%s\\n\' {key} "${{{key}}}"',
            "  else",
            f"    printf '%s=<unset>\\n' {key}",
            "  fi",
        ]
    lines += [
        "  printf '%s\\n' " + shlex.quote("CHAIN_ARGV=" + shlex.join(argv)),
        "} > " + shlex.quote(str(paths["env"])) + " || exit 1",
        shlex.join(argv) + " > " + shlex.quote(str(paths["stdout"])) +
        " 2> " + shlex.quote(str(paths["stderr"])),
        "chain_status=$?",
        'printf \'%s\\n\' "$chain_status" > ' + shlex.quote(str(paths["exit"])),
    ]
    return "\n".join(lines)


def build_launch(args: argparse.Namespace) -> Launch:
    """Refuse conflicting model bindings, receipt collisions and argument overrides."""
    repo = SKILL_DIR.parents[2]
    models = parse_string_list(args.models, "--models", model=True)
    bindings = parse_step_models(args.step_models, models)
    codex_stamp = f"{args.codex_model}@{args.codex_effort}"
    if bindings["codex"] != codex_stamp:
        raise ValueError(
            f"refusing --step-models codex binding {bindings['codex']!r}; "
            f"--codex-model/--codex-effort require {codex_stamp!r}"
        )
    if (args.opencode_variant is not None and
            bindings["opencode"].rsplit("@", 1)[1] != args.opencode_variant):
        raise ValueError(
            f"refusing --step-models opencode binding {bindings['opencode']!r}; "
            f"effort differs from --opencode-variant {args.opencode_variant!r}"
        )
    if args.run_label is not None and not re.fullmatch(r"[a-z0-9-]+", args.run_label):
        raise ValueError(f"refusing --run-label {args.run_label!r}; expected [a-z0-9-]+")
    round_dir = Path(args.round_dir).resolve()
    if not round_dir.is_dir():
        raise ValueError(f"refusing missing --round-dir {round_dir}")
    pack = Path(args.pack).resolve()
    if not pack.is_file():
        raise ValueError(f"refusing missing --pack {pack}")
    stem = "chain" + (f"-{args.run_label}" if args.run_label is not None else "")
    paths = {key: round_dir / f"{stem}.{suffix}" for key, suffix in (
        ("stdout", "stdout.log"), ("stderr", "stderr.log"), ("exit", "exit"),
        ("pid", "pid"), ("env", "env.txt"),
    )}
    _refuse_existing(paths)
    extra = list(args.extra)
    if extra[:1] == ["--"]:
        extra = extra[1:]
    protected = ("--audit-id", "--round", "--round-dir", "--pack",
                 "--personas", "--models", "--step-models")
    for arg in extra:
        option = arg.split("=", 1)[0]
        if option.startswith("--") and len(option) > 2 and any(
                name.startswith(option) for name in protected):
            raise ValueError(f"refusing extra chain argument overriding registered scope: {arg}")
    argv = [
        sys.executable, str(SKILL_DIR / "scripts" / "chain.py"), "adversaries",
        "--audit-id", args.audit_id, "--round", str(args.round),
        "--round-dir", str(round_dir), "--pack", str(pack),
        "--personas", args.personas, "--models", ",".join(models),
        "--step-models", args.step_models, *extra,
    ]
    env = {key: value for key, value in os.environ.items() if not key.startswith("OPENCODE_")}
    env["CODEX_MODEL"] = args.codex_model
    env["CODEX_EFFORT"] = args.codex_effort
    for key, value in (
        ("CODEX_TIMEOUT_SECONDS", args.codex_timeout),
        ("OPENCODE_MODEL", args.opencode_model),
        ("OPENCODE_VARIANT", args.opencode_variant),
        ("OPENCODE_TIMEOUT_SECONDS", args.opencode_timeout),
    ):
        if value is not None:
            env[key] = str(value)
    env["PATH"] = str(repo / ".venv" / "bin") + os.pathsep + env.get("PATH", "")
    for key in ENV_KEYS:
        if key in env and ("\n" in env[key] or "\r" in env[key]):
            raise ValueError(f"refusing multiline chain environment value: {key}")
    if any("\n" in arg or "\r" in arg for arg in argv):
        raise ValueError("refusing multiline chain argv in CHAIN_ARGV receipt")
    return Launch(argv, env, _shell_wrapper(argv, paths), paths)


def main(argv: list[str] | None = None) -> int:
    """Refuse unsafe launches and write the detached shell's pid without replacement."""
    parser = argparse.ArgumentParser(description="Launch one Minsky adversaries chain detached")
    parser.add_argument("--audit-id", required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--round-dir", required=True)
    parser.add_argument("--pack", required=True)
    parser.add_argument("--personas", required=True, help="Comma-separated persona slugs")
    parser.add_argument("--models", required=True, help="Explicit JSON array of model@effort stamps")
    parser.add_argument("--step-models", required=True)
    parser.add_argument("--codex-model", required=True)
    parser.add_argument("--codex-effort", required=True)
    parser.add_argument("--codex-timeout", type=int)
    parser.add_argument("--opencode-model")
    parser.add_argument("--opencode-variant")
    parser.add_argument("--opencode-timeout", type=int)
    parser.add_argument("--run-label")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        launch = build_launch(args)
        _refuse_existing(launch.paths)
        with launch.paths["pid"].open("x", encoding="utf-8") as pid_file:
            try:
                proc = subprocess.Popen(
                    ["/bin/bash", "-c", launch.wrapper], cwd=str(SKILL_DIR.parents[2]),
                    env=launch.env, start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                launch.paths["pid"].unlink()
                raise
            pid_file.write(f"{proc.pid}\n")
    except (OSError, ValueError) as exc:
        print(f"launch-chain: {exc}", file=sys.stderr)
        return 1
    print(f"launched pid {proc.pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
