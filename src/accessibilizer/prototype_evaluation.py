"""Deterministic Semantic Layer and Conversion Warning prototype evaluation."""

from __future__ import annotations

from collections import Counter
from itertools import zip_longest
from pathlib import Path
from typing import Any, Literal, Mapping

from accessibilizer.review import load_yaml
from accessibilizer.vision_prototype import replay_prototype_document


AdjudicationDecision = Literal["accepted", "rejected"]

EXACT_FIELDS: dict[str, tuple[str, ...]] = {
    "heading": ("level", "text"),
    "paragraph": ("text",),
    "formula": ("normalized_math",),
    "figure": ("complexity",),
    "table": (
        "caption",
        "rows",
    ),
}

WORDING_FIELDS: dict[str, tuple[str, ...]] = {
    "formula": ("spoken_math_alternative",),
    "figure": ("figure_alternative", "detailed_figure_description"),
}


def _semantic_nodes(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    pages = document.get("pages")
    if not isinstance(pages, list):
        raise ValueError("prototype run pages must be an array")
    nodes: list[dict[str, Any]] = []
    for expected_page, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or page.get("page") != expected_page:
            raise ValueError("prototype run pages must be complete and ordered")
        semantic_layer = page.get("semantic_layer")
        if not isinstance(semantic_layer, list):
            raise ValueError("prototype page semantic_layer must be an array")
        for node in semantic_layer:
            if not isinstance(node, dict):
                raise ValueError("prototype Semantic Layer nodes must be objects")
            nodes.append(node)
    return nodes


def _gold_nodes(gold: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = gold.get("semantic_layer")
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise ValueError("gold Review Record semantic_layer must be an array of objects")
    return nodes


def _failure(
    *,
    page: object,
    node: object,
    field: str,
    gold: object,
    produced: object,
) -> dict[str, Any]:
    return {
        "page": page,
        "node": node,
        "field": field,
        "gold": gold,
        "produced": produced,
    }


def _compare_semantics(
    run_id: str,
    produced_nodes: list[dict[str, Any]],
    gold_nodes: list[dict[str, Any]],
    decisions: Mapping[str, AdjudicationDecision],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    adjudications: list[dict[str, Any]] = []
    missing = object()

    produced_order = [
        {"page": node.get("page"), "type": node.get("type")}
        for node in produced_nodes
    ]
    gold_order = [
        {"page": node.get("page"), "type": node.get("type")} for node in gold_nodes
    ]
    if produced_order != gold_order:
        failures.append(
            _failure(
                page=None,
                node=None,
                field="logical_reading_order",
                gold=gold_order,
                produced=produced_order,
            )
        )

    for produced, gold in zip_longest(produced_nodes, gold_nodes, fillvalue=missing):
        if produced is missing or gold is missing:
            continue
        assert isinstance(produced, dict)
        assert isinstance(gold, dict)
        page = gold.get("page")
        node_id = gold.get("id")
        produced_type = produced.get("type")
        gold_type = gold.get("type")
        if produced_type != gold_type:
            failures.append(
                _failure(
                    page=page,
                    node=node_id,
                    field="type",
                    gold=gold_type,
                    produced=produced_type,
                )
            )
            continue
        if not isinstance(gold_type, str):
            raise ValueError("gold Semantic Layer node type must be a string")

        for field in EXACT_FIELDS.get(gold_type, ()):
            if produced.get(field) != gold.get(field):
                failures.append(
                    _failure(
                        page=page,
                        node=node_id,
                        field=field,
                        gold=gold.get(field),
                        produced=produced.get(field),
                    )
                )

        for field in WORDING_FIELDS.get(gold_type, ()):
            gold_wording = gold.get(field)
            produced_wording = produced.get(field)
            if produced_wording == gold_wording:
                continue
            adjudication_id = f"{run_id}:{node_id}:{field}"
            decision = decisions.get(adjudication_id)
            adjudications.append(
                {
                    "id": adjudication_id,
                    "run_id": run_id,
                    "page": page,
                    "node": node_id,
                    "field": field,
                    "gold_wording": gold_wording,
                    "produced_wording": produced_wording,
                    "decision": decision,
                }
            )
            if decision == "rejected":
                failures.append(
                    _failure(
                        page=page,
                        node=node_id,
                        field=field,
                        gold=gold_wording,
                        produced=produced_wording,
                    )
                )

    return failures, adjudications


def _warning_pairs(value: object, *, source: str) -> list[tuple[int, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{source} warnings must be an array")
    pairs: list[tuple[int, str]] = []
    for warning in value:
        if not isinstance(warning, dict):
            raise ValueError(f"{source} warnings must contain objects")
        page = warning.get("page")
        code = warning.get("code")
        if not isinstance(page, int) or isinstance(page, bool) or not isinstance(code, str):
            raise ValueError(f"{source} warning page and code are invalid")
        pairs.append((page, code))
    return pairs


def _compare_warnings(
    produced_pairs: list[tuple[int, str]], gold_pairs: list[tuple[int, str]]
) -> dict[str, Any]:
    produced = Counter(produced_pairs)
    gold = Counter(gold_pairs)
    matched = sum((produced & gold).values())
    missing_pairs = sorted((gold - produced).elements())
    additional_pairs = sorted((produced - gold).elements())
    return {
        "passed": not missing_pairs and not additional_pairs,
        "missing": [{"page": page, "code": code} for page, code in missing_pairs],
        "additional": [
            {"page": page, "code": code} for page, code in additional_pairs
        ],
        "recall": matched / sum(gold.values()) if gold else 1.0,
        "precision": matched / sum(produced.values()) if produced else (1.0 if not gold else 0.0),
    }


def evaluate_prototype_document(
    run_dir: Path,
    gold_review_record: Path,
    *,
    adjudication_decisions: Mapping[str, AdjudicationDecision] | None = None,
) -> dict[str, Any]:
    """Replay and compare one full prototype run with the approved Review Record."""
    decisions = adjudication_decisions or {}
    invalid_decisions = sorted(
        decision for decision in decisions.values() if decision not in {"accepted", "rejected"}
    )
    if invalid_decisions:
        raise ValueError("adjudication decisions must be accepted or rejected")

    run = replay_prototype_document(run_dir)
    gold_value: Any = load_yaml(gold_review_record.read_text(encoding="utf-8"))
    if not isinstance(gold_value, dict):
        raise ValueError("gold Review Record must be an object")
    run_id = run.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("prototype run_id must be a string")

    produced_nodes = _semantic_nodes(run)
    gold_nodes = _gold_nodes(gold_value)
    failures, adjudications = _compare_semantics(
        run_id, produced_nodes, gold_nodes, decisions
    )
    known_adjudications = {item["id"] for item in adjudications}
    unknown_decisions = sorted(set(decisions) - known_adjudications)
    if unknown_decisions:
        raise ValueError(f"unknown adjudication decision: {unknown_decisions[0]}")

    produced_warnings = [
        warning
        for page in run["pages"]
        for warning in page["warnings"]
    ]
    warning_fidelity = _compare_warnings(
        _warning_pairs(produced_warnings, source="prototype"),
        _warning_pairs(gold_value.get("warnings"), source="gold Review Record"),
    )
    pending_adjudications = sum(item["decision"] is None for item in adjudications)
    rejected_adjudications = sum(
        item["decision"] == "rejected" for item in adjudications
    )
    semantic_passed = not failures and pending_adjudications == 0
    semantic_fidelity = {
        "passed": semantic_passed,
        "failures": failures,
        "pending_adjudications": pending_adjudications,
        "rejected_adjudications": rejected_adjudications,
    }
    return {
        "run_id": run_id,
        "passed": semantic_passed and warning_fidelity["passed"],
        "semantic_fidelity": semantic_fidelity,
        "warning_fidelity": warning_fidelity,
        "adjudications": adjudications,
    }
