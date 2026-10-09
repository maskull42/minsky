"""W6 round-trip mechanics; refuse corrupt packs with explicit KB fixture metadata."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import zstandard

from byte_sources import PackError, PackIndex, iter_pack_members
import lifecycle_pack as lp
import lifecycle_copies as lc
import lifecycle_register as lr
import lifecycle_seal as ls
from test_hardening import PERSONA, SCRIPTS, provenance, record_call, register_round
from test_lifecycle_w8_register import _git

MUTANT_KILL_MATRIX = {
    "compare_float_mtime": "test_t7_compare_float_mtime_mutant_killed",
    "ignore_extra_members": "test_t7_ignore_extra_members_mutant_killed",
    "emit_lnktype": "test_t7_production_emit_lnktype_mutant_killed",
    "production_float_mtime": "test_t7_production_compare_float_mtime_mutant_killed",
    "size_only": "test_t8_size_only_mutant_killed",
    "skip_cas_row": "test_t8_skip_cas_replica_row_mutant_killed",
    "flag_on_seal_only": "test_t22_flag_on_seal_only_mutant_killed",
}
NANOSECONDS = 1790800000123456789


@pytest.fixture(autouse=True)
def copy_devices(tmp_path: Path, monkeypatch):
    """Keep restic inputs whitespace-free; simulate its separate device without volume access."""
    monkeypatch.chdir(tmp_path)
    real_stat = os.stat
    repository = str(tmp_path / "restic")

    def device_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if isinstance(path, (str, Path)) and os.path.realpath(path) == repository:
            fields = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
            fields["st_dev"] += 1
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(os, "stat", device_stat)
    return real_stat


@pytest.fixture
def members(tmp_path: Path) -> tuple[list[lp.ExpectedMember], dict[str, Path]]:
    root = tmp_path / "audit"
    root.mkdir()
    round_dir = root / "round-2"
    register_round(round_dir)
    output = round_dir / "result.json"
    output.write_text("{}\n")
    record_call(round_dir, uid="packfixture", step="codex", model="gpt-5.6-sol", effort="medium",
                outputs=[output], invoked_at="2026-10-01T00:00:00Z", persona=PERSONA,
                usage={"status": "total-only", "total_tokens": 1})
    first = root / "first"
    first.write_bytes(b"two independent hardlink payloads\n")
    os.utime(first, ns=(NANOSECONDS, NANOSECONDS))
    assert first.stat().st_mtime_ns == NANOSECONDS
    os.link(first, root / "second")
    (root / "internal").symlink_to("first")
    external = tmp_path / "external"
    external.write_bytes(b"dereferenced external bytes\n")
    (root / "external-link").symlink_to(external)
    expected, sources = [], {}
    # These values describe test archives only. No production defaults are assigned
    # to H4's blank directory/link fields or the generated CAS_REFS.tsv member.
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        info = path.lstat()
        target = ""
        if name == "external-link":
            info = external.stat()
        if stat.S_ISDIR(info.st_mode):
            kind, sha, byte_count = "dir", None, 0
        elif stat.S_ISLNK(info.st_mode):
            kind, sha, byte_count, target = "symlink", None, 0, os.readlink(path)
        else:
            kind, sha, byte_count = "file", hashlib.sha256(path.read_bytes()).hexdigest(), info.st_size
        expected.append(lp.ExpectedMember(name, kind, sha, byte_count, stat.S_IMODE(info.st_mode),
                                          info.st_mtime_ns, target))
        sources[name] = path
    return expected, sources


def _write_pack(path: Path, expected: list[lp.ExpectedMember], sources: dict[str, Path],
                *, change=None) -> None:
    with path.open("wb") as compressed:
        with zstandard.ZstdCompressor(level=19).stream_writer(compressed, closefd=False) as stream:
            with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for item in expected:
                    source = sources[item.path]
                    source.lstat()
                    info = tarfile.TarInfo(item.path)
                    info.type = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE,
                                 "symlink": tarfile.SYMTYPE}[item.type]
                    info.size, info.mode, info.mtime = item.bytes, item.mode, item.mtime_ns // 10**9
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.pax_headers = {"MINSKY.mtime_ns": str(item.mtime_ns)}
                    info.linkname = item.symlink_target
                    payload = source.read_bytes() if item.type == "file" else None
                    if item.sha256 is not None:
                        info.pax_headers["MINSKY.sha256"] = item.sha256
                    if change is not None:
                        info, payload = change(info, payload)
                    archive.addfile(info, io.BytesIO(payload) if payload is not None else None)


def test_t7_exact_set_ns_hardlink_and_symlink_round_trip(tmp_path: Path, members) -> None:
    expected, sources = members
    pack = tmp_path / "fixture.tar.zst"
    _write_pack(pack, expected, sources)
    lp.round_trip(pack, expected)
    scanned = {item.info.name: item for item in iter_pack_members(pack)}
    assert set(scanned) == {item.path for item in expected}
    for name in ("first", "second"):
        assert scanned[name].info.type == tarfile.REGTYPE
        assert scanned[name].bytes == sources[name].stat().st_size
        assert scanned[name].sha256 == hashlib.sha256(sources[name].read_bytes()).hexdigest()
        assert int(scanned[name].info.pax_headers["MINSKY.mtime_ns"]) == NANOSECONDS
    assert scanned["internal"].info.type == tarfile.SYMTYPE
    assert scanned["internal"].info.linkname == "first"
    assert scanned["external-link"].info.type == tarfile.REGTYPE
    assert scanned["external-link"].sha256 == hashlib.sha256(sources["external-link"].read_bytes()).hexdigest()
    # W1's content-first map retains its existing last-name behavior for identical payloads.
    index = PackIndex.build(pack)
    sha = scanned["first"].sha256
    assert index.members[sha] == "second"
    assert index.pax_sha256["first"] == index.pax_sha256["second"] == sha


@pytest.mark.parametrize("defect", ["byte", "truncated", "extra", "missing", "duplicate", "hardlink"])
def test_t7_corrupt_truncated_extra_missing_duplicate_and_lnk_refused(tmp_path: Path, members,
                                                                   defect: str) -> None:
    expected, sources = members
    written = list(expected)
    if defect == "extra":
        sources["extra"] = sources["first"]
        written.append(replace(expected[0], path="extra"))
    elif defect == "missing":
        written.pop()
    elif defect == "duplicate":
        written.append(expected[0])

    def change(info, payload):
        if info.name == "first":
            if defect == "byte":
                payload = b"!" + payload[1:]
            elif defect == "hardlink":
                info.type, info.size, info.linkname, payload = tarfile.LNKTYPE, 0, "second", None
        return info, payload

    pack = tmp_path / "bad.tar.zst"
    _write_pack(pack, written, sources, change=change)
    if defect == "truncated":
        pack.write_bytes(pack.read_bytes()[:-1])
    with pytest.raises(PackError):
        lp.round_trip(pack, expected)


@pytest.mark.parametrize("defect", ["mode", "mtime", "missing-mtime", "invalid-mtime", "missing-sha",
                                  "type", "target", "bytes"])
def test_t7_each_member_metadata_mismatch_refused(tmp_path: Path, members, defect: str) -> None:
    expected, sources = members

    def change(info, payload):
        if info.name == "first":
            if defect == "mode":
                info.mode ^= 0o100
            elif defect == "mtime":
                info.pax_headers["MINSKY.mtime_ns"] = str(NANOSECONDS + 1)
            elif defect == "missing-mtime":
                info.pax_headers.pop("MINSKY.mtime_ns")
            elif defect == "invalid-mtime":
                info.pax_headers["MINSKY.mtime_ns"] = "nan"
            elif defect == "missing-sha":
                info.pax_headers.pop("MINSKY.sha256")
            elif defect == "type":
                info.type, info.size, payload = tarfile.DIRTYPE, 0, None
            elif defect == "bytes":
                info.size -= 1
                payload = payload[:-1]
                info.pax_headers["MINSKY.sha256"] = hashlib.sha256(payload).hexdigest()
        if info.name == "internal" and defect == "target":
            info.linkname = "second"
        return info, payload

    pack = tmp_path / "metadata.tar.zst"
    _write_pack(pack, expected, sources, change=change)
    with pytest.raises(PackError):
        lp.round_trip(pack, expected)


def _mutant(tmp_path: Path, name: str, needle: str, replacement: str):
    return _module_mutant(tmp_path, name, needle, replacement, "lifecycle_pack.py")


def _module_mutant(tmp_path: Path, name: str, needle: str, replacement: str, filename: str):
    original = (SCRIPTS / filename).read_text()
    assert name in MUTANT_KILL_MATRIX and original.count(needle) == 1
    path = tmp_path / f"{name}.py"
    path.write_text(original.replace(needle, replacement))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _assert_refused(module, pack: Path, expected: list[lp.ExpectedMember], reason: str) -> None:
    try:
        module.round_trip(pack, expected)
    except PackError as exc:
        assert reason in str(exc)
    else:
        raise AssertionError("round trip accepted invalid pack metadata")


def test_t7_compare_float_mtime_mutant_killed(tmp_path: Path, members) -> None:
    expected, sources = members
    expected = [item for item in expected if item.path == "first"]
    pack = tmp_path / "ns.tar.zst"

    def float_header(info, payload):
        info.mtime = NANOSECONDS / 10**9
        return info, payload

    _write_pack(pack, expected, sources, change=float_header)
    lp.round_trip(pack, expected)
    changed = [replace(item, mtime_ns=item.mtime_ns + 1) if item.path == "first" else item for item in expected]
    with pytest.raises(PackError, match="MINSKY.mtime_ns"):
        lp.round_trip(pack, changed)
    module = _mutant(tmp_path, "compare_float_mtime",
                     'not re.fullmatch(r"-?\\d+", recorded_ns) or int(recorded_ns) != item.mtime_ns',
                     "info.mtime != item.mtime_ns / 10**9")
    with pytest.raises(AssertionError):
        _assert_refused(module, pack, changed, "MINSKY.mtime_ns")


def test_t7_ignore_extra_members_mutant_killed(tmp_path: Path, members) -> None:
    expected, sources = members
    pack = tmp_path / "extra.tar.zst"
    _write_pack(pack, expected, sources)
    changed = [item for item in expected if item.path != "second"]
    with pytest.raises(PackError, match="extra member second"):
        lp.round_trip(pack, changed)
    module = _mutant(tmp_path, "ignore_extra_members",
                     'raise PackError(f"refused pack {pack}: extra member {info.name}")', "continue")
    # The independent count would also kill this mutant. Neutralize that check
    # here so the assertion specifically tests the exact member-set comparison.
    with patch.object(module, "_independent_check") as independent:
        independent.return_value.stdout = "\n".join(item.path for item in changed) + "\n"
        with pytest.raises(AssertionError):
            _assert_refused(module, pack, changed, "extra member second")


@pytest.mark.parametrize("command,defect", [("zstd", "failure"), ("bsdtar", "failure"),
                                          ("bsdtar", "count"), ("zstd", "missing")])
def test_t7_independent_checks_are_required(tmp_path: Path, members, monkeypatch,
                                          command: str, defect: str) -> None:
    expected, sources = members
    pack = tmp_path / "independent.tar.zst"
    _write_pack(pack, expected, sources)
    real_run = lp.subprocess.run

    def run(argv, **kwargs):
        if argv[0] == command:
            if defect == "missing":
                raise FileNotFoundError("fixture missing executable")
            return lp.subprocess.CompletedProcess(argv, 1 if defect == "failure" else 0,
                                                  stdout="", stderr="fixture independent failure")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(lp.subprocess, "run", run)
    with pytest.raises(PackError, match=command):
        lp.round_trip(pack, expected)


@pytest.mark.parametrize("change", ["duplicate", "path", "sha", "mode", "mtime", "symlink"])
def test_t7_invalid_expected_metadata_is_refused(tmp_path: Path, members, change: str) -> None:
    expected, sources = members
    pack = tmp_path / "expected.tar.zst"
    _write_pack(pack, expected, sources)
    first = next(item for item in expected if item.path == "first")
    changed = {"path": replace(first, path="../escape"), "sha": replace(first, sha256="invalid"),
               "mode": replace(first, mode=-1), "mtime": replace(first, mtime_ns=None),
               "symlink": replace(first, symlink_target="unexpected")}
    expected = [*expected, first] if change == "duplicate" else [changed[change]]
    with pytest.raises(PackError, match="expected pack member"):
        lp.round_trip(pack, expected)


ACTOR = "W6b fixture GPT-6.1 Sol@xhigh"
AUDIT_ID = "test-audit"


def _append(audit: dict, event: str, state: str, **values) -> None:
    with lr.AuditLock.acquire(audit["repo"], AUDIT_ID, verb=event, session=ACTOR) as lock:
        row = dict.fromkeys(lr.COLUMNS, "")
        row.update(audit_id=AUDIT_ID, audit_dir=audit["dir"].relative_to(audit["repo"]).as_posix(),
                   actor=ACTOR, event=event, state_after=state, **values)
        lr.append_event(audit["repo"], row, lock=lock)


@pytest.fixture
def sealed_audit(tmp_path: Path, minsky_store: Path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty", "-m", "fixture")
    directory = repo / "audits" / AUDIT_ID
    directory.mkdir(parents=True)
    round_dir = directory / "round-2"
    register_round(round_dir)
    output = round_dir / "result.json"
    output.write_text("{}\n")
    record_call(round_dir, uid="packfixture", step="codex", model="gpt-5.6-sol", effort="medium",
                outputs=[output], persona=PERSONA, invoked_at="2026-10-01T00:00:00Z",
                usage={"status": "total-only", "total_tokens": 1})
    first = directory / "first"
    first.write_bytes(b"independent hardlink bytes\n")
    os.utime(first, ns=(NANOSECONDS, NANOSECONDS))
    os.link(first, directory / "second")
    (directory / ".hidden").write_bytes(b"dotfile\n")
    (directory / "empty").touch()
    (directory / "internal").symlink_to("first")
    external = tmp_path / "external"
    external.write_bytes(b"external bytes\n")
    (directory / "external-link").symlink_to(external)
    runtime = round_dir / "provenance/runtime-blobs/runtime"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"KB synthetic runtime\n")
    blob_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    blob = ls.store.cas_path(minsky_store, blob_sha)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(runtime.read_bytes())
    blob.chmod(0o444)
    lifecycle = directory / "lifecycle"
    lifecycle.mkdir()
    (lifecycle / "classification.md").write_text("Fixture code-only classification\n")
    audit = {"repo": repo, "dir": directory, "copy_a": tmp_path / "copy-a", "store": minsky_store,
             "blob": blob, "blob_sha": blob_sha, "runtime": runtime, "external": external}
    _append(audit, "classify", "finished", **{"class": "code-only"})
    rows, _ = ls.raw_manifest(repo, directory, audit_id=AUDIT_ID)
    manifest = lifecycle / "raw_manifest.tsv"
    manifest.write_bytes(ls._tsv(ls.RAW_MANIFEST_HEADER, rows))
    boot = tmp_path / "boot-freezes/freeze.sqlite"
    boot.parent.mkdir()
    with sqlite3.connect(boot) as connection:
        connection.execute("CREATE TABLE fixture(value TEXT)")
        connection.execute("INSERT INTO fixture VALUES ('preserved audit state')")
    freeze = audit["copy_a"] / "freezes" / boot.name
    freeze.parent.mkdir(parents=True)
    shutil.copyfile(boot, freeze)
    freeze_sha = hashlib.sha256(boot.read_bytes()).hexdigest()
    freezes = repo / ls.FREEZES
    freezes.parent.mkdir(parents=True, exist_ok=True)
    freezes.write_bytes(ls._tsv(ls.FREEZE_HEADER, [
        {"utc": "2026-10-01T00:00:00Z", "audit_id": AUDIT_ID, "event": event,
         "location": location, "path": str(path), "bytes": str(boot.stat().st_size), "sha256": freeze_sha,
         "integrity_check": "ok", "note": "fixture"}
        for event, location, path in (("freeze", "boot", boot), ("freeze-copy", "destination", freeze))]))
    _append(audit, "seal", "sealed", citation_resolvability="pass", manifest_sha256=lp._hash_file(manifest),
            **{"class": "code-only"}, note="freeze:" + freeze_sha + "; ingested:0")   # real seals always record ingested:<n>
    return {**audit, "rows": rows, "manifest": manifest, "boot_freeze": boot,
            "freeze": freeze, "freeze_sha": freeze_sha}


def _production_pack(audit: dict) -> tuple[Path, list[lp.ExpectedMember], bytes]:
    resident = lp.cas_rows(audit["rows"], "code-only")
    refs = lp.cas_refs_bytes(resident)
    expected, _ = lp._plan(audit["dir"], audit["rows"], resident, refs, None)
    lp.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], copy_a_root=audit["copy_a"], actor=ACTOR)
    return audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst", expected, refs


def _copy(audit: dict, copy: str = "A", event: str = "copy", **kwargs) -> None:
    lc.copy_operation(audit["repo"], audit_id=AUDIT_ID, copy=copy, actor=ACTOR,
                      event=event, copy_a_root=audit["copy_a"], **kwargs)


def _copy_rows(audit: dict, event: str = "copy") -> list[dict]:
    return [row for row in lr.read_register(audit["repo"]).rows if row["event"] == event]


def test_t7_production_writer_manifest_hardlinks_metadata_and_determinism(sealed_audit: dict, tmp_path: Path) -> None:
    audit = sealed_audit
    target, expected, refs = _production_pack(audit)
    lp.round_trip(target, expected)
    scanned = {item.info.name: item for item in iter_pack_members(target)}
    resident = {row["path"] for row in lp.cas_rows(audit["rows"], "code-only")}
    assert set(scanned) == {row["path"] for row in audit["rows"]} - resident | {"CAS_REFS.tsv"}
    assert not (audit["dir"] / "CAS_REFS.tsv").exists()
    for name in ("first", "second"):
        assert scanned[name].info.type == tarfile.REGTYPE
        assert scanned[name].bytes == len(b"independent hardlink bytes\n")
        assert scanned[name].sha256 == hashlib.sha256((audit["dir"] / name).read_bytes()).hexdigest()
        assert int(scanned[name].info.pax_headers["MINSKY.mtime_ns"]) == NANOSECONDS
    assert scanned["internal"].info.type == tarfile.SYMTYPE and scanned["internal"].info.linkname == "first"
    assert scanned["internal"].info.mode == 0o777
    assert scanned["external-link"].info.type == tarfile.REGTYPE
    assert scanned["external-link"].sha256 == lp._hash_file(audit["external"])
    assert scanned["CAS_REFS.tsv"].info.mode == 0o644 and scanned["CAS_REFS.tsv"].info.mtime == 0
    assert scanned["CAS_REFS.tsv"].info.pax_headers["MINSKY.mtime_ns"] == "0"
    for name, item in scanned.items():
        if item.info.isdir() or item.info.issym():
            assert "MINSKY.sha256" not in item.info.pax_headers
            assert item.info.mtime == int(item.info.pax_headers["MINSKY.mtime_ns"]) // 10**9
    _, sources = lp._plan(audit["dir"], audit["rows"], lp.cas_rows(audit["rows"], "code-only"), refs, None)
    second = tmp_path / "deterministic.tar.zst"
    lp.write_pack(second, expected, sources, refs)
    assert target.read_bytes() == second.read_bytes()
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["event"] == "pack" and row["state_after"] == "packed"
    assert row["pack_sha256"] == lp._hash_file(target)
    note, sha = row["note"].split("; free_space_sha256:")
    assert note == f"pack_verified:1; members:{len(expected)}; cas_refs_sha256:{hashlib.sha256(refs).hexdigest()}"
    _assert_free_space(audit, row, [json.loads((audit["dir"] / "lifecycle/free_space_checks" / (sha + ".json")).read_bytes())])


@pytest.mark.parametrize("change", ["bytes", "mode", "mtime", "internal-target", "external-target", "manifest"])
def test_t7_production_changed_since_seal_refused(sealed_audit: dict, change: str) -> None:
    audit = sealed_audit
    first = audit["dir"] / "first"
    if change == "bytes":
        original = first.stat()
        first.write_bytes(b"!" + first.read_bytes()[1:])
        os.utime(first, ns=(original.st_atime_ns, original.st_mtime_ns))
    elif change == "mode":
        first.chmod(0o600)
    elif change == "mtime":
        os.utime(first, ns=(NANOSECONDS, NANOSECONDS + 1))
    elif change.endswith("target"):
        link = audit["dir"] / ("internal" if change == "internal-target" else "external-link")
        link.unlink()
        link.symlink_to("second")
    else:
        audit["manifest"].write_bytes(audit["manifest"].read_bytes() + b"changed\n")
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    # Host fix (Astra W12d): call the production entry point itself, so the refusal is proven to come from `pack`,
    # not from a test helper's preliminary `_plan`.
    with pytest.raises((PackError, lr.RegisterError), match="changed (since|after) seal"):
        lp.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], copy_a_root=audit["copy_a"], actor=ACTOR)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert not list(audit["copy_a"].glob("packs/*.partial"))
    assert not list(audit["copy_a"].glob("packs/*.tar.zst"))


def test_t7_production_compare_float_mtime_mutant_killed(sealed_audit: dict, tmp_path: Path) -> None:
    target, expected, _ = _production_pack(sealed_audit)
    lp.round_trip(target, expected)
    module = _mutant(tmp_path, "production_float_mtime",
                     'not re.fullmatch(r"-?\\d+", recorded_ns) or int(recorded_ns) != item.mtime_ns',
                     "info.mtime != item.mtime_ns / 10**9")
    with pytest.raises(PackError, match="MINSKY.mtime_ns"):
        module.round_trip(target, expected)


def test_t7_production_emit_lnktype_mutant_killed(sealed_audit: dict, tmp_path: Path) -> None:
    target, expected, refs = _production_pack(sealed_audit)
    lp.round_trip(target, expected)
    _, sources = lp._plan(sealed_audit["dir"], sealed_audit["rows"],
                          lp.cas_rows(sealed_audit["rows"], "code-only"), refs, None)
    module = _mutant(tmp_path, "emit_lnktype",
                     'info.size, info.mode, info.mtime = item.bytes, item.mode, item.mtime_ns // 10**9',
                     'info.size, info.mode, info.mtime = item.bytes, item.mode, item.mtime_ns // 10**9\n'
                     '                        if item.path == "second":\n'
                     '                            info.type, info.size, info.linkname = tarfile.LNKTYPE, 0, "first"')
    with pytest.raises(PackError, match="second.*changed since seal"):
        module.write_pack(tmp_path / "mutant.tar.zst", expected, sources, refs)
    assert not (tmp_path / "mutant.tar.zst").exists()


def test_t7_round_trip_failure_kept_as_failed(sealed_audit: dict, monkeypatch) -> None:
    monkeypatch.setattr(lp, "round_trip", lambda *args: (_ for _ in ()).throw(PackError("fixture round-trip refusal")))
    with pytest.raises(PackError, match="round-trip"):
        _production_pack(sealed_audit)
    assert (sealed_audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst.failed").exists()
    assert not (sealed_audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst.partial").exists()
    assert lr.read_register(sealed_audit["repo"]).rows[-1]["event"] == "seal"
    assert not (sealed_audit["dir"] / "lifecycle/free_space_checks").exists()


def test_t8_copy_a_three_rows_then_verify_without_copy_writes(sealed_audit: dict) -> None:
    audit = sealed_audit
    target, _, _ = _production_pack(audit)
    _copy(audit)
    rows = _copy_rows(audit)
    assert [lc.parse_copy_note(row)["kind"] for row in rows] == ["pack", "cas-replica", "freeze"]
    assert [row["state_after"] for row in rows] == ["packed", "packed", "replicated(1)"]
    assert {row["copy_location"] for row in rows} == {"A:" + str(audit["copy_a"])}
    assert len({row["lock_id"] for row in rows}) == 1
    replica = ls.store.cas_path(audit["copy_a"] / "cas", audit["blob_sha"])
    assert lp._hash_file(replica) == audit["blob_sha"] and stat.S_IMODE(replica.stat().st_mode) == 0o444
    before = {path: (path.lstat().st_mode, path.lstat().st_mtime_ns, path.read_bytes())
              for path in audit["copy_a"].rglob("*") if path.is_file()}
    _copy(audit, event="verify")
    assert [lc.parse_copy_note(row)["kind"] for row in _copy_rows(audit, "verify")] == ["pack", "cas-replica", "freeze"]
    assert all(row["state_after"] == "replicated(1)" for row in _copy_rows(audit, "verify"))
    assert {path: (path.lstat().st_mode, path.lstat().st_mtime_ns, path.read_bytes())
            for path in audit["copy_a"].rglob("*") if path.is_file()} == before
    assert lp._hash_file(target) == lr.read_register(audit["repo"]).rows[-1]["pack_sha256"]


@pytest.mark.parametrize("object_name", ["pack", "cas", "freeze", "missing-cas"])
def test_t8_copy_a_failure_stops_at_own_row(sealed_audit: dict, object_name: str) -> None:
    audit = sealed_audit
    target, _, _ = _production_pack(audit)
    if object_name in ("cas", "missing-cas"):
        replica = ls.store.cas_path(audit["copy_a"] / "cas", audit["blob_sha"])
        replica.parent.mkdir(parents=True)
        replica.write_bytes(audit["blob"].read_bytes())
        victim = replica
    else:
        victim = target if object_name == "pack" else audit["freeze"]
    if object_name == "missing-cas":
        victim.unlink()
        event = "verify"
    else:
        victim.write_bytes(b"!" + victim.read_bytes()[1:])
        event = "copy"
    with pytest.raises((lr.RegisterError, FileNotFoundError), match="sha256|No such file"):
        _copy(audit, event=event)
    rows = _copy_rows(audit, event)
    assert len(rows) == {"pack": 0, "cas": 1, "freeze": 2, "missing-cas": 1}[object_name]
    assert lc.counting_locations(AUDIT_ID, lr.read_register(audit["repo"])) == set()


@pytest.fixture
def restic_options(tmp_path: Path) -> dict:
    password = tmp_path / "restic-password.txt"
    password.write_text("fixture-only-restic-password\n")
    base = lc._restic_base("restic", password)
    lc._restic(base, "init", "--repository-version", "2")
    return {"restic_repo": "restic", "restic_password_file": password,
            "forget_policy": "--keep-last 1 --keep-tag minsky-lifecycle"}


def test_t8_real_restic_three_rows_and_verify_from_copy(sealed_audit: dict, restic_options: dict) -> None:
    audit = sealed_audit
    _production_pack(audit)
    _copy(audit)
    _copy(audit, copy="B", **restic_options)
    rows = _copy_rows(audit)[-3:]
    assert [lc.parse_copy_note(row)["kind"] for row in rows] == ["pack", "cas-replica", "freeze"]
    assert [row["state_after"] for row in rows] == ["replicated(1)", "replicated(1)", "replicated(2)"]
    snapshots = {lc.parse_copy_note(row)["snapshot"] for row in rows}
    assert len(snapshots) == 1 and None not in snapshots
    audit["blob"].unlink()
    audit["boot_freeze"].unlink()
    (audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst").unlink()
    _copy(audit, copy="B", event="verify", **restic_options)
    assert len(_copy_rows(audit, "verify")) == 3
    assert all(row["state_after"] == "replicated(2)" for row in _copy_rows(audit, "verify"))


def test_t8_restic_substituted_snapshot_object_rehashed(sealed_audit: dict, restic_options: dict) -> None:
    audit = sealed_audit
    _production_pack(audit)
    _copy(audit, copy="B", **restic_options)
    base = lc._restic_base(restic_options["restic_repo"], restic_options["restic_password_file"])
    audit["blob"].chmod(0o600)
    audit["blob"].write_bytes(b"!" + audit["blob"].read_bytes()[1:])
    objects = [str(audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst"), str(audit["blob"]), str(audit["boot_freeze"])]
    output = lc._restic(base, "backup", "--tag", "minsky-lifecycle", "--tag", "audit:" + AUDIT_ID, "--json", "--", *objects)
    snapshot = next(json.loads(line)["snapshot_id"] for line in output.splitlines()
                    if json.loads(line).get("message_type") == "summary")
    previous = _copy_rows(audit)[-3]
    _append(audit, "copy", previous["state_after"], copy_location=previous["copy_location"],
            pack_sha256=previous["pack_sha256"], note=previous["note"].rsplit("snapshot:", 1)[0] + "snapshot:" + snapshot)
    with pytest.raises(lr.RegisterError, match="restic dump.*sha256"):
        _copy(audit, copy="B", event="verify", **restic_options)
    assert len(_copy_rows(audit, "verify")) == 1


@pytest.mark.parametrize("policy", ["--keep-last 1", "--keep-tag minsky-lifecycle --prune",
                                   "--keep-tag minsky-lifecycle --repo other", "--keep-tag minsky-lifecycle,other"])
def test_t8_restic_unsafe_forget_policy_refused(sealed_audit: dict, tmp_path: Path, policy: str) -> None:
    _production_pack(sealed_audit)
    password = tmp_path / "password.txt"
    password.write_text("fixture-only\n")
    with pytest.raises(lr.RegisterError, match="forget-policy"):
        _copy(sealed_audit, copy="B", restic_repo="unused-restic",
              restic_password_file=password, forget_policy=policy)
    assert not _copy_rows(sealed_audit)


def test_t8_size_only_mutant_killed(sealed_audit: dict, tmp_path: Path) -> None:
    audit = sealed_audit
    target, _, _ = _production_pack(audit)
    target.write_bytes(b"!" + target.read_bytes()[1:])
    with pytest.raises(lr.RegisterError, match="sha256"):
        lp.checked_file(target, lr.read_register(audit["repo"]).rows[-1]["pack_sha256"], target.stat().st_size)
    module = _mutant(tmp_path, "size_only", "or _hash_file(path) != sha", "or False")
    with pytest.raises(AssertionError):
        try:
            module.checked_file(target, lr.read_register(audit["repo"]).rows[-1]["pack_sha256"], target.stat().st_size)
        except lr.RegisterError:
            pass
        else:
            raise AssertionError("size-only accepted substituted bytes")


def test_t8_skip_cas_replica_row_mutant_killed(sealed_audit: dict, tmp_path: Path) -> None:
    _production_pack(sealed_audit)
    module = _module_mutant(tmp_path, "skip_cas_row",
                     '            record("cas-replica", f"cas_replica:{len(refs)}/{len(refs)}; cas_refs_sha256:{refs_sha}")',
                     '            pass', "lifecycle_copies.py")
    module.copy_operation(sealed_audit["repo"], audit_id=AUDIT_ID, copy="A", actor=ACTOR,
                          event="copy", copy_a_root=sealed_audit["copy_a"])
    with pytest.raises(AssertionError):
        assert [lc.parse_copy_note(row)["kind"] for row in _copy_rows(sealed_audit)] == ["pack", "cas-replica", "freeze"]


def _flags(audit: dict, finished="2026-08-01T00:00:00Z") -> list[str]:
    from test_hardening import load_script
    cli = load_script("w6_flags", "minsky-lifecycle.py")
    return cli.closeout_flags(AUDIT_ID, finished, lr.read_register(audit["repo"]), date(2026, 10, 2))


def test_t22_complete_copies_clear_deadlines(sealed_audit: dict, restic_options: dict) -> None:
    audit = sealed_audit
    _append(audit, "ruling", "sealed", **{"class": "methodology-bearing"})
    # The method class adds the hash-bound fixture bytes to its CAS before pack.
    for row in lp.cas_rows(audit["rows"], "methodology-bearing"):
        blob = ls.store.cas_path(audit["store"], row["sha256"])
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes((audit["dir"] / row["path"]).read_bytes())
            blob.chmod(0o444)
    assert _flags(audit) == ["overdue-first-copy", "overdue-second-copy"]
    lp.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], copy_a_root=audit["copy_a"], actor=ACTOR)
    _copy(audit)
    assert _flags(audit) == ["overdue-second-copy"]
    _copy(audit, copy="B", **restic_options)
    assert _flags(audit) == []


def test_t22_incomplete_and_older_pack_copies_do_not_count(sealed_audit: dict) -> None:
    audit = sealed_audit
    _production_pack(audit)
    audit["freeze"].write_bytes(b"!" + audit["freeze"].read_bytes()[1:])
    with pytest.raises(lr.RegisterError, match="sha256"):
        _copy(audit)
    assert len(_copy_rows(audit)) == 2 and _flags(audit) == ["overdue-first-copy"]
    shutil.copyfile(audit["boot_freeze"], audit["freeze"])
    _copy(audit)
    assert _flags(audit) == []
    pack_row = next(row for row in reversed(lr.read_register(audit["repo"]).rows) if row["event"] == "pack")
    _append(audit, "pack", "packed", pack_sha256="f" * 64, note=pack_row["note"], **{"class": "code-only"})
    assert _flags(audit) == ["overdue-first-copy"]


@pytest.mark.parametrize("note", ["unparseable", "copy-row:pack; freeze:" + "a" * 64 + "; verified_utc:2026-10-01T00:00:00Z",
                                 "copy-row:pack; pack:" + "a" * 64 + "; verified_utc:2026-13-01T00:00:00Z"])
def test_t22_malformed_copy_notes_refused(sealed_audit: dict, note: str) -> None:
    _production_pack(sealed_audit)
    _append(sealed_audit, "copy", "packed", note=note, copy_location="A:" + str(sealed_audit["copy_a"]))
    with pytest.raises(lr.RegisterError, match="copy"):
        _flags(sealed_audit)


def test_t22_flag_on_seal_only_mutant_killed(sealed_audit: dict, tmp_path: Path) -> None:
    assert _flags(sealed_audit) == ["overdue-first-copy"]
    module = _module_mutant(tmp_path, "flag_on_seal_only", "copies = counting_locations(audit_id, register)",
                     "copies = counting_locations(audit_id, register) or {'sealed'}", "minsky-lifecycle.py")
    with pytest.raises(AssertionError):
        assert module.closeout_flags(AUDIT_ID, "2026-08-01T00:00:00Z", lr.read_register(sealed_audit["repo"]),
                                     date(2026, 10, 2)) == ["overdue-first-copy"]


def test_t22_census_counts_complete_copy(sealed_audit: dict, tmp_path: Path) -> None:
    from test_hardening import load_script
    audit = sealed_audit
    _production_pack(audit)
    cli = load_script("w6_census", "minsky-lifecycle.py")
    db = tmp_path / "census.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE audits(audit_id TEXT, finished_at TEXT)")
        connection.execute("INSERT INTO audits VALUES (?, ?)", (AUDIT_ID, "2026-08-01T00:00:00Z"))
    rd = tmp_path / "rd.tsv"
    rd.write_text(cli.RD_HEADER + "\n")
    report = tmp_path / "census.json"
    assert cli.census(audit["repo"], audits_db=db, rd_register_view=rd, report_out=report,
                      today=date(2026, 10, 2)) == 0
    assert json.loads(report.read_text())["entries"][0]["flags"] == ["overdue-first-copy"]
    _copy(audit)
    assert cli.census(audit["repo"], audits_db=db, rd_register_view=rd, report_out=report,
                      today=date(2026, 10, 2)) == 0
    assert json.loads(report.read_text())["entries"][0]["flags"] == []


@pytest.mark.parametrize("defect", ["zero", "unequal", "freeze", "pack", "refs"])
def test_t22_nonmatching_complete_rows_do_not_count(sealed_audit: dict, defect: str) -> None:
    audit = sealed_audit
    _production_pack(audit)
    packed = lr.read_register(audit["repo"]).rows[-1]
    refs = hashlib.sha256(lp.cas_refs_bytes(lp.cas_rows(audit["rows"], "code-only"))).hexdigest()
    pack_sha = "e" * 64 if defect == "pack" else packed["pack_sha256"]
    freeze_sha = "e" * 64 if defect == "freeze" else audit["freeze_sha"]
    note_refs = "e" * 64 if defect == "refs" else refs
    numbers = "0/0" if defect == "zero" else "1/2" if defect == "unequal" else "1/1"
    for kind, value in (("pack", "pack:" + pack_sha),
                        ("cas-replica", f"cas_replica:{numbers}; cas_refs_sha256:{note_refs}"),
                        ("freeze", "freeze:" + freeze_sha)):
        _append(audit, "copy", "packed", pack_sha256=packed["pack_sha256"],
                copy_location="A:" + str(audit["copy_a"]),
                note=f"copy-row:{kind}; {value}; verified_utc:2026-10-01T00:00:00Z")
    assert _flags(audit) == ["overdue-first-copy"]


@pytest.mark.parametrize("defect", ["missing-keep", "bad-keep-json", "check-failure"])
def test_t8_restic_keep_list_and_check_fail_closed(tmp_path: Path, monkeypatch, defect: str) -> None:
    password = tmp_path / "password.txt"
    password.write_text("fixture-only\n")
    base = lc._restic_base("repo", password)
    snapshot = {"id": "a" * 64, "tags": ["minsky-lifecycle"]}

    def restic(command, *args):
        if args[0] == "snapshots":
            return json.dumps([snapshot])
        if args[0] == "forget":
            return "" if defect == "bad-keep-json" else json.dumps([
                {"keep": [] if defect == "missing-keep" else [snapshot]}])
        raise lr.RegisterError("refused restic check: injected failure")

    monkeypatch.setattr(lc, "_restic", restic)
    with pytest.raises(lr.RegisterError, match="keep-list|restic check"):
        lc._retention(base, lc._policy("--keep-tag minsky-lifecycle"))


def test_t7_cas_and_free_space_fail_closed(sealed_audit: dict, monkeypatch) -> None:
    from free_space import FreeSpaceError
    audit = sealed_audit
    audit["blob"].chmod(0o600)
    raw = audit["blob"].read_bytes()
    audit["blob"].write_bytes(b"!" + raw[1:])
    with pytest.raises(lr.RegisterError, match="sha256"):
        _production_pack(audit)
    audit["blob"].write_bytes(raw)
    monkeypatch.setattr(lp, "check_free_space", lambda *args: (_ for _ in ()).throw(
        FreeSpaceError("refused free-space check: injected floor refusal")))
    with pytest.raises(FreeSpaceError, match="free-space"):
        _production_pack(audit)
    assert not list(audit["copy_a"].glob("packs/*.partial"))
    assert lr.read_register(audit["repo"]).rows[-1]["event"] == "seal"


@pytest.mark.parametrize("target", ["packs", "cas"])
def test_t8_copy_destination_parent_escape_refused(sealed_audit: dict, tmp_path: Path, target: str) -> None:
    audit = sealed_audit
    outside = tmp_path / "outside"
    outside.mkdir()
    if target == "packs":
        (audit["copy_a"] / "packs").symlink_to(outside)
        with pytest.raises(lr.RegisterError, match="outside declared root"):
            _production_pack(audit)
    else:
        _production_pack(audit)
        (audit["copy_a"] / "cas").symlink_to(outside)
        with pytest.raises(lr.RegisterError, match="outside declared root"):
            _copy(audit)
    assert not list(outside.iterdir())


def test_w6c_volume_copy_requires_window_and_accepts_confirmation(tmp_path: Path, monkeypatch) -> None:
    real_resolve = Path.resolve
    marker = tmp_path / "simulated-volume"
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **kw: Path("/Volumes/W6b-never-accessed")
                        if path == marker else real_resolve(path, *a, **kw))
    with pytest.raises(lr.RegisterError, match="window-confirmed required"):
        lc._copy_window(marker, None)
    lc._copy_window(marker, "fixture-window-reference")


def test_w6b_cli_dispatch_usage_and_refusal(sealed_audit: dict, capsys) -> None:
    from test_hardening import load_script
    audit = sealed_audit
    cli = load_script("w6_cli", "minsky-lifecycle.py")
    shared = ["--repo", str(audit["repo"]), "--audit", AUDIT_ID, "--actor", ACTOR]
    with pytest.raises(SystemExit) as error:
        cli.main(["pack", *shared])
    assert error.value.code == 2
    assert cli.main(["pack", *shared, "--audit-dir", str(audit["dir"]), "--copy-a-root", str(audit["copy_a"])]) == 0
    assert cli.main(["replicate", *shared, "--copy", "A", "--copy-a-root", str(audit["copy_a"])]) == 0
    assert cli.main(["verify", *shared, "--copy", "A", "--copy-a-root", str(audit["copy_a"])]) == 0
    assert cli.main(["pack", *shared, "--audit-dir", str(audit["dir"]), "--copy-a-root", str(audit["copy_a"])]) == 1
    assert "refused pack test-audit: latest state sealed required" in capsys.readouterr().err


def test_t8_streamed_dump_drains_large_stderr(tmp_path: Path) -> None:
    fake = tmp_path / "synthetic-dump.py"
    fake.write_text("import sys\nsys.stderr.buffer.write(b'e' * 262144)\n"
                    "sys.stdout.buffer.write(b'fixture object')\n")
    code = ("import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "from lifecycle_copies import _dump_checked\n"
            f"_dump_checked([sys.executable, {str(fake)!r}], 'snapshot', '/object', "
            f"{hashlib.sha256(b'fixture object').hexdigest()!r}, {len(b'fixture object')})\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_t22_unfinished_audit_still_refuses_malformed_copy_note(sealed_audit: dict) -> None:
    from test_hardening import load_script
    _append(sealed_audit, "copy", "sealed", note="unparseable", copy_location="A:" + str(sealed_audit["copy_a"]))
    cli = load_script("w6_unfinished", "minsky-lifecycle.py")
    with pytest.raises(lr.RegisterError, match="copy note format"):
        cli.closeout_flags(AUDIT_ID, None, lr.read_register(sealed_audit["repo"]), date(2026, 10, 2))


def _external_cas_audit(audit: dict, audit_class: str) -> list[dict]:
    """Use real schema-1.1 prepare and seal ingest for dependencies outside the audit tree."""
    audit["runtime"].unlink()
    round_dir = audit["dir"] / "round-2"
    runtime = audit["repo"].parent / "external-runtime"
    runtime.write_bytes(b"schema-1.1 external runtime\n")
    context = audit["repo"].parent / "external-context.txt"
    context.write_bytes(b"external methodology context\n")
    prompt = round_dir / "external-prompt.txt"
    prompt.write_text("external CAS fixture\n")
    assert provenance.main([
        "prepare", "--call-uid", "externalcas", "--audit-id", AUDIT_ID, "--round", "2",
        "--round-dir", str(round_dir), "--step", "codex", "--persona", PERSONA,
        "--origin", "wrapper", "--provider", "openai", "--model", "gpt-5.6-sol",
        "--effort", "medium", "--client-name", "fixture", "--client-version", "1",
        "--prompt-path", str(prompt), "--runtime-json", "{}", "--runtime-file", str(runtime),
        "--context-file", str(context), "--expected-output-path", str(round_dir / "result.json"),
    ]) == 0
    receipt = next((round_dir / "provenance").glob("*.externalcas.preflight.json"))
    value = json.loads(receipt.read_text())
    assert value["schema_version"] == "1.1"
    assert not Path(value["runtime_snapshots"][0]["path"]).is_relative_to(audit["dir"])
    rows, _ = ls.raw_manifest(audit["repo"], audit["dir"], audit_id=AUDIT_ID)
    used, drifted = ls._ingest_plan(audit["dir"], rows, ls.round_inputs(audit["dir"], AUDIT_ID),
                                  audit_class, lp.load_cas(), None)
    assert not drifted
    with lr.AuditLock.acquire(audit["repo"], AUDIT_ID, verb="seal", session=ACTOR) as lock:
        ingested = ls._ingest(audit["repo"], audit["dir"], AUDIT_ID, used, lp.load_cas(),
                              "2026-10-01T00:00:00Z", lock)
    audit["rows"] = rows
    audit["manifest"].write_bytes(ls._tsv(ls.RAW_MANIFEST_HEADER, rows))
    _append(audit, "ruling", "sealed", **{"class": audit_class})
    _append(audit, "seal", "sealed", citation_resolvability="pass",
            manifest_sha256=lp._hash_file(audit["manifest"]), **{"class": audit_class},
            note=f"freeze:{audit['freeze_sha']}; ingested:{ingested}")   # as a real seal records it
    return ls._read_table(audit["repo"], ls.STORE_INDEX, ls.STORE_HEADER)


@pytest.mark.parametrize("audit_class", ["code-only", "methodology-bearing"])
def test_w6c_cas_refs_include_external_dependencies_in_both_copies(sealed_audit: dict,
                                                                restic_options: dict,
                                                                audit_class: str) -> None:
    audit = sealed_audit
    indexed = _external_cas_audit(audit, audit_class)
    runtime = next(row for row in indexed if row["class"] == "runtime-blob")
    # Repeated uses of one sha and another audit's rows must not inflate this audit's coverage.
    index_path = audit["repo"] / ls.STORE_INDEX
    index_path.write_bytes(ls._tsv(ls.STORE_HEADER, [*indexed,
        {**runtime, "use_path": "zz/repeated-runtime"},
        {**runtime, "audit_id": "unrelated-audit", "sha256": "f" * 64}]))
    lp.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"],
            copy_a_root=audit["copy_a"], actor=ACTOR)
    target = audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst"
    with target.open("rb") as source, zstandard.ZstdDecompressor().stream_reader(source) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            raw = next(archive.extractfile(info).read() for info in archive if info.name == "CAS_REFS.tsv")
    refs = [dict(zip(lp.CAS_HEADER, line.split("\t"))) for line in raw.decode().splitlines()[1:]]
    wanted = {row["sha256"] for row in indexed} | {
        row["sha256"] for row in lp.cas_rows(audit["rows"], audit_class)}
    assert {row["sha256"] for row in refs} == wanted
    assert len(refs) == len(wanted) and len(refs) > 0
    assert [row["path"] for row in refs] == sorted(row["path"] for row in refs)
    assert any(row["path"] == runtime["use_path"] for row in refs)
    if audit_class == "methodology-bearing":
        context = str(audit["repo"].parent / "external-context.txt")
        assert any(row["path"] == context for row in refs)
    _copy(audit)
    for row in refs:
        lp.checked_file(ls.store.cas_path(audit["copy_a"] / "cas", row["sha256"]),
                        row["sha256"], int(row["bytes"]))
    _copy(audit, copy="B", **restic_options)
    coverage = [lc.parse_copy_note(row) for row in _copy_rows(audit)
                if lc.parse_copy_note(row)["kind"] == "cas-replica"]
    assert [(row["present"], row["total"]) for row in coverage] == [(len(refs), len(refs))] * 2
    audit["blob"].unlink()
    for row in indexed:
        ls.store.cas_path(audit["store"], row["sha256"]).unlink(missing_ok=True)
    _copy(audit, event="verify")
    _copy(audit, copy="B", event="verify", **restic_options)


@pytest.mark.parametrize("defect", ["missing", "corrupt", "conflicting-bytes"])
def test_w6c_indexed_dependency_fails_closed(sealed_audit: dict, defect: str) -> None:
    audit = sealed_audit
    indexed = _external_cas_audit(audit, "code-only")
    runtime = next(row for row in indexed if row["class"] == "runtime-blob")
    blob = ls.store.cas_path(audit["store"], runtime["sha256"])
    if defect == "missing":
        blob.unlink()
    elif defect == "corrupt":
        blob.chmod(0o600)
        blob.write_bytes(b"!" + blob.read_bytes()[1:])
    else:
        (audit["repo"] / ls.STORE_INDEX).write_bytes(ls._tsv(ls.STORE_HEADER, [
            *indexed, {**runtime, "bytes": str(int(runtime["bytes"]) + 1)}]))
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    with pytest.raises(lr.RegisterError, match="sha256|bytes"):
        lp.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"],
                copy_a_root=audit["copy_a"], actor=ACTOR)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert not list(audit["copy_a"].glob("packs/*"))


@pytest.mark.parametrize("copy", ["A", "B"])
def test_w6c_volume_copy_and_verify_notes_record_window(sealed_audit: dict, restic_options: dict,
                                                     monkeypatch, copy: str) -> None:
    audit = sealed_audit
    _production_pack(audit)
    real_relative = Path.is_relative_to
    targets = {audit["copy_a"], Path("restic"), audit["repo"].parent / "restic"}

    def simulated_volume(path, *other):
        return True if path in targets and other == ("/Volumes",) else real_relative(path, *other)

    monkeypatch.setattr(Path, "is_relative_to", simulated_volume)
    options = restic_options if copy == "B" else {}
    with pytest.raises(lr.RegisterError, match="window-confirmed required"):
        _copy(audit, copy=copy, **options)
    assert not _copy_rows(audit)
    _copy(audit, copy=copy, window_confirmed="production-window-W6c", **options)
    with pytest.raises(lr.RegisterError, match="window-confirmed required"):
        _copy(audit, copy=copy, event="verify", **options)
    _copy(audit, copy=copy, event="verify", window_confirmed="production-window-W6c", **options)
    for row in [*_copy_rows(audit), *_copy_rows(audit, "verify")]:
        assert row["note"].split("; free_space_sha256:")[0].endswith("; window-confirmed:production-window-W6c")
        if copy == "B":
            assert "; snapshot:" in row["note"]
        lc.parse_copy_note(row)
    assert _flags(audit) == []


def test_w6c_copy_b_identity_normalises_aliases(sealed_audit: dict, restic_options: dict) -> None:
    audit = sealed_audit
    _production_pack(audit)
    _copy(audit, copy="B", **restic_options)
    _copy(audit, copy="B", **{**restic_options, "restic_repo": "./restic/."})
    wanted = "B:" + str((audit["repo"].parent / "restic").resolve())
    assert {row["copy_location"] for row in _copy_rows(audit)} == {wanted}
    assert lr.read_register(audit["repo"]).rows[-1]["state_after"] == "replicated(1)"
    pack_notes = [row["note"] for row in _copy_rows(audit) if lc.parse_copy_note(row)["kind"] == "pack"]
    assert all(f"; device:{os.stat('restic').st_dev};" in note for note in pack_notes)


def test_w6c_same_device_copy_b_refused(sealed_audit: dict, restic_options: dict,
                                     monkeypatch, copy_devices) -> None:
    audit = sealed_audit
    _production_pack(audit)
    monkeypatch.setattr(os, "stat", copy_devices)
    with pytest.raises(lr.RegisterError, match="copy B.*same device"):
        _copy(audit, copy="B", **restic_options)
    assert not _copy_rows(audit)
    assert lc._restic(lc._restic_base("restic", restic_options["restic_password_file"]),
                       "snapshots", "--json").strip() == "[]"


@pytest.mark.parametrize("repository", ["local repo", "sftp:server:/repo path", "s3:bucket/repo\t"])
def test_w6c_whitespace_repository_refused(sealed_audit: dict, restic_options: dict,
                                         monkeypatch, repository: str) -> None:
    _production_pack(sealed_audit)
    def no_restic(*args):
        raise AssertionError("whitespace repository reached restic")
    monkeypatch.setattr(lc, "_restic", no_restic)
    with pytest.raises(lr.RegisterError, match="restic-repo"):
        _copy(sealed_audit, copy="B", **{**restic_options, "restic_repo": repository})
    assert not _copy_rows(sealed_audit)


@pytest.mark.parametrize("defect", ["alias", "same-device", "different-device", "legacy-no-device"])
def test_w6c_closeout_counts_normalised_locations_and_devices(sealed_audit: dict, defect: str) -> None:
    audit = sealed_audit
    _production_pack(audit)
    _copy(audit)
    originals = _copy_rows(audit)
    _append(audit, "ruling", "replicated(1)", **{"class": "methodology-bearing"})
    for original in originals:
        if defect == "alias":
            location = "A:" + str(audit["copy_a"]) + "/."
            note = original["note"]
        else:
            location = "B:" + str(audit["repo"].parent / "restic")
            prefix, sha = original["note"].split("; free_space_sha256:")
            note = prefix + "; snapshot:" + "a" * 64 + "; free_space_sha256:" + sha
        if lc.parse_copy_note(original)["kind"] == "pack":
            device = os.stat(audit["copy_a"]).st_dev
            if defect == "different-device":
                note = note.replace(f"; device:{device}", f"; device:{device + 1}")
            elif defect == "legacy-no-device":
                note = note.replace(f"; device:{device}", "")
        _append(audit, "copy", "replicated(1)", copy_location=location,
                pack_sha256=original["pack_sha256"], note=note)
    assert _flags(audit) == ([] if defect in ("different-device", "legacy-no-device")
                             else ["overdue-second-copy"])


@pytest.mark.parametrize("state", ["offloaded", "rehydrated"])
@pytest.mark.parametrize("copy", ["A", "B"])
def test_w6c_verify_after_offload_preserves_state(sealed_audit: dict, restic_options: dict,
                                               state: str, copy: str) -> None:
    audit = sealed_audit
    _production_pack(audit)
    options = restic_options if copy == "B" else {}
    _copy(audit, copy=copy, **options)
    _append(audit, "offload" if state == "offloaded" else "rehydrate", state)
    if copy == "B":
        audit["blob"].unlink()
        audit["boot_freeze"].unlink()
        (audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst").unlink()
    with pytest.raises(lr.RegisterError, match="latest state packed or replicated"):
        _copy(audit, copy=copy, **options)
    _copy(audit, copy=copy, event="verify", **options)
    assert len(_copy_rows(audit, "verify")) == 3
    assert all(row["state_after"] == state for row in _copy_rows(audit, "verify"))


def test_host_restic_local_backend_is_a_local_path(tmp_path: Path) -> None:
    """Host fix (Astra W6c): `local:/path` is restic's local backend and must normalise like the bare path."""
    repo = tmp_path / "restic-repo"
    repo.mkdir()
    assert lc.local_repository(f"local:{repo}") == repo.resolve() == lc.local_repository(str(repo))
    assert lc.normalised_location(f"B:local:{repo}") == lc.normalised_location(f"B:{repo}/.")
    assert lc.local_repository("sftp:host:/srv/restic") is None
    with pytest.raises(lr.RegisterError):
        lc.local_repository("local:")


