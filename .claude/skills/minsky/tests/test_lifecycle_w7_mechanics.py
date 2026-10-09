"""W7 fixture mechanics; refuse unreviewed deletion and preserve byte/metadata-exact copy restoration."""

from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

import lifecycle_offload as lo
import lifecycle_register as lr
import lifecycle_citations as citations
import lifecycle_expire as expiry
from test_hardening import load_script
from test_lifecycle_w6_pack import ACTOR, AUDIT_ID, _append, _copy, _production_pack
import test_lifecycle_w6_pack as w6

cli = load_script("w7_lifecycle", "minsky-lifecycle.py")


@pytest.fixture
def sealed_audit(tmp_path: Path, minsky_store: Path) -> dict:
    return w6.sealed_audit.__wrapped__(tmp_path, minsky_store)


def _shared(audit: dict) -> list[str]:
    return ["--repo", str(audit["repo"]), "--audit", AUDIT_ID, "--actor", ACTOR]


@pytest.mark.parametrize("kind", ["downgrade", "citation-waiver", "classification-confirm",
                                  "challenge-resolved", "legacy-migrate"])
def test_w7_ruling_exact_note_state_and_session(sealed_audit: dict, monkeypatch, kind: str) -> None:
    audit = sealed_audit
    original = lr.read_register(audit["repo"]).rows[-1]
    acquire = lr.AuditLock.acquire
    seen = []

    def capture(repo, audit_id, *, verb, session):
        seen.append((verb, session))
        return acquire(repo, audit_id, verb=verb, session=session)

    monkeypatch.setattr(lr.AuditLock, "acquire", capture)
    words = "  Exact researcher's words — preserve spacing.  "
    assert cli.main(["ruling", *_shared(audit), "--kind", kind, "--verbatim", words,
                     "--researcher-session", "researcher-session-123"]) == 0
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "ruling" and row["note"] == f"{kind}: {words}"
    assert row["actor"] == ACTOR and row["audit_dir"] == original["audit_dir"]
    assert row["state_after"] == original["state_after"] and row["class"] == original["class"]
    assert row["manifest_sha256"] == original["manifest_sha256"]
    assert row["lock_id"] and seen == [("ruling", "researcher-session-123")]
    assert not list(lr.lock_directory(audit["repo"]).glob("*.lock"))


@pytest.mark.parametrize("field,value", [("verbatim", "bad\twords"), ("verbatim", "bad\nwords"),
                                       ("verbatim", "bad\rwords"), ("verbatim", ""),
                                       ("researcher_session", ""), ("researcher_session", "bad\nidentity"),
                                       ("actor", "bad\tactor")])
def test_w7_ruling_refuses_invalid_fields_without_append(sealed_audit: dict, field: str, value: str) -> None:
    audit = sealed_audit
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    values = {"verbatim": "approved exact words", "researcher_session": "researcher", "actor": ACTOR}
    values[field] = value
    with pytest.raises(lr.RegisterError, match="refused"):
        cli.ruling(audit["repo"], audit_id=AUDIT_ID, kind="downgrade", **values)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before


def test_w7_ruling_usage_unknown_kind_and_unregistered_audit(sealed_audit: dict, capsys) -> None:
    audit = sealed_audit
    with pytest.raises(SystemExit) as error:
        cli.main(["ruling", *_shared(audit), "--kind", "downgrade", "--verbatim", "words"])
    assert error.value.code == 2
    with pytest.raises(lr.RegisterError, match="kind"):
        cli.ruling(audit["repo"], audit_id=AUDIT_ID, kind="unsupported", verbatim="words",
                   researcher_session="researcher", actor=ACTOR)
    assert cli.main(["ruling", "--repo", str(audit["repo"]), "--audit", "unknown-audit",
                     "--actor", ACTOR, "--kind", "downgrade", "--verbatim", "words",
                     "--researcher-session", "researcher"]) == 1
    assert "refused ruling unknown-audit: registered audit required" in capsys.readouterr().err


def test_w7_ruling_holds_same_audit_lock(sealed_audit: dict) -> None:
    audit = sealed_audit
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    with lr.AuditLock.acquire(audit["repo"], AUDIT_ID, verb="challenge", session="other"):
        with pytest.raises(lr.LockHeld, match="held lock"):
            cli.ruling(audit["repo"], audit_id=AUDIT_ID, kind="challenge-resolved", verbatim="words",
                       researcher_session="researcher", actor=ACTOR)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before


def test_w7_rehydrate_into_exact_bytes_mode_mtime_and_namespace(sealed_audit: dict, tmp_path: Path) -> None:
    audit = sealed_audit
    _production_pack(audit)
    _copy(audit)
    target = tmp_path / "restored"
    # Restore with the boot runtime CAS absent; only the recorded copy's replica is available.
    audit["blob"].unlink()
    assert cli.main(["rehydrate", *_shared(audit), "--from", "A", "--into", str(target)]) == 0
    actual = {path.relative_to(target).as_posix() for path in target.rglob("*")}
    assert actual == {row["path"] for row in audit["rows"]}
    assert not (target / "CAS_REFS.tsv").exists()
    for row in audit["rows"]:
        path = target / row["path"]
        if row["sha256"]:
            lo.packs.checked_file(path, row["sha256"], int(row["bytes"]))
            info = path.stat()
            assert stat.S_IMODE(info.st_mode) == int(row["mode"], 8)
            assert info.st_mtime_ns == int(row["mtime_ns"])
        elif row["type"] == "symlink":
            assert path.is_symlink() and os.readlink(path) == row["symlink_target"]
        else:
            assert path.is_dir()
    assert not (target / "external-link").is_symlink()
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "rehydrate" and row["state_after"] == "rehydrated"
    assert row["copy_location"] == "A:" + str(audit["copy_a"])


