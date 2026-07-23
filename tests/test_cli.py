from __future__ import annotations

import argparse
import copy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from accessibilizer import review
from accessibilizer.cli import (
    _authoring_contract,
    _generate_report,
    _internal_checks,
    _parse_page_selection,
    _publish_protected_directory,
    _report,
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
            {"id": f"page-{page}-r0000", "page": page, "bbox_points": [0, 0, 612, 792]},
            *[
            {"id": identifier, "page": page, "bbox_points": [10, 10, 600, 780]}
            for identifier in region_ids
            ],
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
    @staticmethod
    def recognition_document(region_id: str, candidate_type: str) -> dict[str, Any]:
        return {
            "recognition": {
                "backend": "fake", "backend_version": "1.0",
                "weights_version": "fake-weights-1.0",
            },
            "proposal_generation": {
                "algorithm": "hybrid-source-regions", "algorithm_version": "1.0",
                "deduplication_pixels": 112,
                "max_nonfallback_area_ratio": 0.8,
                "sources": ["recognition"],
            },
            "source_regions": [
                {"id": "page-1-r0000", "bbox_points": [0, 0, 72, 72]},
                {"id": region_id, "bbox_points": [0, 0, 24, 24]},
            ],
            "candidates": [{
                "id": "page-1-c0001", "source_region": region_id,
                "type": candidate_type, "raw_class": candidate_type,
                "backend": f"fake-{candidate_type}", "text": None,
                "verification": {"eligible": True, "reason_codes": []},
            }],
        }

    def test_whole_page_fallback_warns_with_node_and_region_references(self) -> None:
        document = page_document(1)
        document["semantic_layer"][0]["source_regions"] = ["page-1-r0000"]
        for verified in document["reconstruction"]["verified_regions"]:
            verified["id"] = verified.pop("source_region")
        recognition_document = self.recognition_document(
            "page-1-r0001", "document_structure"
        )
        reviewed = _review_page_document(
            document, recognition_document, (72.0, 72.0)
        )

        warning = next(w for w in reviewed["warnings"] if w["code"] == "imprecise-source-grounding")
        self.assertEqual(warning["semantic_nodes"], ["page-1-s0001"])
        self.assertEqual(warning["source_regions"], ["page-1-r0000"])

    def test_node_specific_warning_references_survive_record_construction(self) -> None:
        document = page_document(1)
        document["warnings"] = [{
            "code": "formula-spoken-fidelity",
            "message": "Review the Spoken Math Alternative.",
            "status": "unresolved",
            "semantic_types": ["formula"],
            "source_regions": ["page-1-r0003"],
        }]
        for verified in document["reconstruction"]["verified_regions"]:
            verified["id"] = verified.pop("source_region")
        recognition_document = self.recognition_document("page-1-r0003", "formula")
        reviewed = _review_page_document(
            document, recognition_document, (72.0, 72.0)
        )

        warning = reviewed["warnings"][0]
        self.assertEqual(warning["semantic_nodes"], ["page-1-s0003"])
        self.assertEqual(warning["source_regions"], ["page-1-r0003"])
        self.assertNotIn("semantic_types", warning)


class ReportCommandTest(unittest.TestCase):
    @staticmethod
    def arguments(**overrides: Any) -> argparse.Namespace:
        values: dict[str, Any] = {
            "bundle": None,
            "source": None,
            "record": None,
            "output": None,
            "replace": False,
            "json": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    @staticmethod
    def fake_generate(source: Path, record: dict[str, Any], output: Path) -> None:
        del source, record
        (output / "regions").mkdir(parents=True)
        (output / "regions" / "new.png").write_bytes(b"new region")
        (output / "review-report.html").write_text("new report", encoding="utf-8")

    @staticmethod
    def fake_render_regions(
        source: Path, record: dict[str, Any], regions: Path
    ) -> None:
        del source
        regions.mkdir(parents=True, exist_ok=True)
        for page in record["pages"]:
            (regions / f"page-{page}.png").write_bytes(b"page")
        for region in record["source_regions"]:
            (regions / f"{region['id']}.png").write_bytes(b"region")

    @staticmethod
    def write_source_and_record(directory: Path) -> tuple[Path, Path]:
        source = directory / "source.pdf"
        source.write_bytes(b"source")
        record = record_for([1])
        record["source_sha256"] = hashlib.sha256(b"source").hexdigest()
        record_path = directory / "review-record.yaml"
        record_path.write_text(review.dump_yaml(record), encoding="utf-8")
        return source, record_path

    def test_standalone_report_creates_a_nested_output_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            source, record_path = self.write_source_and_record(temporary)
            output = temporary / "nested" / "gold-report"

            with patch(
                "accessibilizer.cli._render_report_regions",
                side_effect=self.fake_render_regions,
            ):
                result = _report(self.arguments(
                    source=source, record=record_path, output=output
                ))

            self.assertEqual(result, 0)
            html = (output / "review-report.html").read_text(encoding="utf-8")
            # The interactive report references its Source Region crops and its
            # relative local stylesheet and script; the full page image is loaded
            # by the script at view time from the review-data geometry.
            self.assertIn('src="regions/page-1-r', html)
            self.assertIn('href="review-report.css"', html)
            self.assertIn('src="review-report.js"', html)
            self.assertNotIn("https://", html)
            self.assertTrue((output / "review-report.css").is_file())
            self.assertTrue((output / "review-report.js").is_file())

    def test_bundle_report_preserves_non_report_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "example.accessibilizer"
            bundle.mkdir()
            source, record_path = self.write_source_and_record(bundle)
            self.assertEqual(source, bundle / "source.pdf")
            self.assertEqual(record_path, bundle / "review-record.yaml")
            (bundle / "reviewer-note.txt").write_text("keep me", encoding="utf-8")

            with patch(
                "accessibilizer.cli._render_report_regions",
                side_effect=self.fake_render_regions,
            ):
                result = _report(self.arguments(bundle=bundle))

            self.assertEqual(result, 0)
            self.assertEqual((bundle / "reviewer-note.txt").read_text(), "keep me")
            self.assertTrue((bundle / "review-report.html").is_file())
            self.assertTrue((bundle / "regions" / "page-1.png").is_file())

    def test_bundle_regeneration_rolls_back_both_artifacts_if_publication_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "example.accessibilizer"
            (bundle / "regions").mkdir(parents=True)
            (bundle / "review-report.html").write_text("old report", encoding="utf-8")
            (bundle / "regions" / "old.png").write_bytes(b"old region")
            (bundle / "source.pdf").write_bytes(b"source")
            (bundle / "review-record.yaml").write_text("record", encoding="utf-8")
            real_replace = os.replace
            publication_attempts = 0

            def fail_publication(source: Path | str, destination: Path | str) -> None:
                nonlocal publication_attempts
                if Path(destination) in {bundle, bundle / "regions"}:
                    publication_attempts += 1
                    if publication_attempts == 1:
                        raise OSError("simulated publication failure")
                real_replace(source, destination)

            with (
                patch("accessibilizer.cli._load_bundle_record", return_value=({}, bundle / "review-record.yaml")),
                patch("accessibilizer.cli._generate_report", side_effect=self.fake_generate),
                patch("accessibilizer.cli.ctypes.CDLL", side_effect=AttributeError("renameat2 unavailable")),
                patch("accessibilizer.cli.os.replace", side_effect=fail_publication),
                self.assertRaisesRegex(OSError, "simulated publication failure"),
            ):
                _report(self.arguments(bundle=bundle))

            self.assertEqual((bundle / "review-report.html").read_text(), "old report")
            self.assertEqual((bundle / "regions" / "old.png").read_bytes(), b"old region")
            self.assertFalse((bundle / "regions" / "new.png").exists())

    def test_protected_publication_recovers_an_interrupted_fallback_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            published = temporary / "report"
            transaction = temporary / ".report.replacement"
            previous = transaction / "previous"
            staging = temporary / "staging"
            previous.mkdir(parents=True)
            (transaction / "accessibilizer-transaction").write_text(
                "accessibilizer protected directory replacement 1\n", encoding="utf-8"
            )
            (previous / "version.txt").write_text("old", encoding="utf-8")
            staging.mkdir()
            (staging / "version.txt").write_text("new", encoding="utf-8")

            with patch(
                "accessibilizer.cli._exchange_directories", return_value=False
            ):
                _publish_protected_directory(staging, published, True)

            self.assertEqual((published / "version.txt").read_text(), "new")
            self.assertFalse(transaction.exists())

    def test_protected_publication_refuses_an_unowned_recovery_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            published = temporary / "report"
            published.mkdir()
            staging = temporary / "staging"
            staging.mkdir()
            unowned = temporary / ".report.replacement"
            unowned.mkdir()
            (unowned / "user-data.txt").write_text("keep me", encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError, "ambiguous protected-publication state"
            ):
                _publish_protected_directory(staging, published, False)

            self.assertEqual((unowned / "user-data.txt").read_text(), "keep me")

    def test_report_crop_geometry_rounds_both_pixel_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            source = temporary / "source.pdf"
            source.write_bytes(b"source")
            record = record_for([1])
            record["source_sha256"] = hashlib.sha256(b"source").hexdigest()
            region = next(
                item for item in record["source_regions"] if item["id"] == "page-1-r0001"
            )
            region["bbox_points"] = [0.24, 0.24, 0.76, 0.76]
            commands: list[list[str]] = []

            def succeed(command: list[str]) -> SimpleNamespace:
                commands.append(command)
                if "-x" not in command:
                    Path(command[-1]).with_suffix(".png").write_bytes(b"page")
                return SimpleNamespace(returncode=0, stderr="")

            with patch("accessibilizer.cli._run", side_effect=succeed):
                _generate_report(source, record, temporary / "report")

            crop = next(command for command in commands if "-x" in command)
            self.assertEqual(crop[crop.index("-x") + 1], "0")
            self.assertEqual(crop[crop.index("-W") + 1], "2")


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
