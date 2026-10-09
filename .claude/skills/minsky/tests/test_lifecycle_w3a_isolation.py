"""KB-sized W3a regressions for progress paths, free-space policy and session isolation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import conftest as isolation
import free_space
import progress
from test_hardening import MODELS, PERSONA, SCRIPTS, load_script


def _assert_progress_isolation(module: ModuleType, root: Path) -> None:
    assert module.progress_root() == root
    path = module.emit("w3a-progress", "audit_open", mode="audit")
    assert path == root / "w3a-progress" / "progress.ndjson"
    assert json.loads(path.read_text(encoding="utf-8"))["event"] == "audit_open"
    default_dir = progress.REPO_ROOT / "codex-audits" / "w3a-progress"
    assert not default_dir.exists() and not default_dir.is_symlink()


def _write_policy(tmp_path: Path, **overrides) -> Path:
    config = {
        "schema": "minsky-free-space/1", "floor_bytes_min": 10, "margin_bytes": 4,
        "production_floor_source": None,
        **overrides,
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _run_audit_db(db: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "audit-db.py"), "--db", str(db), *args],
        text=True, capture_output=True, env=env,
    )


def test_progress_environment_isolates_emission(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "events"
    monkeypatch.setenv("MINSKY_PROGRESS_ROOT", str(root))
    _assert_progress_isolation(progress, root)


def test_progress_environment_mutant_is_killed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "events"
    monkeypatch.setenv("MINSKY_PROGRESS_ROOT", str(root))
    original = (SCRIPTS / "progress.py").read_text(encoding="utf-8")
    start = original.index("def progress_root() -> Path:")
    end = original.index("\n\ndef progress_path(", start)
    mutant = original[:start] + (
        'def progress_root() -> Path:\n    return REPO_ROOT / "codex-audits"\n'
    ) + original[end:]
    copy = tmp_path / "progress_ignores_environment.py"
    copy.write_text(mutant, encoding="utf-8")
    module = load_script("w3a_progress_ignores_environment", str(copy))
    # The same acceptance assertion kills the named mutant before it can write.
    with pytest.raises(AssertionError):
        _assert_progress_isolation(module, root)
    assert not root.exists()


@pytest.mark.parametrize("value", ["relative/events", ""])
def test_progress_environment_refuses_invalid_roots(monkeypatch, value: str) -> None:
    monkeypatch.setenv("MINSKY_PROGRESS_ROOT", value)
    with pytest.raises(ValueError, match="MINSKY_PROGRESS_ROOT"):
        progress.emit("w3a-progress", "audit_open")


def test_progress_default_and_explicit_roots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MINSKY_PROGRESS_ROOT", raising=False)
    assert progress.progress_root() == progress.REPO_ROOT / "codex-audits"
    assert progress.progress_path("w3a-progress") == (
        progress.REPO_ROOT / "codex-audits" / "w3a-progress" / "progress.ndjson"
    )
    monkeypatch.setenv("MINSKY_PROGRESS_ROOT", "invalid-relative-root")
    explicit = tmp_path / "explicit"
    assert progress.progress_path("w3a-progress", explicit) == explicit / "w3a-progress" / "progress.ndjson"
    assert progress.emit_to(explicit, "w3a-progress", "audit_close") == (
        explicit / "w3a-progress" / "progress.ndjson"
    )


@pytest.mark.parametrize("audit_id,event,fields,message", [
    ("bad/id", "audit_open", {}, "audit_id"),
    (".hidden", "audit_open", {}, "audit_id"),
    ("", "audit_open", {}, "audit_id"),
    ("valid", "unknown", {}, "unknown event"),
    ("valid", "audit_open", {"message": "x" * 512}, "POSIX-atomic limit"),
])
def test_emit_to_preserves_refusals(tmp_path: Path, audit_id: str, event: str,
                                   fields: dict, message: str) -> None:
    root = tmp_path / "events"
    with pytest.raises(ValueError, match=message):
        progress.emit_to(root, audit_id, event, **fields)
    assert not root.exists()


@pytest.mark.parametrize("override", [False, True])
def test_audit_db_progress_root_derivation_and_override(tmp_path: Path, override: bool) -> None:
    db = tmp_path / "x" / "audit.db"
    env = dict(os.environ)
    env.pop("MINSKY_PROGRESS_ROOT", None)
    expected = db.parent / "progress"
    if override:
        expected = tmp_path / "override-events"
        env["MINSKY_PROGRESS_ROOT"] = str(expected)
    initialized = _run_audit_db(db, env, "initdb")
    assert initialized.returncode == 0, initialized.stderr
    opened = _run_audit_db(
        db, env, "open", "--audit-id", "w3a-db", "--branch", "test", "--commit", "0" * 40,
        "--mode", "audit", "--scope", "explicit", "--files", '["artifact.md"]',
        "--personas", json.dumps([PERSONA]), "--models", json.dumps([MODELS[1]]),
    )
    assert opened.returncode == 0, opened.stderr
    closed = _run_audit_db(
        db, env, "close", "--audit-id", "w3a-db", "--commit", "0" * 40,
        "--rounds", "1", "--convergence", "agree",
    )
    assert closed.returncode == 0, closed.stderr
    events = [json.loads(line) for line in (
        expected / "w3a-db" / "progress.ndjson"
    ).read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["audit_open", "audit_close"]
    if override:
        assert not (db.parent / "progress").exists()


def test_audit_db_emit_failure_remains_nonfatal(tmp_path: Path, monkeypatch, capsys) -> None:
    helper = load_script("w3a_audit_db", "audit-db.py")
    monkeypatch.setattr(helper, "_PROGRESS_ROOT", tmp_path / "events")
    monkeypatch.setenv("MINSKY_PROGRESS_ROOT", "")
    helper._emit_safe("w3a-db", "audit_open")
    assert "progress emit failed (non-fatal)" in capsys.readouterr().err
    assert not (tmp_path / "events").exists()


def test_progress_tail_shares_progress_path(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "events"
    monkeypatch.setenv("MINSKY_PROGRESS_ROOT", str(root))
    tail = load_script("w3a_progress_tail", "progress-tail.py")
    path = progress.emit("w3a-tail", "audit_close", convergence_status="agree", num_rounds=1)
    assert tail.progress_path("w3a-tail") == progress.progress_path("w3a-tail") == path
    replay = subprocess.run(
        [sys.executable, str(SCRIPTS / "progress-tail.py"), "w3a-tail", "--once", "--no-wait-for-file"],
        text=True, capture_output=True,
    )
    assert replay.returncode == 0, replay.stderr
    assert "CLOSE w3a-tail" in replay.stdout


def test_real_free_space_policy_reads_production_floor() -> None:
    policy = free_space.load_policy()
    repo = SCRIPTS.parents[3]
    config = SCRIPTS.parent / "config" / "free_space.json"
    source = json.loads(config.read_bytes())["production_floor_source"]
    if source is None:
        assert policy["production_floor_opt_out"] is True
        assert policy["production_floor_bytes"] is None
        assert policy["floor_bytes"] == policy["floor_bytes_min"]
        assert policy["production_source_sha256"] is None
        return
    assert policy == {
        "floor_bytes": 5 * 2**30, "margin_bytes": 2 * 2**30, "floor_bytes_min": 5 * 2**30,
        "production_floor_bytes": 5 * 2**30,
        "production_floor_source": {"path": "scripts/r3_continuous.py", "name": "MIN_FREE_BYTES"},
        "origin": "config", "config_path": str(config),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "production_source_sha256": hashlib.sha256((repo / source["path"]).read_bytes()).hexdigest(),
        "production_floor_opt_out": False,
    }


def test_free_space_policy_opt_out_is_explicit_and_logged(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="free_space"):
        policy = free_space.load_policy(_write_policy(tmp_path), tmp_path)
    assert policy["production_floor_bytes"] is None
    assert policy["production_floor_source"] is None
    assert policy["floor_bytes"] == policy["floor_bytes_min"] == 10
    assert "production_floor_source null opt-out" in caplog.text


@pytest.mark.parametrize("floor_min,expected", [(10, 32), (40, 40)])
def test_free_space_policy_parses_only_ast_without_execution(tmp_path: Path, floor_min: int,
                                                           expected: int) -> None:
    source = tmp_path / "production.py"
    source.write_text('FLOOR: int = (2 + 2) * 2**3\nraise RuntimeError("must never execute")\n', encoding="utf-8")
    config = _write_policy(
        tmp_path, floor_bytes_min=floor_min,
        production_floor_source={"path": source.name, "name": "FLOOR"},
    )
    policy = free_space.load_policy(config, tmp_path)
    assert policy["production_floor_bytes"] == 32
    assert policy["floor_bytes"] == expected


@pytest.mark.parametrize("text", [
    None, "OTHER = 5\n", "FLOOR = int(5)\n", "FLOOR = '5'\n", "FLOOR = -5\n",
    "FLOOR = True\n", "FLOOR = 5.0\n", "FLOOR = 5 - 1\n", "FLOOR = OTHER\n",
    "FLOOR =\n", "FLOOR = 5\nFLOOR = 6\n", "FLOOR = 5\nFLOOR += 1\n",
    "FLOOR, OTHER = (5, 6)\n", "FLOOR: int\n",
])
def test_free_space_policy_refuses_bad_production_source(tmp_path: Path, text: str | None) -> None:
    source = tmp_path / "production.py"
    if text is not None:
        source.write_text(text, encoding="utf-8")
    config = _write_policy(tmp_path, production_floor_source={"path": source.name, "name": "FLOOR"})
    with pytest.raises(free_space.FreeSpaceError) as exc:
        free_space.load_policy(config, tmp_path)
    assert source.name in str(exc.value) and "FLOOR" in str(exc.value)


@pytest.mark.parametrize("rebinding", [
    "if True:\n    FLOOR = 100\n",
    "try:\n    FLOOR = 100\nexcept Exception:\n    pass\n",
    "(FLOOR := 100)\n",
    "del FLOOR\n",
    "import os as FLOOR\n",
    "from x import FLOOR\n",
    "def f():\n    global FLOOR\n",
    "def f():\n    FLOOR = 1\n",
    "for FLOOR in ():\n    pass\n",
    "with open(__file__) as FLOOR:\n    pass\n",
    "import FLOOR\n",
    "def outer():\n    FLOOR = 1\n    def inner():\n        nonlocal FLOOR\n",
    "def FLOOR():\n    pass\n",
    "async def FLOOR():\n    pass\n",
    "class FLOOR:\n    pass\n",
    "try:\n    pass\nexcept Exception as FLOOR:\n    pass\n",
    "match 1:\n    case FLOOR:\n        pass\n",
    "match []:\n    case [*FLOOR]:\n        pass\n",
    "match {}:\n    case {**FLOOR}:\n        pass\n",
    "def f[FLOOR]():\n    pass\n",
    "def f[**FLOOR]():\n    pass\n",
    "def f[*FLOOR]():\n    pass\n",
    "import FLOOR.sub\n",
], ids=["conditional", "try", "walrus", "del", "import-alias", "from-import",
        "global", "function-local", "for", "with", "import-name", "nonlocal",
        "function-name", "async-function-name", "class-name", "except-target",
        "match-as", "match-star", "match-mapping", "type-var", "param-spec",
        "type-var-tuple", "dotted-import"])
def test_free_space_policy_refuses_rebindings(tmp_path: Path, rebinding: str) -> None:
    source = tmp_path / "production.py"
    source.write_text("FLOOR = 10\n" + rebinding, encoding="utf-8")
    config = _write_policy(tmp_path, production_floor_source={"path": source.name, "name": "FLOOR"})
    with pytest.raises(free_space.FreeSpaceError) as exc:
        free_space.load_policy(config, tmp_path)
    count = 3 if "nonlocal" in rebinding else 2
    assert str(exc.value) == (
        f"refused production_floor_source file 'production.py' name 'FLOOR' ({source}): "
        f"named floor 'FLOOR' must have exactly one binding in the source; found {count}"
    )


def test_free_space_policy_counts_only_the_bound_import_name(tmp_path: Path) -> None:
    source = tmp_path / "production.py"
    source.write_text("FLOOR = 10\nimport other.FLOOR\nimport FLOOR.sub as other\n"
                      "from FLOOR import other\n", encoding="utf-8")
    config = _write_policy(tmp_path, production_floor_source={"path": source.name, "name": "FLOOR"})
    assert free_space.load_policy(config, tmp_path)["production_floor_bytes"] == 10


@pytest.mark.parametrize("star_import", [
    "from os import *\n",
    "def f():\n    from os import *\n",
], ids=["module-level", "function-body"])
def test_free_space_policy_refuses_star_imports(tmp_path: Path, star_import: str) -> None:
    source = tmp_path / "production.py"
    source.write_text("FLOOR = 10\n" + star_import, encoding="utf-8")
    config = _write_policy(tmp_path, production_floor_source={"path": source.name, "name": "FLOOR"})
    with pytest.raises(free_space.FreeSpaceError) as exc:
        free_space.load_policy(config, tmp_path)
    assert str(exc.value) == (
        f"refused production_floor_source file 'production.py' name 'FLOOR' ({source}): "
        "named floor 'FLOOR' must have exactly one binding in the source; found 2"
        " (a star import may bind it)"
    )


def test_free_space_policy_leaves_function_parameters_uncounted(tmp_path: Path) -> None:
    source = tmp_path / "production.py"
    source.write_text("FLOOR = 10\ndef f(FLOOR):\n    return FLOOR\n", encoding="utf-8")
    config = _write_policy(tmp_path, production_floor_source={"path": source.name, "name": "FLOOR"})
    assert free_space.load_policy(config, tmp_path)["production_floor_bytes"] == 10


def test_production_floor_signature_still_returns_an_int(tmp_path: Path) -> None:
    config = SCRIPTS.parent / "config" / "free_space.json"
    source = json.loads(config.read_bytes())["production_floor_source"]
    repo = SCRIPTS.parents[3]
    if source is None:
        (tmp_path / "production.py").write_text("MIN_FREE_BYTES = 5 * 2**30\n", encoding="utf-8")
        source = {"path": "production.py", "name": "MIN_FREE_BYTES"}
        repo = tmp_path
    assert free_space._production_floor(source, repo) == 5368709120


def test_w10c_real_policy_tests_accept_public_opt_out(tmp_path: Path, monkeypatch, caplog) -> None:
    skill = tmp_path / "public-skill"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    config = skill / "config/free_space.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "schema": "minsky-free-space/1", "floor_bytes_min": 5368709120,
        "margin_bytes": 2147483648, "production_floor_source": None,
    }), encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "SCRIPTS", scripts)
    monkeypatch.setattr(free_space, "SKILL_DIR", skill)
    with caplog.at_level(logging.INFO, logger="free_space"):
        test_real_free_space_policy_reads_production_floor()
    test_production_floor_signature_still_returns_an_int(tmp_path)
    assert "production_floor_source null opt-out" in caplog.text
    assert not (skill / "scripts/r3_continuous.py").exists()


@pytest.mark.parametrize("opt_out", [False, True])
def test_free_space_policy_hashes_exact_decoded_bytes(tmp_path: Path, opt_out: bool) -> None:
    source = tmp_path / "production.py"
    source_bytes = '# caf\u00e9\r\nFLOOR = 10\r\n'.encode("utf-8")
    source.write_bytes(source_bytes)
    source_ref = None if opt_out else {"path": source.name, "name": "FLOOR"}
    config = _write_policy(tmp_path, production_floor_source=source_ref)
    config_bytes = config.read_bytes().replace(b", ", b",\r\n ") + b"\r\n"
    config.write_bytes(config_bytes)
    policy = free_space.load_policy(config, tmp_path)
    assert policy["config_sha256"] == hashlib.sha256(config_bytes).hexdigest()
    source_sha256 = None if opt_out else hashlib.sha256(source_bytes).hexdigest()
    assert policy["production_source_sha256"] == source_sha256
    assert policy["origin"] == "config" and policy["config_path"] == str(config)
    assert policy["production_floor_source"] == source_ref
    assert policy["production_floor_opt_out"] is opt_out


def test_free_space_policy_hashes_the_single_reads_it_parses(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "production.py"
    source_bytes = b"FLOOR = 32\n"
    source.write_bytes(source_bytes)
    config = _write_policy(tmp_path, production_floor_source={"path": source.name, "name": "FLOOR"})
    config_bytes = config.read_bytes()
    original_read = Path.read_bytes
    reads = []

    def changing_read(path: Path) -> bytes:
        payload = original_read(path)
        reads.append(path)
        if path == config:
            path.write_bytes(config_bytes.replace(b'"floor_bytes_min": 10', b'"floor_bytes_min": 100'))
        elif path == source:
            path.write_bytes(b"FLOOR = 100\n")
        return payload

    monkeypatch.setattr(Path, "read_bytes", changing_read)
    policy = free_space.load_policy(config, tmp_path)
    assert reads == [config, source]
    assert policy["floor_bytes_min"] == 10 and policy["production_floor_bytes"] == policy["floor_bytes"] == 32
    assert policy["config_sha256"] == hashlib.sha256(config_bytes).hexdigest()
    assert policy["production_source_sha256"] == hashlib.sha256(source_bytes).hexdigest()


def test_free_space_equal_policy_floors_preserve_distinct_opt_out_lineage(tmp_path: Path) -> None:
    source = tmp_path / "production.py"
    source.write_text("FLOOR = 10\n", encoding="utf-8")
    config = _write_policy(tmp_path, production_floor_source={"path": source.name, "name": "FLOOR"})
    configured = free_space.load_policy(config, tmp_path)
    opted_out = free_space.load_policy(_write_policy(tmp_path), tmp_path)
    assert configured["floor_bytes"] == opted_out["floor_bytes"] == 10
    assert configured != opted_out
    assert configured["production_floor_opt_out"] is False
    assert opted_out["production_floor_opt_out"] is True
    assert opted_out["production_floor_bytes"] is opted_out["production_source_sha256"] is None


def test_check_free_space_equal_floors_preserve_distinct_opt_out_lineage(tmp_path: Path) -> None:
    source = tmp_path / "production.py"
    source.write_text("FLOOR = 10\n", encoding="utf-8")
    config = _write_policy(tmp_path, production_floor_source={"path": source.name, "name": "FLOOR"})
    configured = free_space.load_policy(config, tmp_path)
    opted_out = free_space.load_policy(_write_policy(tmp_path), tmp_path)
    disk_usage = lambda _: SimpleNamespace(free=17)
    first = free_space.check_free_space(tmp_path, 3, configured, disk_usage=disk_usage)
    second = free_space.check_free_space(tmp_path, 3, opted_out, disk_usage=disk_usage)
    assert first["floor_bytes"] == second["floor_bytes"] == 10
    assert first["required_bytes"] == second["required_bytes"] == 14
    assert first != second
    assert first["production_floor_opt_out"] is False
    assert second["production_floor_opt_out"] is True
    assert second["production_floor_source"] is second["production_floor_bytes"] is None
    assert second["production_source_sha256"] is None


@pytest.mark.parametrize("floor_min,opt_out", [(10, False), (40, False), (10, True)])
def test_check_free_space_receipt_hashes_and_recomputes_policy(
        tmp_path: Path, caplog, floor_min: int, opt_out: bool) -> None:
    source = tmp_path / "production.py"
    source_bytes = b"FLOOR = (2 + 2) * 2**3\r\n"
    source.write_bytes(source_bytes)
    source_ref = None if opt_out else {"path": source.name, "name": "FLOOR"}
    config = _write_policy(tmp_path, floor_bytes_min=floor_min, production_floor_source=source_ref)
    config_bytes = config.read_bytes()
    policy = free_space.load_policy(config, tmp_path)
    with caplog.at_level(logging.INFO, logger="free_space"):
        result = free_space.check_free_space(tmp_path, 3, policy,
                                            disk_usage=lambda _: SimpleNamespace(free=100))
    assert result["policy_origin"] == "config" and result["config_path"] == str(config)
    assert result["config_sha256"] == hashlib.sha256(config_bytes).hexdigest()
    assert result["production_floor_source"] == source_ref
    assert result["production_source_sha256"] == (None if opt_out else hashlib.sha256(source_bytes).hexdigest())
    assert result["floor_bytes_min"] == floor_min
    assert result["production_floor_bytes"] == (None if opt_out else 32)
    assert result["production_floor_opt_out"] is opt_out
    floor = max(result["floor_bytes_min"], result["production_floor_bytes"]) if not opt_out else floor_min
    assert result["floor_bytes"] == floor
    assert result["required_bytes"] == floor + result["margin_bytes"]
    production_detail = "None" if opt_out else "32 bytes"
    assert caplog.messages[-1] == (
        f"free-space check target={tmp_path}: free_bytes=100 bytes, incoming_bytes=3 bytes, "
        f"floor_bytes={floor} bytes, margin_bytes=4 bytes, required_bytes={floor + 4} bytes, "
        f"free_after_bytes=97 bytes, policy_origin=config, config_path={config}, "
        f"config_sha256={result['config_sha256']}, floor_bytes_min={floor_min} bytes, "
        f"production_floor_bytes={production_detail}, production_floor_source={source_ref}, "
        f"production_source_sha256={result['production_source_sha256']}, production_floor_opt_out={opt_out}"
    )


@pytest.mark.parametrize("overrides", [
    {"schema": "wrong"}, {"floor_bytes_min": -1}, {"margin_bytes": -1},
    {"floor_bytes_min": True}, {"margin_bytes": 1.0}, {"floor_bytes_min": "10"},
    {"production_floor_source": {}}, {"production_floor_source": False},
    {"production_floor_source": {"path": "/outside.py", "name": "FLOOR"}},
    {"production_floor_source": {"path": "../outside.py", "name": "FLOOR"}},
    {"production_floor_source": {"path": "production.py", "name": "not a name"}},
])
def test_free_space_policy_refuses_invalid_config_values(tmp_path: Path, overrides: dict) -> None:
    with pytest.raises(free_space.FreeSpaceError, match="refused"):
        free_space.load_policy(_write_policy(tmp_path, **overrides), tmp_path)


@pytest.mark.parametrize("text", [None, "{truncated", "[]", '{"schema":"minsky-free-space/1"}',
                                  '{"schema":"minsky-free-space/1","floor_bytes_min":10,"margin_bytes":4}'])
def test_free_space_policy_refuses_missing_or_unparsable_config(tmp_path: Path, text: str | None) -> None:
    config = tmp_path / "policy.json"
    if text is not None:
        config.write_text(text, encoding="utf-8")
    with pytest.raises(free_space.FreeSpaceError) as exc:
        free_space.load_policy(config, tmp_path)
    assert str(config) in str(exc.value)


def test_check_free_space_exact_boundary_and_logged_bytes(tmp_path: Path, caplog) -> None:
    policy = {"floor_bytes": 10, "margin_bytes": 4}
    with caplog.at_level(logging.INFO, logger="free_space"):
        result = free_space.check_free_space(tmp_path, 3, policy, disk_usage=lambda _: SimpleNamespace(free=17))
    assert result == {
        "target": str(tmp_path), "free_bytes": 17, "incoming_bytes": 3, "floor_bytes": 10,
        "margin_bytes": 4, "required_bytes": 14, "free_after_bytes": 14,
        "policy_origin": "caller", "config_path": None, "config_sha256": None,
        "floor_bytes_min": None, "production_floor_bytes": None, "production_floor_source": None,
        "production_source_sha256": None, "production_floor_opt_out": None,
    }
    assert caplog.messages[-1] == (
        f"free-space check target={tmp_path}: free_bytes=17 bytes, incoming_bytes=3 bytes, "
        "floor_bytes=10 bytes, margin_bytes=4 bytes, required_bytes=14 bytes, free_after_bytes=14 bytes, "
        "policy_origin=caller, config_path=None, config_sha256=None, floor_bytes_min=None, "
        "production_floor_bytes=None, production_floor_source=None, production_source_sha256=None, "
        "production_floor_opt_out=None"
    )


@pytest.mark.parametrize("free,incoming", [(13, 0), (16, 3)])
def test_check_free_space_refuses_shortfall_and_incoming(tmp_path: Path, free: int, incoming: int) -> None:
    with pytest.raises(free_space.FreeSpaceError) as exc:
        free_space.check_free_space(
            tmp_path, incoming, {"floor_bytes": 10, "margin_bytes": 4},
            disk_usage=lambda _: SimpleNamespace(free=free),
        )
    message = str(exc.value)
    assert "refused" in message and str(tmp_path) in message
    for key, value in {
        "free_bytes": free, "incoming_bytes": incoming, "floor_bytes": 10, "margin_bytes": 4,
        "required_bytes": 14, "free_after_bytes": free - incoming,
    }.items():
        assert f"{key}={value} bytes" in message
    assert message == (
        f"refused free-space check target={tmp_path}: free_bytes={free} bytes, incoming_bytes={incoming} bytes, "
        f"floor_bytes=10 bytes, margin_bytes=4 bytes, required_bytes=14 bytes, "
        f"free_after_bytes={free - incoming} bytes, policy_origin=caller, config_path=None, "
        "config_sha256=None, floor_bytes_min=None, production_floor_bytes=None, "
        "production_floor_source=None, production_source_sha256=None, production_floor_opt_out=None"
    )


@pytest.mark.parametrize("incoming", [-1, True, 1.0, "1"])
def test_check_free_space_refuses_invalid_incoming(tmp_path: Path, incoming: object) -> None:
    with pytest.raises(free_space.FreeSpaceError, match="incoming_bytes"):
        free_space.check_free_space(tmp_path, incoming, {"floor_bytes": 10, "margin_bytes": 4})


def test_test_tmp_default_and_absolute_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MINSKY_TEST_TMP", raising=False)
    assert isolation._resolve_test_tmp() == (Path.home() / "Library" / "Application Support" / "MARS" / "minsky-test-tmp"
                                             / "default.noindex")
    monkeypatch.setenv("MINSKY_TEST_TMP", str(tmp_path))
    assert isolation._resolve_test_tmp() == tmp_path


@pytest.mark.parametrize("value", ["relative", ""])
def test_test_tmp_refuses_relative_or_empty_override(monkeypatch, value: str) -> None:
    monkeypatch.setenv("MINSKY_TEST_TMP", value)
    with pytest.raises(ValueError, match="MINSKY_TEST_TMP"):
        isolation._resolve_test_tmp()


@pytest.mark.parametrize("relative", [None, ".", "../outside"])
def test_session_guard_refuses_missing_or_nonisolated_basetemp(tmp_path: Path, monkeypatch,
                                                              relative: str | None) -> None:
    root = tmp_path / "guard-root"
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))
    requested = str(root / relative) if relative is not None else None
    config = SimpleNamespace(option=SimpleNamespace(basetemp=requested))
    with pytest.raises(pytest.exit.Exception) as exc:
        isolation.pytest_configure(config)
    assert exc.value.returncode == 2
    assert str(root) in str(exc.value) and str(requested) in str(exc.value)
    assert not root.exists()


def test_session_guard_refuses_basetemp_symlink_escape(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "guard-root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))
    config = SimpleNamespace(option=SimpleNamespace(basetemp=str(root / "escape" / "run")))
    with pytest.raises(pytest.exit.Exception) as exc:
        isolation.pytest_configure(config)
    assert exc.value.returncode == 2 and str(outside) in str(exc.value)


def test_session_guard_exits_two_on_free_space_refusal(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "guard-root"
    monkeypatch.setenv("MINSKY_TEST_TMP", str(root))

    def refuse(tmp_root: Path) -> None:
        raise free_space.FreeSpaceError(f"refused free-space check target={tmp_root}: free_bytes=0 bytes")

    monkeypatch.setattr(isolation, "_require_test_free_space", refuse)
    config = SimpleNamespace(option=SimpleNamespace(basetemp=str(root / "run")))
    with pytest.raises(pytest.exit.Exception) as exc:
        isolation.pytest_configure(config)
    assert exc.value.returncode == 2 and "free_bytes=0 bytes" in str(exc.value)


@pytest.mark.parametrize("change,leftover,expected_status", [(False, False, 0), (True, False, 1), (False, True, 1)])
def test_session_finish_checks_census_and_cleans_basetemp(tmp_path: Path, change: bool,
                                                       leftover: bool, expected_status: int) -> None:
    root = tmp_path / "guard-root"
    basetemp = root / "run"
    basetemp.mkdir(parents=True)
    (basetemp / "fixture.txt").write_text("temporary", encoding="utf-8")
    protected = tmp_path / "protected-tree"
    protected.mkdir()
    before = {protected: isolation._lstat_census(protected)}
    if change:
        (protected / "added.txt").write_text("changed", encoding="utf-8")
    if leftover:
        (root / "leftover.txt").write_text("leftover", encoding="utf-8")
    messages = []
    reporter = SimpleNamespace(write_line=lambda message, **kwargs: messages.append(message))
    session = SimpleNamespace(
        exitstatus=0,
        config=SimpleNamespace(
            _minsky_guard=(root, basetemp, before),
            pluginmanager=SimpleNamespace(getplugin=lambda _: reporter),
        ),
    )
    isolation.pytest_sessionfinish(session, 0)
    assert session.exitstatus == expected_status
    assert not basetemp.exists()
    if change:
        assert any(str(protected / "added.txt") in message for message in messages)
    if leftover:
        assert any(str(root / "leftover.txt") in message for message in messages)
        assert (root / "leftover.txt").is_file()
    else:
        assert not list(root.iterdir())


def test_require_test_free_space_refuses_fake_low_space(tmp_path: Path) -> None:
    root = tmp_path / "guard-root"
    with pytest.raises(free_space.FreeSpaceError, match="refused"):
        isolation._require_test_free_space(root, disk_usage=lambda _: SimpleNamespace(free=0))
    assert root.is_dir()


def test_lstat_census_detects_addition_and_size_change(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    assert isolation._lstat_census(root) == {".": None}
    root.mkdir()
    empty = isolation._lstat_census(root)
    file = root / "added.txt"
    file.write_text("a", encoding="utf-8")
    added = isolation._lstat_census(root)
    assert added != empty and "added.txt" in added
    file.write_text("longer", encoding="utf-8")
    changed = isolation._lstat_census(root)
    assert changed["added.txt"][1] != added["added.txt"][1]


def test_lstat_census_detects_replaced_symlink_without_following(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "unlisted.txt").write_text("external", encoding="utf-8")
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)
    first = isolation._lstat_census(root)
    assert set(first) == {".", "link"}
    link.unlink()
    link.symlink_to(tmp_path / "different-and-missing")
    second = isolation._lstat_census(root)
    assert first["link"] != second["link"]
    assert set(second) == {".", "link"}
