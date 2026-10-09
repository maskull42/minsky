"""Synthetic wrapper harness; refuse real credentials, providers and in-place execution."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import _resolve_test_tmp
from test_hardening import MODELS, PERSONA, SKILL_DIR, STEP_MODELS, provenance, register_round


COPIED_DIRECTORIES = ("scripts", "config", "schemas", "personas")
PROMPT = "synthetic wrapper prompt\n"
SYNTHETIC_CREDENTIAL = "obviously-fake-w10d-credential-not-a-secret"


@dataclass
class _Wrapper:
    root: Path
    skill: Path
    round_dir: Path
    fakebin: Path
    store: Path

    def run(self, **variables: str) -> subprocess.CompletedProcess[str]:
        """Refuse in-place dispatch; every case sets a temporary progress root."""
        assert self.skill.is_relative_to(self.root / "repo")
        environment = {
            "PATH": f"{self.fakebin}:/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "MINSKY_STORE_ROOT": str(self.store),
            # Host fix (Astra W10d2): conftest also supports an unset MINSKY_TEST_TMP; pass the resolved root.
            "MINSKY_TEST_TMP": str(_resolve_test_tmp()),
            "LANG": "C",
        }
        environment.update(MINSKY_PROGRESS_ROOT=str(self.root / "progress-events"), **variables)
        return subprocess.run([
            "bash", str(self.skill / "scripts/invoke-opencode.sh"),
            "--cwd", str(self.round_dir / "opencode"), "--audit-id", "test-audit",
            "--round", "2", "--persona", PERSONA,
        ], input=PROMPT, text=True, capture_output=True, env=environment,
            cwd=self.root / "repo", timeout=30)

    def calls(self, name: str) -> list[list[str]]:
        """Read only the named fake's argv log; refuse malformed JSON."""
        path = self.fakebin / f"{name}.argv.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.fixture
def wrapper(tmp_path: Path, minsky_store: Path, request: pytest.FixtureRequest) -> _Wrapper:
    """Copy every skill directory used by prepare; refuse dispatch to a real executable."""
    skill = tmp_path / "repo/.claude/skills/minsky"
    for directory in COPIED_DIRECTORIES:
        shutil.copytree(SKILL_DIR / directory, skill / directory,
                        ignore=shutil.ignore_patterns("__pycache__"))
    # Keep the copied policy intact and supply the production source it parses, never imports.
    source = json.loads((skill / "config/free_space.json").read_bytes())["production_floor_source"]
    if source is not None:
        production = Path(source["path"])
        assert (not production.is_absolute() and production.as_posix() == source["path"]
                and not {"..", ".git"}.intersection(production.parts) and production != Path(".")), (
            f"refused production_floor_source path {source['path']!r}"
        )
        (tmp_path / "repo" / production).parent.mkdir(parents=True)
        shutil.copy2(SKILL_DIR.parents[2] / production, tmp_path / "repo" / production)
    (tmp_path / "home").mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    opencode = fakebin / "opencode"
    opencode.write_text('''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

version = sys.argv[1:] == ["--version"]
log = Path(__file__).with_name("opencode.version.argv.jsonl" if version else "opencode.argv.jsonl")
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if version:
    print("opencode-fixture 1.0")
    sys.exit(0)
sys.exit(3)
''', encoding="utf-8")
    opencode.chmod(0o755)
    timeout = fakebin / "timeout"
    timeout.write_text('''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

with Path(__file__).with_name("timeout.argv.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(1)
''', encoding="utf-8")
    timeout.chmod(0o755)
    (fakebin / "python3").symlink_to(sys.executable)
    round_dir = tmp_path / "round"
    stamp = getattr(request, "param", MODELS[2])
    if stamp == MODELS[2]:
        register_round(round_dir)
    else:
        models = [*MODELS[:2], stamp]
        step_models = {**STEP_MODELS, "opencode": stamp}
        assert provenance.main([
            "register-round", "--audit-id", "test-audit", "--round", "2",
            "--round-dir", str(round_dir), "--personas", json.dumps([PERSONA]),
            "--models", json.dumps(models), "--step-models", json.dumps(step_models),
        ]) == 0
    return _Wrapper(tmp_path, skill, round_dir, fakebin, minsky_store)