def test_host_missing_or_short_store_index_refuses_pack(sealed_audit: dict) -> None:
    """Host fix (Astra W6c): a seal that recorded ingested:<n> > 0 needs n indexed objects; a missing index never shrinks deps."""
    audit = sealed_audit
    index = audit["repo"] / ls.STORE_INDEX
    assert not index.exists()
    _append(audit, "seal", "sealed", citation_resolvability="pass", manifest_sha256=lp._hash_file(audit["manifest"]),
            **{"class": "code-only"}, note="freeze:" + audit["freeze_sha"] + "; ingested:1")
    with pytest.raises(lr.RegisterError, match="store index .* missing"):
        lp.audit_cas_rows(audit["repo"], AUDIT_ID, audit["rows"], "code-only")


def _fixed_free_space(target: Path, incoming_bytes: int) -> dict:
    from free_space import check_free_space
    return check_free_space(target, incoming_bytes, {"floor_bytes": 100, "margin_bytes": 20},
                            disk_usage=lambda path: SimpleNamespace(free=10**9))


def _assert_free_space(audit: dict, row: dict, results: list[dict]) -> list[Path]:
    match = re.search(r"; free_space_sha256:([0-9a-f]{64}(?:,[0-9a-f]{64})*)$", row["note"])
    assert match is not None, row["note"]
    shas = match[1].split(",")
    assert len(shas) == len(results)
    paths = []
    for sha, result in zip(shas, results):
        path = audit["dir"] / "lifecycle/free_space_checks" / (sha + ".json")
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == sha
        assert raw == json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        value = json.loads(raw)
        assert value["required_bytes"] == value["floor_bytes"] + value["margin_bytes"]
        paths.append(path)
    return paths


