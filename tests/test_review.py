from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from typing import Any

import re

from accessibilizer.review import (
    REVIEW_RECORD_SCHEMA_VERSION,
    REVIEW_REPORT_VERSION,
    ReviewRecordError,
    build_review_record,
    commit_resolutions,
    dump_yaml,
    is_finalizable,
    load_yaml,
    render_review_report,
    review_record_schema,
    review_report_css,
    review_report_javascript,
    unresolved_warnings,
    validate_review_record,
)


def review_data(html: str) -> dict[str, Any]:
    """Parse the embedded review-data JSON payload out of a rendered report."""
    match = re.search(
        r'<script type="application/json" id="review-data">(.*?)</script>',
        html,
        re.S,
    )
    assert match is not None, "report is missing its embedded review-data payload"
    return json.loads(match.group(1))  # type: ignore[no-any-return]


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
        {"id": f"page-{page}-c0001", "type": "text", "text": "current",
         "source_region": f"page-{page}-r0001"},
        {"id": f"page-{page}-c0002", "type": "formula", "text": None,
         "source_region": f"page-{page}-r0003"},
    ]


def source_regions_for(page: int) -> list[dict[str, Any]]:
    return [
        {"id": f"page-{page}-r0000", "page": page, "bbox_points": [0, 0, 612, 792]},
        {"id": f"page-{page}-r0001", "page": page, "bbox_points": [10, 10, 590, 120]},
        {"id": f"page-{page}-r0002", "page": page, "bbox_points": [10, 130, 590, 250]},
        {"id": f"page-{page}-r0003", "page": page, "bbox_points": [10, 260, 590, 360]},
        {"id": f"page-{page}-r0004", "page": page, "bbox_points": [10, 370, 590, 520]},
        {"id": f"page-{page}-r0005", "page": page, "bbox_points": [10, 530, 590, 780]},
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
        "semantic_nodes": ["page-1-s0003"],
        "source_regions": ["page-1-r0003"],
    },
]


def page_document(page: int = 1, **overrides: Any) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [HEADING, PARAGRAPH, FORMULA, FIGURE, TABLE]
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "page": page,
        "source_sha256": "a" * 64,
        "title": "Electric Current",
        "language": "en-US",
        "page_dimensions": {"width_points": 612, "height_points": 792},
        "source_regions": source_regions_for(page),
        "semantic_layer": [
            {
                **node,
                "id": f"page-{page}-s{index:04d}",
                "source_regions": [f"page-{page}-r{index:04d}"],
            }
            for index, node in enumerate(nodes, start=1)
        ],
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


def semantic_node(node: dict[str, Any], index: int, page: int = 1) -> dict[str, Any]:
    return {
        **copy.deepcopy(node),
        "id": f"page-{page}-s{index:04d}",
        "page": page,
        "source_regions": [f"page-{page}-r0001"],
    }


