"""Install a temporary public skill; refuse old Python and preserve existing files."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SKILL = Path(__file__).resolve().parents[1]


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "repo with spaces"
    skill = repo / ".claude/skills/minsky"
    shutil.copytree(SKILL, skill, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    (skill / "config/store.json").unlink(missing_ok=True)
    # The fixture supplies the two public example files, never the real deployment's files.
    (repo / ".opencode/agents").mkdir(parents=True)
    (repo / ".opencode/agents/minsky-reviewer.md.template").write_text("EDIT: research workspace\n", encoding="utf-8")
    (repo / ".minsky").mkdir()
    (repo / ".minsky/binding.yaml.example").write_text("schema: synthetic-binding\n", encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path / "home"), "PYTHONDONTWRITEBYTECODE": "1"}
    return repo, skill, env


def _run(installation: tuple[Path, Path, dict[str, str]], root: Path,
         python: str = sys.executable) -> subprocess.CompletedProcess[str]:
    repo, skill, env = installation
    return subprocess.run([str(skill / "scripts/setup.sh"), "--store-root", str(root), "--python", python],
                          cwd=repo.parent, env=env, capture_output=True, text=True)


def _files(root: Path) -> dict[str, tuple[str, int]]:
    return {path.relative_to(root).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode)
            for path in root.rglob("*") if path.is_file()}


def test_setup_installs_and_second_run_changes_nothing(tmp_path: Path, installation) -> None:
    repo, skill, env = installation
    root = tmp_path / 'store with "quotes"'
    result = _run(installation, root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EDIT:" in result.stdout and '"validation": "ok"' in result.stdout
    assert json.loads((skill / "config/store.json").read_bytes()) == {
        "store_id": "mars-minsky-cas-v1", "root": str(root), "enabled": True,
    }
    assert (root / "STORE_ID").read_bytes() == b"mars-minsky-cas-v1\n"
    assert (repo / ".opencode/agents/minsky-reviewer.md").read_bytes() == (repo / ".opencode/agents/minsky-reviewer.md.template").read_bytes()
    assert (repo / ".minsky/binding.yaml").read_bytes() == (repo / ".minsky/binding.yaml.example").read_bytes()
    assert (repo / ".minsky/audits.db").is_file()
    check = subprocess.run([sys.executable, str(skill / "scripts/store.py"), "check"],
                           env={**env, "MINSKY_STORE_ROOT": str(root)}, capture_output=True, text=True)
    assert check.returncode == 0 and json.loads(check.stdout)["validation"] == "ok"
    before = _files(repo), _files(root)
    second = _run(installation, root)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "skipped render config/store.json" in second.stdout
    assert before == (_files(repo), _files(root))


def test_setup_preserves_preexisting_different_store_json(tmp_path: Path, installation) -> None:
    _, skill, _ = installation
    config = skill / "config/store.json"
    original = json.dumps({"store_id": "mars-minsky-cas-v1", "root": str(tmp_path / "other-store"),
                           "enabled": True}, indent=2) + "\n"
    config.write_text(original, encoding="utf-8")
    result = _run(installation, tmp_path / "store")
    assert config.read_text(encoding="utf-8") == original
    assert result.returncode != 0 and "resolve configured store root" in result.stderr
    assert str(config) in result.stderr and str(tmp_path / "other-store") in result.stderr
    assert not (tmp_path / "store").exists()
    assert "skipped render config/store.json" in result.stdout


def _version_python(tmp_path: Path, version: str) -> Path:
    """Answer only the version query; refuse to substitute other Python execution."""
    fake = tmp_path / ("python-" + version.replace(".", ""))
    query = 'import sys; print("%d.%d" % sys.version_info[:2])'
    fake.write_text("#!/bin/sh\n"
                    f'if [ "$1" = "-c" ] && [ "$2" = {shlex.quote(query)} ]; then\n'
                    f"    echo {shlex.quote(version)}\n    exit 0\nfi\n"
                    f'exec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    fake.chmod(0o755)
    return fake


def test_setup_refuses_python_310(tmp_path: Path, installation) -> None:
    fake = _version_python(tmp_path, "3.10")
    _, skill, _ = installation
    result = _run(installation, tmp_path / "store", str(fake))
    assert result.returncode != 0 and "Python version step" in result.stderr and ">= 3.11" in result.stderr
    assert not (skill / "config/store.json").exists() and not (tmp_path / "store").exists()


def test_setup_accepts_python_311_version_step(tmp_path: Path, installation) -> None:
    fake = _version_python(tmp_path, "3.11")
    _, skill, _ = installation
    root = tmp_path / "store"
    result = _run(installation, root, str(fake))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (skill / "config/store.json").is_file()
    assert (root / "STORE_ID").read_bytes() == b"mars-minsky-cas-v1\n"


def test_setup_honours_configured_root_without_store_root(tmp_path: Path, installation) -> None:
    repo, skill, env = installation
    root = tmp_path / "configured store"
    config = skill / "config/store.json"
    original = json.dumps({"store_id": "mars-minsky-cas-v1", "root": str(root),
                           "enabled": True}, indent=2) + "\n"
    config.write_text(original, encoding="utf-8")
    result = subprocess.run([str(skill / "scripts/setup.sh"), "--python", sys.executable],
                            cwd=repo.parent, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_text(encoding="utf-8") == original
    assert (root / "STORE_ID").read_bytes() == b"mars-minsky-cas-v1\n"
    assert "skipped render config/store.json" in result.stdout
    assert not Path(env["HOME"]).exists()


@pytest.mark.parametrize("root", [None, 17, "", "relative-store"])
def test_setup_refuses_configured_root_without_absolute_string(tmp_path: Path, installation,
                                                             root: object) -> None:
    repo, skill, env = installation
    config = skill / "config/store.json"
    original = json.dumps({"store_id": "mars-minsky-cas-v1", "root": root, "enabled": True}) + "\n"
    config.write_text(original, encoding="utf-8")
    result = subprocess.run([str(skill / "scripts/setup.sh"), "--python", sys.executable],
                            cwd=repo.parent, env=env, capture_output=True, text=True)
    assert result.returncode == 1 and "resolve configured store root" in result.stderr
    assert str(config) in result.stderr and "must be an absolute string" in result.stderr
    assert config.read_text(encoding="utf-8") == original
    assert not Path(env["HOME"]).exists()
    assert not (repo / ".opencode/agents/minsky-reviewer.md").exists()


def test_setup_failed_step_is_named(tmp_path: Path, installation) -> None:
    _, skill, _ = installation
    (skill / "config/store.json.example").unlink()
    result = _run(installation, tmp_path / "store")
    assert result.returncode != 0 and "render config/store.json" in result.stderr


def test_setup_refuses_relative_store_root(installation) -> None:
    result = _run(installation, Path("relative-store"))
    assert result.returncode != 0 and "resolve store root" in result.stderr


def test_setup_refuses_empty_explicit_store_root(tmp_path: Path, installation) -> None:
    repo, skill, env = installation
    config = skill / "config/store.json"
    original = json.dumps({"store_id": "mars-minsky-cas-v1", "root": str(tmp_path / "configured"),
                           "enabled": True}) + "\n"
    config.write_text(original, encoding="utf-8")
    result = subprocess.run([str(skill / "scripts/setup.sh"), "--python", sys.executable,
                             "--store-root", ""], cwd=repo.parent, env=env, capture_output=True, text=True)
    assert result.returncode == 1 and "resolve store root" in result.stderr
    assert "store root must be absolute" in result.stderr
    assert config.read_text(encoding="utf-8") == original
    assert not (tmp_path / "configured").exists()


def test_setup_default_root_uses_isolated_home(tmp_path: Path, installation) -> None:
    repo, skill, env = installation
    result = subprocess.run([str(skill / "scripts/setup.sh"), "--python", sys.executable],
                            cwd=repo.parent, env={**env, "XDG_DATA_HOME": ""}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = Path(env["HOME"]) / ("Library/Application Support/minsky/store" if sys.platform == "darwin"
                                  else ".local/share/minsky/store")
    assert json.loads((skill / "config/store.json").read_bytes())["root"] == str(expected)
    assert (expected / "STORE_ID").read_bytes() == b"mars-minsky-cas-v1\n"
