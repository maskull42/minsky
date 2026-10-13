"""PUB-210 fixture checks; refuse public gates carrying activation or evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import publish_check

GATES = ".claude/skills/minsky/config/real_data_gates.json"


def _unmet() -> dict:
    return {"schema": "minsky-real-data-gates/1",
            **{gate: {"met": False, "evidence": None} for gate in ("G1", "G2", "G3")}}


def _tree(tmp_path: Path, value: object) -> Path:
    tree = tmp_path / "tree"
    path = tree / GATES
    path.parent.mkdir(parents=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))
    return tree


def _one_hit(tree: Path) -> dict:
    report = publish_check.check_tree(tree)
    assert report["file_count"] == 1
    assert report["result"] == "hits" and report["errors"] == []
    assert len(report["hits"]) == 1
    hit = report["hits"][0]
    assert (hit["rule"], hit["path"], hit["line"]) == ("gates-not-unmet", GATES, 0)
    assert hit["matched_text"]
    return hit


def test_b1_all_unmet_passes(tmp_path: Path) -> None:
    report = publish_check.check_tree(_tree(tmp_path, _unmet()))
    assert report["file_count"] == 1
    assert report["result"] == "clean" and report["hits"] == report["errors"] == []


def test_b2_met_g1_is_one_hit(tmp_path: Path) -> None:
    value = _unmet()
    value["G1"] = {"met": True, "evidence": "fixture restore"}
    _one_hit(_tree(tmp_path, value))


def test_b3_unmet_with_evidence_is_one_hit(tmp_path: Path) -> None:
    value = _unmet()
    value["G2"]["evidence"] = "fixture evidence"
    _one_hit(_tree(tmp_path, value))


@pytest.mark.parametrize("scope", ["top", "gate"])
def test_b4_extra_key_is_one_hit(tmp_path: Path, scope: str) -> None:
    value = _unmet()
    (value if scope == "top" else value["G3"])["extra"] = None
    _one_hit(_tree(tmp_path, value))


@pytest.mark.parametrize("payload", [b"{invalid", b"\0", b"\xff"], ids=["json", "nul", "utf8"])
def test_b5_invalid_json_is_one_hit(tmp_path: Path, payload: bytes) -> None:
    _one_hit(_tree(tmp_path, payload))


@pytest.mark.parametrize("path", ["readme.txt", "config/real_data_gates.json"])
def test_b6_absent_file_passes(tmp_path: Path, path: str) -> None:
    tree = tmp_path / "tree"
    target = tree / path
    target.parent.mkdir(parents=True)
    target.write_text("Fixture release.\n", encoding="utf-8")
    report = publish_check.check_tree(tree)
    assert report["file_count"] == 1
    assert report["result"] == "clean" and report["hits"] == report["errors"] == []


@pytest.mark.parametrize("defect", ["object", "schema", "missing-gate", "gate-object", "missing-met",
                                    "missing-evidence", "numeric-false", "null-met", "string-met",
                                    "empty-evidence", "multiple-gates"])
def test_gate_shape_is_exact(tmp_path: Path, defect: str) -> None:
    value = _unmet()
    if defect == "object":
        value = []
    elif defect == "schema":
        value["schema"] = "minsky-real-data-gates/2"
    elif defect == "missing-gate":
        del value["G3"]
    elif defect == "gate-object":
        value["G2"] = []
    elif defect in ("missing-met", "missing-evidence"):
        del value["G1"][defect.removeprefix("missing-")]
    elif defect in ("numeric-false", "null-met", "string-met"):
        value["G2"]["met"] = {"numeric-false": 0, "null-met": None, "string-met": "false"}[defect]
    elif defect == "empty-evidence":
        value["G3"]["evidence"] = ""
    else:
        for gate in ("G1", "G2", "G3"):
            value[gate] = {"met": True, "evidence": "fixture evidence"}
    _one_hit(_tree(tmp_path, value))


def test_gate_hit_uses_exact_allowance(tmp_path: Path) -> None:
    value = _unmet()
    value["G1"]["met"] = True
    tree = _tree(tmp_path, value)
    hit = _one_hit(tree)
    allow = tmp_path / "allow.tsv"
    allow.write_text(f"{GATES}\tgates-not-unmet\t{hit['matched_text']}\tfixture ruling\n", encoding="utf-8")
    report = publish_check.check_tree(tree, allow_file=allow)
    assert report["file_count"] == 1
    assert report["result"] == "clean" and report["hits"] == report["errors"] == []
    assert report["allowed"] == [{**hit, "reason": "fixture ruling"}]