def test_w7_rehydrate_in_place_refuses_differing_present_file_before_writes(sealed_audit: dict, capsys) -> None:
    audit = sealed_audit
    _production_pack(audit)
    _copy(audit)
    removed = audit["dir"] / ".hidden"
    removed.unlink()
    changed = audit["dir"] / "first"
    changed.write_bytes(b"changed present bytes\n")
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    assert cli.main(["rehydrate", *_shared(audit), "--from", "A", "--in-place"]) == 1
    assert "sha256/bytes" in capsys.readouterr().err
    assert not removed.exists() and changed.read_bytes() == b"changed present bytes\n"
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before


def test_w7_restore_refuses_traversing_cas_member_before_writes(sealed_audit: dict, tmp_path: Path) -> None:
    audit = sealed_audit
    pack, _, _ = _production_pack(audit)
    _copy(audit)
    _, packed, _, resident = lo.copies._pack_context(audit["repo"], AUDIT_ID)
    manifest = [{**row, "path": "../escape"} if row["class"] == "runtime-blob" and row["sha256"] else row
                for row in audit["rows"]]
    into = tmp_path / "restored"
    with pytest.raises(lo.packs.PackError, match="unsafe relative path"):
        lo.restore_namespace(pack, audit["copy_a"] / "cas", manifest, "code-only", resident, packed,
                             into, in_place=False)
    assert not into.exists() and not (tmp_path / "escape").exists()


def test_w7_rehydrate_in_place_restores_absent_raw_files(sealed_audit: dict) -> None:
    audit = sealed_audit
    # This fixture's external link is a pack-dereferenced member; make it absent as well.
    _production_pack(audit)
    _copy(audit)
    for path in (audit["runtime"], audit["dir"] / "external-link", audit["dir"] / "first"):
        path.unlink()
    assert cli.main(["rehydrate", *_shared(audit), "--from", "A", "--in-place"]) == 0
    for row in audit["rows"]:
        if row["sha256"]:
            lo.packs.checked_file(audit["dir"] / row["path"], row["sha256"], int(row["bytes"]))


@pytest.mark.parametrize("restore_failure", [False, True])
def test_w7_rehydrate_copy_b_restores_without_boot_sources_and_cleans_scratch(sealed_audit: dict,
                                                                           tmp_path: Path, monkeypatch,
                                                                           restore_failure: bool) -> None:
    audit = sealed_audit
    w6.copy_devices.__wrapped__(tmp_path, monkeypatch)
    options = w6.restic_options.__wrapped__(tmp_path)
    pack, _, _ = _production_pack(audit)
    _copy(audit, copy="B", **options)
    for path in (pack, audit["blob"], audit["boot_freeze"]):
        path.unlink()
    target = tmp_path / "restored-B"
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    if restore_failure:
        restic = lo.copies._restic

        def refuse_restore(base, *args):
            if args[0] == "restore":
                raise lr.RegisterError("refused restic restore: injected failure")
            return restic(base, *args)

        monkeypatch.setattr(lo.copies, "_restic", refuse_restore)
        with pytest.raises(lr.RegisterError, match="injected failure"):
            lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, copy="B", actor=ACTOR, into=target, **options)
        assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
        assert not target.exists()
    else:
        lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, copy="B", actor=ACTOR, into=target, **options)
        assert {path.relative_to(target).as_posix() for path in target.rglob("*")} == {
            row["path"] for row in audit["rows"]}
        for row in audit["rows"]:
            if row["sha256"]:
                restored = target / row["path"]
                lo.packs.checked_file(restored, row["sha256"], int(row["bytes"]))
                assert restored.stat().st_mtime_ns == int(row["mtime_ns"])
        assert lr.read_register(audit["repo"]).rows[-1]["copy_location"].startswith("B:")
    assert not (tmp_path / f".{AUDIT_ID}.copy-B-restore").exists()


@pytest.mark.parametrize("defect", ["nonempty", "symlink-root", "missing-copy", "corrupt-replica", "corrupt-pack"])
def test_w7_rehydrate_refuses_unsafe_target_or_changed_copy(sealed_audit: dict, tmp_path: Path,
                                                          capsys, defect: str) -> None:
    audit = sealed_audit
    target, _, _ = _production_pack(audit)
    if defect != "missing-copy":
        _copy(audit)
    into = tmp_path / "restored"
    if defect == "nonempty":
        into.mkdir()
        (into / "foreign").write_bytes(b"keep\n")
    elif defect == "symlink-root":
        into.symlink_to(audit["dir"], target_is_directory=True)
    elif defect == "corrupt-replica":
        blob = lo.store.cas_path(audit["copy_a"] / "cas", audit["blob_sha"])
        blob.chmod(0o600)
        blob.write_bytes(b"corrupt copy\n")
    elif defect == "corrupt-pack":
        target.write_bytes(b"corrupt compressed pack\n")
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    assert cli.main(["rehydrate", *_shared(audit), "--from", "A", "--into", str(into)]) == 1
    assert "refused" in capsys.readouterr().err
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    if defect == "nonempty":
        assert (into / "foreign").read_bytes() == b"keep\n"
    elif defect != "symlink-root":
        assert not into.exists()


