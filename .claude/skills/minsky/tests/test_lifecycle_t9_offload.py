"""T9 (host-owned): offload is a verified move, never the loss of the last copy (v0.2 §6.6; LC-F19, F20, F21, F22).

Written line by line by the IMPLEMENT host (Claude Opus 5.5, session ebef6afd), 2 Oct 2026. The judgements encoded:
- bytes leave boot only after a restore REHEARSAL from the copy itself has passed, with the boot CAS and the live tree out of the
  picture (a corrupted replica must fail even though boot still holds good bytes);
- the removal set is fixed in a COMMITTED intent before anything is removed; an uncommitted intent, a new raw member or a changed
  member removes nothing;
- a crash after removal is recoverable; git-tracked files are never removed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import lifecycle_offload as lo
import lifecycle_register as lr
from test_hardening import load_script
from test_lifecycle_w6_pack import ACTOR, AUDIT_ID, _copy, _production_pack
from test_lifecycle_w8_register import _git
import test_lifecycle_w6_pack as w6

cli = load_script("t9_lifecycle", "minsky-lifecycle.py")
RUNTIME = "round-2/provenance/runtime-blobs/runtime"


@pytest.fixture
def offload_ready(tmp_path: Path, minsky_store: Path) -> dict:
    """A sealed, packed code-only audit with ONE complete copy A, its records and call manifests committed."""
    audit = w6.sealed_audit.__wrapped__(tmp_path, minsky_store)
    # A real `classify` row records the classification file's path in its note (W5); W6's fixture leaves it empty.
    w6._append(audit, "classify", "sealed", **{"class": "code-only"}, note="lifecycle/classification.md")
    _production_pack(audit)
    _copy(audit)
    commit(audit, "lifecycle records")
    return audit


def commit(audit: dict, message: str) -> None:
    repo, rel = audit["repo"], audit["dir"].relative_to(audit["repo"]).as_posix()
    paths = [f"{rel}/lifecycle", "documentation", f"{rel}/round-2/round-scope.json",
             f"{rel}/round-2/provenance/*.call.json", f"{rel}/round-2/provenance/*.preflight.json"]
    _git(repo, "add", "--", *paths)
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message)


def offload(audit: dict, phase: str) -> None:
    lo.offload(audit["repo"], audit_id=AUDIT_ID, actor=ACTOR, phase=phase, audit_dir=audit["dir"],
               copy_a_root=audit["copy_a"])


def events(audit: dict) -> list[str]:
    return [row["event"] for row in lr.read_register(audit["repo"]).rows]


def test_t9_full_offload_removes_only_the_committed_intent_and_records_completion(offload_ready: dict) -> None:
    audit = offload_ready
    offload(audit, "prepare")
    intent_path = audit["dir"] / "lifecycle/offload_intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in intent["removal_set"]] == [RUNTIME]   # the only untracked raw-class member
    assert len(intent["rehearsals"]) == 1 and intent["rehearsals"][0]["verdicts"]
    assert (audit["dir"] / RUNTIME).exists(), "prepare deletes nothing"
    commit(audit, "offload intent")
    survivors = {p.relative_to(audit["dir"]).as_posix() for p in audit["dir"].rglob("*") if p.is_file()} - {RUNTIME}
    offload(audit, "execute")
    assert not (audit["dir"] / RUNTIME).exists()
    after = {p.relative_to(audit["dir"]).as_posix() for p in audit["dir"].rglob("*") if p.is_file()}
    assert survivors <= after                                                 # nothing else removed
    for name in ("STUB.json", "TOMBSTONE.md"):
        assert (audit["dir"] / "lifecycle" / name).is_file()
    ledger = (audit["repo"] / "documentation/cold_storage_ledger.md").read_text(encoding="utf-8")
    assert "minsky lifecycle offload (plan v0.2 §6.6)" in ledger
    rows = lr.read_register(audit["repo"]).rows
    assert rows[-1]["event"] == "offload" and rows[-1]["state_after"] == "offloaded"
    lo.rehydrate(audit["repo"], audit_id=AUDIT_ID, copy="A", actor=ACTOR, in_place=True)
    restored = audit["dir"] / RUNTIME                        # the move is reversible from the copy
    assert restored.is_file() and w6.hashlib.sha256(restored.read_bytes()).hexdigest() == audit["blob_sha"]


def test_t9_uncommitted_intent_removes_nothing(offload_ready: dict) -> None:
    """LC-F20: a failed (here: missing) pre-removal commit removes nothing."""
    audit = offload_ready
    offload(audit, "prepare")
    with pytest.raises(lr.RegisterError, match="committed|HEAD"):
        offload(audit, "execute")
    assert (audit["dir"] / RUNTIME).exists()
    assert "offload" not in events(audit)


def test_t9_new_raw_member_after_intent_refuses_everything(offload_ready: dict) -> None:
    audit = offload_ready
    offload(audit, "prepare")
    commit(audit, "offload intent")
    late = audit["dir"] / "round-2/codex/_codex_logs/late.log"
    late.parent.mkdir(parents=True)
    late.write_text("arrived after the intent\n", encoding="utf-8")
    with pytest.raises(lr.RegisterError, match="new raw member"):
        offload(audit, "execute")
    assert (audit["dir"] / RUNTIME).exists() and late.exists()


def test_t9_changed_intent_member_refuses_before_any_removal(offload_ready: dict) -> None:
    audit = offload_ready
    offload(audit, "prepare")
    commit(audit, "offload intent")
    target = audit["dir"] / RUNTIME
    target.chmod(0o644)
    target.write_bytes(b"changed after the intent\n")
    with pytest.raises(lr.RegisterError, match="sha256|bytes"):
        offload(audit, "execute")
    assert target.read_bytes() == b"changed after the intent\n"


def test_t9_crash_after_removal_is_resumable(offload_ready: dict, monkeypatch) -> None:
    audit = offload_ready
    offload(audit, "prepare")
    commit(audit, "offload intent")
    original = lo._write_record

    def crash(path: Path, raw: bytes) -> None:
        if path.name == "STUB.json":
            raise OSError("simulated kill after removal, before the completion records")
        original(path, raw)

    monkeypatch.setattr(lo, "_write_record", crash)
    with pytest.raises(OSError, match="simulated kill"):
        offload(audit, "execute")
    assert not (audit["dir"] / RUNTIME).exists() and "offload" not in events(audit)
    monkeypatch.setattr(lo, "_write_record", original)
    offload(audit, "resume")
    assert events(audit)[-1] == "offload"


def test_t9_rehearsal_reads_only_the_copy(offload_ready: dict) -> None:
    """Mutants killed: 'drop the rehearsal' and 'fall through to a live/boot path' (LC-F19, F21, F22)."""
    audit = offload_ready
    audit["blob"].chmod(0o644)
    audit["blob"].unlink()                                   # boot CAS gone: the copy alone must suffice
    offload(audit, "prepare")
    (audit["dir"] / "lifecycle/offload_intent.json").unlink()   # discard; now corrupt the copy's replica instead
    replica = w6.ls.store.cas_path(audit["copy_a"] / "cas", audit["blob_sha"])
    replica.chmod(0o644)
    replica.write_bytes(b"corrupted replica\n")
    with pytest.raises(lr.RegisterError):
        offload(audit, "prepare")
    assert not (audit["dir"] / "lifecycle/offload_intent.json").exists()


def test_t9_tracked_raw_member_is_never_removed(offload_ready: dict) -> None:
    """AT-B: a git-tracked file is never removed, even if its class is raw."""
    audit = offload_ready
    rel = audit["dir"].relative_to(audit["repo"]).as_posix() + "/" + RUNTIME
    _git(audit["repo"], "add", "-f", "--", rel)
    _git(audit["repo"], "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "tracked raw blob")
    with pytest.raises(lr.RegisterError):
        offload(audit, "prepare")
        commit(audit, "offload intent")
        offload(audit, "execute")
    assert (audit["dir"] / RUNTIME).exists()
    assert "offload" not in events(audit)
