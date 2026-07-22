from __future__ import annotations

import copy
from pathlib import Path
import unittest
from typing import Any
from unittest.mock import patch

from accessibilizer import review
from accessibilizer.cli import (
    _authoring_contract,
    _internal_checks,
    _parse_page_selection,
    _review_page_document,
)


def page_document(page: int) -> dict[str, Any]:
    region_ids = [f"page-{page}-r{index:04d}" for index in range(1, 6)]
    nodes: list[dict[str, Any]] = [
        {"type": "heading", "level": 1, "text": "Electric Current"},
        {"type": "paragraph", "text": "Electric current is the rate charge flows."},
        {
            "type": "formula",
            "normalized_math": "I = Q / delta t",
            "spoken_math_alternative": "I equals Q divided by delta t.",
        },
        {
            "type": "figure",
            "complexity": "complex",
            "figure_alternative": "A wire carrying current.",
            "detailed_figure_description": "A wire passes through a surface.",
        },
        {
            "type": "table",
            "caption": "Resistivity",
            "rows": [
                {"cells": [
                    {"kind": "header", "text": "Material", "scope": "col",
                     "row_span": 1, "col_span": 1},
                ]},
            ],
        },
    ]
    return {
        "schema_version": "1.0",
        "page": page,
        "source_sha256": "a" * 64,
        "title": "Electric Current",
        "language": "en-US",
        "page_dimensions": {"width_points": 612, "height_points": 792},
        "source_regions": [
            {"id": identifier, "page": page, "bbox_points": [10, 10, 600, 780]}
            for identifier in region_ids
        ],
        "semantic_layer": [
            {
                **node,
                "id": f"page-{page}-s{index:04d}",
                "source_regions": [region_ids[index - 1]],
            }
            for index, node in enumerate(nodes, start=1)
        ],
        "warnings": [],
        "candidates": [
            {"id": f"page-{page}-c0001", "type": "formula", "text": None,
             "source_region": f"page-{page}-r0003"},
        ],
        "reconstruction": {
            "document_class": "stem_instructional",
            "page_prompt_version": "1.0",
            "page_schema_version": "1.0",
            "primary_language_is_english": True,
            "provider_endpoint": "http://localhost:11434/v1",
            "provider_model": "exact-model",
            "reading_order": ["heading", "paragraph", "formula", "figure", "table"],
            "reading_order_is_unambiguous": True,
            "region_prompt_version": "1.0",
            "region_schema_version": "1.0",
            "verified_regions": [
                {"agrees_with_page": True, "source_region": f"page-{page}-r0003", "type": "formula"},
                {"agrees_with_page": True, "source_region": f"page-{page}-r0004", "type": "figure"},
                {"agrees_with_page": True, "source_region": f"page-{page}-r0005", "type": "table"},
            ],
        },
    }


def record_for(pages: list[int]) -> dict[str, Any]:
    return review.build_review_record(
        source_sha256="a" * 64,
        title="Electric Current",
        language="en-US",
        provider_endpoint="http://localhost:11434/v1",
        provider_model="exact-model",
        page_prompt_version="1.0",
        page_schema_version="1.0",
        region_prompt_version="1.0",
        region_schema_version="1.0",
        pages=[page_document(p) for p in pages],
    )


class PageSelectionTest(unittest.TestCase):
    def test_default_selects_the_whole_document(self) -> None:
        self.assertEqual(_parse_page_selection(None, 11), list(range(1, 12)))

    def test_single_page(self) -> None:
        self.assertEqual(_parse_page_selection("3", 11), [3])

    def test_range(self) -> None:
        self.assertEqual(_parse_page_selection("2-4", 11), [2, 3, 4])

    def test_comma_list_is_sorted_and_deduplicated(self) -> None:
        self.assertEqual(_parse_page_selection("5,1,5,3", 11), [1, 3, 5])

    def test_mixed_ranges_and_singletons(self) -> None:
        self.assertEqual(_parse_page_selection("1,3-4,7", 11), [1, 3, 4, 7])

    def test_page_beyond_the_document_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_page_selection("12", 11)

    def test_reversed_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_page_selection("4-2", 11)

    def test_non_numeric_spec_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_page_selection("all", 11)