@pytest.mark.parametrize("state", ["packed"])
def test_w7_promote_ingests_and_leaves_pending_on_copy_failure(sealed_audit: dict, monkeypatch, state: str) -> None:
    audit = sealed_audit
    if state == "packed":
        _production_pack(audit)
    def failed_copy(repo, **kwargs):
        assert not list(lr.lock_directory(repo).glob("*.lock"))
        raise lr.RegisterError("refused forced promotion copy failure")
    monkeypatch.setattr(lo.copies, "copy_operation", failed_copy)
    with pytest.raises(lr.RegisterError, match="forced promotion copy failure"):
        cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="new methodological citation", by="researcher", actor=ACTOR)
    rows = lr.read_register(audit["repo"]).rows
    assert rows[-1]["event"] == "promotion-pending" and rows[-1]["state_after"] == state
    assert not any(row["event"] == "promote" for row in rows)
    selected = [row for row in audit["rows"] if row["hash_bound"] == "1" and row["sha256"]]
    for row in selected:
        lo.packs.checked_file(lo.store.cas_path(audit["store"], row["sha256"]), row["sha256"], int(row["bytes"]))
    indexed = lo.packs._read_table(audit["repo"], lo.packs.STORE_INDEX, lo.packs.STORE_HEADER)
    assert {(row["use_path"], row["class"]) for row in indexed} == {(row["path"], row["class"]) for row in selected}


def test_w7_archive_stops_at_first_refusal_without_classifying(sealed_audit: dict, monkeypatch, capsys) -> None:
    audit = sealed_audit
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("archive dispatched a later or classification step")

    monkeypatch.setattr(lo.packs, "pack", forbidden)
    assert cli.main(["archive", *_shared(audit)]) == 1
    assert "archive step seal refused" in capsys.readouterr().err
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before


@pytest.mark.parametrize("phase", ["prepare", "execute", "resume"])
def test_w7_offload_requires_pack_before_any_mutation(sealed_audit: dict, capsys, phase: str) -> None:
    audit = sealed_audit
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    assert cli.main(["offload", *_shared(audit), "--" + phase]) == 1
    assert "latest state packed or replicated(n) required" in capsys.readouterr().err
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert audit["runtime"].exists()


def test_w7_expire_cli_reports_unavailable_titan_and_never_expires(sealed_audit: dict, capsys) -> None:
    audit = sealed_audit
    _production_pack(audit)
    _copy(audit)
    _append(audit, "offload", "offloaded", **{"class": "code-only"})
    assert cli.main(["expire", *_shared(audit), "--execute"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert not report["eligible"] and report["deletion_list"] == [str(audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst")]
    assert "titan source unavailable (no sanctioned read)" in report["reasons"]
    assert "refused expire test-audit: titan source unavailable (no sanctioned read)" in captured.err
    assert (audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst").exists()
    assert lr.read_register(audit["repo"]).rows[-1]["event"] == "census"
    assert not (audit["dir"] / "lifecycle/EXPIRED.tsv").exists()


def test_w7b_promotion_resumes_with_immutable_pack_refs_and_separate_locks(sealed_audit: dict,
                                                                         tmp_path: Path, monkeypatch) -> None:
    audit = sealed_audit
    w6.copy_devices.__wrapped__(tmp_path, monkeypatch)
    options = w6.restic_options.__wrapped__(tmp_path)
    pack, _, original_refs = _production_pack(audit)
    _copy(audit)
    original_bytes = pack.read_bytes()
    operation = lo.copies.copy_operation
    def failed_copy(*args, **kwargs):
        raise lr.RegisterError("refused forced second-copy failure")
    monkeypatch.setattr(lo.copies, "copy_operation", failed_copy)
    with pytest.raises(lr.RegisterError, match="forced second-copy failure"):
        cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="citation upgrade", by="researcher", actor=ACTOR)
    def separately_locked(repo, **kwargs):
        assert not list(lr.lock_directory(repo).glob("*.lock"))
        return operation(repo, **kwargs)
    monkeypatch.setattr(lo.copies, "copy_operation", separately_locked)
    cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="citation upgrade", by="researcher", actor=ACTOR, **options)
    rows = lr.read_register(audit["repo"]).rows
    assert sum(row["event"] == "promotion-pending" for row in rows) == 1
    assert rows[-1]["event"] == "promote" and rows[-1]["class"] == "methodology-bearing"
    ingested = len({row["sha256"] for row in audit["rows"] if row["hash_bound"] == "1" and row["sha256"]})
    prefix, shas = rows[-1]["note"].split("; free_space_sha256:")
    assert prefix == f"reason:citation upgrade; by:researcher; ingested:{ingested}"
    assert len(shas.split(",")) == 2
    w6._assert_free_space(audit, rows[-1], [json.loads(
        (audit["dir"] / "lifecycle/free_space_checks" / (sha + ".json")).read_bytes()) for sha in shas.split(",")])
    assert citations.effective_class(audit["repo"], AUDIT_ID) == "methodology-bearing"
    assert len(lo.copies.counting_locations(AUDIT_ID, lr.read_register(audit["repo"]))) == 2
    assert pack.read_bytes() == original_bytes
    packed = next(row for row in rows if row["event"] == "pack")
    assert lo.packs.cas_refs_bytes(lo.copies.read_cas_refs(pack, b"untrusted changing selection", packed)) == original_refs
    _copy(audit, event="verify")
    _copy(audit, copy="B", event="verify", **options)
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="citation upgrade", by="researcher", actor=ACTOR, **options)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before


def test_host_promotion_from_packed_creates_both_missing_copies(sealed_audit: dict, tmp_path: Path, monkeypatch) -> None:
    """Host fix (Astra W7c): from `packed` with NO copy, one promote invocation creates copy A and copy B, then promotes."""
    audit = sealed_audit
    w6.copy_devices.__wrapped__(tmp_path, monkeypatch)
    options = w6.restic_options.__wrapped__(tmp_path)
    _production_pack(audit)
    assert lo.copies.counting_locations(AUDIT_ID, lr.read_register(audit["repo"])) == set()
    cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="citation upgrade", by="researcher", actor=ACTOR, **options)
    rows = lr.read_register(audit["repo"]).rows
    assert rows[-1]["event"] == "promote" and rows[-1]["class"] == "methodology-bearing"
    locations = lo.copies.counting_locations(AUDIT_ID, lr.read_register(audit["repo"]))
    assert len(locations) == 2 and {location[:2] for location in locations} == {"A:", "B:"}


def test_w7b_rehydrate_records_current_class_and_preserves_pack_decoding(sealed_audit: dict,
                                                                        tmp_path: Path) -> None:
    audit = sealed_audit
    _production_pack(audit)
    _copy(audit)
    _append(audit, "challenge", "replicated(1)", **{"class": "methodology-bearing"}, note="unresolved challenge")
    lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, copy="A", actor=ACTOR, into=tmp_path / "restored")
    assert lr.read_register(audit["repo"]).rows[-1]["class"] == "methodology-bearing"
    assert w6._flags(audit) == ["overdue-second-copy"]
    _copy(audit, event="verify")
    assert lr.read_register(audit["repo"]).rows[-1]["class"] == "methodology-bearing"
    assert w6._flags(audit) == ["overdue-second-copy"]


