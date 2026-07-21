from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from typing import Any

from accessibilizer.review import (
    REVIEW_RECORD_SCHEMA_VERSION,
    ReviewRecordError,
    build_review_record,
    commit_resolutions,
    dump_yaml,
    is_finalizable,
    load_yaml,
    render_review_report,
    review_record_schema,
    unresolved_warnings,
    validate_review_record,
)


ROOT = Path(__file__).resolve().parents[1]

HEADING = {"type": "heading", "level": 1, "text": "Electric Current"}
PARAGRAPH = {"type": "paragraph", "text": "Electric current is the rate charge flows."}
FORMULA = {
    "type": "formula",
    "normalized_math": "I = Q / delta t",
    "spoken_math_alternative": "I equals Q divided by delta t.",
}
FIGURE = {
    "type": "figure",
    "complexity": "complex",
    "figure_alternative": "A wire carrying current.",
    "detailed_figure_description": "A wire passes through a surface.",
}
SIMPLE_FIGURE = {
    "type": "figure",
    "complexity": "simple",
    "figure_alternative": "A wire carrying current.",
}
LINK = {"type": "link", "text": "Ohm's Law", "href": "https://example.org/ohm"}
TABLE: dict[str, Any] = {
    "type": "table",
    "caption": "Resistivity of common materials",
    "rows": [
        {
            "cells": [
                {"kind": "header", "text": "Material", "scope": "col", "row_span": 1, "col_span": 1},
                {"kind": "header", "text": "Resistivity", "scope": "col", "row_span": 1, "col_span": 1},
            ]
        },
        {
            "cells": [
                {"kind": "header", "text": "Copper", "scope": "row", "row_span": 1, "col_span": 1},
                {"kind": "data", "text": "1.68e-8", "scope": "none", "row_span": 1, "col_span": 1},
            ]
        },
    ],
}

PAGE_RECONSTRUCTION = {
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
    "verified_regions": [],
}


def candidates_for(page: int) -> list[dict[str, Any]]:
    return [
        {"id": f"page-{page}-r0001", "type": "text", "text": "current",
         "crop": f"regions/page-{page}-r0001.png"},
        {"id": f"page-{page}-r0003", "type": "formula", "text": None,
         "crop": f"regions/page-{page}-r0003.png"},
    ]


WARNINGS: list[dict[str, Any]] = [
    {
        "code": "ambiguous-reading-order",
        "message": "More than one order is plausible.",
        "status": "unresolved",
    },
    {
        "code": "recognition-disagreement",
        "message": "The formula region disagrees.",
        "status": "unresolved",
        "region": "page-1-r0003",
    },
]


def page_document(page: int = 1, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "page": page,
        "source_sha256": "a" * 64,
        "title": "Electric Current",
        "language": "en-US",
        "semantic_layer": [HEADING, PARAGRAPH, FORMULA, FIGURE, TABLE],
        "warnings": [],
        "candidates": candidates_for(page),
        "reconstruction": PAGE_RECONSTRUCTION,
    }
    document.update(overrides)
    return copy.deepcopy(document)


def build(pages: list[dict[str, Any]]) -> dict[str, Any]:
    return build_review_record(
        source_sha256="a" * 64,
        title="Electric Current",
        language="en-US",
        provider_endpoint="http://localhost:11434/v1",
        provider_model="exact-model",
        page_prompt_version="1.0",
        page_schema_version="1.0",
        region_prompt_version="1.0",
        region_schema_version="1.0",
        pages=pages,
    )


def built_record(**overrides: Any) -> dict[str, Any]:
    record = build([page_document(1, warnings=copy.deepcopy(WARNINGS))])
    record.update(overrides)
    return record


def resolve(record: dict[str, Any], index: int, **fields: Any) -> dict[str, Any]:
    record = copy.deepcopy(record)
    record["warnings"][index]["resolution"] = fields
    return record


