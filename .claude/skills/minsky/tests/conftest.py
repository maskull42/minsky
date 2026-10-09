"""Guard test isolation; refuse unsafe basetemp paths, low space and evidence-tree changes."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[2]
SCRIPTS = SKILL_DIR / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from free_space import FreeSpaceError, check_free_space


@pytest.fixture(autouse=True)
def minsky_store(tmp_path: Path, monkeypatch) -> Path:
    """Give each test a marked store; refuse roots outside MINSKY_TEST_TMP."""
    from store import STORE_ID, load_store_config

    root = tmp_path / "_minsky_store"
    assert root.resolve().is_relative_to(_resolve_test_tmp().resolve())
    root.mkdir()
    (root / "STORE_ID").write_text(STORE_ID + "\n", encoding="utf-8")
    monkeypatch.setenv("MINSKY_STORE_ROOT", str(root))
    cfg = load_store_config()
    assert cfg.root.resolve() == root.resolve()
    assert cfg.root.resolve().is_relative_to(_resolve_test_tmp().resolve())
    return root


def _resolve_test_tmp() -> Path:
    """Resolve the fixture root; refuse relative MINSKY_TEST_TMP overrides."""
    value = os.environ.get("MINSKY_TEST_TMP")
    if value is None:
        # G1E (ruling G-E): Spotlight skips a directory named *.noindex and everything below it.
        return Path.home() / "Library" / "Application Support" / "MARS" / "minsky-test-tmp" / "default.noindex"
    root = Path(value)
    if not value or not root.is_absolute():
        raise ValueError(f"MINSKY_TEST_TMP must be absolute; got {value!r}")
    return root


def _lstat_census(root: Path) -> dict[str, tuple[int, int, int, int] | None]:
    """Record metadata without following links; refuse unreadable existing paths."""
    records = {}

    def visit(path: Path) -> None:
        info = path.lstat()
        records[path.relative_to(root).as_posix()] = (
            info.st_mode, info.st_size, info.st_mtime_ns, info.st_ino,
        )
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)

    try:
        root.lstat()
    except FileNotFoundError:
        return {".": None}
    visit(root)
    return records


def _require_test_free_space(tmp_root: Path, disk_usage=shutil.disk_usage) -> dict:
    """Refuse a fixture root with less than the configured free-space headroom."""
    tmp_root.mkdir(parents=True, exist_ok=True)
    return check_free_space(tmp_root, 0, disk_usage=disk_usage)


def pytest_configure(config: pytest.Config) -> None:
    """Refuse a session without isolated basetemp or sufficient free space."""
    requested = config.option.basetemp
    try:
        tmp_root = _resolve_test_tmp().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        pytest.exit(f"refused MINSKY_TEST_TMP {os.environ.get('MINSKY_TEST_TMP')!r} "
                    f"with --basetemp {requested!r}: {exc}", returncode=2)
    try:
        basetemp = Path(requested).resolve() if requested else None
    except (OSError, ValueError, RuntimeError) as exc:
        pytest.exit(f"refused --basetemp {requested!r} with MINSKY_TEST_TMP {tmp_root}: {exc}", returncode=2)
    if basetemp is None or basetemp == tmp_root or not basetemp.is_relative_to(tmp_root):
        pytest.exit(f"refused --basetemp {requested!r} (resolved {basetemp}): "
                    f"must be strictly inside MINSKY_TEST_TMP {tmp_root}", returncode=2)
    try:
        _require_test_free_space(tmp_root)
        before = {root: _lstat_census(root) for root in (
            REPO_ROOT / "codex-audits", REPO_ROOT / ".minsky",
        )}
    except (OSError, FreeSpaceError) as exc:
        pytest.exit(str(exc), returncode=2)
    config._minsky_guard = (tmp_root, basetemp, before)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail changed evidence trees or leftover fixtures; remove only the validated basetemp."""
    guard = getattr(session.config, "_minsky_guard", None)
    if guard is None:
        return
    tmp_root, basetemp, before = guard
    errors = []
    for root, original in before.items():
        try:
            current = _lstat_census(root)
            for relative in sorted(original.keys() | current.keys()):
                if relative not in original or relative not in current or original[relative] != current[relative]:
                    errors.append(f"test isolation changed path: {root / relative}")
        except OSError as exc:
            errors.append(f"test isolation census refused {root}: {exc}")
    try:
        if basetemp.exists() or basetemp.is_symlink():
            shutil.rmtree(basetemp)
    except OSError as exc:
        errors.append(f"test isolation could not remove --basetemp {basetemp}: {exc}")
    try:
        for entry in sorted(tmp_root.iterdir()):
            errors.append(f"test isolation leftover in MINSKY_TEST_TMP: {entry}")
    except OSError as exc:
        errors.append(f"test isolation could not inspect MINSKY_TEST_TMP {tmp_root}: {exc}")
    if errors:
        session.exitstatus = 1
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        for error in errors:
            if reporter is not None:
                reporter.write_line(error, red=True)
            else:
                sys.stderr.write(error + "\n")