def test_w7b_rehydrate_internal_links_to_root_and_manifest_directories(sealed_audit: dict,
                                                                       tmp_path: Path) -> None:
    audit = sealed_audit
    links = {"root-link": ".", "dir-link": "round-2", "round-2/up": ".."}
    for name, target in links.items():
        (audit["dir"] / name).symlink_to(target)
    rows, _ = w6.ls.raw_manifest(audit["repo"], audit["dir"], audit_id=AUDIT_ID)
    audit["rows"] = rows
    audit["manifest"].write_bytes(w6.ls._tsv(w6.ls.RAW_MANIFEST_HEADER, rows))
    _append(audit, "seal", "sealed", **{"class": "code-only"}, citation_resolvability="pass",
            manifest_sha256=lo.packs._hash_file(audit["manifest"]), note="freeze:" + audit["freeze_sha"] + "; ingested:0")
    _production_pack(audit)
    _copy(audit)
    into = tmp_path / "restored"
    lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, copy="A", actor=ACTOR, into=into)
    for name, target in links.items():
        assert (into / name).is_symlink() and os.readlink(into / name) == target


@pytest.mark.parametrize("failure_step", ["seal", "pack", "replicate(A)", "replicate(B)", None])
def test_w7b_archive_dispatches_public_steps_without_holding_a_lock(sealed_audit: dict, tmp_path: Path,
                                                                   monkeypatch, failure_step: str | None) -> None:
    audit = sealed_audit
    _append(audit, "ruling", "sealed", **{"class": "methodology-bearing"})
    calls = []
    def operation(name):
        def run(repo, **kwargs):
            assert not list(lr.lock_directory(repo).glob("*.lock"))
            calls.append(name)
            if name == failure_step:
                raise lr.RegisterError("refused fixture step")
        return run
    monkeypatch.setattr(w6.ls, "seal", operation("seal"))
    monkeypatch.setattr(lo.packs, "pack", operation("pack"))
    def copy_operation(repo, **kwargs):
        operation("replicate(" + kwargs["copy"] + ")")(repo, **kwargs)
    monkeypatch.setattr(lo.copies, "copy_operation", copy_operation)
    arguments = {"audit_id": AUDIT_ID, "actor": ACTOR, "audit_dir": audit["dir"],
                 "audits_db": tmp_path / "audits.sqlite", "freeze_dir": tmp_path / "freeze-dir",
                 "freeze_copy": audit["freeze"], "copy_a_root": audit["copy_a"]}
    expected = ["seal", "pack", "replicate(A)", "replicate(B)"]
    if failure_step:
        with pytest.raises(lr.RegisterError, match="archive step " + failure_step.replace("(", r"\(").replace(")", r"\)")):
            cli.archive(audit["repo"], **arguments)
        expected = expected[:expected.index(failure_step) + 1]
    else:
        cli.archive(audit["repo"], **arguments)
    assert calls == expected


@pytest.mark.parametrize("titan_result", [[], [{"category": "Coding"}], RuntimeError("reader unavailable")])
def test_w7b_titan_reader_records_a_version_or_refuses(sealed_audit: dict, titan_result) -> None:
    audit = sealed_audit
    def reader(audit_id):
        assert audit_id == AUDIT_ID
        if isinstance(titan_result, Exception):
            raise titan_result
        return titan_result
    report = expiry.expire(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, titan_reader=reader)
    if isinstance(titan_result, Exception):
        assert "titan source unavailable: reader unavailable" in report["reasons"]
    else:
        expected = hashlib.sha256(json.dumps(titan_result, sort_keys=True).encode()).hexdigest()
        assert {"path": "TITAN", "sha256": expected} in report["source_versions"]
        assert not any(reason.startswith("titan source") for reason in report["reasons"])