class BuildRecordTest(unittest.TestCase):
    def test_record_carries_identity_layer_candidates_and_provenance(self) -> None:
        record = built_record()
        self.assertEqual(record["schema_version"], REVIEW_RECORD_SCHEMA_VERSION)
        self.assertEqual(record["pages"], [1])
        self.assertEqual(record["source_sha256"], "a" * 64)
        self.assertEqual([node["type"] for node in record["semantic_layer"]],
                         ["heading", "paragraph", "formula", "figure", "table"])
        # Every node is tagged with the source page it came from.
        self.assertTrue(all(node["page"] == 1 for node in record["semantic_layer"]))
        self.assertEqual(record["candidates"], candidates_for(1))
        self.assertEqual(record["reconstruction"]["provider_model"], "exact-model")
        self.assertEqual(record["reconstruction"]["pages"][0]["page"], 1)
        self.assertEqual(
            record["reconstruction"]["pages"][0]["document_class"], "stem_instructional"
        )

    def test_warnings_get_stable_ids_and_start_unresolved_with_empty_history(self) -> None:
        record = built_record()
        self.assertEqual([w["id"] for w in record["warnings"]], ["w0001", "w0002"])
        self.assertTrue(all(w["resolution"] is None for w in record["warnings"]))
        self.assertTrue(all(w["history"] == [] for w in record["warnings"]))
        self.assertTrue(all(w["page"] == 1 for w in record["warnings"]))
        self.assertIsNone(record["warnings"][0]["region"])
        self.assertEqual(record["warnings"][1]["region"], "page-1-r0003")

    def test_multiple_pages_flatten_into_one_ordered_document(self) -> None:
        record = build([
            page_document(1, warnings=[copy.deepcopy(WARNINGS[0])]),
            page_document(2, warnings=[copy.deepcopy(WARNINGS[1])]),
        ])
        self.assertEqual(record["pages"], [1, 2])
        pages_seen = [node["page"] for node in record["semantic_layer"]]
        self.assertEqual(pages_seen, [1] * 5 + [2] * 5)
        # Warnings are renumbered across the whole document and keep their page.
        self.assertEqual([w["id"] for w in record["warnings"]], ["w0001", "w0002"])
        self.assertEqual([w["page"] for w in record["warnings"]], [1, 2])
        # Candidates from both pages are retained.
        self.assertEqual(len(record["candidates"]), 4)
        self.assertEqual([p["page"] for p in record["reconstruction"]["pages"]], [1, 2])
        validate_review_record(record)


class SchemaValidationTest(unittest.TestCase):
    def test_a_freshly_built_record_validates(self) -> None:
        validate_review_record(built_record())

    def test_schema_is_versioned(self) -> None:
        self.assertEqual(review_record_schema()["properties"]["schema_version"]["const"], "2.0")

    def test_a_heading_hierarchy_of_levels_one_through_six_validates(self) -> None:
        record = built_record()
        for level in range(1, 7):
            node = {"type": "heading", "level": level, "text": f"Level {level}", "page": 1}
            record["semantic_layer"].append(node)
        validate_review_record(record)

    def test_a_heading_level_outside_one_through_six_is_rejected(self) -> None:
        record = built_record()
        record["semantic_layer"][0] = {"type": "heading", "level": 7, "text": "x", "page": 1}
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_link_node_validates_and_requires_text_and_href(self) -> None:
        record = built_record()
        record["semantic_layer"].append({**copy.deepcopy(LINK), "page": 1})
        validate_review_record(record)
        record["semantic_layer"][-1].pop("href")
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_node_without_a_page_is_rejected(self) -> None:
        record = built_record()
        record["semantic_layer"][0].pop("page")
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_simple_figure_without_a_detailed_description_validates(self) -> None:
        record = built_record()
        record["semantic_layer"][3] = {**copy.deepcopy(SIMPLE_FIGURE), "page": 1}
        validate_review_record(record)

    def test_a_simple_figure_carrying_a_detailed_description_is_rejected(self) -> None:
        record = built_record()
        record["semantic_layer"][3] = {
            **copy.deepcopy(SIMPLE_FIGURE),
            "detailed_figure_description": "A simple figure should not carry this.",
            "page": 1,
        }
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_complex_figure_without_a_detailed_description_is_rejected(self) -> None:
        record = built_record()
        figure = dict(record["semantic_layer"][3])
        figure.pop("detailed_figure_description")
        record["semantic_layer"][3] = figure
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_semantic_table_with_headers_and_a_caption_validates(self) -> None:
        validate_review_record(built_record())

    def test_a_captionless_table_validates(self) -> None:
        record = built_record()
        table = {**copy.deepcopy(TABLE), "page": 1}
        del table["caption"]
        record["semantic_layer"][4] = table
        validate_review_record(record)

    def test_a_data_cell_carrying_a_scope_is_rejected(self) -> None:
        record = built_record()
        table = {**copy.deepcopy(TABLE), "page": 1}
        table["rows"][1]["cells"][1]["scope"] = "col"
        record["semantic_layer"][4] = table
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_header_cell_without_a_scope_is_rejected(self) -> None:
        record = built_record()
        table = {**copy.deepcopy(TABLE), "page": 1}
        table["rows"][0]["cells"][0]["scope"] = "none"
        record["semantic_layer"][4] = table
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_table_without_rows_is_rejected(self) -> None:
        record = built_record()
        table = {**copy.deepcopy(TABLE), "page": 1}
        table["rows"] = []
        record["semantic_layer"][4] = table
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_unknown_top_level_field_is_rejected(self) -> None:
        record = built_record()
        record["surprise"] = True
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_unknown_resolution_status_is_rejected(self) -> None:
        record = resolve(built_record(), 0, status="ignored", reviewer="jdoe")
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_not_applicable_without_reason_is_rejected(self) -> None:
        record = resolve(built_record(), 0, status="not_applicable", reviewer="jdoe")
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_not_applicable_with_reason_is_accepted(self) -> None:
        record = resolve(
            built_record(), 0, status="not_applicable", reason="Handwriting is legible.",
            reviewer="jdoe", timestamp="2026-07-20T00:00:00Z",
        )
        validate_review_record(record)

    def test_resolution_without_reviewer_is_rejected(self) -> None:
        record = resolve(built_record(), 0, status="accepted")
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_blank_reviewer_is_rejected(self) -> None:
        record = resolve(built_record(), 0, status="accepted", reviewer="   ")
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)