def _capture_free_space(monkeypatch, module) -> list[dict]:
    results = []
    def check(target, incoming):
        result = _fixed_free_space(target, incoming)
        results.append(result)
        return result
    monkeypatch.setattr(module, "check_free_space", check)
    return results


def test_w12_t7_pack_check_persisted_and_repeat_preserves_sidecar(sealed_audit: dict, monkeypatch) -> None:
    audit = sealed_audit
    results = _capture_free_space(monkeypatch, lp)
    _production_pack(audit)
    row = lr.read_register(audit["repo"]).rows[-1]
    assert row["note"].startswith("pack_verified:1; members:")
    paths = _assert_free_space(audit, row, results)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in paths}
    with pytest.raises(lr.RegisterError, match="latest state sealed required"):
        _production_pack(audit)
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in paths} == before
    assert set(paths[0].parent.iterdir()) == set(paths)


def test_w12_t7_replicate_every_row_records_check_and_reuses_sidecar(sealed_audit: dict, monkeypatch) -> None:
    audit = sealed_audit
    _production_pack(audit)
    _copy(audit)
    results = _capture_free_space(monkeypatch, lc)
    _copy(audit)
    rows = _copy_rows(audit)[-3:]
    paths = _assert_free_space(audit, rows[0], results)
    for row in rows[1:]:
        assert _assert_free_space(audit, row, results) == paths
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in paths}
    _copy(audit)
    for row in _copy_rows(audit)[-3:]:
        assert _assert_free_space(audit, row, results[-1:]) == paths
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in paths} == before