def test_w7c_sealed_promotion_then_pack_and_both_copies(sealed_audit: dict, tmp_path: Path,
                                                       monkeypatch) -> None:
    audit = sealed_audit
    w6.copy_devices.__wrapped__(tmp_path, monkeypatch)
    options = w6.restic_options.__wrapped__(tmp_path)
    cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="sealed upgrade", by="researcher", actor=ACTOR)
    rows = lr.read_register(audit["repo"]).rows
    assert [row["event"] for row in rows[-2:]] == ["promotion-pending", "promote"]
    assert rows[-1]["state_after"] == "sealed" and rows[-1]["pack_sha256"] == ""
    assert rows[-1]["class"] == "methodology-bearing"
    assert not any(row["event"] in ("pack", "copy") for row in rows)
    selected = [row for row in audit["rows"] if row["hash_bound"] == "1" and row["sha256"]]
    for row in selected:
        lo.packs.checked_file(lo.store.cas_path(audit["store"], row["sha256"]), row["sha256"], int(row["bytes"]))
    lo.packs.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"],
                  copy_a_root=audit["copy_a"], actor=ACTOR)
    _copy(audit)
    _copy(audit, copy="B", **options)
    rows = lr.read_register(audit["repo"]).rows
    assert rows[-1]["state_after"] == "replicated(2)"
    assert all(row["class"] == "methodology-bearing" for row in rows if row["event"] in ("pack", "copy"))
    packed = next(row for row in rows if row["event"] == "pack")
    refs = lo.copies.read_cas_refs(audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst", b"", packed)
    assert {row["sha256"] for row in selected} <= {row["sha256"] for row in refs}
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="sealed upgrade", by="researcher", actor=ACTOR)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before


@pytest.mark.parametrize("state", ["open", "finished", "rehydrated"])   # host: offloaded is now promotable (T12)
def test_w7c_promote_refuses_other_states_before_ingest(sealed_audit: dict, state: str) -> None:
    audit = sealed_audit
    if state in ("offloaded", "rehydrated"):
        _production_pack(audit)
        _copy(audit)
    _append(audit, "census", state, **{"class": "code-only"})
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    with pytest.raises(lr.RegisterError, match=r"latest state sealed, packed, replicated\(n\) or offloaded required"):
        cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="upgrade", by="researcher", actor=ACTOR)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert not (audit["repo"] / lo.packs.STORE_INDEX).exists()


def test_w7c_promote_refuses_state_change_between_copy_steps(sealed_audit: dict, tmp_path: Path,
                                                           monkeypatch) -> None:
    audit = sealed_audit
    w6.copy_devices.__wrapped__(tmp_path, monkeypatch)
    options = w6.restic_options.__wrapped__(tmp_path)
    _production_pack(audit)
    _copy(audit)
    operation = lo.copies.copy_operation
    def change_state(repo, **kwargs):
        operation(repo, **kwargs)
        _append(audit, "census", "rehydrated", **{"class": "code-only"})   # host: a state promote must still refuse
    monkeypatch.setattr(lo.copies, "copy_operation", change_state)
    with pytest.raises(lr.RegisterError, match="found rehydrated"):
        cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="upgrade", by="researcher", actor=ACTOR, **options)
    rows = lr.read_register(audit["repo"]).rows
    assert rows[-1]["state_after"] == "rehydrated"
    assert any(row["event"] == "promotion-pending" for row in rows)
    assert not any(row["event"] == "promote" for row in rows)
    assert len(lo.copies.counting_locations(AUDIT_ID, lr.read_register(audit["repo"]))) == 2


def test_w7c_copies_static_analysis_has_no_unused_import() -> None:
    result = subprocess.run(["/opt/anaconda3/bin/pyflakes", str(Path(lo.copies.__file__))],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("internal", [False, True])
def test_w7c_multi_version_refs_replicate_verify_and_rehydrate(sealed_audit: dict, tmp_path: Path,
                                                             monkeypatch, internal: bool) -> None:
    audit = sealed_audit
    w6.copy_devices.__wrapped__(tmp_path, monkeypatch)
    options = w6.restic_options.__wrapped__(tmp_path)
    indexed = w6._external_cas_audit(audit, "methodology-bearing")
    original = next(row for row in indexed if (not Path(row["use_path"]).is_absolute()
                    if internal else row["use_path"].endswith("external-context.txt")))
    source = tmp_path / "second-version"
    source.write_bytes(b"another preserved version of the same context\n")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    lo.store.ingest_file(lo.packs.load_cas(), source, sha, byte_count=source.stat().st_size,
                         audit_id=AUDIT_ID, round_label="round-2", source_path=original["use_path"])
    indexed.append({**original, "sha256": sha, "bytes": str(source.stat().st_size)})
    (audit["repo"] / lo.packs.STORE_INDEX).write_bytes(w6.ls._tsv(lo.packs.STORE_HEADER, indexed))
    lo.packs.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"],
                  copy_a_root=audit["copy_a"], actor=ACTOR)
    pack = audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst"
    _copy(audit)
    _copy(audit, copy="B", **options)
    packed = next(row for row in lr.read_register(audit["repo"]).rows if row["event"] == "pack")
    refs = lo.copies.read_cas_refs(pack, b"", packed)
    versions = [row for row in refs if row["path"] == original["use_path"]]
    assert {row["sha256"] for row in versions} == {original["sha256"], sha}
    # Neither verify nor restore may consult the live context or the boot CAS.
    if not internal:
        Path(original["use_path"]).unlink()
    for row in refs:
        lo.store.cas_path(audit["store"], row["sha256"]).unlink()
    _copy(audit, event="verify")
    _copy(audit, copy="B", event="verify", **options)
    for copy in ("A", "B"):
        target = tmp_path / f"restored-{copy}"
        lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, copy=copy, actor=ACTOR, into=target,
                     **(options if copy == "B" else {}))
        expected = {row["path"] for row in audit["rows"]} - {original["use_path"]}
        assert {path.relative_to(target).as_posix() for path in target.rglob("*")} == expected
        assert "multi-version:" + original["use_path"] in lr.read_register(audit["repo"]).rows[-1]["note"]
    for row in versions:
        lo.packs.checked_file(lo.store.cas_path(audit["copy_a"] / "cas", row["sha256"]),
                              row["sha256"], int(row["bytes"]))
    if not internal:
        assert not Path(original["use_path"]).exists()