class AuthoringContractTest(unittest.TestCase):
    def test_projects_only_pdf_authoring_fields(self) -> None:
        contract = _authoring_contract(record_for([1, 2]))
        self.assertEqual(contract["schema_version"], "2.0")
        self.assertEqual([p["source_page"] for p in contract["pages"]], [1, 2])
        for grouped in contract["pages"]:
            self.assertEqual(len(grouped["semantic_layer"]), 5)
            self.assertTrue(
                all(
                    not {"page", "id", "source_regions"}.intersection(node)
                    for node in grouped["semantic_layer"]
                )
            )
        self.assertFalse(
            {"source_regions", "candidates", "warnings", "page_dimensions"}.intersection(contract)
        )
        self.assertEqual(contract["pages"][0]["semantic_layer"][0]["type"], "heading")


class SourceEvidenceTest(unittest.TestCase):
    def test_whole_page_fallback_warns_with_node_and_region_references(self) -> None:
        document = page_document(1)
        document["semantic_layer"][0]["source_regions"] = ["page-1-r0000"]
        for verified in document["reconstruction"]["verified_regions"]:
            verified["id"] = verified.pop("source_region")
        recognition_document = {
            "rendering": {"dpi": 300},
            "candidates": [{
                "id": "page-1-r0001", "type": "document_structure",
                "bbox_pixels": [0, 0, 100, 100],
            }],
        }
        with patch("accessibilizer.cli.recognition.png_size", return_value=(100, 100)):
            reviewed = _review_page_document(
                document, recognition_document, Path("page.png"), (72.0, 72.0)
            )

        warning = next(w for w in reviewed["warnings"] if w["code"] == "imprecise-source-grounding")
        self.assertEqual(warning["semantic_nodes"], ["page-1-s0001"])
        self.assertEqual(warning["source_regions"], ["page-1-r0000"])


class InternalChecksTest(unittest.TestCase):
    def extracted(self, record: dict[str, Any]) -> dict[str, Any]:
        # The authored structure tree that a faithful author + inspect returns:
        # pages in authored order, each with its semantic layer (no source page).
        return {
            "pages": [
                {"semantic_layer": page["semantic_layer"]}
                for page in _authoring_contract(record)["pages"]
            ]
        }

    def test_a_faithful_output_passes_every_category(self) -> None:
        record = record_for([1, 2])
        result = _internal_checks(record, self.extracted(record))
        self.assertTrue(result["passed"], result["checks"])
        self.assertTrue(all(result["categories"].values()))
        self.assertEqual(
            set(result["categories"]),
            {
                "review-record-consistency",
                "reading-order",
                "source-region-coverage",
                "alternatives",
                "table-relationships",
                "recognition-agreement",
            },
        )

    def test_a_reordered_structure_tree_fails_reading_order(self) -> None:
        record = record_for([1])
        extracted = self.extracted(record)
        extracted["pages"][0]["semantic_layer"].reverse()
        result = _internal_checks(record, extracted)
        self.assertFalse(result["passed"])
        self.assertFalse(result["categories"]["reading-order"])

    def test_a_missing_figure_alternative_fails_alternatives(self) -> None:
        record = record_for([1])
        figure = next(n for n in record["semantic_layer"] if n["type"] == "figure")
        figure["figure_alternative"] = ""
        result = _internal_checks(record, self.extracted(record))
        self.assertFalse(result["categories"]["alternatives"])

    def test_a_header_without_scope_fails_table_relationships(self) -> None:
        record = record_for([1])
        table = next(n for n in record["semantic_layer"] if n["type"] == "table")
        table["rows"][0]["cells"][0]["scope"] = "none"
        result = _internal_checks(record, self.extracted(record))
        self.assertFalse(result["categories"]["table-relationships"])

    def test_an_unverified_region_fails_recognition_agreement(self) -> None:
        record = record_for([1])
        record["reconstruction"]["pages"][0]["verified_regions"] = []
        result = _internal_checks(record, self.extracted(record))
        self.assertFalse(result["categories"]["recognition-agreement"])

    def test_a_warning_reference_without_a_source_region_fails_coverage(self) -> None:
        record = record_for([1])
        record["warnings"].append({
            "id": "w0001", "code": "recognition-disagreement", "message": "x",
            "page": 1, "semantic_nodes": [], "source_regions": ["page-1-r9999"],
            "resolution": None, "history": [],
        })
        result = _internal_checks(record, self.extracted(record))
        self.assertFalse(result["categories"]["source-region-coverage"])


if __name__ == "__main__":
    unittest.main()