@pytest.mark.parametrize("operation", ["pack", "replicate"])
def test_w12_t7_conflicting_sidecar_refused_before_operation(sealed_audit: dict, monkeypatch, operation: str) -> None:
    audit = sealed_audit
    if operation == "replicate":
        _production_pack(audit)
    module = lp if operation == "pack" else lc
    def check(target, incoming):
        result = _fixed_free_space(target, incoming)
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        path = audit["dir"] / "lifecycle/free_space_checks" / (hashlib.sha256(raw).hexdigest() + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"different bytes\n")
        audit["conflict"] = path
        return result
    monkeypatch.setattr(module, "check_free_space", check)
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    with pytest.raises(lr.RegisterError, match="free_space_checks/.*existing bytes differ"):
        (_production_pack if operation == "pack" else _copy)(audit)
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert audit["conflict"].read_bytes() == b"different bytes\n"
    if operation == "pack":
        assert not list(audit["copy_a"].glob("packs/*.tar.zst"))
    else:
        assert not (audit["copy_a"] / "cas").exists()


@pytest.mark.parametrize("operation", ["pack", "replicate"])
def test_w12_t7_refused_check_writes_no_sidecar_or_row(sealed_audit: dict, monkeypatch, operation: str) -> None:
    from free_space import FreeSpaceError
    audit = sealed_audit
    if operation == "replicate":
        _production_pack(audit)
    sidecars = audit["dir"] / "lifecycle/free_space_checks"
    before_files = {path: path.read_bytes() for path in sidecars.glob("*.json")}
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    def refuse(target, incoming):
        raise FreeSpaceError(f"refused free-space check target={target}: fixture floor refusal")
    monkeypatch.setattr(lp if operation == "pack" else lc, "check_free_space", refuse)
    with pytest.raises(FreeSpaceError, match="fixture floor refusal"):
        (_production_pack if operation == "pack" else _copy)(audit)
    assert {path: path.read_bytes() for path in sidecars.glob("*.json")} == before_files
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before


