"""Deterministic fidelity evaluation for replayed vision-prototype runs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import json
import math
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


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

_NEAR_WHOLE_PAGE_AREA_RATIO = 0.8


def _box(value: object) -> tuple[float, float, float, float] | None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and math.isfinite(float(number))
            for number in value
        )
    ):
        return None
    return tuple(float(number) for number in value)  # type: ignore[return-value]


def _rectangle_union_area(
    boxes: list[tuple[float, float, float, float]],
) -> float:
    """Return exact union area for the small set of gold rectangles on one node."""
    x_edges = sorted({coordinate for box in boxes for coordinate in (box[0], box[2])})
    area = 0.0
    for left, right in zip(x_edges, x_edges[1:]):
        intervals = sorted(
            (box[1], box[3]) for box in boxes if box[0] < right and box[2] > left
        )
        covered_height = 0.0
        if intervals:
            start, end = intervals[0]
            for interval_start, interval_end in intervals[1:]:
                if interval_start > end:
                    covered_height += end - start
                    start, end = interval_start, interval_end
                else:
                    end = max(end, interval_end)
            covered_height += end - start
        area += (right - left) * covered_height
    return area


def _semantically_matched_nodes(
    produced_nodes: list[dict[str, Any]], gold_nodes: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    unmatched_by_signature: dict[str, list[dict[str, Any]]] = {}
    for gold_node in gold_nodes:
        unmatched_by_signature.setdefault(_semantic_signature(gold_node), []).append(
            gold_node
        )
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for produced_node in produced_nodes:
        candidates = unmatched_by_signature.get(_semantic_signature(produced_node), [])
        if candidates:
            matches.append((produced_node, candidates.pop(0)))
    return matches


def _gold_node_geometry(
    gold_node: Mapping[str, Any], gold_regions: Mapping[object, dict[str, Any]]
) -> tuple[
    list[tuple[float, float, float, float]],
    tuple[float, float, float, float],
]:
    boxes = [
        _box(gold_regions[reference].get("bbox_points"))
        for reference in gold_node.get("source_regions", [])
        if reference in gold_regions
    ]
    valid_boxes = [bounds for bounds in boxes if bounds is not None]
    if not valid_boxes:
        raise ValueError(
            f"gold node {gold_node.get('id')} has no valid Source Region geometry"
        )
    union = (
        min(bounds[0] for bounds in valid_boxes),
        min(bounds[1] for bounds in valid_boxes),
        max(bounds[2] for bounds in valid_boxes),
        max(bounds[3] for bounds in valid_boxes),
    )
    return valid_boxes, union


def _geometry_failures(
    replayed_run: Mapping[str, Any], gold_review_record: Mapping[str, Any]
) -> list[dict[str, Any]]:
    run_id = replayed_run["run_id"]
    gold_regions_value = gold_review_record.get("source_regions")
    if not isinstance(gold_regions_value, list):
        return []
    gold_regions = {
        region.get("id"): region
        for region in gold_regions_value
        if isinstance(region, dict) and isinstance(region.get("id"), str)
    }
    gold_nodes_value = gold_review_record.get("semantic_layer", [])
    assert isinstance(gold_nodes_value, list)
    failures: list[dict[str, Any]] = []
    pages = replayed_run.get("pages")
    assert isinstance(pages, list)
    for page_record in pages:
        assert isinstance(page_record, dict)
        page = page_record["page"]
        dimensions = page_record.get("page_dimensions")
        regions_value = page_record.get("source_regions")
        if not isinstance(dimensions, dict) or not isinstance(regions_value, list):
            continue
        width = dimensions.get("width_points")
        height = dimensions.get("height_points")
        if not (
            isinstance(width, (int, float))
            and isinstance(height, (int, float))
            and math.isfinite(float(width))
            and math.isfinite(float(height))
            and width > 0
            and height > 0
        ):
            raise ValueError("prototype page dimensions must be finite and positive")
        page_area = float(width) * float(height)
        nodes = page_record.get("semantic_layer")
        assert isinstance(nodes, list)
        expected_nodes = [
            node
            for node in gold_nodes_value
            if isinstance(node, dict) and node.get("page") == page
        ]
        matched_nodes = _semantically_matched_nodes(nodes, expected_nodes)
        gold_context_by_region: dict[str, list[tuple[str, list[float]]]] = {}
        for produced_node, gold_node in matched_nodes:
            _, gold_union = _gold_node_geometry(gold_node, gold_regions)
            for reference in produced_node.get("source_regions", []):
                if isinstance(reference, str):
                    gold_context_by_region.setdefault(reference, []).append(
                        (gold_node["id"], list(gold_union))
                    )

        regions: dict[str, dict[str, Any]] = {}
        geometry_ids: dict[tuple[float, float, float, float], str] = {}
        for region in regions_value:
            if not isinstance(region, dict) or not isinstance(region.get("id"), str):
                raise ValueError("prototype Source Regions must have string identities")
            region_id = region["id"]
            bounds = _box(region.get("bbox_points"))
            regions[region_id] = region
            if (
                bounds is None
                or region.get("page") != page
                or not (0 <= bounds[0] < bounds[2] <= float(width))
                or not (0 <= bounds[1] < bounds[3] <= float(height))
            ):
                contexts = gold_context_by_region.get(region_id, [(None, None)])
                failures.extend(
                    {
                        "run_id": run_id,
                        "page": page,
                        "node": node_id,
                        "source_region": region_id,
                        "rule": "finite-nonempty-contained",
                        "gold_bounds": gold_bounds,
                        "produced_bounds": list(bounds) if bounds is not None else None,
                    }
                    for node_id, gold_bounds in contexts
                )
                continue
            prior_id = geometry_ids.get(bounds)
            if prior_id is not None:
                contexts = gold_context_by_region.get(region_id, [(None, None)])
                failures.extend(
                    {
                        "run_id": run_id,
                        "page": page,
                        "node": node_id,
                        "source_region": region_id,
                        "rule": "deterministic-identity",
                        "duplicate_of": prior_id,
                        "gold_bounds": gold_bounds,
                        "produced_bounds": list(bounds),
                    }
                    for node_id, gold_bounds in contexts
                )
            else:
                geometry_ids[bounds] = region_id

        for produced_node, gold_node in matched_nodes:
            node_id = gold_node.get("id")
            valid_gold_boxes, gold_union_box = _gold_node_geometry(
                gold_node, gold_regions
            )
            center = (
                (gold_union_box[0] + gold_union_box[2]) / 2,
                (gold_union_box[1] + gold_union_box[3]) / 2,
            )
            references = produced_node.get("source_regions")
            if not isinstance(references, list):
                references = []
            valid_produced: list[tuple[str, tuple[float, float, float, float]]] = []
            for reference in references:
                region = regions.get(reference) if isinstance(reference, str) else None
                bounds = _box(region.get("bbox_points")) if region else None
                if (
                    isinstance(reference, str)
                    and region is not None
                    and bounds is not None
                    and region.get("page") == page
                ):
                    valid_produced.append((reference, bounds))
            if not any(
                bounds[0] <= center[0] <= bounds[2]
                and bounds[1] <= center[1] <= bounds[3]
                for _, bounds in valid_produced
            ):
                missed_regions: list[
                    tuple[str | None, tuple[float, float, float, float] | None]
                ] = list(valid_produced) or [(None, None)]
                failures.extend(
                    {
                        "run_id": run_id,
                        "page": page,
                        "node": node_id,
                        "source_region": region_id,
                        "rule": "gold-content-center-contained",
                        "gold_bounds": list(gold_union_box),
                        "produced_bounds": list(bounds) if bounds is not None else None,
                    }
                    for region_id, bounds in missed_regions
                )
            gold_area_ratio = _rectangle_union_area(valid_gold_boxes) / page_area
            if gold_area_ratio < _NEAR_WHOLE_PAGE_AREA_RATIO:
                for region_id, bounds in valid_produced:
                    produced_area_ratio = (
                        (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
                    ) / page_area
                    if produced_area_ratio >= _NEAR_WHOLE_PAGE_AREA_RATIO:
                        failures.append(
                            {
                                "run_id": run_id,
                                "page": page,
                                "node": node_id,
                                "source_region": region_id,
                                "rule": "unjustified-near-whole-page",
                                "gold_bounds": list(gold_union_box),
                                "produced_bounds": list(bounds),
                            }
                        )
    return failures


def _write_visual_review_artifacts(
    failures: list[dict[str, Any]],
    replayed_run: Mapping[str, Any],
    destination: Path | None,
) -> list[str]:
    if destination is None or not failures:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    dimensions_by_page = {
        page["page"]: page.get("page_dimensions")
        for page in replayed_run["pages"]
        if isinstance(page, dict) and isinstance(page.get("page"), int)
    }
    artifacts: list[str] = []
    for index, failure in enumerate(failures, start=1):
        page = failure["page"]
        dimensions = dimensions_by_page.get(page)
        if not isinstance(dimensions, dict):
            continue
        width = float(dimensions["width_points"])
        height = float(dimensions["height_points"])
        produced = _box(failure.get("produced_bounds"))
        gold = _box(failure.get("gold_bounds"))
        visible = [bounds for bounds in (produced, gold) if bounds is not None]
        if visible:
            x0 = max(0.0, min(bounds[0] for bounds in visible) - 5)
            y0 = max(0.0, min(bounds[1] for bounds in visible) - 5)
            x1 = min(width, max(bounds[2] for bounds in visible) + 5)
            y1 = min(height, max(bounds[3] for bounds in visible) + 5)
        else:
            x0, y0, x1, y1 = 0.0, 0.0, width, height
        overlays: list[str] = []
        for bounds, color, label in (
            (gold, "#0969da", "Gold content"),
            (produced, "#cf222e", "Produced Source Region"),
        ):
            if bounds is None:
                continue
            overlays.append(
                f'<rect x="{bounds[0]:g}" y="{bounds[1]:g}" '
                f'width="{bounds[2] - bounds[0]:g}" '
                f'height="{bounds[3] - bounds[1]:g}" fill="none" '
                f'stroke="{color}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>'
                f'<text x="{bounds[0]:g}" y="{max(bounds[1] - 2, y0 + 3):g}" '
                f'fill="{color}" font-size="4">{label}</text>'
            )
        title = escape(
            f'{failure["run_id"]} · page {page} · {failure["node"] or "no node"} · '
            f'{failure["source_region"] or "no Source Region"} · {failure["rule"]}'
        )
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="{x0:g} {y0:g} {x1 - x0:g} {y1 - y0:g}">'
            f'<title>{title}</title>'
            f'<rect x="0" y="0" width="{width:g}" height="{height:g}" fill="#fff"/>'
            + "".join(overlays)
            + "</svg>\n"
        )
        artifact = destination / f"geometry-failure-{index:04d}-page-{page}.svg"
        artifact.write_text(svg, encoding="utf-8")
        artifacts.append(str(artifact))
    return artifacts


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
    visual_review_dir: Path | None = None,
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
    geometry_failures = _geometry_failures(replayed_run, gold_review_record)
    visual_review_artifacts = _write_visual_review_artifacts(
        geometry_failures, replayed_run, visual_review_dir
    )
    has_pending_adjudication = any(
        item["reviewer_decision"] is None for item in adjudications
    )
    passed = (
        not failures
        and not any(warning_failures.values())
        and not geometry_failures
        and not has_pending_adjudication
    )
    return {
        "run_id": run_id,
        "passed": passed,
        "semantic_fidelity_failures": failures,
        "warning_failures": warning_failures,
        "geometry_failures": geometry_failures,
        "visual_review_artifacts": visual_review_artifacts,
        "adjudication_queue": adjudications,
    }
