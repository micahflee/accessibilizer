"""The full-sample gold Review Record migration fixture describes the supplied
11-page electrical-current notes (issue #41, parent #37).

These tests do not compare model output; they guard the fixture so it
cannot silently drift away from the schema, the sample it describes, or the
internal semantic invariants that a conforming conversion must satisfy. The
record remains a draft until the maintainer approval tracked by issue #15; this
migration does not claim that approval.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any
import unittest

from accessibilizer.checkpoint import file_sha256
from accessibilizer.review import (
    REVIEW_RECORD_SCHEMA_VERSION,
    load_yaml,
    validate_review_record,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"
GOLD = ROOT / "testdata" / "gold-review-record.yaml"

EXPECTED_PAGES = list(range(1, 12))

REGION_ID = re.compile(r"^page-[0-9]+-r[0-9]{4,}$")
NODE_ID = re.compile(r"^page-[0-9]+-s[0-9]{4,}$")
WARNING_ID = re.compile(r"^w[0-9]{4,}$")
# The page-6 copper resistivity is defined with this exponent but substituted with
# 10^-8; the fixture must preserve it verbatim rather than "correcting" the source.
SOURCE_ERROR_EXPONENT = "10^{-9}"


class GoldReviewRecordTests(unittest.TestCase):
    record: dict[str, Any]
    regions_by_id: dict[str, dict[str, Any]]
    nodes_by_id: dict[str, dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.record = load_yaml(GOLD.read_text(encoding="utf-8"))
        cls.regions_by_id = {r["id"]: r for r in cls.record["source_regions"]}
        cls.nodes_by_id = {n["id"]: n for n in cls.record["semantic_layer"]}

    def test_gold_record_is_valid_review_record_3(self) -> None:
        self.assertEqual(self.record["schema_version"], "3.0")
        self.assertEqual(REVIEW_RECORD_SCHEMA_VERSION, "3.0")
        validate_review_record(self.record)

    def test_gold_record_describes_every_sample_page(self) -> None:
        self.assertEqual(self.record["pages"], EXPECTED_PAGES)
        reconstructed = [page["page"] for page in self.record["reconstruction"]["pages"]]
        self.assertEqual(reconstructed, EXPECTED_PAGES)

    def test_gold_record_is_bound_to_the_immutable_sample_pdf(self) -> None:
        # The fixture only means anything against the exact document it describes.
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
        # The fixture exercises the full Semantic Layer vocabulary the pipeline can
        # author, so an authoring regression for any kind is caught here.
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

    def test_verified_regions_resolve_to_source_evidence(self) -> None:
        for page in self.record["reconstruction"]["pages"]:
            for region in page["verified_regions"]:
                self.assertIn(region["source_region"], self.regions_by_id)

    def test_every_semantic_node_has_stable_identity_and_nonfallback_evidence(
        self,
    ) -> None:
        node_ids = [node["id"] for node in self.record["semantic_layer"]]
        self.assertEqual(len(node_ids), 117)
        self.assertEqual(len(node_ids), len(set(node_ids)))
        for node in self.record["semantic_layer"]:
            self.assertRegex(node["id"], NODE_ID)
            self.assertTrue(node["source_regions"])
            for reference in node["source_regions"]:
                self.assertRegex(reference, REGION_ID)
                self.assertFalse(reference.endswith("-r0000"))
                self.assertEqual(self.regions_by_id[reference]["page"], node["page"])

    def test_source_regions_have_tight_in_page_geometry(self) -> None:
        dimensions = {
            page["page"]: (page["width_points"], page["height_points"])
            for page in self.record["page_dimensions"]
        }
        referenced = {
            reference
            for node in self.record["semantic_layer"]
            for reference in node["source_regions"]
        }
        self.assertEqual(set(self.regions_by_id), referenced)
        for identifier in self.regions_by_id:
            self.assertFalse(identifier.endswith("-r0000"))
            region = self.regions_by_id[identifier]
            x0, y0, x1, y1 = region["bbox_points"]
            width, height = dimensions[region["page"]]
            self.assertTrue(0 <= x0 < x1 <= width, identifier)
            self.assertTrue(0 <= y0 < y1 <= height, identifier)
            self.assertLess((x1 - x0) * (y1 - y0), width * height * 0.8, identifier)

    def test_shared_and_multi_region_evidence_is_explicit(self) -> None:
        references = [
            reference
            for node in self.record["semantic_layer"]
            for reference in node["source_regions"]
        ]
        self.assertTrue(
            any(
                len(node["source_regions"]) > 1
                for node in self.record["semantic_layer"]
            )
        )
        self.assertLess(len(set(references)), len(references))

    def test_recognition_candidates_are_empty(self) -> None:
        self.assertEqual(self.record["candidates"], [])

    def test_warnings_are_source_linked_and_start_unresolved(self) -> None:
        self.assertTrue(self.record["warnings"], "the fixture records expected warnings")
        for warning in self.record["warnings"]:
            self.assertRegex(warning["id"], WARNING_ID)
            self.assertIsNone(warning["resolution"], "expected warnings start unresolved")
            self.assertEqual(warning["history"], [])
            self.assertIn(warning["page"], self.record["pages"])
            self.assertIn("semantic_nodes", warning)
            self.assertIn("source_regions", warning)
            for node in warning["semantic_nodes"]:
                self.assertIn(node, self.nodes_by_id)
            for region in warning["source_regions"]:
                self.assertIn(region, self.regions_by_id)

    def test_genuine_ambiguities_remain_explicit(self) -> None:
        # The fixture must not silently guess: the real ambiguities in the sample
        # are recorded as the Conversion Warnings a faithful conversion must raise.
        codes = {warning["code"] for warning in self.record["warnings"]}
        self.assertIn("suspected-source-error", codes)  # page 6 copper resistivity exponent
        self.assertIn("ambiguous-reading-order", codes)  # multi-column pages
        self.assertIn("table-merged-cells", codes)  # Table 25-1 section rows

    def test_the_six_expected_warning_concerns_remain_explicit(self) -> None:
        concerns = [
            (warning["page"], warning["code"]) for warning in self.record["warnings"]
        ]
        self.assertEqual(
            concerns,
            [
                (1, "ambiguous-reading-order"),
                (3, "ambiguous-reading-order"),
                (3, "table-merged-cells"),
                (6, "ambiguous-reading-order"),
                (6, "suspected-source-error"),
                (7, "ambiguous-reading-order"),
            ],
        )

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
        # fixture preserves the source verbatim and flags it rather than "fixing" it.
        source_errors = [
            w for w in self.record["warnings"] if w["code"] == "suspected-source-error"
        ]
        self.assertTrue(source_errors)
        preserved = any(
            SOURCE_ERROR_EXPONENT in self.nodes_by_id[node].get("normalized_math", "")
            for warning in source_errors
            for node in warning["semantic_nodes"]
        )
        self.assertTrue(preserved, "the flagged region must preserve the source's 10^-9 exponent")

    def test_maintainer_review_corrections_preserve_source_fidelity(self) -> None:
        boxed_power = self.nodes_by_id["page-6-s0012"]
        self.assertEqual(boxed_power["normalized_math"], "P = IV")
        self.assertEqual(
            boxed_power["spoken_math_alternative"], "Power equals I times V."
        )

        omega_example = self.nodes_by_id["page-8-s0014"]
        self.assertEqual(omega_example["text"], "Example: Convert 60 Hz to omega.")

        misconception = self.nodes_by_id["page-9-s0010"]["text"]
        self.assertIn("electrons (as current) are hella slow", misconception)
        self.assertNotIn("very slow", misconception)

    def test_standalone_report_generates_every_page_and_referenced_crop_offline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "gold-review"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "accessibilizer",
                    "report",
                    "--source",
                    str(SOURCE),
                    "--record",
                    str(GOLD),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output / "review-report.html").is_file())
            for page in EXPECTED_PAGES:
                self.assertGreater(
                    (output / "regions" / f"page-{page}.png").stat().st_size, 0
                )
            for identifier in self.regions_by_id:
                self.assertGreater(
                    (output / "regions" / f"{identifier}.png").stat().st_size, 0
                )


if __name__ == "__main__":
    unittest.main()