class YamlRoundTripTest(unittest.TestCase):
    def test_record_round_trips_through_yaml(self) -> None:
        record = resolve(
            built_record(), 0, status="accepted", reviewer="jdoe",
            timestamp="2026-07-20T00:00:00Z",
        )
        loaded = load_yaml(dump_yaml(record))
        self.assertEqual(loaded, record)

    def test_multiline_text_dumps_as_a_readable_block_scalar(self) -> None:
        record = built_record()
        record["semantic_layer"][3]["detailed_figure_description"] = "Line one.\nLine two."
        text = dump_yaml(record)
        self.assertIn("|-", text)
        self.assertEqual(load_yaml(text), record)

    def test_loading_a_non_mapping_is_rejected(self) -> None:
        with self.assertRaises(ReviewRecordError):
            load_yaml("- just\n- a\n- list\n")


class FinalizabilityTest(unittest.TestCase):
    def test_a_record_with_unresolved_warnings_is_not_finalizable(self) -> None:
        record = built_record()
        self.assertFalse(is_finalizable(record))
        self.assertEqual(len(unresolved_warnings(record)), 2)

    def test_fully_resolved_record_is_finalizable(self) -> None:
        record = built_record()
        record["warnings"][0]["resolution"] = {"status": "accepted", "reviewer": "jdoe"}
        record["warnings"][1]["resolution"] = {
            "status": "corrected", "reviewer": "jdoe",
        }
        self.assertTrue(is_finalizable(record))
        self.assertEqual(unresolved_warnings(record), [])