class BuildRecordTest(unittest.TestCase):
    def test_record_carries_identity_layer_candidates_and_provenance(self) -> None:
        record = built_record()
        self.assertEqual(record["schema_version"], REVIEW_RECORD_SCHEMA_VERSION)
        self.assertEqual(record["pages"], [1])
        self.assertEqual(record["page_dimensions"][0]["width_points"], 612)
        self.assertEqual(record["source_regions"], source_regions_for(1))
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
        self.assertEqual(record["warnings"][0]["semantic_nodes"], [])
        self.assertEqual(record["warnings"][0]["source_regions"], [])
        self.assertEqual(record["warnings"][1]["semantic_nodes"], ["page-1-s0003"])
        self.assertEqual(record["warnings"][1]["source_regions"], ["page-1-r0003"])

    def test_multiple_pages_flatten_into_one_ordered_document(self) -> None:
        record = build([
            page_document(1, warnings=[copy.deepcopy(WARNINGS[0])]),
            page_document(2, warnings=[copy.deepcopy(WARNINGS[0])]),
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
        self.assertEqual(review_record_schema()["properties"]["schema_version"]["const"], "3.0")

    def test_duplicate_source_region_node_candidate_and_warning_ids_are_rejected(self) -> None:
        for collection in ("source_regions", "semantic_layer", "candidates", "warnings"):
            with self.subTest(collection=collection):
                record = built_record()
                record[collection].append(copy.deepcopy(record[collection][0]))
                with self.assertRaises(ReviewRecordError):
                    validate_review_record(record)

    def test_source_region_bounds_must_be_finite_nonnegative_ordered_and_on_page(self) -> None:
        invalid_bounds = (
            [-1, 0, 10, 10],
            [0, 0, 0, 10],
            [0, 10, 10, 9],
            [0, 0, 613, 10],
            [0, 0, 10, float("inf")],
        )
        for bounds in invalid_bounds:
            with self.subTest(bounds=bounds):
                record = built_record()
                record["source_regions"][0]["bbox_points"] = bounds
                with self.assertRaises(ReviewRecordError):
                    validate_review_record(record)

    def test_a_record_may_omit_the_whole_page_fallback_region(self) -> None:
        record = built_record()
        record["source_regions"] = [
            region
            for region in record["source_regions"]
            if region["id"] != "page-1-r0000"
        ]

        validate_review_record(record)

    def test_a_whole_page_fallback_region_must_use_exact_bounds(self) -> None:
        record = built_record()
        fallback = next(
            region
            for region in record["source_regions"]
            if region["id"] == "page-1-r0000"
        )
        fallback["bbox_points"] = [0, 0, 611, 792]

        with self.assertRaisesRegex(ReviewRecordError, "whole-page fallback"):
            validate_review_record(record)

    def test_reconstruction_must_describe_every_converted_page_exactly_once(self) -> None:
        for mutation in ("missing", "duplicate"):
            with self.subTest(mutation=mutation):
                record = built_record()
                existing = copy.deepcopy(record["reconstruction"]["pages"][0])
                record["reconstruction"]["pages"] = (
                    [] if mutation == "missing" else [existing, copy.deepcopy(existing)]
                )
                with self.assertRaisesRegex(ReviewRecordError, "reconstruction"):
                    validate_review_record(record)

    def test_semantic_node_references_must_be_nonempty_resolved_and_same_page(self) -> None:
        record = build([page_document(1), page_document(2)])
        node = record["semantic_layer"][0]
        for references in ([], ["page-1-r9999"], ["page-2-r0001"]):
            with self.subTest(references=references):
                changed = copy.deepcopy(record)
                changed["semantic_layer"][0]["source_regions"] = references
                with self.assertRaises(ReviewRecordError):
                    validate_review_record(changed)

    def test_candidate_reference_must_resolve_and_match_its_id_page(self) -> None:
        record = build([page_document(1), page_document(2)])
        record["candidates"][0]["source_region"] = "page-2-r0001"
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_record_may_have_no_recognition_candidates(self) -> None:
        record = build([page_document(1, candidates=[])])
        self.assertEqual(record["candidates"], [])
        validate_review_record(record)

    def test_warning_references_must_resolve_and_agree_with_its_page(self) -> None:
        record = build([
            page_document(1, warnings=copy.deepcopy(WARNINGS)),
            page_document(2),
        ])
        record["warnings"][1]["source_regions"] = ["page-2-r0001"]
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_page_and_document_wide_warnings_may_have_no_references(self) -> None:
        record = built_record()
        record["warnings"][0]["page"] = None
        validate_review_record(record)

    def test_a_heading_hierarchy_of_levels_one_through_six_validates(self) -> None:
        record = built_record()
        for level in range(1, 7):
            node = semantic_node(
                {"type": "heading", "level": level, "text": f"Level {level}"},
                6 + level,
            )
            record["semantic_layer"].append(node)
        validate_review_record(record)

    def test_a_heading_level_outside_one_through_six_is_rejected(self) -> None:
        record = built_record()
        record["semantic_layer"][0] = semantic_node(
            {"type": "heading", "level": 7, "text": "x"}, 1
        )
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_link_node_validates_and_requires_text_and_href(self) -> None:
        record = built_record()
        record["semantic_layer"].append(semantic_node(LINK, 6))
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
        record["semantic_layer"][3] = semantic_node(SIMPLE_FIGURE, 4)
        validate_review_record(record)

    def test_a_simple_figure_carrying_a_detailed_description_is_rejected(self) -> None:
        record = built_record()
        record["semantic_layer"][3] = semantic_node(
            {
                **copy.deepcopy(SIMPLE_FIGURE),
                "detailed_figure_description": "A simple figure should not carry this.",
            },
            4,
        )
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
        table = semantic_node(TABLE, 5)
        del table["caption"]
        record["semantic_layer"][4] = table
        validate_review_record(record)

    def test_a_data_cell_carrying_a_scope_is_rejected(self) -> None:
        record = built_record()
        table = semantic_node(TABLE, 5)
        table["rows"][1]["cells"][1]["scope"] = "col"
        record["semantic_layer"][4] = table
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_header_cell_without_a_scope_is_rejected(self) -> None:
        record = built_record()
        table = semantic_node(TABLE, 5)
        table["rows"][0]["cells"][0]["scope"] = "none"
        record["semantic_layer"][4] = table
        with self.assertRaises(ReviewRecordError):
            validate_review_record(record)

    def test_a_table_without_rows_is_rejected(self) -> None:
        record = built_record()
        table = semantic_node(TABLE, 5)
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
        self.assertIn('<html lang="en-US"', html)
        self.assertEqual(html.count("<h1"), 1)
        self.assertIn("<main", html)

    def test_report_requires_javascript_and_loads_only_relative_local_assets(self) -> None:
        html = self.report(built_record())
        self.assertIn('<html lang="en-US" class="no-js">', html)
        self.assertIn("<noscript>", html)
        self.assertIn("requires JavaScript", html)
        self.assertIn('<link rel="stylesheet" href="review-report.css">', html)
        self.assertIn('<script src="review-report.js"></script>', html)
        # No external, remote, or absolute references at view time.
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)
        # The interactive shell is hidden until the script reveals it.
        self.assertIn('class="report-app" aria-labelledby="component-navigator-heading" hidden', html)

    def test_css_and_js_are_offline_dependency_free_assets(self) -> None:
        css = review_report_css()
        js = review_report_javascript()
        self.assertNotIn("http://", css)
        self.assertNotIn("https://", css)
        # The client script never opens a network connection or loads code. The
        # only URL it names is the SVG namespace required by createElementNS.
        self.assertNotIn("fetch(", js)
        self.assertNotIn("XMLHttpRequest", js)
        self.assertNotIn("import(", js)
        self.assertNotIn("https://", js)
        self.assertEqual(js.count("http://"), js.count("http://www.w3.org/2000/svg"))
        self.assertIn("review-data", js)

    def test_report_lists_the_converted_pages_in_a_metadata_disclosure(self) -> None:
        record = build([page_document(1), page_document(2)])
        html = self.report(record)
        self.assertIn("<details class=\"doc-metadata\">", html)
        self.assertIn("1, 2", html)

    def test_warning_state_is_conveyed_with_text_not_color_alone(self) -> None:
        html = self.report(built_record())
        self.assertIn("Unresolved", html)
        # No colour word carries meaning in the generated HTML (styling lives in
        # the separate stylesheet).
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

    def test_warning_table_shows_every_referenced_source_region(self) -> None:
        record = built_record()
        record["warnings"][1]["source_regions"] = ["page-1-r0003", "page-1-r0004"]

        html = self.report(record)

        self.assertIn("<span> Region page-1-r0003", html)
        self.assertIn("<span> Region page-1-r0004", html)

    def test_source_region_shows_the_retained_recognized_text_as_context(self) -> None:
        record = built_record()
        # Point the warning at a candidate that carries recognized text.
        record["warnings"][1]["source_regions"] = ["page-1-r0001"]
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
        # Neither the visible Component panel nor the embedded data payload lets
        # the source-derived string become executable markup.
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("\\u003cscript\\u003e", html)
        # The parsed payload still round-trips the real text for announcements.
        data = review_data(html)
        paragraph = next(c for c in data["components"] if c["id"] == "page-1-s0002")
        self.assertEqual(paragraph["atContent"], "<script>alert(1)</script>")

    def test_link_component_exposes_text_and_destination_without_an_active_remote_resource(self) -> None:
        record = built_record()
        record["semantic_layer"].append(semantic_node(LINK, 6))

        html = self.report(record)

        self.assertIn("<dt>Link text</dt><dd>Ohm&#x27;s Law</dd>", html)
        self.assertIn("<dt>Destination</dt><dd><code>https://example.org/ohm</code></dd>", html)
        self.assertNotIn('href="https://example.org/ohm"', html)

    def test_components_show_heading_level_and_table_cell_spans_in_all_details(self) -> None:
        record = built_record()
        table = next(
            node for node in record["semantic_layer"] if node["type"] == "table"
        )
        table["rows"][0]["cells"][0]["row_span"] = 2
        table["rows"][0]["cells"][0]["col_span"] = 3

        html = self.report(record)

        self.assertIn("<dt>Heading level</dt><dd>1</dd>", html)
        self.assertIn('rowspan="2" colspan="3"', html)

    def test_every_semantic_node_becomes_a_component_in_reading_order(self) -> None:
        record = build([page_document(1), page_document(2)])
        html = self.report(record)
        expected = [node["id"] for node in record["semantic_layer"]]
        found = re.findall(r'<article id="([^"]+)" class="component"', html)
        self.assertEqual(found, expected)
        data = review_data(html)
        self.assertEqual([c["id"] for c in data["components"]], expected)
        self.assertEqual([c["index"] for c in data["components"]], list(range(len(expected))))

    def test_embedded_data_carries_page_geometry_and_region_boxes(self) -> None:
        record = built_record()
        html = self.report(record)
        data = review_data(html)
        self.assertEqual(data["reportVersion"], REVIEW_REPORT_VERSION)
        self.assertEqual(data["pageDimensions"]["1"], {"width": 612.0, "height": 792.0})
        self.assertEqual(data["regions"]["page-1-r0001"]["bbox"], [10.0, 10.0, 590.0, 120.0])
        self.assertEqual(data["regions"]["page-1-r0001"]["page"], 1)
        formula = next(c for c in data["components"] if c["id"] == "page-1-s0003")
        self.assertEqual(formula["page"], 1)
        self.assertEqual(formula["typeLabel"], "Formula")
        self.assertEqual(formula["regions"], ["page-1-r0003"])

    def test_component_concise_view_shows_the_specified_fields(self) -> None:
        html = self.report(built_record())
        # Type, page, primary AT content, warning status, and region count are all
        # present outside the disclosure.
        self.assertIn('<h3 class="component-type">Formula</h3>', html)
        self.assertIn("Assistive-technology content:", html)
        self.assertIn("I equals Q divided by delta t.", html)
        self.assertIn("attached Conversion Warning(s)", html)
        self.assertIn("referenced Source Region(s)", html)
        self.assertIn('<details class="all-details"><summary>All details</summary>', html)

    def test_report_associates_every_warning_scope_without_array_position_inference(self) -> None:
        record = built_record()
        record["warnings"].append({
            "id": "w0003", "code": "region-only", "message": "Region concern.",
            "page": 1, "semantic_nodes": [], "source_regions": ["page-1-r0004"],
            "resolution": None, "history": [],
        })
        html = self.report(record)
        data = review_data(html)
        by_id = {c["id"]: c for c in data["components"]}
        # The formula owns w0002 by node/region; the figure owns w0003 by region.
        self.assertEqual(by_id["page-1-s0003"]["warningIds"], ["w0002"])
        self.assertEqual(by_id["page-1-s0004"]["warningIds"], ["w0003"])
        # An unreferenced page warning marks no Component.
        self.assertEqual(by_id["page-1-s0001"]["warningIds"], [])
        self.assertIn('data-warning-ids="w0002"', html)
        self.assertIn('data-warning-ids="w0003"', html)

    def test_components_show_explicitly_attached_and_region_warnings(self) -> None:
        record = built_record()
        record["warnings"].append({
            "id": "w0003", "code": "region-only", "message": "Inspect this figure region.",
            "page": 1, "semantic_nodes": [], "source_regions": ["page-1-r0004"],
            "resolution": None, "history": [],
        })

        html = self.report(record)

        self.assertIn(
            '<article id="page-1-s0003" class="component" data-index="2" '
            'data-page="1" data-regions="page-1-r0003" data-warning-ids="w0002"',
            html,
        )
        self.assertIn(
            '<article id="page-1-s0004" class="component" data-index="3" '
            'data-page="1" data-regions="page-1-r0004" data-warning-ids="w0003"',
            html,
        )
        self.assertIn('href="#warning-w0002">w0002: recognition-disagreement</a>', html)
        self.assertIn('href="#warning-w0003">w0003: region-only</a>', html)
        self.assertIn('id="warning-w0003"', html)

    def test_document_wide_warnings_are_reachable_and_never_fake_components(self) -> None:
        record = built_record()
        # Promote the reference-free warning to a document-wide one.
        record["warnings"][0]["page"] = None
        html = self.report(record)
        data = review_data(html)
        # It appears in the always-reachable warnings summary, not as a Component.
        self.assertIn('href="#warning-w0001">w0001: ambiguous-reading-order</a>', html)
        self.assertNotIn("w0001", [wid for c in data["components"] for wid in c["warningIds"]])
        self.assertEqual(len(data["components"]), len(record["semantic_layer"]))

    def test_page_level_unreferenced_warnings_appear_in_the_scope_summary(self) -> None:
        html = self.report(built_record())
        # w0001 (page 1, no node or region references) is a page-scoped warning:
        # it belongs in the always-reachable summary, labelled by page, and is
        # never turned into a Component.
        self.assertIn('Page 1: <a href="#warning-w0001"', html)
        data = review_data(html)
        self.assertNotIn(
            "w0001", [wid for c in data["components"] for wid in c["warningIds"]]
        )

    def test_report_has_an_accessible_warnings_filter_and_disclosures(self) -> None:
        html = self.report(built_record())
        self.assertIn('id="warnings-only"', html)
        self.assertIn('aria-pressed="false"', html)
        self.assertIn("<details", html)
        self.assertIn("Recognition Candidates", html)
        self.assertIn('id="filter-empty"', html)

    def test_splitter_is_an_accessible_separator_with_a_reset(self) -> None:
        html = self.report(built_record())
        self.assertIn('role="separator"', html)
        self.assertIn('aria-orientation="vertical"', html)
        self.assertIn('id="reset-panes"', html)
        self.assertIn('role="toolbar"', html)


class SchemaDriftTest(unittest.TestCase):
    def test_in_code_schema_matches_the_published_json_schema(self) -> None:
        published = json.loads(
            (ROOT / "schemas" / "review-record-3.0.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(published, review_record_schema())


if __name__ == "__main__":
    unittest.main()