def test_w12_t7_cli_emits_free_space_info_once(sealed_audit: dict, monkeypatch, capsys) -> None:
    import logging
    from test_hardening import load_script
    audit = sealed_audit
    logger = logging.getLogger("free_space")
    root = logging.getLogger()
    root_handlers, root_level = root.handlers[:], root.level
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.WARNING)
    monkeypatch.setattr(logger, "propagate", True)
    _capture_free_space(monkeypatch, lp)
    _capture_free_space(monkeypatch, lc)
    cli = load_script("w12_cli", "minsky-lifecycle.py")
    assert cli.main(["pack", "--repo", str(audit["repo"]), "--audit", AUDIT_ID, "--actor", ACTOR,
                     "--audit-dir", str(audit["dir"]), "--copy-a-root", str(audit["copy_a"])]) == 0
    assert cli.main(["replicate", "--repo", str(audit["repo"]), "--audit", AUDIT_ID, "--actor", ACTOR,
                     "--copy", "A", "--copy-a-root", str(audit["copy_a"])]) == 0
    error = capsys.readouterr().err
    assert error.count("INFO free_space: free-space check target=") == 2
    assert logger.handlers == [] and logger.level == logging.WARNING and logger.propagate
    assert root.handlers == root_handlers and root.level == root_level