def _assert_prepared(wrapper: _Wrapper, result: subprocess.CompletedProcess[str], source: str) -> None:
    # record publishes the call manifest, then its legacy DB index refuses the fixture's absent git root.
    assert result.returncode == 2
    assert "audit DB provenance insert failed" in result.stderr
    assert "cannot locate repo root" in result.stderr
    assert wrapper.calls("opencode.version") == [["--version"]]
    assert wrapper.calls("opencode") == []
    calls = wrapper.calls("timeout")
    assert len(calls) == 1
    preflights = list((wrapper.round_dir / "provenance").glob("*.preflight.json"))
    assert len(preflights) == 1
    receipt = json.loads(preflights[0].read_text(encoding="utf-8"))
    assert receipt["runtime"]["credential_source"] == source
    expected_argv = [*receipt["runtime"]["argv"][1:-1], PROMPT.rstrip("\n")]
    assert calls[0] == expected_argv
    manifests = list((wrapper.round_dir / "provenance").glob("*.call.json"))
    assert len(manifests) == 1
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["runtime"] == receipt["runtime"]
        assert manifest["runtime"]["credential_source"] == source
        assert manifest["exit_code"] == 1
        assert manifest["exit_status"] == "error"


def test_default_requires_repo_dotenv(wrapper: _Wrapper) -> None:
    result = wrapper.run()
    assert result.returncode == 1
    assert "required env file not found" in result.stderr
    assert wrapper.calls("opencode.version") == wrapper.calls("opencode") == wrapper.calls("timeout") == []
    assert list((wrapper.round_dir / "provenance").glob("*.preflight.json")) == []


def test_opencode_credentials_without_repo_dotenv(wrapper: _Wrapper) -> None:
    result = wrapper.run(MINSKY_OPENCODE_CREDENTIALS="opencode")
    _assert_prepared(wrapper, result, "opencode")


def test_invalid_credentials_mode_refuses_before_writes(wrapper: _Wrapper) -> None:
    directory = wrapper.round_dir / "provenance"
    directory.mkdir()
    sentinel = directory / "existing.txt"
    sentinel.write_text("existing provenance must survive\n", encoding="utf-8")
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    assert not (wrapper.round_dir / "opencode").exists()
    result = wrapper.run(MINSKY_OPENCODE_CREDENTIALS="bogus")
    assert result.returncode == 64
    assert "MINSKY_OPENCODE_CREDENTIALS" in result.stderr
    assert not (wrapper.round_dir / "opencode").exists()
    assert wrapper.calls("opencode.version") == wrapper.calls("opencode") == wrapper.calls("timeout") == []
    after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    assert after == before


def test_default_records_repo_dotenv_without_credential_value(wrapper: _Wrapper) -> None:
    (wrapper.root / "repo/.env").write_text(f"GOOGLE_API_KEY={SYNTHETIC_CREDENTIAL}\n", encoding="utf-8")
    result = wrapper.run()
    _assert_prepared(wrapper, result, "repo-dotenv")
    leaks = [path.relative_to(wrapper.round_dir).as_posix() for path in wrapper.round_dir.rglob("*")
             if path.is_file() and SYNTHETIC_CREDENTIAL.encode("utf-8") in path.read_bytes()]
    assert leaks == []


@pytest.mark.parametrize("wrapper", ["test-model@high"], indirect=True)
def test_opencode_credentials_accepts_other_provider(wrapper: _Wrapper) -> None:
    result = wrapper.run(MINSKY_OPENCODE_CREDENTIALS="opencode",
                         OPENCODE_MODEL="openai/test-model", OPENCODE_VARIANT="high")
    _assert_prepared(wrapper, result, "opencode")
    assert "credential importer does not support" not in result.stderr