def _orphaned_intent(audit: dict, monkeypatch) -> tuple[Path, bytes]:
    """Interrupt only the register append after real Phase-1 validation and the durable intent write."""
    _append(audit, "classify", "sealed", **{"class": "code-only"}, note="lifecycle/classification.md")
    _production_pack(audit)
    _copy(audit)
    paths = [str(audit["manifest"].relative_to(audit["repo"])),
             str((audit["dir"] / "lifecycle/classification.md").relative_to(audit["repo"])),
             lr.REGISTER_PATH, w6.ls.FREEZES]
    w6._git(audit["repo"], "add", "--", *paths)
    w6._git(audit["repo"], "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "fixture anchors")
    event = lo._event
    def interrupt(repo, audit_id, audit_dir, actor, kind, state, **kwargs):
        if kind == "offload-intent":
            raise lr.RegisterError("refused injected interruption before intent append")
        return event(repo, audit_id, audit_dir, actor, kind, state, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(lo, "_event", interrupt)
        with pytest.raises(lr.RegisterError, match="injected interruption"):
            lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare")
    path = audit["dir"] / "lifecycle/offload_intent.json"
    raw = path.read_bytes()
    assert not any(row["event"] == "offload-intent" for row in lr.read_register(audit["repo"]).rows)
    return path, raw


def test_w7c_prepare_recovers_orphaned_intent_without_rewriting(sealed_audit: dict, monkeypatch) -> None:
    audit = sealed_audit
    path, raw = _orphaned_intent(audit, monkeypatch)
    mtime = path.stat().st_mtime_ns
    lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare")
    assert path.read_bytes() == raw and path.stat().st_mtime_ns == mtime
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "offload-intent"
    prefix, sha = row["note"].split("; free_space_sha256:")
    assert prefix == "intent_sha256:" + hashlib.sha256(raw).hexdigest()
    w6._assert_free_space(audit, row, [json.loads((audit["dir"] / "lifecycle/free_space_checks" / (sha + ".json")).read_bytes())])
    assert audit["runtime"].exists()
    with pytest.raises(lr.RegisterError, match="already exists"):
        lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare")
    relative = path.relative_to(audit["repo"]).as_posix()
    w6._git(audit["repo"], "add", "--", relative, lr.REGISTER_PATH)
    w6._git(audit["repo"], "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "fixture intent")
    lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="execute")
    assert not audit["runtime"].exists()
    assert lr.read_register(audit["repo"]).rows[-1]["event"] == "offload"


@pytest.mark.parametrize("field", ["removal_set", "pack_sha256", "copies", "audit_id", "manifest_sha256",
                                  "removal_bytes_type"])
def test_w7c_prepare_refuses_different_orphaned_intent(sealed_audit: dict, monkeypatch, field: str) -> None:
    audit = sealed_audit
    path, raw = _orphaned_intent(audit, monkeypatch)
    intent = json.loads(raw)
    if field == "removal_bytes_type":
        member = next(entry for entry in intent["removal_set"] if "bytes" in entry)
        member["bytes"] = float(member["bytes"])
        field = "removal_set"
    else:
        intent[field] = [] if field in ("removal_set", "copies") else "different"
    raw = (json.dumps(intent, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    with pytest.raises(lr.RegisterError, match=field + " differs"):
        lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare")
    assert path.read_bytes() == raw
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert audit["runtime"].exists()


@pytest.mark.parametrize("defect", ["pack", "replica", "freeze", "classification", "raw"])
def test_w7c_prepare_revalidates_orphaned_intent_preconditions(sealed_audit: dict, monkeypatch, defect: str) -> None:
    audit = sealed_audit
    path, raw = _orphaned_intent(audit, monkeypatch)
    damaged = {"pack": audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst",
               "replica": lo.store.cas_path(audit["copy_a"] / "cas", audit["blob_sha"]),
               "freeze": audit["freeze"], "classification": audit["dir"] / "lifecycle/classification.md",
               "raw": audit["runtime"]}[defect]
    damaged.chmod(0o644)
    damaged.write_bytes(b"changed preserved bytes\n")
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    with pytest.raises(lr.RegisterError, match="HEAD bytes or status differ" if defect == "classification" else "sha256/bytes"):
        lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare")
    assert path.read_bytes() == raw
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert audit["runtime"].exists()


def _w12_ready(audit: dict, tmp_path: Path, monkeypatch, copy: str) -> dict:
    from test_lifecycle_t9_offload import commit
    _append(audit, "classify", "sealed", **{"class": "code-only"}, note="lifecycle/classification.md")
    _production_pack(audit)
    options = {}
    if copy == "B":
        w6.copy_devices.__wrapped__(tmp_path, monkeypatch)
        options = w6.restic_options.__wrapped__(tmp_path)
    _copy(audit, copy=copy, **options)
    commit(audit, "W12 fixture lifecycle records")
    return options


@pytest.mark.parametrize("copy", ["A", "B"])
def test_w12_t9_offload_prepare_records_checks(sealed_audit: dict, tmp_path: Path, monkeypatch, copy: str) -> None:
    audit = sealed_audit
    options = _w12_ready(audit, tmp_path, monkeypatch, copy)
    results = w6._capture_free_space(monkeypatch, lo)
    lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare", **options)
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "offload-intent"
    intent = audit["dir"] / "lifecycle/offload_intent.json"
    assert row["note"].split("; free_space_sha256:")[0] == "intent_sha256:" + hashlib.sha256(intent.read_bytes()).hexdigest()
    assert len(results) == (1 if copy == "A" else 2)
    w6._assert_free_space(audit, row, results)
    assert audit["runtime"].exists()


def test_w12_t9_offload_execute_copy_b_records_check(sealed_audit: dict, tmp_path: Path, monkeypatch) -> None:
    from test_lifecycle_t9_offload import commit
    audit = sealed_audit
    options = _w12_ready(audit, tmp_path, monkeypatch, "B")
    lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare", **options)
    commit(audit, "W12 fixture intent")
    results = w6._capture_free_space(monkeypatch, lo)
    lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="execute", **options)
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "offload" and len(results) == 1
    w6._assert_free_space(audit, row, results)
    assert not audit["runtime"].exists()


@pytest.mark.parametrize("copy", ["A", "B"])
def test_w12_t9_rehydrate_records_checks_in_order(sealed_audit: dict, tmp_path: Path, monkeypatch, copy: str) -> None:
    audit = sealed_audit
    options = _w12_ready(audit, tmp_path, monkeypatch, copy)
    results = w6._capture_free_space(monkeypatch, lo)
    lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, copy=copy,
                 into=tmp_path / "restored", **options)
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "rehydrate" and row["note"].startswith("into:")
    assert len(results) == (1 if copy == "A" else 2)
    w6._assert_free_space(audit, row, results)


@pytest.mark.parametrize("copy", ["A", "B"])
def test_w12_t9_identical_rehydrate_reuses_sidecars(sealed_audit: dict, tmp_path: Path, monkeypatch, copy: str) -> None:
    audit = sealed_audit
    options = _w12_ready(audit, tmp_path, monkeypatch, copy)
    results = w6._capture_free_space(monkeypatch, lo)
    lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, copy=copy, in_place=True, **options)
    count = len(results)
    paths = w6._assert_free_space(audit, lr.read_register(audit["repo"]).rows[-1], results)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in paths}
    before_names = set(paths[0].parent.iterdir())
    lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, copy=copy, in_place=True, **options)
    assert len(results) == count * 2 and results[:count] == results[count:]
    assert w6._assert_free_space(audit, lr.read_register(audit["repo"]).rows[-1], results[count:]) == paths
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in paths} == before
    assert set(paths[0].parent.iterdir()) == before_names