def test_w12c_t7_complete_pack_operations_are_deterministic(sealed_audit: dict, tmp_path: Path,
                                                         monkeypatch) -> None:
    from free_space import check_free_space
    first = sealed_audit
    repo = tmp_path / "identical-repo"
    shutil.copytree(first["repo"], repo, symlinks=True)
    second = {**first, "repo": repo, "dir": repo / "audits" / AUDIT_ID,
              "copy_a": tmp_path / "second-copy-a"}
    for source in sorted([first["dir"], *first["dir"].rglob("*")],
                         key=lambda path: len(path.parts), reverse=True):
        target = second["dir"] / source.relative_to(first["dir"])
        info = source.lstat()
        ns = info.st_mtime_ns if stat.S_ISREG(info.st_mode) else NANOSECONDS
        for path in (source, target):
            os.utime(path, ns=(ns, ns), follow_symlinks=False)
            assert path.lstat().st_mtime_ns == ns
        assert source.lstat().st_mode == target.lstat().st_mode
        if stat.S_ISREG(info.st_mode):
            assert source.read_bytes() == target.read_bytes()
        elif stat.S_ISLNK(info.st_mode):
            assert os.readlink(source) == os.readlink(target)

    results = []
    free_bytes = iter((10**9, 2 * 10**9))
    def check(target, incoming):
        result = check_free_space(target, incoming, {"floor_bytes": 100, "margin_bytes": 20},
                                  disk_usage=lambda path: SimpleNamespace(free=next(free_bytes)))
        results.append(result)
        return result
    monkeypatch.setattr(lp, "check_free_space", check)
    targets, sidecars = [], []
    for audit in (first, second):
        lp.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"],
                copy_a_root=audit["copy_a"], actor=ACTOR)
        target = audit["copy_a"] / "packs" / f"{AUDIT_ID}.tar.zst"
        targets.append(target)
        row = lr.read_register(audit["repo"]).rows[-1]
        assert row["event"] == "pack" and row["state_after"] == "packed"
        assert row["pack_sha256"] == lp._hash_file(target)
        paths = _assert_free_space(audit, row, results[-1:])
        assert set(paths[0].parent.iterdir()) == set(paths)
        sidecars.append(paths[0])
    assert [result["free_bytes"] for result in results] == [10**9, 2 * 10**9]
    assert sidecars[0].name != sidecars[1].name
    assert targets[0].read_bytes() == targets[1].read_bytes()
    for target in targets:
        lifecycle = next(item for item in iter_pack_members(target) if item.info.name == "lifecycle")
        assert lifecycle.info.pax_headers["MINSKY.mtime_ns"] == str(NANOSECONDS)


