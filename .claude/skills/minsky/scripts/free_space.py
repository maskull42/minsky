"""Free-space policy; refuse malformed policy or production floors and insufficient headroom."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import shutil
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
_LOG = logging.getLogger(__name__)


class FreeSpaceError(RuntimeError):
    """A policy, production source or free-space check was refused."""


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise FreeSpaceError(f"refused {label}: expected a non-negative int, got {value!r}")
    return value


def _int_expression(node: ast.AST) -> int:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult, ast.Pow)):
        left, right = _int_expression(node.left), _int_expression(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left ** right
    raise FreeSpaceError("expected an int expression of int constants with + * ** only")


def _production_floor(source: object, repo_root: Path) -> int:
    """Refuse unsupported floors; the scope-blind binding guard also refuses local reuse."""
    return _production_floor_with_sha256(source, repo_root)[0]


def _production_floor_with_sha256(source: object, repo_root: Path) -> tuple[int, str]:
    """Hash the parsed bytes; refuse unsupported assignments and scope-blind name reuse.

    Function parameters (ast.arg) stay uncounted. A parameter cannot rebind the module-level name.
    """
    if not isinstance(source, dict):
        raise FreeSpaceError(f"refused production_floor_source: expected path and name, got {source!r}")
    source_path, name = source.get("path"), source.get("name")
    label = f"production_floor_source file {source_path!r} name {name!r}"
    if not isinstance(source_path, str) or not source_path or Path(source_path).is_absolute():
        raise FreeSpaceError(f"refused {label}: path must be repo-relative")
    if not isinstance(name, str) or not name.isidentifier():
        raise FreeSpaceError(f"refused {label}: name must be an identifier")
    filename = repo_root / source_path
    try:
        if not filename.resolve().is_relative_to(repo_root.resolve()):
            raise FreeSpaceError("source path is outside repo_root")
        source_bytes = filename.read_bytes()
        tree = ast.parse(source_bytes.decode("utf-8"), filename=str(filename))
        star_import = any(isinstance(node, ast.alias) and node.name == "*" for node in ast.walk(tree))
        count = sum(
            (isinstance(node, ast.Name) and node.id == name
             and isinstance(node.ctx, (ast.Store, ast.Del)))
            or (isinstance(node, ast.alias) and (node.name == "*" or node.asname == name
                or (node.asname is None and node.name.split(".")[0] == name)))
            or (isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names)
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                  ast.ExceptHandler, ast.MatchAs, ast.MatchStar,
                                  ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)) and node.name == name)
            or (isinstance(node, ast.MatchMapping) and node.rest == name)
            for node in ast.walk(tree)
        )
        if count != 1:
            raise FreeSpaceError(
                f"named floor {name!r} must have exactly one binding in the source; found {count}"
                f"{' (a star import may bind it)' if star_import else ''}"
            )
        assignments = []
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
                targets = [statement.target]
            else:
                continue
            if any(isinstance(node, ast.Name) and node.id == name
                   for target in targets for node in ast.walk(target)):
                if isinstance(statement, ast.AugAssign) or not all(
                    isinstance(target, ast.Name) for target in targets
                ):
                    raise FreeSpaceError("named module-level assignment has an unsupported shape")
                assignments.append(statement.value)
        if not assignments:
            raise FreeSpaceError("named module-level assignment is missing")
        if len(assignments) != 1:
            raise FreeSpaceError("named module-level assignment is ambiguous")
        floor = _nonnegative_int(_int_expression(assignments[0]), "production floor")
        return floor, hashlib.sha256(source_bytes).hexdigest()
    except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError, FreeSpaceError) as exc:
        raise FreeSpaceError(f"refused {label} ({filename}): {exc}") from exc


def load_policy(config_path: Path | None = None, repo_root: Path | None = None) -> dict:
    """Load policy; refuse missing or malformed config and unsupported production assignments."""
    config_path = config_path if config_path is not None else SKILL_DIR / "config" / "free_space.json"
    repo_root = repo_root if repo_root is not None else SKILL_DIR.parents[2]
    try:
        config_bytes = config_path.read_bytes()
        config = json.loads(config_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise FreeSpaceError(f"refused free-space config {config_path}: {exc}") from exc
    if not isinstance(config, dict) or config.get("schema") != "minsky-free-space/1":
        raise FreeSpaceError(f"refused free-space config {config_path}: wrong schema")
    floor_min = _nonnegative_int(config.get("floor_bytes_min"), f"{config_path} floor_bytes_min")
    margin = _nonnegative_int(config.get("margin_bytes"), f"{config_path} margin_bytes")
    if "production_floor_source" not in config:
        raise FreeSpaceError(f"refused free-space config {config_path}: missing production_floor_source")
    source = config["production_floor_source"]
    production_floor, production_sha256 = (None, None) if source is None else (
        _production_floor_with_sha256(source, repo_root)
    )
    if source is None:
        _LOG.info("free-space config %s: production_floor_source null opt-out", config_path)
    return {
        "floor_bytes": max(floor_min, production_floor) if production_floor is not None else floor_min,
        "margin_bytes": margin,
        "floor_bytes_min": floor_min,
        "production_floor_bytes": production_floor,
        "production_floor_source": source,
        "origin": "config",
        "config_path": str(config_path),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "production_source_sha256": production_sha256,
        "production_floor_opt_out": source is None,
    }


def check_free_space(target: Path, incoming_bytes: int, policy: dict | None = None,
                     *, disk_usage=shutil.disk_usage) -> dict:
    """Refuse invalid byte counts or writes leaving less than the policy floor plus margin."""
    incoming_bytes = _nonnegative_int(incoming_bytes, f"{target} incoming_bytes")
    policy = load_policy() if policy is None else policy
    if not isinstance(policy, dict):
        raise FreeSpaceError(f"refused free-space policy for {target}: expected a dict")
    floor = _nonnegative_int(policy.get("floor_bytes"), f"{target} floor_bytes")
    margin = _nonnegative_int(policy.get("margin_bytes"), f"{target} margin_bytes")
    try:
        free = _nonnegative_int(disk_usage(target).free, f"{target} free_bytes")
    except OSError as exc:
        raise FreeSpaceError(f"refused disk usage for {target}: {exc}") from exc
    result = {
        "target": str(target), "free_bytes": free, "incoming_bytes": incoming_bytes,
        "floor_bytes": floor, "margin_bytes": margin, "required_bytes": floor + margin,
        "free_after_bytes": free - incoming_bytes,
        "policy_origin": policy.get("origin", "caller"),
        "config_path": policy.get("config_path"),
        "config_sha256": policy.get("config_sha256"),
        "floor_bytes_min": policy.get("floor_bytes_min"),
        "production_floor_bytes": policy.get("production_floor_bytes"),
        "production_floor_source": policy.get("production_floor_source"),
        "production_source_sha256": policy.get("production_source_sha256"),
        "production_floor_opt_out": policy.get("production_floor_opt_out"),
    }
    byte_fields = {"free_bytes", "incoming_bytes", "floor_bytes", "margin_bytes", "required_bytes",
                   "free_after_bytes", "floor_bytes_min", "production_floor_bytes"}
    detail = ", ".join(f"{key}={value}" + (" bytes" if key in byte_fields and type(value) is int else "")
                       for key, value in result.items() if key != "target")
    _LOG.info("free-space check target=%s: %s", target, detail)
    if result["free_after_bytes"] < result["required_bytes"]:
        raise FreeSpaceError(f"refused free-space check target={target}: {detail}")
    return result
