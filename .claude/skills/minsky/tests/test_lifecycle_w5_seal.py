"""W5 mechanical seal tests; refuse real evidence paths and non-isolated fixture writes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import lifecycle_register as lr
import lifecycle_seal as ls
from test_hardening import PERSONA, SCRIPTS, finding_doc, load_script, record_call, register_round
from test_lifecycle_w8_register import _git

cli = load_script("w5_lifecycle", "minsky-lifecycle.py")
converge = load_script("w5_converge", "converge.py")
ACTOR = "fixture-session GPT-6.1 Sol@xhigh"
AUDIT_ID = "test-audit"
MUTANT_KILL_MATRIX = {
    "skip_dotfiles": "test_t6_skip_dotfiles_and_self_reference_mutants_killed",
    "include_manifest": "test_t6_skip_dotfiles_and_self_reference_mutants_killed",
    "wall_clock_digest": "test_t13_wall_clock_digest_mutant_killed",
    "skip_lsof": "test_t17_skip_lsof_mutant_killed",
}
GLOBS = [
    ("phd_project_context/**", "phd_project_context/context.md", "other/context.md"),
    ("documentation/*pipeline_plan*", "documentation/a_pipeline_plan.md", "documentation/sub/pipeline_plan.md"),
    ("documentation/plans/**", "documentation/plans/a/b.md", "documentation/notes/b.md"),
    (".claude/agents/**", ".claude/agents/a.md", ".claude/personas/a.md"),
    (".claude/skills/*quality*/**", ".claude/skills/a-quality-b/x.md", ".claude/skills/other/x.md"),
    (".claude/skills/minsky/**", ".claude/skills/minsky/scripts/a.py", ".claude/skills/other/a.py"),
    ("src/agents/*quality*", "src/agents/quality_service.py", "src/agents/sub/quality.py"),
    ("scripts/r3_*", "scripts/r3_run.py", "scripts/sub/r3_run.py"),
    ("data/**/profiles/**", "data/profiles/a.json", "data/a/profile/a.json"),
    ("**/*profile*.json", "profile.json", "a/profile.txt"),
    ("**/export*/**", "exports/a.json", "a/import/a.json"),
    (".claude/skills/minsky/personas/source-corpus/**", ".claude/skills/minsky/personas/source-corpus/a.txt", ".claude/skills/minsky/personas/other/a.txt"),
    ("**/rubric*", "rubric.md", "a/rubrics/sub/x.md"),
]


def _mutant(tmp_path: Path, name: str, needle: str, replacement: str):
    original = (SCRIPTS / "lifecycle_seal.py").read_text()
    assert name in MUTANT_KILL_MATRIX and original.count(needle) == 1
    path = tmp_path / f"{name}.py"
    path.write_text(original.replace(needle, replacement))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("pattern,positive,negative", GLOBS)
def test_glob_match_each_trigger(pattern: str, positive: str, negative: str) -> None:
    assert ls.glob_match(pattern, positive)
    assert not ls.glob_match(pattern, negative)
    assert not ls.glob_match(pattern, "/outside/" + positive)
    assert not ls.glob_match(pattern, "../" + positive)


def test_trigger_file_order_and_zero_segment_double_star() -> None:
    assert (SCRIPTS.parent / "config/methodology_triggers.txt").read_text().splitlines() == [row[0] for row in GLOBS]
    assert ls.glob_match("data/**/profiles/**", "data/profiles")
    assert ls.glob_match("**/export*/**", "export")
    assert ls.glob_match("**/rubric*", "a/b/rubric.md")


@pytest.mark.parametrize("text,line,quote,expected", [
    ("one\nexact quote\nthree\n", 2, "exact   quote", True),
    ("one\ntwo\nquote\nfour\nfive\n", 1, "quote", True),
    ("one\ntwo\nquote\nfour\nfive\n", 5, "quote", True),
    ("a\nb\nc\nd\nwhole file\n", 1, "whole file", True),
    ("a" * 40 + "suffix\n", 1, "a" * 30, False),
    ("a" * 30 + "suffix\n", 1, "a" * 30, True),
    ("a" * 29 + "!\n", 1, "a" * 29, False),
    ("a" * 30 + "\n", 1, "a" * 40, True),
    ("text\n", 1, "  ", False),
    ("quote\n", 99, "quote", True),
])
def test_quoted_line_matches_equivalence(tmp_path: Path, text: str, line: int, quote: str, expected: bool) -> None:
    path = tmp_path / "citation.txt"
    path.write_text(text)
    assert converge.whitespace_tolerant_check(path, line, quote) is expected
    assert converge.quoted_line_matches(text, line, quote) is expected


def test_matcher_file_handling_refuses_missing_unreadable_and_replaces_utf8(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "citation.txt"
    assert not converge.whitespace_tolerant_check(path, 1, "quote")
    path.write_bytes(b"quote\xff\n")
    assert converge.whitespace_tolerant_check(path, 1, "quote\ufffd")
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("unreadable")))
    assert not converge.whitespace_tolerant_check(path, 1, "quote")


@pytest.fixture
def audit(tmp_path: Path, monkeypatch, minsky_store: Path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, text in (("documentation/phd_work_log.md", "# Fixture work log\n"),
                      ("scripts/r3_continuous.py", "MIN_FREE_BYTES = 5 * 2**30\n"),
                      ("artifact.txt", "audited source\n")):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "fixture")
    directory = repo / "audits" / AUDIT_ID
    directory.mkdir(parents=True)
    db = tmp_path / "fixture.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript((SCRIPTS.parent / "schemas/audit-db.sql").read_text())
        conn.execute("INSERT INTO audits(audit_id,started_at,finished_at,branch,git_commit_at_start,mode,scope_kind,"
                     "files_audited,personas_active,models_used) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (AUDIT_ID, "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", "fixture", "0" * 40,
                      "audit", "explicit", '["artifact.txt"]', '[]', '[]'))
    progress_root = tmp_path / "events"
    monkeypatch.setenv("MINSKY_PROGRESS_ROOT", str(progress_root))
    event = progress_root / AUDIT_ID / "progress.ndjson"
    event.parent.mkdir(parents=True)
    event.write_text('{"event":"audit_close"}\n')
    config = repo / ".claude/skills/minsky/config/store.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"store_id": "mars-minsky-cas-v1", "root": str(minsky_store), "enabled": True}))
    statement = tmp_path / "statement.txt"
    statement.write_text("No finding or adopted decision changed method, theory, a rubric, a profile, training data, evaluation or the audit method itself. Evidence: artifact.txt:1.\n")
    ls.classify(repo, audit_id=AUDIT_ID, audit_dir=directory, audits_db=db, actor=ACTOR,
                requested_class="code-only", statement_file=statement)
    return {"repo": repo, "dir": directory, "db": db, "event": event,
            "freeze_dir": tmp_path / "freezes", "freeze_copy": tmp_path / "copy"}


def _seal(audit: dict, **kwargs) -> None:
    ls.seal(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], audits_db=audit["db"], actor=ACTOR,
            freeze_dir=audit["freeze_dir"], freeze_copy=audit["freeze_copy"], **kwargs)


def _before(audit: dict) -> tuple:
    return ((audit["repo"] / lr.REGISTER_PATH).read_bytes(),
            {path.relative_to(audit["dir"]).as_posix(): path.read_bytes()
             for path in audit["dir"].rglob("*") if path.is_file()})


def _assert_refused(audit: dict, match: str, **kwargs) -> None:
    before = _before(audit)
    with pytest.raises(lr.RegisterError, match=match):
        _seal(audit, **kwargs)
    assert _before(audit) == before
    assert not audit["freeze_dir"].exists()
    assert not (audit["repo"] / ls.STORE_INDEX).exists()
    assert not (audit["repo"] / ls.FREEZES).exists()


def test_t6_manifest_dotfiles_empty_links_hardlinks_exclusions(audit: dict, tmp_path: Path) -> None:
    directory = audit["dir"]
    (directory / ".hidden").write_bytes(b"dotfile")
    (directory / "empty").touch()
    (directory / "internal").symlink_to(".hidden")
    external = tmp_path / "external"
    external.write_bytes(b"external bytes")
    (directory / "external-link").symlink_to(external)
    (directory / "first").write_bytes(b"hardlink bytes")
    os.link(directory / "first", directory / "second")
    for relative in ls.EXCLUSIONS:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("excluded")
    rows, _ = ls.raw_manifest(audit["repo"], directory, audit_id=AUDIT_ID)
    by_path = {row["path"]: row for row in rows}
    assert list(by_path) == sorted(by_path)
    assert ".hidden" in by_path and by_path["empty"]["bytes"] == "0"
    assert not (set(by_path) & ls.EXCLUSIONS)
    assert by_path["internal"]["type"] == "symlink"
    assert by_path["internal"]["sha256"] == by_path["internal"]["bytes"] == ""
    assert by_path["external-link"]["sha256"] == hashlib.sha256(external.read_bytes()).hexdigest()
    assert by_path["external-link"]["symlink_map"] == str(external.resolve())
    assert by_path["external-link"]["mtime_ns"] == str(external.stat().st_mtime_ns)
    assert by_path["first"]["hardlink_group"] == by_path["second"]["hardlink_group"] == "h1"


def test_t6_skip_dotfiles_and_self_reference_mutants_killed(audit: dict, tmp_path: Path) -> None:
    (audit["dir"] / ".hidden").write_text("hidden")
    (audit["dir"] / "lifecycle/raw_manifest.tsv").write_text("existing excluded manifest")
    for name, needle, replacement in (
        ("skip_dotfiles", "paths.append(path)", "paths.append(path) if not path.name.startswith('.') else None"),
        ("include_manifest", '"lifecycle/raw_manifest.tsv", ', ""),
    ):
        module = _mutant(tmp_path, name, needle, replacement)
        rows, _ = module.raw_manifest(audit["repo"], audit["dir"], audit_id=AUDIT_ID)
        names = {row["path"] for row in rows}
        with pytest.raises(AssertionError):
            assert ".hidden" in names and "lifecycle/raw_manifest.tsv" not in names
    rows, _ = ls.raw_manifest(audit["repo"], audit["dir"], audit_id=AUDIT_ID)
    names = {row["path"] for row in rows}
    assert ".hidden" in names and "lifecycle/raw_manifest.tsv" not in names


def test_t17_live_chain_pid_refuses_without_outputs(audit: dict) -> None:
    process = subprocess.Popen(["sleep", "30"])
    try:
        (audit["dir"] / "chain.pid").write_text(str(process.pid))
        _assert_refused(audit, "live chain writer")
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_t17_open_file_refuses_without_outputs(audit: dict) -> None:
    path = audit["dir"] / "open.txt"
    path.write_text("held open")
    process = subprocess.Popen([sys.executable, "-c", "import sys; f=open(sys.argv[1]); print('ready',flush=True); sys.stdin.read()", str(path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        _assert_refused(audit, "refused.*lsof")
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_t17_missing_audit_close_refuses_without_outputs(audit: dict) -> None:
    audit["event"].write_text('{"event":"round_end"}\n')
    _assert_refused(audit, "audit_close required")


def test_t17_modified_tracked_file_refuses_without_outputs(audit: dict) -> None:
    path = audit["dir"] / "tracked.md"
    path.write_text("committed")
    _git(audit["repo"], "add", "--", str(path))
    _git(audit["repo"], "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "audit output")
    path.write_text("modified")
    _assert_refused(audit, "uncommitted tracked audit file")


def test_t17_real_looking_gate_refuses_without_outputs(audit: dict, monkeypatch) -> None:
    monkeypatch.setenv("MINSKY_TEST_TMP", str(audit["repo"].parent / "separate-fixture-root"))
    _assert_refused(audit, "gate G1")


def test_t17_existing_manifest_refuses_without_outputs(audit: dict) -> None:
    (audit["dir"] / "lifecycle/raw_manifest.tsv").write_text("preexisting")
    _assert_refused(audit, "existing seal output")


def test_t17_legacy_registration_and_audit_close_override_are_recorded(audit: dict) -> None:
    with sqlite3.connect(audit["db"]) as conn:
        conn.execute("UPDATE audits SET finished_at=NULL")
    audit["event"].unlink()
    _seal(audit, legacy_registration="verbatim historical registration", audit_close_override="verbatim close override")
    digest = json.loads((audit["dir"] / "lifecycle/digest.json").read_text())
    assert digest["overrides"] == {"legacy-registration": "verbatim historical registration", "audit-close-override": "verbatim close override"}
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "seal" and row["state_after"] == "sealed"
    assert "verbatim historical registration" in row["note"]
    assert "verbatim close override" in row["note"]
    assert row["actor"] == ACTOR and row["lock_id"]


def test_t17_lsof_failure_is_not_no_writer(audit: dict, monkeypatch) -> None:
    original = ls.subprocess.run

    def run(argv, **kwargs):
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 1, "", "lsof failed")
        return original(argv, **kwargs)

    monkeypatch.setattr(ls.subprocess, "run", run)
    _assert_refused(audit, "refused lsof")


def test_t17_skip_lsof_mutant_killed(audit: dict, tmp_path: Path, monkeypatch) -> None:
    path = audit["dir"] / "open.txt"
    with path.open("w"):
        _assert_refused(audit, "refused.*lsof")
        mutant = _mutant(tmp_path, "skip_lsof", '    _lsof(["+D", str(directory)])\n', "")
        monkeypatch.setattr(ls, "seal", mutant.seal)
        with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
            _assert_refused(audit, "refused.*lsof")


def test_t13_digest_identical_seals_and_narrative_only_in_markdown(audit: dict, tmp_path: Path, monkeypatch) -> None:
    narrative = tmp_path / "narrative.txt"
    narrative.write_text("NARRATIVE_SENTINEL only the host wrote this.\n")
    copies = []
    for index in (1, 2):
        root = tmp_path / f"copy-{index}"
        shutil.copytree(audit["repo"], root)
        fixture = {**audit, "repo": root, "dir": root / "audits" / AUDIT_ID,
                   "freeze_dir": tmp_path / f"freeze-{index}", "freeze_copy": tmp_path / f"backup-{index}"}
        monkeypatch.setattr(ls, "utc_now", lambda index=index: f"2026-10-0{index}T02:00:00Z")
        _seal(fixture, narrative_file=narrative)
        copies.append((fixture["dir"] / "lifecycle/digest.json").read_bytes())
        assert "NARRATIVE_SENTINEL" in (fixture["dir"] / "lifecycle/DIGEST.md").read_text()
        assert "NARRATIVE_SENTINEL" not in copies[-1].decode()
        assert str(tmp_path) not in copies[-1].decode()
    assert copies[0] == copies[1]


def test_t13_wall_clock_digest_mutant_killed(audit: dict, tmp_path: Path, monkeypatch) -> None:
    original = ls.build_digest
    mutant = _mutant(tmp_path, "wall_clock_digest", "    return portable\n",
                     '    portable["wall_clock"] = utc_now()\n    return portable\n')

    args = (audit["repo"], audit["dir"], ls.audit_record(audit["db"], AUDIT_ID), [], [])
    kwargs = {"audit_class": "code-only", "citations": {}, "verdicts": [], "layouts": [], "drifted": [], "overrides": {}}
    monkeypatch.setattr(ls, "utc_now", lambda: "2026-10-01T00:00:00Z")
    monkeypatch.setattr(mutant, "utc_now", lambda: "2026-10-01T00:00:00Z")
    first = original(*args, **kwargs)
    mutated_first = mutant.build_digest(*args, **kwargs)
    monkeypatch.setattr(ls, "utc_now", lambda: "2026-10-02T00:00:00Z")
    monkeypatch.setattr(mutant, "utc_now", lambda: "2026-10-02T00:00:00Z")
    assert first == original(*args, **kwargs)
    with pytest.raises(AssertionError):
        assert mutated_first == mutant.build_digest(*args, **kwargs)


def test_t13_digest_records_real_calls_usage_and_live_verdict(audit: dict) -> None:
    root = audit["dir"] / "round-2"
    register_round(root)
    output = root / "codex" / f"{PERSONA}.json"
    output.parent.mkdir()
    output.write_text(json.dumps(finding_doc()))
    record_call(root, uid="w5digestcall", step="codex", model="gpt-5.6-sol", effort="medium",
                persona=PERSONA, outputs=[output], invoked_at="2026-10-01T00:00:00Z",
                usage={"status": "total-only", "total_tokens": 7})
    (root / "converge.json").write_text(json.dumps({"finding_index": [], "agreement_matrix": {}, "decision": "incomplete"}))
    (root / "claude-synth").mkdir()
    (root / "claude-synth/resolutions.json").write_text("[]")
    _seal(audit)
    digest = json.loads((audit["dir"] / "lifecycle/digest.json").read_text())
    assert digest["calls"][0]["usage"] == {"status": "total-only", "total_tokens": 7}
    assert digest["calls"][0]["model"] == "gpt-5.6-sol"
    assert digest["live_verdicts"][0]["verdict"] == "ok-live"
    assert (audit["dir"] / "lifecycle/verdicts/round-2.live.json").is_file()
    assert not (audit["repo"] / ls.STORE_INDEX).exists()


def test_cli_requires_actor_and_rejects_unknown_flags(audit: dict) -> None:
    for args in (["challenge", "--repo", str(audit["repo"]), "--audit", AUDIT_ID, "--by", "host", "--reason", "r"],
                 ["seal", "--repo", str(audit["repo"]), "--audit", AUDIT_ID, "--unknown"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 2


@pytest.mark.parametrize("preexisting", [False, True])
def test_seal_shared_ingest_publishes_runtime_and_repairs_missing_index(
        audit: dict, minsky_store: Path, monkeypatch, preexisting: bool) -> None:
    path = audit["dir"] / "legacy/provenance/runtime-blobs/blob"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"runtime bytes requiring CAS publication")
    sha = ls.sha256_file(path)
    target = ls.store.cas_path(minsky_store, sha)
    if preexisting:
        target.parent.mkdir(parents=True)
        target.write_bytes(path.read_bytes())
        target.chmod(0o444)
    calls = []
    original = ls.store.ingest_file

    def ingest(cfg, src, identity, **kwargs):
        result = original(cfg, src, identity, **kwargs)
        calls.append((src, identity, kwargs, result, list(cfg.free_space_checks)))
        return result

    monkeypatch.setattr(ls.store, "ingest_file", ingest)
    _seal(audit)
    assert len(calls) == 1
    src, identity, kwargs, created, checks = calls[0]
    assert src == path and identity == sha and created is not preexisting
    assert kwargs == {"byte_count": path.stat().st_size, "audit_id": AUDIT_ID,
                      "round_label": "legacy", "source_path": "legacy/provenance/runtime-blobs/blob"}
    assert [check["incoming_bytes"] for check in checks] == ([0] if preexisting else [path.stat().st_size] * 2)
    assert target.read_bytes() == path.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o444
    assert not list(minsky_store.rglob("*.tmp"))
    lines = (minsky_store / "index.tsv").read_text().splitlines()
    assert lines[0] == "\t".join(ls.store.INDEX_HEADER) and len(lines) == 2
    index = dict(zip(ls.store.INDEX_HEADER, lines[1].split("\t")))
    assert index["sha256"] == sha and index["bytes"] == str(path.stat().st_size)
    assert index["first_audit_id"] == AUDIT_ID and index["first_round"] == "legacy"
    assert index["first_source_path"] == "legacy/provenance/runtime-blobs/blob"
    used = ls._read_table(audit["repo"], ls.STORE_INDEX, ls.STORE_HEADER)
    assert len(used) == 1 and used[0]["sha256"] == sha and used[0]["class"] == "runtime-blob"
    assert lr.read_register(audit["repo"]).rows[-1]["event"] == "seal"


@pytest.mark.parametrize("audit_class,drifted", [("code-only", False), ("methodology-bearing", False),
                                               ("methodology-bearing", True)])
def test_seal_ingests_hash_bound_files_and_external_context_only_by_class(
        audit: dict, minsky_store: Path, audit_class: str, drifted: bool) -> None:
    root = audit["dir"] / "round-2"
    register_round(root)
    output = root / "codex" / f"{PERSONA}.json"
    output.parent.mkdir()
    output.write_text(json.dumps(finding_doc()))
    context = audit["repo"] / "artifact.txt"
    context_sha = ls.sha256_file(context)
    record_call(root, uid="w5bclasscall", step="codex", model="gpt-5.6-sol", effort="medium",
                persona=PERSONA, outputs=[output], invoked_at="2026-10-01T00:00:00Z",
                usage={"status": "total-only", "total_tokens": 7}, context_files=[context])
    (root / "converge.json").write_text(json.dumps({"finding_index": [], "agreement_matrix": {}, "decision": "incomplete"}))
    (root / "claude-synth").mkdir()
    (root / "claude-synth/resolutions.json").write_text("[]")
    if audit_class == "methodology-bearing":
        ls.classify(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], audits_db=audit["db"], actor=ACTOR,
                    requested_class=audit_class)
    if drifted:
        context.write_text("changed after the audit call\n")
    _seal(audit)
    rows = ls._read_table(audit["repo"], ls.STORE_INDEX, ls.STORE_HEADER)
    output_sha = ls.sha256_file(output)
    if audit_class == "code-only":
        assert rows == []
        assert not ls.store.cas_path(minsky_store, output_sha).exists()
        assert not ls.store.cas_path(minsky_store, context_sha).exists()
    else:
        manifest = ls._read_table(audit["repo"], Path("audits") / AUDIT_ID / "lifecycle/raw_manifest.tsv",
                                  ls.RAW_MANIFEST_HEADER)
        bound = {row["sha256"] for row in manifest if row["hash_bound"] == "1" and row["sha256"]}
        expected = bound | (set() if drifted else {context_sha})
        assert {row["sha256"] for row in rows} == expected and output_sha in expected
        for sha in expected:
            target = ls.store.cas_path(minsky_store, sha)
            assert ls.sha256_file(target) == sha and stat.S_IMODE(target.stat().st_mode) == 0o444
        assert ls.store.cas_path(minsky_store, context_sha).exists() is not drifted
        digest = json.loads((audit["dir"] / "lifecycle/digest.json").read_text())
        assert bool(digest["context_drifted_not_ingested"]) is drifted


@pytest.mark.parametrize("kind", ["symlink", "dangling-symlink", "directory"])
def test_shared_ingest_refuses_symlink_and_nonfile_targets(
        tmp_path: Path, minsky_store: Path, kind: str) -> None:
    source = tmp_path / "runtime"
    source.write_bytes(b"runtime bytes")
    sha = ls.sha256_file(source)
    target = ls.store.cas_path(minsky_store, sha)
    target.parent.mkdir(parents=True)
    if kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(source if kind == "symlink" else tmp_path / "absent")
    cfg = ls.store.load_store_config()
    with pytest.raises(ls.store.StoreConfigError, match=f"runtime snapshot mismatch: {target}"):
        ls.store.ingest_file(cfg, source, sha, byte_count=source.stat().st_size, audit_id=AUDIT_ID,
                             round_label=2, source_path=str(source))
    assert cfg.free_space_checks == [] and not (minsky_store / "index.tsv").exists()
    assert not list(minsky_store.rglob("*.tmp"))


def test_classify_upgrade_before_seal_remains_allowed(audit: dict) -> None:
    original = (audit["dir"] / "lifecycle/classification.md").read_bytes()
    prior_state = lr.read_register(audit["repo"]).rows[-1]["state_after"]
    ls.classify(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], audits_db=audit["db"], actor=ACTOR,
                requested_class="methodology-bearing")
    assert (audit["dir"] / "lifecycle/classification.md").read_bytes() == original
    assert "class: methodology-bearing" in (audit["dir"] / "lifecycle/classification.2.md").read_text()
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "classify" and row["class"] == "methodology-bearing"
    assert row["state_after"] == prior_state


@pytest.mark.parametrize("requested_class", [None, "code-only", "methodology-bearing"])
def test_classify_after_seal_refuses_and_names_promote(audit: dict, requested_class: str | None) -> None:
    _seal(audit)
    before = _before(audit)
    with pytest.raises(lr.RegisterError, match=f"post-seal classification {AUDIT_ID}: use promote"):
        ls.classify(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], audits_db=audit["db"], actor=ACTOR,
                    requested_class=requested_class)
    assert _before(audit) == before


def test_freeze_restore_cli_requires_volume_window_and_records_reference(
        audit: dict, tmp_path: Path, monkeypatch, capsys) -> None:
    _seal(audit)
    rows = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)
    actual = Path(rows[1]["path"])
    source = Path("/Volumes/W5b-never-read") / actual.name
    rows[1]["path"] = str(source)
    table = audit["repo"] / ls.FREEZES
    table.write_bytes(ls._tsv(ls.FREEZE_HEADER, rows))
    # Simulate the recorded volume with boot-only bytes; no operation may touch /Volumes/.
    real_resolve, real_symlink, real_stat = Path.resolve, Path.is_symlink, Path.stat
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **kw: path if path == source else real_resolve(path, *a, **kw))
    monkeypatch.setattr(Path, "is_symlink", lambda path: False if path == source else real_symlink(path))
    monkeypatch.setattr(Path, "stat", lambda path, *a, **kw: real_stat(actual if path == source else path, *a, **kw))
    real_hash, real_copy = ls.sha256_file, ls._copy_checked
    reads = []

    def sha256_file(path):
        if path == source:
            reads.append("hash")
        return real_hash(actual if path == source else path)

    def copy_checked(src, dst, sha):
        assert src == source
        reads.append("copy")
        return real_copy(actual, dst, sha)

    monkeypatch.setattr(ls, "sha256_file", sha256_file)
    monkeypatch.setattr(ls, "_copy_checked", copy_checked)
    scratch = tmp_path / "restore"
    argv = ["freeze-restore-check", "--repo", str(audit["repo"]), "--freeze-row", "3",
            "--scratch", str(scratch), "--actor", ACTOR]
    before = _before(audit), table.read_bytes()
    assert cli.main(argv) == 1
    error = capsys.readouterr().err
    assert str(source) in error and "--window-confirmed required" in error
    assert reads == [] and not scratch.exists()
    assert (_before(audit), table.read_bytes()) == before
    assert cli.main([*argv, "--window-confirmed", "production-session-ref"]) == 0
    assert reads == ["hash", "copy"]
    restored = ls._read_table(audit["repo"], ls.FREEZES, ls.FREEZE_HEADER)[-1]
    assert restored["event"] == "restore-demo" and restored["integrity_check"] == "ok"
    assert restored["sha256"] == rows[1]["sha256"]
    assert restored["note"] == f"freeze-row:3; actor:{ACTOR}; window:production-session-ref"
    assert not list(scratch.iterdir())
    assert lr.read_register(audit["repo"]).rows[-1]["event"] == "verify"


def test_freeze_restore_refuses_unconfirmed_volume_scratch(audit: dict) -> None:
    _seal(audit)
    table = audit["repo"] / ls.FREEZES
    before = _before(audit), table.read_bytes()
    with pytest.raises(lr.RegisterError, match="freeze restore scratch through /Volumes/.*--window-confirmed required"):
        ls.freeze_restore_check(audit["repo"], freeze_row=2, scratch=Path("/Volumes/W5b-never-written"), actor=ACTOR)
    assert (_before(audit), table.read_bytes()) == before


@pytest.mark.parametrize("reference", ["", " ", "bad\nreference"])
def test_freeze_restore_refuses_empty_or_delimited_window_reference(
        audit: dict, tmp_path: Path, reference: str) -> None:
    before = _before(audit)
    with pytest.raises(lr.RegisterError, match="--window-confirmed"):
        ls.freeze_restore_check(audit["repo"], freeze_row=2, scratch=tmp_path / "restore", actor=ACTOR,
                                window_confirmed=reference)
    assert _before(audit) == before


def test_existing_cas_blob_is_rehashed_before_seal(audit: dict, minsky_store: Path) -> None:
    path = audit["dir"] / "legacy/provenance/runtime-blobs/blob"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"recorded runtime bytes")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    target = ls.store.cas_path(minsky_store, sha)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"wrong bytes behind correct CAS filename")
    _assert_refused(audit, "CAS blob hash")


@pytest.mark.parametrize("directory,kind", [
    ("round-legacy", "pack"),
    ("history/pack-round", "pack"),
    ("history/self-round", "claude-self/findings"),
    ("history/codex-round", "codex"),
    ("history/opencode-round", "opencode"),
    ("history/synth-round", "decision"),
])
def test_w5c_legacy_classification_scans_pack_and_all_evidence(
        audit: dict, tmp_path: Path, directory: str, kind: str) -> None:
    (audit["dir"] / "lifecycle/classification.md").unlink()
    (audit["repo"] / lr.REGISTER_PATH).write_text("\t".join(lr.COLUMNS) + "\n")
    root = audit["dir"] / directory
    root.mkdir(parents=True)
    evidence = {"file_path": "documentation/plans/legacy-method.md"}
    if kind == "pack":
        (root / "pack.xml").write_text('<pack><file path="documentation/plans/legacy-method.md"/></pack>')
    elif kind == "decision":
        (root / "claude-synth").mkdir()
        (root / "claude-synth/resolutions.json").write_text(json.dumps([{"evidence": evidence}]))
    else:
        folder = root / kind
        folder.mkdir(parents=True)
        (folder / f"{PERSONA}.json").write_text(json.dumps(finding_doc(findings=[{"evidence": evidence}])))
    assert ls.round_inputs(audit["dir"], AUDIT_ID) == []
    before = _before(audit)
    with pytest.raises(lr.RegisterError, match="trigger glob:documentation/plans/.*legacy-method.md"):
        ls.classify(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], audits_db=audit["db"], actor=ACTOR,
                    requested_class="code-only", statement_file=tmp_path / "statement.txt")
    assert _before(audit) == before
    ls.classify(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], audits_db=audit["db"], actor=ACTOR)
    classification = (audit["dir"] / "lifecycle/classification.md").read_text()
    assert "documentation/plans/legacy-method.md" in classification
    assert "class: methodology-bearing" in classification
    assert ls.citation_resolvability(audit["dir"], audit_id=AUDIT_ID, repo=audit["repo"], store_root=None)["result"] == "pass"


@pytest.mark.parametrize("relative,payload", [
    ("round-legacy/pack.xml", "<truncated"),
    (f"round-legacy/codex/{PERSONA}.json", "{truncated"),
    ("round-legacy/claude-synth/resolutions.json", "{truncated"),
    ("round-legacy/round-scope.json", "{truncated"),
])
def test_w5c_unparseable_classification_input_is_unknown(
        audit: dict, tmp_path: Path, relative: str, payload: str) -> None:
    (audit["dir"] / "lifecycle/classification.md").unlink()
    (audit["repo"] / lr.REGISTER_PATH).write_text("\t".join(lr.COLUMNS) + "\n")
    path = audit["dir"] / relative
    path.parent.mkdir(parents=True)
    path.write_text(payload)
    before = _before(audit)
    with pytest.raises(lr.RegisterError, match="unknown"):
        ls.classify(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], audits_db=audit["db"], actor=ACTOR,
                    requested_class="code-only", statement_file=tmp_path / "statement.txt")
    assert _before(audit) == before
    ls.classify(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], audits_db=audit["db"], actor=ACTOR)
    text = (audit["dir"] / "lifecycle/classification.md").read_text()
    assert "unknown" in text and relative in text and "class: methodology-bearing" in text


@pytest.mark.parametrize("verb", ["classify", "seal", "raw_manifest"])
def test_w5c_volume_window_refuses_before_any_audit_read(
        audit: dict, tmp_path: Path, monkeypatch, verb: str) -> None:
    root = audit["dir"] / "round-2"
    register_round(root)
    scope = root / "round-scope.json"
    external = tmp_path / "external-scope.json"
    external.write_bytes(scope.read_bytes())
    scope.unlink()
    scope.symlink_to(external)
    second = root / "nested/pack.xml"
    second.parent.mkdir()
    second.symlink_to(external)
    # Real links and all bytes stay on boot. Simulate only their resolved volume prefix.
    realpath = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda path, *a, **kw:
                        "/Volumes/W5c-never-read/scope.json" if Path(path) == external
                        else realpath(path, *a, **kw))
    read_bytes, read_text = Path.read_bytes, Path.read_text
    reads = []

    def check_read(path: Path) -> None:
        if path.is_relative_to(audit["dir"]):
            reads.append(path)
            raise AssertionError(f"audit read before volume window gate: {path}")

    def bytes_read(path: Path) -> bytes:
        check_read(path)
        return read_bytes(path)

    def text_read(path: Path, *args, **kwargs) -> str:
        check_read(path)
        return read_text(path, *args, **kwargs)

    register_before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    monkeypatch.setattr(Path, "read_bytes", bytes_read)
    monkeypatch.setattr(Path, "read_text", text_read)
    with pytest.raises(lr.RegisterError, match="--window-confirmed required") as exc:
        if verb == "classify":
            ls.classify(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], audits_db=audit["db"], actor=ACTOR)
        elif verb == "seal":
            _seal(audit)
        else:
            ls.raw_manifest(audit["repo"], audit["dir"], audit_id=AUDIT_ID)
    assert str(scope) in str(exc.value) and str(second) in str(exc.value)
    assert reads == []
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == register_before
    assert not audit["freeze_dir"].exists()
    assert not (audit["repo"] / ls.STORE_INDEX).exists()
    assert not (audit["dir"] / "lifecycle/raw_manifest.tsv").exists()


def test_w5c_moved_audit_shared_context_is_hash_bound_and_ingested(
        audit: dict, minsky_store: Path) -> None:
    root = audit["dir"] / "round-1"
    register_round(root)
    shared = audit["dir"] / "shared.txt"
    shared.write_text("preserved shared audit context\n")
    sha = ls.sha256_file(shared)
    output = root / "codex" / f"{PERSONA}.json"
    output.parent.mkdir()
    output.write_text(json.dumps(finding_doc()))
    record_call(root, uid="w5cmovedcall", step="codex", model="gpt-5.6-sol", effort="medium",
                persona=PERSONA, outputs=[output], invoked_at="2026-10-01T00:00:00Z",
                usage={"status": "total-only", "total_tokens": 7}, context_files=[shared])
    (root / "converge.json").write_text(json.dumps({"finding_index": [], "agreement_matrix": {}, "decision": "incomplete"}))
    (root / "claude-synth").mkdir()
    (root / "claude-synth/resolutions.json").write_text("[]")
    recorded_audit = audit["dir"]
    moved = recorded_audit.with_name("moved-audit")
    recorded_audit.rename(moved)
    fixture = {**audit, "dir": moved}
    info = ls.round_inputs(moved, AUDIT_ID)[0]
    assert ls.audit_relative(str(recorded_audit / "shared.txt"), moved, info) == "shared.txt"
    rows, _ = ls.raw_manifest(audit["repo"], moved, audit_id=AUDIT_ID)
    row = next(row for row in rows if row["path"] == "shared.txt")
    assert row["hash_bound"] == "1" and row["sha256"] == sha
    ls.classify(audit["repo"], audit_id=AUDIT_ID, audit_dir=moved, audits_db=audit["db"], actor=ACTOR,
                requested_class="methodology-bearing")
    _seal(fixture)
    target = ls.store.cas_path(minsky_store, sha)
    assert target.read_bytes() == (moved / "shared.txt").read_bytes()
    assert ls.sha256_file(target) == sha and stat.S_IMODE(target.stat().st_mode) == 0o444
    used = ls._read_table(audit["repo"], ls.STORE_INDEX, ls.STORE_HEADER)
    assert any(row["sha256"] == sha and row["use_path"] == "shared.txt" for row in used)
    digest = json.loads((moved / "lifecycle/digest.json").read_text())
    assert digest["context_drifted_not_ingested"] == []


def test_w5c_confirmed_volume_window_allows_seal_and_records_reference(
        audit: dict, tmp_path: Path, monkeypatch) -> None:
    external = tmp_path / "external.txt"
    external.write_bytes(b"confirmed window content")
    link = audit["dir"] / "external-link"
    link.symlink_to(external)
    realpath, resolve = os.path.realpath, Path.resolve
    monkeypatch.setattr(os.path, "realpath", lambda path, *a, **kw:
                        "/Volumes/W5c-never-read/external.txt" if Path(path) == external
                        else realpath(path, *a, **kw))
    # The gate sees a simulated volume; subsequent hashing still reads only fixture bytes.
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **kw:
                        Path(realpath(path, *a, **kw)) if path in (link, external) else resolve(path, *a, **kw))
    with pytest.raises(lr.RegisterError, match="--window-confirmed required"):
        _seal(audit)
    _seal(audit, window_confirmed="production-session-ref")
    rows = ls._read_table(audit["repo"], Path("audits") / AUDIT_ID / "lifecycle/raw_manifest.tsv",
                          ls.RAW_MANIFEST_HEADER)
    row = next(row for row in rows if row["path"] == "external-link")
    assert row["sha256"] == hashlib.sha256(external.read_bytes()).hexdigest()
    assert row["bytes"] == str(external.stat().st_size)
    digest = json.loads((audit["dir"] / "lifecycle/digest.json").read_text())
    assert digest["overrides"] == {"window-confirmed": "production-session-ref"}
    assert "window-confirmed:production-session-ref" in lr.read_register(audit["repo"]).rows[-1]["note"]
