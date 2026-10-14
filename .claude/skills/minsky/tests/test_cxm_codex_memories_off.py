"""Refuse Codex dispatch or receipts that omit the explicit context-isolation flags."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import _resolve_test_tmp
from test_hardening import PERSONA, provenance
from test_w10d_opencode_credentials import PROMPT, _Wrapper, wrapper as w10d_wrapper


@pytest.fixture
def codex_call(tmp_path: Path, minsky_store: Path, monkeypatch,
               request: pytest.FixtureRequest) -> _Wrapper:
    """Refuse real dispatch: reuse the copied W10d harness with fake Codex and DB index."""
    with monkeypatch.context() as registration:
        registration.setattr(provenance, "SKILL_DIR", tmp_path / "repo/.claude/skills/minsky")
        wrapper = w10d_wrapper.__wrapped__(tmp_path, minsky_store, request)
    codex = wrapper.fakebin / "codex"
    codex.write_text('''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

version = sys.argv[1:] == ["--version"]
log = Path(__file__).with_name("codex.version.argv.jsonl" if version else "codex.argv.jsonl")
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if version:
    print("codex-fixture CXM")
    sys.exit(0)
persona = "agentic-context-engineering"
Path(persona + ".json").write_text(json.dumps({"persona": persona, "findings": [],
    "verdict": {"agree": "true", "reasoning": "Synthetic successful review."}}), encoding="utf-8")
print("tokens used\\n10")
''', encoding="utf-8")
    codex.chmod(0o755)
    (wrapper.fakebin / "timeout").write_text('''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

with Path(__file__).with_name("timeout.argv.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
expected = str(Path(__file__).with_name("codex"))
if sys.argv[3] != expected:
    sys.exit("refused timeout dispatch outside fake Codex: " + sys.argv[3])
os.execv(expected, sys.argv[3:])
''', encoding="utf-8")
    # Stub only the legacy DB boundary; actual prepare/record and validation run.
    (wrapper.skill / "scripts/audit-db.py").write_text('''import json
import sys
from pathlib import Path

log = Path(__file__).resolve().parents[5] / "fakebin/audit-index.jsonl"
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
''', encoding="utf-8")
    environment = {
        "PATH": f"{wrapper.fakebin}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "MINSKY_STORE_ROOT": str(minsky_store),
        "MINSKY_TEST_TMP": str(_resolve_test_tmp()),
        "MINSKY_PROGRESS_ROOT": str(tmp_path / "progress-events"),
        "LANG": "C",
        "CODEX_MODEL": "gpt-5.6-sol",
        "CODEX_EFFORT": "medium",
    }
    result = subprocess.run([
        "bash", str(wrapper.skill / "scripts/invoke-codex.sh"),
        "--cwd", str(wrapper.round_dir / "codex"), "--audit-id", "test-audit",
        "--round", "2", "--persona", PERSONA,
    ], input=PROMPT, text=True, capture_output=True, env=environment,
        cwd=tmp_path / "repo", timeout=30)
    assert result.returncode == 0, result.stderr
    assert wrapper.calls("codex.version") == [["--version"]]
    return wrapper


def _assert_context_flags(argv: list[str], source: str) -> None:
    """Refuse missing, duplicate, reordered or post-stdin isolation options."""
    settings = ("project_doc_max_bytes=0", "memories.use_memories=false",
                "memories.generate_memories=false")
    pairs = list(zip(argv, argv[1:]))
    for setting in settings:
        assert pairs.count(("-c", setting)) == 1, f"{source}: expected exactly one -c {setting}"
        assert argv.count(setting) == 1, f"{source}: duplicate {setting}"
    expected = [token for setting in settings for token in ("-c", setting)] + ["-"]
    assert argv[-len(expected):] == expected, f"{source}: isolation flags must immediately precede final -"


def test_executed_argv_disables_memories(codex_call: _Wrapper) -> None:
    """Refuse executed argv with either memory flag absent, duplicated or misplaced."""
    calls = codex_call.calls("codex")
    assert len(calls) == 1, "expected exactly one fake Codex execution"
    assert calls[0][0] == "exec"
    _assert_context_flags(calls[0], "executed Codex argv")


def test_recorded_argv_matches_execution(codex_call: _Wrapper) -> None:
    """Refuse either receipt omitting the flags or differing from captured execution."""
    calls = codex_call.calls("codex")
    timeouts = codex_call.calls("timeout")
    assert len(calls) == len(timeouts) == 1, "expected one fake Codex and timeout execution"
    for suffix in ("preflight", "call"):
        paths = list((codex_call.round_dir / "provenance").glob(f"*.{suffix}.json"))
        assert len(paths) == 1, f"expected exactly one {suffix} receipt"
        receipt = json.loads(paths[0].read_text(encoding="utf-8"))
        argv = receipt["runtime"]["argv"]
        _assert_context_flags(argv, suffix)
        assert argv == [str(codex_call.fakebin / "timeout"), *timeouts[0]], suffix
        assert argv[3:] == [str(codex_call.fakebin / "codex"), *calls[0]], suffix