class CommitResolutionsTest(unittest.TestCase):
    def test_commit_fills_reviewer_and_stamps_timestamp(self) -> None:
        record = resolve(built_record(), 0, status="accepted")
        committed = commit_resolutions(
            record, baseline=None, reviewer="jdoe", now="2026-07-20T00:00:00Z"
        )
        resolution = committed["warnings"][0]["resolution"]
        self.assertEqual(resolution["reviewer"], "jdoe")
        self.assertEqual(resolution["timestamp"], "2026-07-20T00:00:00Z")
        # Unresolved warnings stay unresolved.
        self.assertIsNone(committed["warnings"][1]["resolution"])

    def test_commit_does_not_overwrite_an_explicit_reviewer(self) -> None:
        record = resolve(built_record(), 0, status="accepted", reviewer="alice")
        committed = commit_resolutions(
            record, baseline=None, reviewer="jdoe", now="2026-07-20T00:00:00Z"
        )
        self.assertEqual(committed["warnings"][0]["resolution"]["reviewer"], "alice")

    def test_changing_a_resolution_pushes_the_prior_one_into_history(self) -> None:
        first = commit_resolutions(
            resolve(built_record(), 0, status="accepted", reviewer="jdoe"),
            baseline=None, reviewer="jdoe", now="2026-07-20T00:00:00Z",
        )
        edited = resolve(first, 0, status="corrected", reviewer="jdoe")
        second = commit_resolutions(
            edited, baseline=first, reviewer="jdoe", now="2026-07-21T00:00:00Z"
        )
        history = second["warnings"][0]["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "accepted")
        self.assertEqual(history[0]["timestamp"], "2026-07-20T00:00:00Z")
        self.assertEqual(second["warnings"][0]["resolution"]["status"], "corrected")
        self.assertEqual(second["warnings"][0]["resolution"]["timestamp"], "2026-07-21T00:00:00Z")

    def test_recommitting_an_unchanged_resolution_does_not_grow_history(self) -> None:
        first = commit_resolutions(
            resolve(built_record(), 0, status="accepted", reviewer="jdoe"),
            baseline=None, reviewer="jdoe", now="2026-07-20T00:00:00Z",
        )
        second = commit_resolutions(
            first, baseline=first, reviewer="jdoe", now="2026-07-21T00:00:00Z"
        )
        self.assertEqual(second["warnings"][0]["history"], [])
        self.assertEqual(
            second["warnings"][0]["resolution"]["timestamp"], "2026-07-20T00:00:00Z"
        )


class ReviewReportTest(unittest.TestCase):
    def report(self, record: dict[str, Any]) -> str:
        return render_review_report(record)

    def test_report_declares_language_and_has_one_top_level_heading(self) -> None:
        html = self.report(built_record())
        self.assertIn('<html lang="en-US">', html)
        self.assertEqual(html.count("<h1"), 1)
        self.assertIn("<main", html)

    def test_report_lists_the_converted_pages(self) -> None:
        record = build([page_document(1), page_document(2)])
        html = self.report(record)
        self.assertIn("1, 2", html)

    def test_warning_state_is_conveyed_with_text_not_color_alone(self) -> None:
        html = self.report(built_record())
        self.assertIn("Unresolved", html)
        # No colour is used to carry meaning anywhere in the document.
        self.assertNotIn("color", html.lower())

    def test_warnings_render_as_a_semantic_table_with_row_and_column_headers(self) -> None:
        html = self.report(built_record())
        self.assertIn("<table", html)
        self.assertIn('scope="col"', html)
        self.assertIn('scope="row"', html)

    def test_warnings_show_the_page_they_concern(self) -> None:
        html = self.report(built_record())
        self.assertIn("Page 1", html)

    def test_source_region_warnings_show_a_labelled_crop(self) -> None:
        html = self.report(built_record())
        self.assertIn('src="regions/page-1-r0003.png"', html)
        self.assertIn('alt="Source region page-1-r0003', html)

    def test_source_region_shows_the_retained_recognized_text_as_context(self) -> None:
        record = built_record()
        # Point the warning at a candidate that carries recognized text.
        record["warnings"][1]["region"] = "page-1-r0001"
        html = self.report(record)
        self.assertIn("Recognized text: current", html)

    def test_resolved_warnings_show_reviewer_attribution(self) -> None:
        record = resolve(
            built_record(), 0, status="accepted", reviewer="jdoe",
            timestamp="2026-07-20T00:00:00Z",
        )
        record["warnings"][1]["resolution"] = {
            "status": "corrected", "reviewer": "jdoe", "timestamp": "2026-07-20T00:00:00Z",
        }
        html = self.report(record)
        self.assertIn("jdoe", html)
        self.assertNotIn("Unresolved", html)

    def test_report_escapes_untrusted_source_derived_text(self) -> None:
        record = built_record()
        record["semantic_layer"][1]["text"] = "<script>alert(1)</script>"
        html = self.report(record)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class SchemaDriftTest(unittest.TestCase):
    def test_in_code_schema_matches_the_published_json_schema(self) -> None:
        published = json.loads(
            (ROOT / "schemas" / "review-record-2.0.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(published, review_record_schema())


if __name__ == "__main__":
    unittest.main()