def test_w12_t9_identical_prepare_recovery_reuses_sidecar(sealed_audit: dict, monkeypatch) -> None:
    audit = sealed_audit
    monkeypatch.setattr(lo, "utc_now", lambda: "2026-10-03T00:00:00Z")
    results = w6._capture_free_space(monkeypatch, lo)
    _orphaned_intent(audit, monkeypatch)
    first = results[:]
    sidecars = audit["dir"] / "lifecycle/free_space_checks"
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in sidecars.iterdir()}
    lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare")
    assert results[len(first):] == first
    w6._assert_free_space(audit, lr.read_register(audit["repo"]).rows[-1], first)
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in sidecars.iterdir()} == before


@pytest.mark.parametrize("operation", ["prepare", "execute-B", "rehydrate-A", "rehydrate-B"])
def test_w12_t9_conflicting_sidecar_refused_before_operation(sealed_audit: dict, tmp_path: Path,
                                                          monkeypatch, operation: str) -> None:
    from test_lifecycle_t9_offload import commit
    audit = sealed_audit
    copy = "B" if operation.endswith("-B") else "A"
    options = _w12_ready(audit, tmp_path, monkeypatch, copy)
    if operation == "execute-B":
        lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare", **options)
        commit(audit, "W12 fixture intent")
    def check(target, incoming):
        result = w6._fixed_free_space(target, incoming)
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        path = audit["dir"] / "lifecycle/free_space_checks" / (hashlib.sha256(raw).hexdigest() + ".json")
        path.write_bytes(b"different bytes\n")
        audit["conflict"] = path
        return result
    monkeypatch.setattr(lo, "check_free_space", check)
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    with pytest.raises(lr.RegisterError, match="free_space_checks/.*existing bytes differ"):
        if operation.startswith("rehydrate"):
            lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, copy=copy,
                         into=tmp_path / "restored", **options)
        else:
            lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR,
                       phase="execute" if operation == "execute-B" else "prepare", **options)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert audit["conflict"].read_bytes() == b"different bytes\n"
    assert audit["runtime"].exists() and not (tmp_path / "restored").exists()


@pytest.mark.parametrize("operation", ["prepare", "execute-B", "rehydrate-A", "rehydrate-B"])
def test_w12_t9_refused_check_writes_no_sidecar_or_row(sealed_audit: dict, tmp_path: Path,
                                                    monkeypatch, operation: str) -> None:
    from free_space import FreeSpaceError
    from test_lifecycle_t9_offload import commit
    audit = sealed_audit
    copy = "B" if operation.endswith("-B") else "A"
    options = _w12_ready(audit, tmp_path, monkeypatch, copy)
    if operation == "execute-B":
        lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare", **options)
        commit(audit, "W12 fixture intent")
    sidecars = audit["dir"] / "lifecycle/free_space_checks"
    before_files = {path: path.read_bytes() for path in sidecars.iterdir()}
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    def refuse(target, incoming):
        raise FreeSpaceError(f"refused free-space check target={target}: fixture floor refusal")
    monkeypatch.setattr(lo, "check_free_space", refuse)
    with pytest.raises(FreeSpaceError, match="fixture floor refusal"):
        if operation.startswith("rehydrate"):
            lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, copy=copy,
                         into=tmp_path / "restored", **options)
        else:
            lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR,
                       phase="execute" if operation == "execute-B" else "prepare", **options)
    assert {path: path.read_bytes() for path in sidecars.iterdir()} == before_files
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert audit["runtime"].exists() and not (tmp_path / "restored").exists()


