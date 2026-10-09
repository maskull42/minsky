"""Exercise the release gate; refuse each sensitive fixture and inexact allowances."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import publish_check


@pytest.mark.parametrize("rule,path,payload", [
    ("users-path", "notes.txt", b"path /Users/example/project\n"),
    ("email", "notes.txt", b"contact fixture@example.invalid\n"),
    ("env-content", ".env", b"plain text\n"),
    ("env-content", ".env.local", b"plain text\n"),
    ("env-content", "notes.txt", b" export API_TOKEN = sensitive-fixture\n"),
    ("env-content", "key.txt", b"PRIVATE_KEY=value\n"),
    ("env-content", "secret.txt", b"APP_SECRET=value\n"),
    ("env-content", "password.txt", b"PASSWORD=value\n"),
    ("database", "fixture.db", b"fixture"),
    ("database", "fixture.sqlite", b"fixture"),
    ("database", "fixture.sqlite3", b"fixture"),
    ("database", "fixture-wal", b"fixture"),
    ("database", "fixture-shm", b"fixture"),
    ("audit-records", "codex-audits/example/receipt.txt", b"fixture"),
    ("audit-records", ".minsky/audits.db", b"fixture"),
])
def test_each_rule_refuses(tmp_path: Path, rule: str, path: str, payload: bytes) -> None:
    tree = tmp_path / "tree"
    target = tree / path
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    output = tmp_path / "report.json"
    assert publish_check.main(["--tree", str(tree), "--json-out", str(output)]) == 1
    report = json.loads(output.read_bytes())
    assert report["schema"] == "minsky-publish-check/1"
    assert report["tree"] == str(tree) and report["file_count"] == 1
    hits = [hit for hit in report["hits"] if hit["rule"] == rule]
    assert hits and all(hit["path"] == path and hit["matched_text"] for hit in hits)
    assert hits[0]["line"] == (1 if path in ("notes.txt", "key.txt", "secret.txt", "password.txt") else 0)


def test_clean_tree_passes_and_git_is_excluded(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / ".git").mkdir(parents=True)
    (tree / ".git/hidden").write_text("/Users/example", encoding="utf-8")
    (tree / "readme.txt").write_text("Portable example.\nEMPTY_KEY=\n", encoding="utf-8")
    report = tmp_path / "report.json"
    assert publish_check.main(["--tree", str(tree), "--json-out", str(report)]) == 0
    value = json.loads(report.read_bytes())
    assert value["hits"] == value["allowed"] == value["skipped_binary"] == []
    assert value["result"] == "clean" and value["file_count"] == 1


def test_binary_is_skipped_but_name_rules_apply(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "asset.bin").write_bytes(b"\0/Users/example fixture@example.invalid")
    (tree / "asset.sqlite").write_bytes(b"\0PRIVATE_KEY=value")
    report = publish_check.check_tree(tree)
    assert report["skipped_binary"] == ["asset.bin", "asset.sqlite"]
    assert report["hits"] == [{"rule": "database", "path": "asset.sqlite", "line": 0,
                              "matched_text": "asset.sqlite"}]


def test_allow_suppresses_only_one_exact_hit(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("/Users/example\n/Users/example\n", encoding="utf-8")
    (tree / "b.txt").write_text("/Users/example\n", encoding="utf-8")
    allow = tmp_path / "allow.tsv"
    allow.write_text("a.txt\tusers-path\t/Users/\tsynthetic fixture\n", encoding="utf-8")
    report = publish_check.check_tree(tree, allow_file=allow)
    assert len(report["allowed"]) == 1 and report["allowed"][0]["line"] == 1
    assert [(hit["path"], hit["line"]) for hit in report["hits"]] == [("a.txt", 2), ("b.txt", 1)]
    assert report["result"] == "hits"
    (tree / "a.txt").write_text("/Users/example\n", encoding="utf-8")
    (tree / "b.txt").unlink()
    assert publish_check.check_tree(tree, allow_file=allow)["result"] == "clean"


@pytest.mark.parametrize("entry", [
    "other.txt\tusers-path\t/Users/\twrong path\n",
    "a.txt\temail\t/Users/\twrong rule\n",
    "a.txt\tusers-path\t/Users/example\twrong text\n",
])
def test_unused_allow_is_error(tmp_path: Path, entry: str) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("/Users/example\n", encoding="utf-8")
    allow = tmp_path / "allow.tsv"
    allow.write_text(entry, encoding="utf-8")
    output = tmp_path / "report.json"
    assert publish_check.main(["--tree", str(tree), "--allow-file", str(allow), "--json-out", str(output)]) == 2
    report = json.loads(output.read_bytes())
    assert report["result"] == "error" and report["errors"] and not report["allowed"]


def test_deny_literal_is_caught(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "notes.txt").write_text("synthetic-private-audit-id\n", encoding="utf-8")
    deny = tmp_path / "deny.txt"
    deny.write_text("synthetic-private-audit-id\n", encoding="utf-8")
    report = tmp_path / "report.json"
    assert publish_check.main(["--tree", str(tree), "--deny-file", str(deny), "--json-out", str(report)]) == 1
    assert json.loads(report.read_bytes())["hits"] == [{"rule": "deny-literal", "path": "notes.txt",
                                                      "line": 1, "matched_text": "synthetic-private-audit-id"}]


def test_errors_name_inputs_and_existing_report_is_preserved(tmp_path: Path, capsys) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    output = tmp_path / "report.json"
    output.write_text("preserved", encoding="utf-8")
    assert publish_check.main(["--tree", str(tree), "--json-out", str(output)]) == 2
    assert output.read_text() == "preserved" and str(output) in capsys.readouterr().err
    (tree / "link").symlink_to(output)
    with pytest.raises(publish_check.PublishError, match="link"):
        publish_check.check_tree(tree)


@pytest.mark.parametrize("route", ["direct", "output-symlink", "tree-symlink", "relative"])
def test_report_inside_tree_is_refused_before_scan(tmp_path: Path, monkeypatch, capsys,
                                                 route: str) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    output = tree / "report.json"
    if route == "output-symlink":
        alias = tmp_path / "output-alias"
        alias.symlink_to(tree, target_is_directory=True)
        output = alias / "report.json"
    elif route == "tree-symlink":
        alias = tmp_path / "tree-alias"
        alias.symlink_to(tree, target_is_directory=True)
        tree = alias
    elif route == "relative":
        monkeypatch.chdir(tmp_path)
        tree, output = Path("tree"), Path("tree/report.json")
    monkeypatch.setattr(publish_check, "check_tree", lambda *a, **k: pytest.fail("refused output was scanned"))
    assert publish_check.main(["--tree", str(tree), "--json-out", str(output)]) == 2
    error = capsys.readouterr().err
    assert str(output) in error and str(tree) in error and "resolves inside tree" in error
    assert not output.exists()
    assert list(tree.iterdir()) == []


def test_users_rule_mutant_is_killed(tmp_path: Path, monkeypatch) -> None:
    source = Path(publish_check.__file__).read_text(encoding="utf-8")
    old = ('                for match in re.finditer(r"/Users/", line):\n'
           '                    hit("users-path", relative, number, match.group())\n')
    assert source.count(old) == 1
    path = tmp_path / "publish_check_mutant.py"
    path.write_text(source.replace(old, "", 1), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("publish_check_mutant", path)
    assert spec and spec.loader
    mutant = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mutant)
    monkeypatch.setitem(test_each_rule_refuses.__globals__, "publish_check", mutant)
    with pytest.raises(AssertionError):
        test_each_rule_refuses(tmp_path, "users-path", "notes.txt", b"path /Users/example/project\n")
