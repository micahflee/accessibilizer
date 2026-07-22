"""The full-sample gold Review Record is the acceptance oracle for the supplied
11-page electrical-current notes (issue #14, parent #1).

These tests do not compare model output; they guard the *oracle itself* so it
cannot silently drift away from the schema, the sample it describes, or the
internal semantic invariants that a conforming conversion must satisfy. The
record was approved as gold by the maintainer (ADR 0020) and must keep
describing the approved content while issue #41 migrates its source evidence to
Review Record 3.0. The legacy fixture is deliberately not accepted by the runtime.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import unittest

from accessibilizer.checkpoint import file_sha256
from accessibilizer.review import (
    REVIEW_RECORD_SCHEMA_VERSION,
    ReviewRecordError,
    load_yaml,
    validate_review_record,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"
GOLD = ROOT / "testdata" / "gold-review-record.yaml"

EXPECTED_PAGES = list(range(1, 12))

# Legacy identity patterns retained until issue #41 migrates the gold fixture.
REGION_ID = re.compile(r"^page-[0-9]+-r[0-9]{4,}$")
WARNING_ID = re.compile(r"^w[0-9]{4,}$")
# The page-6 copper resistivity is defined with this exponent but substituted with
# 10^-8; the oracle must preserve it verbatim rather than "correcting" the source.
SOURCE_ERROR_EXPONENT = "10^{-9}"


class GoldReviewRecordTests(unittest.TestCase):
    record: dict[str, Any]
    candidates_by_id: dict[str, dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.record = load_yaml(GOLD.read_text(encoding="utf-8"))
        cls.candidates_by_id = {c["id"]: c for c in cls.record["candidates"]}

    def test_legacy_gold_record_is_not_accepted_as_review_record_3(self) -> None:
        self.assertEqual(self.record["schema_version"], "2.0")
        self.assertEqual(REVIEW_RECORD_SCHEMA_VERSION, "3.0")
        with self.assertRaises(ReviewRecordError):
            validate_review_record(self.record)

    def test_gold_record_describes_every_sample_page(self) -> None:
        self.assertEqual(self.record["pages"], EXPECTED_PAGES)
        reconstructed = [page["page"] for page in self.record["reconstruction"]["pages"]]
        self.assertEqual(reconstructed, EXPECTED_PAGES)

    def test_gold_record_is_bound_to_the_immutable_sample_pdf(self) -> None:
        # The oracle only means anything against the exact document it describes.
        self.assertEqual(self.record["source_sha256"], file_sha256(SOURCE))

    def test_document_structure_identifies_title_language_and_headings(self) -> None:
        self.assertTrue(self.record["title"].strip())
        self.assertEqual(self.record["language"], "en")
        headings = [n for n in self.record["semantic_layer"] if n["type"] == "heading"]
        self.assertTrue(any(h["level"] == 1 for h in headings), "expected a document title heading")

    def test_every_semantic_node_names_a_converted_page(self) -> None:
        pages = set(self.record["pages"])
        node_pages = {node["page"] for node in self.record["semantic_layer"]}
        self.assertTrue(node_pages.issubset(pages))
        # Every page contributes at least one semantic node; no page is silently empty.
        self.assertEqual(node_pages, pages)

    def test_reading_order_covers_every_supported_node_kind(self) -> None:
        # The oracle exercises the full Semantic Layer vocabulary the pipeline can
        # author, so an authoring regression for any kind is caught by acceptance.
        kinds = {node["type"] for node in self.record["semantic_layer"]}
        self.assertEqual(
            kinds,
            {"heading", "paragraph", "formula", "figure", "table"},
        )

    def test_every_required_alternative_is_present(self) -> None:
        missing: list[str] = []
        for node in self.record["semantic_layer"]:
            if node["type"] == "formula" and not node.get("spoken_math_alternative"):
                missing.append("formula spoken_math_alternative")
            if node["type"] == "figure":
                if not node.get("figure_alternative"):
                    missing.append("figure_alternative")
                if node.get("complexity") == "complex" and not node.get(
                    "detailed_figure_description"
                ):
                    missing.append("detailed_figure_description")
        self.assertEqual(missing, [])

    def test_semantic_tables_carry_scoped_header_relationships(self) -> None:
        tables = [n for n in self.record["semantic_layer"] if n["type"] == "table"]
        self.assertTrue(tables, "the sample contains printed Semantic Tables")
        for table in tables:
            cells = [cell for row in table["rows"] for cell in row["cells"]]
            self.assertTrue(
                any(cell["kind"] == "header" for cell in cells),
                "a table has no header cell",
            )
            for cell in cells:
                if cell["kind"] == "header":
                    self.assertNotEqual(cell["scope"], "none", "a header cell has no scope")

    def test_recognition_agreement_is_grounded_for_every_region_node(self) -> None:
        # Every Formula, Figure, and Semantic Table sits on a page whose
        # reconstruction cross-checked an independent crop of that type.
        verified: dict[int, set[str]] = {
            page["page"]: {region["type"] for region in page["verified_regions"]}
            for page in self.record["reconstruction"]["pages"]
        }
        ungrounded = [
            f"{node['type']} on page {node['page']}"
            for node in self.record["semantic_layer"]
            if node["type"] in {"formula", "figure", "table"}
            and node["type"] not in verified.get(node["page"], set())
        ]
        self.assertEqual(ungrounded, [])

    def test_verified_region_ids_resolve_to_retained_candidate_crops(self) -> None:
        for page in self.record["reconstruction"]["pages"]:
            for region in page["verified_regions"]:
                self.assertIn(region["id"], self.candidates_by_id)

    def test_every_semantic_node_is_source_linked_to_a_crop(self) -> None:
        # Every expected semantic — text, headings, and grounded regions alike — is
        # connected to visual evidence, so nothing in the oracle is unverifiable.
        self.assertEqual(len(self.candidates_by_id), len(self.record["candidates"]))
        self.assertEqual(len(self.record["candidates"]), len(self.record["semantic_layer"]))
        for candidate in self.record["candidates"]:
            self.assertRegex(candidate["id"], REGION_ID)
            self.assertTrue(candidate["crop"].endswith(".png"))
            self.assertIn(candidate["id"], candidate["crop"])

    def test_warnings_are_source_linked_and_start_unresolved(self) -> None:
        self.assertTrue(self.record["warnings"], "the oracle records expected warnings")
        for warning in self.record["warnings"]:
            self.assertRegex(warning["id"], WARNING_ID)
            self.assertIsNone(warning["resolution"], "expected warnings start unresolved")
            self.assertEqual(warning["history"], [])
            self.assertIn(warning["page"], self.record["pages"])
            region = warning["region"]
            if region is not None:
                self.assertIn(
                    region,
                    self.candidates_by_id,
                    "a warning names a source region with no retained crop",
                )

    def test_genuine_ambiguities_remain_explicit(self) -> None:
        # The oracle must not silently guess: the real ambiguities in the sample
        # are recorded as the Conversion Warnings a faithful conversion must raise.
        codes = {warning["code"] for warning in self.record["warnings"]}
        self.assertIn("suspected-source-error", codes)  # page 6 copper resistivity exponent
        self.assertIn("ambiguous-reading-order", codes)  # multi-column pages
        self.assertIn("table-merged-cells", codes)  # Table 25-1 section rows

    def test_reading_order_ambiguity_is_declared_consistently_with_its_warnings(self) -> None:
        # A page's reconstruction and its warnings must agree: exactly the pages
        # marked ambiguous carry an ambiguous-reading-order warning, matching how
        # the pipeline (reconcile_page) raises one only when the order is uncertain.
        ambiguous_pages = {
            page["page"]
            for page in self.record["reconstruction"]["pages"]
            if not page["reading_order_is_unambiguous"]
        }
        warned_pages = {
            warning["page"]
            for warning in self.record["warnings"]
            if warning["code"] == "ambiguous-reading-order"
        }
        self.assertEqual(ambiguous_pages, warned_pages)

    def test_the_suspected_source_error_preserves_the_original_faithfully(self) -> None:
        # Page 6 defines rho_cu with a 10^-9 exponent but substitutes 10^-8; the
        # oracle preserves the source verbatim and flags it rather than "fixing" it.
        source_errors = [
            w for w in self.record["warnings"] if w["code"] == "suspected-source-error"
        ]
        self.assertTrue(source_errors)
        preserved = any(
            SOURCE_ERROR_EXPONENT in self.candidates_by_id[w["region"]]["text"]
            for w in source_errors
            if w["region"] in self.candidates_by_id
        )
        self.assertTrue(preserved, "the flagged region must preserve the source's 10^-9 exponent")


if __name__ == "__main__":
    unittest.main()
