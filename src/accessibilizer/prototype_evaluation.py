"""Deterministic fidelity evaluation for replayed vision-prototype runs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import json
from typing import Any


_EXACT_FIELDS: dict[str, tuple[str, ...]] = {
    "heading": ("level", "text"),
    "paragraph": ("text",),
    "formula": ("normalized_math",),
    "figure": ("complexity",),
    "table": ("caption", "rows"),
}

_WORDING_FIELDS: dict[str, tuple[str, ...]] = {
    "formula": ("spoken_math_alternative",),
    "figure": ("figure_alternative", "detailed_figure_description"),
}


def _semantic_signature(node: Mapping[str, Any]) -> str:
    node_type = node.get("type")
    if not isinstance(node_type, str):
        return json.dumps({"type": node_type}, ensure_ascii=False, sort_keys=True)
    fields = (
        "type",
        *_EXACT_FIELDS.get(node_type, ()),
    )
    return json.dumps(
        {field: node.get(field) for field in fields},
        ensure_ascii=False,
        sort_keys=True,
    )


def _nodes_by_page(document: Mapping[str, Any]) -> dict[int, list[dict[str, Any]]]:
    pages = document.get("pages")
    if not isinstance(pages, list):
        raise ValueError("prototype run pages must be an array")
    result: dict[int, list[dict[str, Any]]] = {}
    for page_record in pages:
        if not isinstance(page_record, dict) or not isinstance(page_record.get("page"), int):
            raise ValueError("prototype run pages must identify an integer page")
        page = page_record["page"]
        nodes = page_record.get("semantic_layer")
        if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
            raise ValueError("prototype page semantic_layer must be an array of nodes")
        if page in result:
            raise ValueError(f"prototype run contains duplicate page {page}")
        result[page] = nodes
    return result


def _warnings(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    pages = document.get("pages")
    assert isinstance(pages, list)
    for page_record in pages:
        assert isinstance(page_record, dict)
        page_warnings = page_record.get("warnings")
        if not isinstance(page_warnings, list) or not all(
            isinstance(warning, dict) for warning in page_warnings
        ):
            raise ValueError("prototype page warnings must be an array")
        warnings.extend(page_warnings)
    return warnings


def _warning_differences(
    produced_warnings: list[dict[str, Any]],
    gold_warnings: object,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(gold_warnings, list) or not all(
        isinstance(warning, dict) for warning in gold_warnings
    ):
        raise ValueError("gold Review Record warnings must be an array")
    try:
        expected = Counter(
            (warning["page"], warning["code"]) for warning in gold_warnings
        )
        produced = Counter(
            (warning["page"], warning["code"]) for warning in produced_warnings
        )
    except (KeyError, TypeError) as error:
        raise ValueError("prototype warnings must identify a page and code") from error

    def expanded(counter: Counter[tuple[int, str]]) -> list[dict[str, Any]]:
        return [
            {"page": page, "code": code}
            for (page, code), count in sorted(counter.items())
            for _ in range(count)
        ]

    return {
        "recall": expanded(expected - produced),
        "precision": expanded(produced - expected),
    }


def evaluate_prototype_fidelity(
    replayed_run: Mapping[str, Any],
    gold_review_record: Mapping[str, Any],
    *,
    reviewer_decisions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compare a replayed normalized run with the approved gold Review Record.

    Source-derived semantics and Logical Reading Order are compared exactly.
    Wording-only alternatives are returned for Reviewer adjudication without an LLM.
    """
    run_id = replayed_run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("prototype run must have a nonempty run_id")
    decisions = reviewer_decisions or {}
    invalid_decisions = set(decisions.values()) - {
        "meaning-equivalent",
        "not-meaning-equivalent",
    }
    if invalid_decisions:
        raise ValueError("Reviewer decisions must state whether wording is meaning-equivalent")
    produced_by_page = _nodes_by_page(replayed_run)
    gold_nodes = gold_review_record.get("semantic_layer")
    gold_pages = gold_review_record.get("pages")
    if not isinstance(gold_nodes, list) or not all(
        isinstance(node, dict) for node in gold_nodes
    ):
        raise ValueError("gold Review Record semantic_layer must be an array")
    if not isinstance(gold_pages, list) or not all(isinstance(page, int) for page in gold_pages):
        raise ValueError("gold Review Record pages must be an integer array")

    failures: list[dict[str, Any]] = []
    adjudications: list[dict[str, Any]] = []
    expected_pages = set(gold_pages)
    for unexpected_page in sorted(set(produced_by_page) - expected_pages):
        failures.append(
            {"page": unexpected_page, "node": None, "field": "page", "gold": None, "produced": unexpected_page}
        )

    for page in gold_pages:
        expected_nodes = [node for node in gold_nodes if node.get("page") == page]
        produced_nodes = produced_by_page.get(page, [])
        expected_types = [node.get("type") for node in expected_nodes]
        produced_types = [node.get("type") for node in produced_nodes]
        expected_semantics = [_semantic_signature(node) for node in expected_nodes]
        produced_semantics = [_semantic_signature(node) for node in produced_nodes]
        is_semantic_reordering = (
            expected_semantics != produced_semantics
            and Counter(expected_semantics) == Counter(produced_semantics)
        )
        if produced_types != expected_types or is_semantic_reordering:
            failures.append(
                {
                    "page": page,
                    "node": None,
                    "field": "logical_reading_order",
                    "gold": expected_semantics,
                    "produced": produced_semantics,
                }
            )
        maximum = max(len(expected_nodes), len(produced_nodes))
        for index in range(maximum):
            expected = expected_nodes[index] if index < len(expected_nodes) else None
            produced = produced_nodes[index] if index < len(produced_nodes) else None
            node_id = expected.get("id") if expected is not None else produced.get("id") if produced else None
            if expected is None or produced is None:
                failures.append(
                    {
                        "page": page,
                        "node": node_id,
                        "field": "logical_reading_order",
                        "gold": expected.get("type") if expected else None,
                        "produced": produced.get("type") if produced else None,
                    }
                )
                continue
            node_type = expected.get("type")
            if produced.get("type") != node_type:
                failures.append(
                    {
                        "page": page,
                        "node": node_id,
                        "field": "type",
                        "gold": node_type,
                        "produced": produced.get("type"),
                    }
                )
                continue
            if not isinstance(node_type, str) or node_type not in _EXACT_FIELDS:
                raise ValueError(f"unsupported gold Semantic Layer node type: {node_type}")
            for field in _EXACT_FIELDS[node_type]:
                if produced.get(field) != expected.get(field):
                    failures.append(
                        {
                            "page": page,
                            "node": node_id,
                            "field": field,
                            "gold": expected.get(field),
                            "produced": produced.get(field),
                        }
                    )
            for field in _WORDING_FIELDS.get(node_type, ()):
                if produced.get(field) == expected.get(field):
                    continue
                gold_wording = expected.get(field)
                produced_wording = produced.get(field)
                if not isinstance(gold_wording, str) or not isinstance(
                    produced_wording, str
                ):
                    failures.append(
                        {
                            "page": page,
                            "node": node_id,
                            "field": field,
                            "gold": gold_wording,
                            "produced": produced_wording,
                        }
                    )
                    continue
                item_id = f"{run_id}:{node_id}:{field}"
                decision = decisions.get(item_id)
                adjudications.append(
                    {
                        "id": item_id,
                        "run_id": run_id,
                        "page": page,
                        "node": node_id,
                        "field": field,
                        "gold_wording": gold_wording,
                        "produced_wording": produced_wording,
                        "reviewer_decision": decision,
                    }
                )
                if decision == "not-meaning-equivalent":
                    failures.append(
                        {
                            "page": page,
                            "node": node_id,
                            "field": field,
                            "gold": gold_wording,
                            "produced": produced_wording,
                        }
                    )

    warning_failures = _warning_differences(
        _warnings(replayed_run), gold_review_record.get("warnings")
    )
    has_pending_adjudication = any(
        item["reviewer_decision"] is None for item in adjudications
    )
    passed = (
        not failures
        and not any(warning_failures.values())
        and not has_pending_adjudication
    )
    return {
        "run_id": run_id,
        "passed": passed,
        "semantic_fidelity_failures": failures,
        "warning_failures": warning_failures,
        "adjudication_queue": adjudications,
    }
