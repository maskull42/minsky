"""KB-sized W1 source units, T3 remaps and a legacy reconciliation smoke test."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard

from test_hardening import MODELS, PERSONA, SCRIPTS, provenance, record_call, register_round
from byte_sources import (
    RAW_MANIFEST_HEADER, ArchiveSource, LiveSource, PackError, PackIndex, Resolution,
    load_symlink_map, parse_remaps,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _cas_blob(root: Path, digest: str, payload: bytes) -> Path:
    path = root / "sha256" / digest[:2] / digest[2:4] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _member(name: str, payload: bytes, *, pax: str | None = None) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    if pax is not None:
        member.pax_headers = {"MINSKY.sha256": pax}
    return member


def _pack(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for member, payload in members:
            archive.addfile(member, io.BytesIO(payload) if payload is not None else None)
    path.write_bytes(zstandard.ZstdCompressor().compress(raw.getvalue()))
    return path


def _assert_overlap_refused(parser, values: list[str]) -> None:
    with pytest.raises(ValueError, match="overlapping remap prefixes") as exc:
        parser(values)
    for value in values:
        assert value.split("=")[0].rstrip("/") in str(exc.value)


def test_live_source_hashes_exact_path_and_ignores_expected(tmp_path: Path) -> None:
    path = tmp_path / "evidence.txt"
    path.write_bytes(b"observable bytes")
    link = tmp_path / "link.txt"
    link.symlink_to(path)
    source = LiveSource()
    assert source.resolve(str(link), "0" * 64) == Resolution(True, _sha(b"observable bytes"), "live", str(link))
    assert source.output_key(str(link), str(tmp_path)) == str(link)
    path.unlink()
    assert source.resolve(str(link), None) == Resolution(False, None, "live", "")


def test_remaps_are_ordered_applied_and_logged(tmp_path: Path) -> None:
    first = tmp_path / "new-first"
    second = tmp_path / "new-second"
    first.mkdir()
    second.mkdir()
    (first / "out.txt").write_bytes(b"first")
    (second / "out.txt").write_bytes(b"second")
    pairs = parse_remaps([f"/old/first/={first}/", f"/old/second={second}"])
    assert pairs == [("/old/first", str(first)), ("/old/second", str(second))]
    source = ArchiveSource(store_root=None, pack=None, remaps=pairs, no_live=False)
    assert source.resolve("/old/first/out.txt", "0" * 64) == Resolution(True, _sha(b"first"), "remap", str(first / "out.txt"))
    assert source.resolve("/old/second/out.txt", None) == Resolution(True, _sha(b"second"), "remap", str(second / "out.txt"))
    assert source.remap_log == [
        {"recorded": "/old/first/out.txt", "opened": str(first / "out.txt"), "remap": ["/old/first", str(first)]},
        {"recorded": "/old/second/out.txt", "opened": str(second / "out.txt"), "remap": ["/old/second", str(second)]},
    ]


@pytest.mark.parametrize("values", [
    ["/a/b=/new/one", "/a/b=/new/two"],
    ["/a/b=/new/one", "/a/b/c=/new/two"],
    ["/a/b/c=/new/one", "/a/b=/new/two"],
])
def test_overlapping_remap_prefixes_are_refused(values: list[str]) -> None:
    _assert_overlap_refused(parse_remaps, values)
    with pytest.raises(ValueError, match="overlapping remap prefixes"):
        ArchiveSource(store_root=None, pack=None,
                      remaps=[tuple(value.split("=")) for value in values], no_live=False)


def test_remap_ambiguity_mutant_is_killed(tmp_path: Path, monkeypatch) -> None:
    original = (SCRIPTS / "byte_sources.py").read_text(encoding="utf-8")
    check = (
        "        for previous, _ in remaps:\n"
        "            if _relative_to(old, previous) is not None or _relative_to(previous, old) is not None:\n"
        "                raise ValueError(f\"overlapping remap prefixes: {previous!r} and {old!r}\")\n"
    )
    assert original.count(check) == 1
    path = tmp_path / "byte_sources_first_match_without_ambiguity_check.py"
    path.write_text(original.replace(check, ""), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("w1_remap_mutant", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        _assert_overlap_refused(module.parse_remaps, ["/a/b=/one", "/a/b/c=/two"])


def test_remap_prefixes_are_component_wise(tmp_path: Path) -> None:
    pairs = parse_remaps([f"/a/b={tmp_path}/one", f"/a/bc={tmp_path}/two"])
    assert [old for old, _ in pairs] == ["/a/b", "/a/bc"]
    (tmp_path / "two").mkdir()
    (tmp_path / "two" / "out").write_bytes(b"two")
    source = ArchiveSource(store_root=None, pack=None, remaps=pairs, no_live=False)
    assert source.resolve("/a/bc/out", None).detail == str(tmp_path / "two" / "out")
    assert source.remap_log[0]["remap"] == ["/a/bc", str(tmp_path / "two")]
    assert not source.resolve("/a/bcd/out", None).present
    assert len(source.remap_log) == 1


@pytest.mark.parametrize("value", [
    "/a", "/a=/b=/c", "=/b", "/a=", "relative=/b", "/a=relative", "/=/b", "/a=/",
])
def test_remaps_refuse_invalid_pairs(value: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_remaps([value])
    assert value in str(exc.value)


@pytest.mark.parametrize("expected", [None, "0" * 64])
def test_no_live_ignores_remaps_without_inspecting_paths(tmp_path: Path, monkeypatch,
                                                       expected: str | None) -> None:
    recorded = tmp_path / "recorded" / "out"
    recorded.parent.mkdir()
    recorded.write_bytes(b"live")
    remapped = tmp_path / "remapped" / "out"
    remapped.parent.mkdir()
    remapped.write_bytes(b"remapped")
    source = ArchiveSource(store_root=None, pack=None,
                          remaps=[(str(recorded.parent), str(remapped.parent))], no_live=True)

    def refuse_inspection(path: Path) -> bool:
        raise AssertionError(f"no-live inspected {path}")

    monkeypatch.setattr(Path, "is_file", refuse_inspection)
    assert source.resolve(str(recorded), expected) == Resolution(False, None, "unresolved", "")
    assert source.remap_log == []


def test_missing_remap_use_is_logged(tmp_path: Path) -> None:
    source = ArchiveSource(store_root=None, pack=None, remaps=[("/old", str(tmp_path))], no_live=False)
    assert source.resolve("/old/missing", None) == Resolution(False, None, "unresolved", "")
    assert source.remap_log == [{"recorded": "/old/missing", "opened": str(tmp_path / "missing"),
                                 "remap": ["/old", str(tmp_path)]}]


def test_cas_resolves_by_rehashed_content_before_pack_or_remap(tmp_path: Path) -> None:
    payload = b"preserved evidence"
    digest = _sha(payload)
    path = _cas_blob(tmp_path / "store", digest, payload)
    source = ArchiveSource(store_root=tmp_path / "store", pack=tmp_path / "missing.tar.zst",
                          remaps=[("/old", str(tmp_path))], no_live=False)
    assert source.resolve("/old/out", digest) == Resolution(True, digest, "cas", str(path))
    assert source.remap_log == []


def test_corrupt_cas_does_not_resolve_and_names_blob(tmp_path: Path) -> None:
    digest = _sha(b"expected")
    path = _cas_blob(tmp_path / "store", digest, b"corrupt")
    source = ArchiveSource(store_root=tmp_path / "store", pack=None, remaps=[], no_live=True)
    assert source.resolve("/old/out", digest) == Resolution(False, None, "unresolved", f"cas-corrupt:{path}")


@pytest.mark.parametrize("fallback", ["pack", "remap"])
def test_corrupt_cas_continues_to_next_source(tmp_path: Path, fallback: str) -> None:
    payload = b"expected"
    digest = _sha(payload)
    _cas_blob(tmp_path / "store", digest, b"corrupt")
    pack = None
    remaps = []
    if fallback == "pack":
        pack = _pack(tmp_path / "raw.tar.zst", [(_member("evidence/out", payload, pax=digest), payload)])
    else:
        (tmp_path / "out").write_bytes(payload)
        remaps = [("/old", str(tmp_path))]
    source = ArchiveSource(store_root=tmp_path / "store", pack=pack, remaps=remaps,
                          no_live=fallback == "pack")
    result = source.resolve("/old/out", digest)
    assert result.present and result.sha256 == digest and result.resolved_via == fallback


def test_missing_store_never_falls_through_to_recorded_live_path(tmp_path: Path) -> None:
    path = tmp_path / "live.txt"
    path.write_bytes(b"live")
    source = ArchiveSource(store_root=tmp_path / "missing-store", pack=None, remaps=[], no_live=False)
    assert source.resolve(str(path), _sha(b"live")) == Resolution(False, None, "unresolved", "")


def test_pack_index_hashes_payloads_and_is_built_once_lazily(tmp_path: Path, monkeypatch) -> None:
    payload = b"first evidence"
    other = b"second evidence"
    digest = _sha(payload)
    pack = _pack(tmp_path / "raw.tar.zst", [
        (_member("round-2/first", payload, pax=digest), payload),
        (_member("round-2/second", other), other),
    ])
    index = PackIndex.build(pack)
    assert index.members == {digest: "round-2/first", _sha(other): "round-2/second"}
    assert index.pax_sha256 == {"round-2/first": digest}
    built = []
    original = PackIndex.build

    def build(path: Path) -> PackIndex:
        built.append(path)
        return original(path)

    monkeypatch.setattr(PackIndex, "build", build)
    source = ArchiveSource(store_root=None, pack=pack, remaps=[], no_live=True)
    assert built == []
    assert source.resolve("/unrelated/name", digest) == Resolution(True, digest, "pack", "round-2/first")
    assert source.resolve("/another/name", _sha(other)).detail == "round-2/second"
    assert source.resolve("/missing", "0" * 64) == Resolution(False, None, "unresolved", "")
    assert built == [pack]


@pytest.mark.parametrize("pax", ["0" * 64, ""])
def test_pack_refuses_disagreeing_pax_hash(tmp_path: Path, pax: str) -> None:
    payload = b"evidence"
    pack = _pack(tmp_path / "raw.tar.zst", [(_member("evidence/out", payload, pax=pax), payload)])
    source = ArchiveSource(store_root=None, pack=pack, remaps=[], no_live=True)
    with pytest.raises(PackError, match="evidence/out.*MINSKY.sha256"):
        source.resolve("/old/out", _sha(payload))


@pytest.mark.parametrize("kind", [tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_pack_refuses_hardlinks_and_special_members(tmp_path: Path, kind: bytes) -> None:
    member = tarfile.TarInfo("unsafe-member")
    member.type = kind
    member.linkname = "another-member"
    pack = _pack(tmp_path / "raw.tar.zst", [(member, None)])
    with pytest.raises(PackError, match="unsafe-member"):
        PackIndex.build(pack)


def test_pack_member_name_cannot_prove_expected_content(tmp_path: Path) -> None:
    recorded = "/recorded/round-2/out.txt"
    payload = b"different bytes"
    pack = _pack(tmp_path / "raw.tar.zst", [(_member(recorded, payload), payload)])
    source = ArchiveSource(store_root=None, pack=pack, remaps=[], no_live=True)
    assert source.resolve(recorded, _sha(b"expected bytes")) == Resolution(False, None, "unresolved", "")


def test_pack_index_skips_directories_and_symlinks_and_hashes_empty_files(tmp_path: Path) -> None:
    directory = tarfile.TarInfo("directory")
    directory.type = tarfile.DIRTYPE
    link = tarfile.TarInfo("symlink")
    link.type = tarfile.SYMTYPE
    link.linkname = "empty"
    pack = _pack(tmp_path / "raw.tar.zst", [(directory, None), (link, None), (_member("empty", b""), b"")])
    assert PackIndex.build(pack).members == {_sha(b""): "empty"}


def test_unknown_hash_uses_only_remap(tmp_path: Path) -> None:
    path = tmp_path / "out"
    path.write_bytes(b"remapped")
    source = ArchiveSource(store_root=tmp_path / "missing-store", pack=tmp_path / "missing.tar.zst",
                          remaps=[("/old", str(tmp_path))], no_live=False)
    assert source.resolve("/old/out", None) == Resolution(True, _sha(b"remapped"), "remap", str(path))


@pytest.mark.parametrize("recorded,root,expected", [
    ("/old/round-2/codex/out.json", "/old/round-2", "codex/out.json"),
    ("/old/round-2", "/old/round-2", "."),
    ("/old/round-20/out", "/old/round-2", "abs:/old/round-20/out"),
    ("/outside/out", "/old/round-2", "abs:/outside/out"),
    ("/old/round-2/out", None, "abs:/old/round-2/out"),
])
def test_output_key_strips_only_recorded_root_components(recorded: str, root: str | None,
                                                         expected: str) -> None:
    source = ArchiveSource(store_root=None, pack=None, remaps=[], no_live=True)
    assert source.output_key(recorded, root) == expected


def test_output_key_applies_longest_symlink_map_before_root_stripping() -> None:
    source = ArchiveSource(store_root=None, pack=None, remaps=[], no_live=True,
                          symlink_map=[("/external/raw", "raw"), ("/external/raw/deep", "linked")])
    assert source.output_key("/external/raw/out", "/old/round-2") == "raw/out"
    assert source.output_key("/external/raw/deep/out", "/old/round-2") == "linked/out"
    assert source.output_key("/external/raw-other/out", "/old/round-2") == "abs:/external/raw-other/out"
    assert source.output_key("/external/raw/out", None) == "abs:/external/raw/out"


def test_load_symlink_map_has_fixed_header_and_longest_first(tmp_path: Path) -> None:
    assert load_symlink_map(tmp_path) == []
    manifest = tmp_path / "lifecycle" / "raw_manifest.tsv"
    manifest.parent.mkdir()
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RAW_MANIFEST_HEADER, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in [
            {"path": "round-2/raw", "type": "symlink", "symlink_map": "/external/raw"},
            {"path": "round-2/deep", "type": "symlink", "symlink_map": "/external/raw/deep"},
            {"path": "round-2/internal", "type": "symlink"},
            {"path": "round-2/file", "type": "file", "symlink_map": "/ignored"},
        ]:
            writer.writerow(row)
    assert load_symlink_map(tmp_path) == [
        ("/external/raw/deep", "round-2/deep"), ("/external/raw", "round-2/raw"),
    ]


@pytest.mark.parametrize("header", [
    "", "path\ttype\n", "\t".join(reversed(RAW_MANIFEST_HEADER)) + "\n",
    "\t".join(RAW_MANIFEST_HEADER) + "\r\n",
    '"path"\t' + "\t".join(RAW_MANIFEST_HEADER[1:]) + "\n",
])
def test_load_symlink_map_refuses_wrong_header(tmp_path: Path, header: str) -> None:
    manifest = tmp_path / "lifecycle" / "raw_manifest.tsv"
    manifest.parent.mkdir()
    manifest.write_text(header, encoding="utf-8")
    with pytest.raises(ValueError, match="wrong header") as exc:
        load_symlink_map(tmp_path)
    assert str(manifest) in str(exc.value)


@pytest.mark.parametrize("origin,step,model,effort,persona", [
    ("wrapper", "codex", "gpt-5.6-sol", "medium", PERSONA),
    ("host", "claude_self", "gpt-6-astra", "not_exposed", None),
])
def test_legacy_reconcile_smoke_passes_then_refuses_changed_or_missing_output(
        tmp_path: Path, origin: str, step: str, model: str, effort: str, persona: str | None) -> None:
    round_dir = tmp_path / "round-2"
    register_round(round_dir)
    output = round_dir / "out.json"
    output.write_text("{}\n", encoding="utf-8")
    context = round_dir / "pack.xml"
    context.write_text("<pack/>\n", encoding="utf-8")
    manifest = record_call(
        round_dir, uid="w1smokecall", step=step, model=model, effort=effort, persona=persona,
        outputs=[output], invoked_at="2026-09-05T10:00:00Z", origin=origin,
        raw_response=origin == "wrapper", usage={"status": "total-only", "total_tokens": 1},
        context_files=[context],
    )
    kwargs = {"step": step, "persona": persona, "output_path": output,
              "registered_models": MODELS, "required_context_paths": [context]}
    ok, reason, record = provenance.reconcile_artifact(round_dir, **kwargs)
    assert ok and reason == "ok" and record["_path"] == str(manifest)
    output.write_text("changed\n", encoding="utf-8")
    assert provenance.reconcile_artifact(round_dir, **kwargs)[:2] == (False, f"output hash mismatch: {output}")
    output.unlink()
    assert provenance.reconcile_artifact(round_dir, **kwargs)[:2] == (False, f"output is missing: {output}")