def test_w12c_t7_failed_pack_write_leaves_no_sidecar_or_row(sealed_audit: dict, monkeypatch) -> None:
    audit = sealed_audit
    results = _capture_free_space(monkeypatch, lp)
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    def fail(destination, expected, sources, refs):
        assert len(results) == 1
        raise PackError(f"refused pack write {destination}: fixture write failure")
    monkeypatch.setattr(lp, "write_pack", fail)
    with pytest.raises(PackError, match="fixture write failure"):
        lp.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"],
                copy_a_root=audit["copy_a"], actor=ACTOR)
    assert not (audit["dir"] / "lifecycle/free_space_checks").exists()
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
    assert not list(audit["copy_a"].glob("packs/*"))


@pytest.mark.parametrize("existing", ["absent-parents", "absent-sidecar", "identical"])
def test_w12d_free_space_preflight_creates_nothing(tmp_path: Path, existing: str) -> None:
    directory = tmp_path / "audit"
    directory.mkdir()
    result = {"target": "synthetic-check", "note": "caf\u00e9", "free_bytes": 1234}
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    sha = hashlib.sha256(raw).hexdigest()
    path = directory / "lifecycle/free_space_checks" / (sha + ".json")
    if existing != "absent-parents":
        path.parent.mkdir(parents=True)
    if existing == "identical":
        assert lp.record_free_space(directory, result) == sha
    before = {entry: entry.lstat() for entry in [directory, *directory.rglob("*")]}
    assert lp.free_space_sidecar_preflight(directory, result) == (sha, raw)
    assert {entry: entry.lstat() for entry in [directory, *directory.rglob("*")]} == before
    if existing == "identical":
        assert path.read_bytes() == raw


