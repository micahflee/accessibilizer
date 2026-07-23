#!/usr/bin/env python3
"""Build the deterministic 11-page offline replay fixture from captured recognition.

Run recognition with the pinned container first, then pass the directory containing
``page-N.json`` documents here. The output deliberately keeps only Source Regions
selected by the captured page responses or referenced by Recognition Candidates;
the full proposal-coverage behavior remains exercised live in ``test_recognition``.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import io
import json
from pathlib import Path
from typing import Any

import yaml

from accessibilizer import page as page_module
from accessibilizer import recognition
from accessibilizer.checkpoint import file_sha256


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"
GOLD = ROOT / "testdata" / "gold-review-record.yaml"


def _intersection(first: list[float], second: list[float]) -> float:
    return max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0.0, min(first[3], second[3]) - max(first[1], second[1])
    )


def _area(bbox: list[float]) -> float:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def _match_score(proposal: list[float], gold: list[float]) -> tuple[float, float]:
    intersection = _intersection(proposal, gold)
    union = _area(proposal) + _area(gold) - intersection
    return (
        intersection / union if union else 0.0,
        intersection / _area(gold),
    )


def _usefully_covers(proposal: list[float], gold: list[float]) -> bool:
    iou, containment = _match_score(proposal, gold)
    return iou >= 0.5 or (
        containment >= 0.8 and _area(proposal) <= 2 * _area(gold)
    )


def _candidate_with_current_eligibility(candidate: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(candidate)
    verification = updated["verification"]
    verification["reason_codes"] = [
        reason
        for reason in verification["reason_codes"]
        if reason != "missing-layout-confidence"
    ]
    verification["eligible"] = not verification["reason_codes"]
    return updated


def build_fixture(recognition_directory: Path) -> dict[str, Any]:
    gold: dict[str, Any] = yaml.safe_load(GOLD.read_text(encoding="utf-8"))
    gold_regions = {
        region["id"]: region for region in gold["source_regions"]
    }
    reconstruction = {
        page["page"]: page for page in gold["reconstruction"]["pages"]
    }
    pages: list[dict[str, Any]] = []
    for page_number in gold["pages"]:
        document = json.loads(
            (recognition_directory / f"page-{page_number}.json").read_text(
                encoding="utf-8"
            )
        )
        candidates = [
            _candidate_with_current_eligibility(candidate)
            for candidate in document["candidates"]
        ]
        regions_by_id = {
            region["id"]: region for region in document["source_regions"]
        }
        nonfallback_regions = [
            region
            for region in document["source_regions"]
            if not region["id"].endswith("-r0000")
        ]
        candidate_boxes = [
            tuple(regions_by_id[candidate["source_region"]]["bbox_pixels"])
            for candidate in candidates
            if not candidate["source_region"].endswith("-r0000")
        ]
        selected_boxes = recognition.select_model_binding_boxes(
            [
                tuple(region["bbox_pixels"])
                for region in nonfallback_regions
            ],
            candidate_boxes=candidate_boxes,
        )
        visible_regions = [
            region
            for region in nonfallback_regions
            if tuple(region["bbox_pixels"]) in selected_boxes
        ]
        page_gold_regions = {
            identifier: region
            for identifier, region in gold_regions.items()
            if region["page"] == page_number
        }
        selected_by_gold: dict[str, str] = {}
        for identifier, gold_region in page_gold_regions.items():
            matches = [
                region
                for region in visible_regions
                if _usefully_covers(
                    region["bbox_points"], gold_region["bbox_points"]
                )
            ]
            if not matches:
                raise RuntimeError(
                    f"page {page_number} gold Source Region {identifier} "
                    "has no model-visible useful proposal"
                )
            selected = max(
                matches,
                key=lambda region: (
                    *_match_score(
                        region["bbox_points"], gold_region["bbox_points"]
                    ),
                    -_area(region["bbox_points"]),
                    region["id"],
                ),
            )
            selected_by_gold[identifier] = selected["id"]

        nodes: list[dict[str, Any]] = []
        for gold_node in gold["semantic_layer"]:
            if gold_node["page"] != page_number:
                continue
            node = {
                key: copy.deepcopy(value)
                for key, value in gold_node.items()
                if key not in {"id", "page"}
            }
            node["source_regions"] = [
                selected_by_gold[identifier]
                for identifier in node["source_regions"]
            ]
            if node["type"] == "table":
                node["boundaries_are_uncertain"] = False
                node["headers_are_uncertain"] = False
            if node["type"] == "figure":
                node.setdefault("detailed_figure_description", None)
            nodes.append(node)

        gold_warnings = [
            warning
            for warning in gold["warnings"]
            if warning["page"] == page_number
        ]
        page_reconstruction = reconstruction[page_number]
        page_response = {
            "title": gold["title"],
            "language": gold["language"],
            "primary_language_is_english": page_reconstruction[
                "primary_language_is_english"
            ],
            "document_class": page_reconstruction["document_class"],
            "reading_order_is_unambiguous": page_reconstruction[
                "reading_order_is_unambiguous"
            ],
            "nodes": nodes,
            "suspected_source_errors": [
                warning["message"]
                for warning in gold_warnings
                if warning["code"] == "suspected-source-error"
            ],
            "suspected_prompt_injection": False,
        }
        targets = page_module._region_verification_targets(
            candidates,
            page_response,
            pdf_words=document["pdf_text_evidence"]["words"],
            source_regions=document["source_regions"],
        )
        verifications = [
            {
                "target": target,
                "response": {
                    "transcription": "",
                    "agrees_with_page": True,
                    "suspected_prompt_injection": False,
                },
            }
            for target in targets
        ]
        retained_region_ids = {
            region_id
            for node in nodes
            for region_id in node["source_regions"]
        } | {
            candidate["source_region"]
            for candidate in candidates
        }
        source_regions = [
            region
            for region in document["source_regions"]
            if region["id"] in retained_region_ids
        ]
        pages.append(
            {
                "page": page_number,
                "proposal_generation": {
                    **document["proposal_generation"],
                    "algorithm_version": recognition.PROPOSAL_ALGORITHM_VERSION,
                },
                "recognition": document["recognition"],
                "source_regions": source_regions,
                "gold_region_matches": [
                    {
                        "gold_bbox_points": gold_regions[gold_id][
                            "bbox_points"
                        ],
                        "source_region": source_region,
                    }
                    for gold_id, source_region in selected_by_gold.items()
                ],
                "candidates": candidates,
                "pdf_words": document["pdf_text_evidence"]["words"],
                "page_response": page_response,
                "region_verifications": verifications,
            }
        )
    return {
        "schema_version": "1.0",
        "source_sha256": file_sha256(SOURCE),
        "recognition_capture": "paddleocr-2.7.3-ppstructure-default",
        "pages": pages,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recognition_directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    fixture = build_fixture(args.recognition_directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as raw_stream:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw_stream, mtime=0
        ) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as stream:
                json.dump(
                    fixture,
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )


if __name__ == "__main__":
    main()