@pytest.mark.parametrize("state", ["sealed", "packed", "packed-no-copies"])
def test_w12b_promotion_records_checks_only_on_completion(sealed_audit: dict, tmp_path: Path,
                                                       monkeypatch, state: str) -> None:
    audit = sealed_audit
    options = {}
    if state != "sealed":
        w6.copy_devices.__wrapped__(tmp_path, monkeypatch)
        options = w6.restic_options.__wrapped__(tmp_path)
        _production_pack(audit)
        if state == "packed":
            _copy(audit)
            _copy(audit, copy="B", **options)
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    before_count = len(lr.read_register(audit["repo"]).rows)
    results, pending_bytes = [], []
    def check(target, incoming):
        pending = lr.read_register(audit["repo"]).rows[-1]
        assert pending["event"] == "promotion-pending"
        assert pending["note"] == "reason:recorded upgrade; by:researcher"
        pending_bytes.append((audit["repo"] / lr.REGISTER_PATH).read_bytes())
        result = w6._fixed_free_space(target, incoming)
        results.append(result)
        return result
    monkeypatch.setattr(lo, "check_free_space", check)
    cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="recorded upgrade", by="researcher", actor=ACTOR, **options)
    rows = lr.read_register(audit["repo"]).rows
    new_rows = rows[before_count:]
    assert [row["event"] for row in new_rows] == ["promotion-pending"] + (
        ["copy"] * 6 if state == "packed-no-copies" else []) + ["promote"]
    assert len(results) == (1 if state == "sealed" else 2)
    assert results[0]["target"] == str(audit["store"].resolve())
    if state != "sealed":
        assert results[1]["target"] == str((audit["copy_a"] / "_promotion").resolve())
    paths = w6._assert_free_space(audit, rows[-1], results)
    promotion_shas = {path.stem for path in paths}
    for row in new_rows[1:-1]:
        assert all(sha not in row["note"] for sha in promotion_shas)
    ingested = len({row["sha256"] for row in audit["rows"] if row["hash_bound"] == "1" and row["sha256"]})
    prefix, _ = rows[-1]["note"].split("; free_space_sha256:")
    assert prefix == f"reason:recorded upgrade; by:researcher; ingested:{ingested}"
    after = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    assert all(raw == pending_bytes[0] and raw.startswith(before) for raw in pending_bytes)
    assert after.startswith(pending_bytes[0]) and after.count(b"promotion-pending") == 1
    snapshot = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}
    cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="recorded upgrade", by="researcher", actor=ACTOR, **options)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == after
    assert {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths} == snapshot


def test_w12b_promotion_copy_failure_preserves_unreferenced_checks(sealed_audit: dict, monkeypatch) -> None:
    audit = sealed_audit
    _production_pack(audit)
    results = w6._capture_free_space(monkeypatch, lo)
    def failed_copy(repo, **kwargs):
        raise lr.RegisterError("refused W12b promotion copy failure")
    monkeypatch.setattr(lo.copies, "copy_operation", failed_copy)
    with pytest.raises(lr.RegisterError, match="W12b promotion copy failure"):
        cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="failed upgrade", by="researcher", actor=ACTOR)
    assert len(results) == 2
    pending = lr.read_register(audit["repo"]).rows[-1]
    assert pending["event"] == "promotion-pending" and pending["note"] == "reason:failed upgrade; by:researcher"
    paths = []
    for result in results:
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        sha = hashlib.sha256(raw).hexdigest()
        path = audit["dir"] / "lifecycle/free_space_checks" / (sha + ".json")
        assert path.read_bytes() == raw and hashlib.sha256(path.read_bytes()).hexdigest() == sha
        paths.append(path)
    before = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}
    # The retry's CAS check has zero incoming bytes; the identical scratch check reuses its sidecar.
    with pytest.raises(lr.RegisterError, match="W12b promotion copy failure"):
        cli.promote(audit["repo"], audit_id=AUDIT_ID, reason="failed upgrade", by="researcher", actor=ACTOR)
    assert results[2]["incoming_bytes"] == 0 and results[3] == results[1]
    assert {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths} == before
    assert lr.read_register(audit["repo"]).rows[-1] == pending


@pytest.mark.parametrize("phase", ["execute", "resume"])
def test_w12b_offload_without_checks_has_no_carried_reference(sealed_audit: dict, tmp_path: Path,
                                                           monkeypatch, phase: str) -> None:
    from test_lifecycle_t9_offload import commit
    audit = sealed_audit
    _w12_ready(audit, tmp_path, monkeypatch, "A")
    results = w6._capture_free_space(monkeypatch, lo)
    lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase="prepare")
    intent = lr.read_register(audit["repo"]).rows[-1]
    paths = w6._assert_free_space(audit, intent, results)
    before = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}
    commit(audit, "W12b fixture intent")
    count = len(results)
    lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase=phase)
    rows = lr.read_register(audit["repo"]).rows
    assert len(results) == count
    assert rows[-1]["event"] == "offload" and "free_space_sha256:" not in rows[-1]["note"]
    intent_after = next(row for row in reversed(rows) if row["event"] == "offload-intent")
    assert intent_after == intent and w6._assert_free_space(audit, intent_after, results) == paths
    assert {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths} == before