@pytest.mark.parametrize("defect", ["lifecycle-link", "checks-link", "lifecycle-file", "checks-file",
                                  "sidecar-link", "dangling-sidecar-link", "sidecar-dir", "sidecar-fifo",
                                  "sidecar-bytes"])
def test_w12d_free_space_preflight_refuses_unsafe_paths(tmp_path: Path, defect: str) -> None:
    directory = tmp_path / "audit"
    directory.mkdir()
    result = {"free_bytes": 1234}
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    path = directory / "lifecycle/free_space_checks" / (hashlib.sha256(raw).hexdigest() + ".json")
    outside = tmp_path / "outside"
    outside.mkdir()
    if defect.startswith("lifecycle") or defect.startswith("checks"):
        refused = path.parent.parent if defect.startswith("lifecycle") else path.parent
        refused.parent.mkdir(exist_ok=True)
        if defect.endswith("link"):
            refused.symlink_to(outside, target_is_directory=True)
        else:
            refused.write_bytes(b"not a directory\n")
    else:
        refused = path
        path.parent.mkdir(parents=True)
        if defect.endswith("link"):
            target = outside / "target"
            if not defect.startswith("dangling"):
                target.write_bytes(raw)
            path.symlink_to(target)
        elif defect.endswith("dir"):
            path.mkdir()
        elif defect.endswith("fifo"):
            os.mkfifo(path)
        else:
            path.write_bytes(b"different bytes\n")
    before = {entry: entry.lstat() for entry in [directory, *directory.rglob("*")]}
    with pytest.raises(lr.RegisterError) as error:
        lp.free_space_sidecar_preflight(directory, result)
    assert str(refused) in str(error.value)
    assert {entry: entry.lstat() for entry in [directory, *directory.rglob("*")]} == before


def test_w12d_t7_pack_plans_once_after_preflight(sealed_audit: dict, monkeypatch) -> None:
    audit = sealed_audit
    events = []
    results = _capture_free_space(monkeypatch, lp)
    preflight, plan, write, verify, record = (lp.free_space_sidecar_preflight, lp._plan, lp.write_pack,
                                            lp.round_trip, lp.record_free_space)
    def observed(name, function):
        def call(*args, **kwargs):
            events.append(name)
            assert len(results) == 1
            assert not (audit["dir"] / "lifecycle/free_space_checks").exists()
            return function(*args, **kwargs)
        return call
    for name, attribute, function in (("preflight", "free_space_sidecar_preflight", preflight),
                                      ("plan", "_plan", plan), ("write", "write_pack", write),
                                      ("verify", "round_trip", verify), ("record", "record_free_space", record)):
        monkeypatch.setattr(lp, attribute, observed(name, function))
    lp.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], copy_a_root=audit["copy_a"], actor=ACTOR)
    assert events == ["preflight", "plan", "write", "verify", "record"]
    assert results[0]["incoming_bytes"] == len(lp.cas_refs_bytes(lp.cas_rows(audit["rows"], "code-only"))) + sum(
        int(row["bytes"]) for row in audit["rows"] if row["sha256"] and row["class"] != "runtime-blob")
    _assert_free_space(audit, lr.read_register(audit["repo"]).rows[-1], results)


def test_w12d_t7_sidecar_write_rechecks_after_preflight(sealed_audit: dict, monkeypatch) -> None:
    audit = sealed_audit
    results = _capture_free_space(monkeypatch, lp)
    before = (audit["repo"] / lr.REGISTER_PATH).read_bytes()
    real_verify = lp.round_trip
    def verify(target, expected):
        real_verify(target, expected)
        assert not (audit["dir"] / "lifecycle/free_space_checks").exists()
        sha, _ = lp.free_space_sidecar_preflight(audit["dir"], results[0])
        path = audit["dir"] / "lifecycle/free_space_checks" / (sha + ".json")
        path.parent.mkdir()
        path.write_bytes(b"raced conflicting bytes\n")
        audit["conflict"] = path
    monkeypatch.setattr(lp, "round_trip", verify)
    with pytest.raises(lr.RegisterError, match="free_space_checks/.*existing bytes differ"):
        lp.pack(audit["repo"], audit_id=AUDIT_ID, audit_dir=audit["dir"], copy_a_root=audit["copy_a"], actor=ACTOR)
    assert audit["conflict"].read_bytes() == b"raced conflicting bytes\n"
    assert (audit["repo"] / lr.REGISTER_PATH).read_bytes() == before
