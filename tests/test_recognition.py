from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from accessibilizer import recognition
from accessibilizer.recognition import (
    FakeBackend,
    RawCandidate,
    assign_region_ids,
    build_recognition_document,
    parse_pdf_text_bbox,
    pixels_to_points,
    recognize_page,
    raster_region_proposals,
    select_model_binding_boxes,
    select_backend,
    validate_recognition_document,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"

POPPLER = shutil.which("pdftoppm") is not None and shutil.which("pdftotext") is not None


class RegionIdOrderingTest(unittest.TestCase):
    def test_ids_are_assigned_top_to_bottom_left_to_right_regardless_of_input_order(
        self,
    ) -> None:
        candidates = [
            RawCandidate("figure", (10, 400, 90, 480), None, None, "backend"),
            RawCandidate("text", (200, 20, 260, 40), None, None, "backend"),
            RawCandidate("formula", (10, 20, 80, 40), None, None, "backend"),
        ]

        ordered = assign_region_ids(3, candidates)

        self.assertEqual(
            [(identifier, candidate.type) for identifier, candidate in ordered],
            [
                ("page-3-r0001", "formula"),
                ("page-3-r0002", "text"),
                ("page-3-r0003", "figure"),
            ],
        )

    def test_ordering_is_stable_for_equal_positions(self) -> None:
        candidates = [
            RawCandidate("table", (10, 20, 80, 40), None, None, "backend"),
            RawCandidate("text", (10, 20, 80, 40), None, None, "backend"),
        ]

        ordered = assign_region_ids(1, candidates)

        self.assertEqual(
            [candidate.type for _, candidate in ordered], ["table", "text"]
        )


class PixelConversionTest(unittest.TestCase):
    def test_pixels_convert_to_points_using_the_render_resolution(self) -> None:
        self.assertEqual(pixels_to_points((150, 300, 450, 600), 300), [36.0, 72.0, 108.0, 144.0])


class RasterProposalTest(unittest.TestCase):
    def test_multiscale_raster_geometry_keeps_lines_and_a_useful_parent(self) -> None:
        width, height = 200, 160
        pixels = bytearray([255] * width * height * 3)
        for y0, y1 in ((20, 28), (38, 46)):
            for y in range(y0, y1):
                for x in range(20, 150):
                    offset = (y * width + x) * 3
                    pixels[offset:offset + 3] = b"\x00\x00\x00"

        proposals = raster_region_proposals(width, height, pixels)

        self.assertTrue(any(y0 <= 20 and y1 >= 28 for x0, y0, x1, y1 in proposals))
        self.assertTrue(any(y0 <= 20 and y1 >= 46 for x0, y0, x1, y1 in proposals))
        self.assertTrue(
            all((x1 - x0) * (y1 - y0) < width * height * 0.8 for x0, y0, x1, y1 in proposals)
        )


@unittest.skipUnless(POPPLER, "poppler is required for gold proposal coverage")
class GoldProposalCoverageTest(unittest.TestCase):
    def test_every_gold_source_region_has_a_useful_deterministic_proposal(self) -> None:
        gold = yaml.safe_load((ROOT / "testdata" / "gold-review-record.yaml").read_text())
        gold_by_page: dict[int, list[list[float]]] = {
            page: [] for page in range(1, 12)
        }
        for region in gold["source_regions"]:
            gold_by_page[region["page"]].append(region["bbox_points"])

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            for page in range(1, 12):
                width, height, pixels = recognition._render_page_rgb(
                    source_pdf=SOURCE,
                    page=page,
                    dpi=recognition.RECOGNITION_DPI,
                    temporary_base=temporary / f"page-{page}",
                )
                raster_proposals = raster_region_proposals(width, height, pixels)
                proposals = [
                    pixels_to_points(proposal, recognition.RECOGNITION_DPI)
                    for proposal in raster_proposals
                ]
                model_binding_proposals = [
                    pixels_to_points(proposal, recognition.RECOGNITION_DPI)
                    for proposal in select_model_binding_boxes(
                        raster_proposals
                    )
                ]
                self.assertLessEqual(len(model_binding_proposals), 1500)
                for expected in gold_by_page[page]:
                    self.assertTrue(
                        any(self.usefully_covers(proposal, expected) for proposal in proposals),
                        f"page {page} gold Source Region {expected} has no useful proposal",
                    )
                    self.assertTrue(
                        any(
                            self.usefully_covers(proposal, expected)
                            for proposal in model_binding_proposals
                        ),
                        f"page {page} gold Source Region {expected} is not model-visible",
                    )

    @staticmethod
    def usefully_covers(proposal: list[float], gold: list[float]) -> bool:
        px0, py0, px1, py1 = proposal
        gx0, gy0, gx1, gy1 = gold
        intersection = max(0.0, min(px1, gx1) - max(px0, gx0)) * max(
            0.0, min(py1, gy1) - max(py0, gy0)
        )
        proposal_area = (px1 - px0) * (py1 - py0)
        gold_area = (gx1 - gx0) * (gy1 - gy0)
        union = proposal_area + gold_area - intersection
        iou = intersection / union if union else 0.0
        containment = intersection / gold_area
        return iou >= 0.5 or (containment >= 0.8 and proposal_area <= 2 * gold_area)


class PdfTextEvidenceTest(unittest.TestCase):
    def test_words_and_geometry_are_parsed_from_pdftotext_bbox_output(self) -> None:
        xhtml = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<doc>
<page width="612.000000" height="792.000000">
<word xMin="72.00" yMin="80.00" xMax="120.50" yMax="92.00">Ohm&amp;s</word>
<word xMin="130.00" yMin="80.00" xMax="160.00" yMax="92.00">Law</word>
</page>
</doc>
</body></html>
"""

        words = parse_pdf_text_bbox(xhtml)

        self.assertEqual(
            words,
            [
                {"text": "Ohm&s", "bbox_points": [72.0, 80.0, 120.5, 92.0]},
                {"text": "Law", "bbox_points": [130.0, 80.0, 160.0, 92.0]},
            ],
        )

    def test_pages_without_extractable_text_produce_no_words(self) -> None:
        xhtml = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>'
            '<page width="612" height="792"></page></doc></body></html>'
        )

        self.assertEqual(parse_pdf_text_bbox(xhtml), [])


class DocumentValidationTest(unittest.TestCase):
    def valid_document(self) -> dict[str, Any]:
        return build_recognition_document(
            page=1,
            source_sha256="a" * 64,
            dpi=300,
            renderer="pdftoppm",
            renderer_version="pdftoppm version 24.0",
            backend=FakeBackend(),
            page_size=(1000, 1400),
            candidates=[
                (
                    "page-1-r0001",
                    RawCandidate("text", (10, 20, 80, 40), "hi", 0.9, "fake-text"),
                )
            ],
            words=[{"text": "hi", "bbox_points": [2.4, 4.8, 19.2, 9.6]}],
            extractor="pdftotext",
            extractor_version="pdftotext version 24.0",
        )

    def test_a_built_document_is_schema_valid_and_marks_evidence_non_authoritative(
        self,
    ) -> None:
        document = self.valid_document()

        validate_recognition_document(document)
        published = json.loads(
            (ROOT / "schemas" / "recognition-2.0.schema.json").read_text()
        )
        self.assertEqual(list(Draft202012Validator(published).iter_errors(document)), [])

        self.assertEqual(document["schema_version"], "2.0")
        self.assertFalse(document["pdf_text_evidence"]["authoritative"])
        candidate = document["candidates"][0]
        self.assertEqual(candidate["id"], "page-1-c0001")
        self.assertRegex(candidate["source_region"], r"^page-1-r[0-9]{4}$")
        self.assertNotIn("bbox_points", candidate)
        self.assertEqual(candidate["raw_class"], "text")
        self.assertEqual(candidate["ocr_text_confidence"], 0.9)
        self.assertIn("verification", candidate)
        regions = document["source_regions"]
        self.assertEqual(regions[0]["id"], "page-1-r0000")
        self.assertEqual(regions[0]["bbox_pixels"], [0, 0, 1000, 1400])
        self.assertTrue(
            any(region["bbox_points"] == [2.4, 4.8, 19.2, 9.6] for region in regions)
        )

    def test_broad_candidate_is_retained_on_fallback_but_cannot_trigger_disagreement(
        self,
    ) -> None:
        document = build_recognition_document(
            page=1,
            source_sha256="a" * 64,
            dpi=300,
            renderer="pdftoppm",
            renderer_version="pdftoppm version 24.0",
            backend=FakeBackend(),
            page_size=(1000, 1000),
            candidates=[
                (
                    "ignored",
                    RawCandidate(
                        "formula", (20, 10, 980, 930), "not the formula", 0.8203,
                        "paddleocr-formula", raw_class="formula", layout_confidence=0.99,
                    ),
                )
            ],
            words=[],
            extractor="pdftotext",
            extractor_version="pdftotext version 24.0",
        )

        validate_recognition_document(document)
        candidate = document["candidates"][0]
        self.assertEqual(candidate["source_region"], "page-1-r0000")
        self.assertFalse(candidate["verification"]["eligible"])
        self.assertIn("source-region-too-large", candidate["verification"]["reason_codes"])

    def test_source_regions_are_type_neutral_deduplicated_and_keep_useful_parents(
        self,
    ) -> None:
        document = build_recognition_document(
            page=2,
            source_sha256="a" * 64,
            dpi=100,
            renderer="pdftoppm",
            renderer_version="pdftoppm version 24.0",
            backend=FakeBackend(),
            page_size=(1000, 1000),
            candidates=[
                ("ignored", RawCandidate("text", (10, 10, 100, 40), "one", 0.9, "a")),
                ("ignored", RawCandidate("formula", (10, 10, 100, 40), "one", 0.9, "b")),
                ("ignored", RawCandidate("figure", (5, 5, 300, 300), None, None, "c")),
            ],
            words=[],
            extractor="pdftotext",
            extractor_version="pdftotext version 24.0",
        )

        nonfallback = document["source_regions"][1:]
        self.assertEqual(len(nonfallback), 2)
        self.assertTrue(all("type" not in region for region in nonfallback))
        self.assertEqual(
            [candidate["source_region"] for candidate in document["candidates"]],
            ["page-2-r0001", "page-2-r0002", "page-2-r0002"],
        )

    def test_unknown_candidate_type_is_rejected(self) -> None:
        document = self.valid_document()
        document["candidates"][0]["type"] = "sidebar"

        with self.assertRaises(ValueError):
            validate_recognition_document(document)

    def test_authoritative_evidence_is_rejected(self) -> None:
        document = self.valid_document()
        document["pdf_text_evidence"]["authoritative"] = True

        with self.assertRaises(ValueError):
            validate_recognition_document(document)


class BackendSelectionTest(unittest.TestCase):
    def test_fake_backend_is_selected_by_name(self) -> None:
        backend = select_backend({"ACCESSIBILIZER_RECOGNITION_BACKEND": "fake"})
        self.assertIsInstance(backend, FakeBackend)

    def test_paddleocr_is_the_default_backend(self) -> None:
        backend = select_backend({})
        self.assertEqual(backend.name, "paddleocr")

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            select_backend({"ACCESSIBILIZER_RECOGNITION_BACKEND": "tesseract"})

    def test_fake_backend_covers_every_required_candidate_type(self) -> None:
        candidates = FakeBackend().detect(Path("unused.png"), (1000, 1400))
        self.assertEqual(
            {candidate.type for candidate in candidates},
            {"text", "handwriting", "formula", "table", "figure", "document_structure"},
        )

    def test_paddle_backend_disables_the_crashing_ir_optimizer(self) -> None:
        options: dict[str, object] = {}
        expected_pipeline = object()
        paddleocr = ModuleType("paddleocr")

        def pp_structure(**kwargs: object) -> object:
            options.update(kwargs)
            return expected_pipeline

        setattr(paddleocr, "PPStructure", pp_structure)
        with patch.dict(sys.modules, {"paddleocr": paddleocr}):
            pipeline = recognition.PaddleBackend()._pipeline()

        self.assertIs(pipeline, expected_pipeline)
        self.assertEqual(options, {"show_log": False, "ir_optim": False})


@unittest.skipUnless(POPPLER, "poppler (pdftoppm/pdftotext) is required")
class RecognizePageOnHostTest(unittest.TestCase):
    def test_recognize_page_produces_schema_valid_candidates_and_stable_crops(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            regions = workspace / "regions"
            recognition_directory = workspace / "recognition"
            regions.mkdir()
            recognition_directory.mkdir()

            result = recognize_page(
                source_pdf=SOURCE,
                page=1,
                dpi=recognition.RECOGNITION_DPI,
                regions_dir=regions,
                recognition_dir=recognition_directory,
                backend=FakeBackend(),
                source_sha256="b" * 64,
                renderer_version="pdftoppm test",
                extractor_version="pdftotext test",
            )

            document = json.loads(result.document_path.read_text())
            validate_recognition_document(document)
            self.assertEqual(document["page"], 1)
            types = {candidate["type"] for candidate in document["candidates"]}
            self.assertEqual(
                types,
                {
                    "text",
                    "handwriting",
                    "formula",
                    "table",
                    "figure",
                    "document_structure",
                },
            )
            for candidate in document["candidates"]:
                region = next(
                    region for region in document["source_regions"]
                    if region["id"] == candidate["source_region"]
                )
                crop = workspace / region["crop"]
                self.assertTrue(crop.is_file(), region["crop"])
                self.assertGreater(crop.stat().st_size, 0)
                self.assertTrue(candidate["id"].startswith("page-1-c"))
            self.assertTrue(document["proposal_generation"]["algorithm_version"])
            overlay = workspace / document["overlay"]
            self.assertTrue(overlay.is_file())
            self.assertGreater(overlay.stat().st_size, 0)
            self.assertIn(overlay, result.artifacts)
            overlays = [workspace / value for value in document["overlays"]]
            self.assertGreater(len(overlays), 1)
            self.assertTrue(all(path.is_file() for path in overlays))
            self.assertTrue(all(path in result.artifacts for path in overlays))
            visible = [
                region for region in document["source_regions"]
                if region["model_visible"]
            ]
            self.assertLess(len(visible), len(document["source_regions"]))
            self.assertIn(result.document_path, result.artifacts)
            self.assertFalse(document["pdf_text_evidence"]["authoritative"])


if __name__ == "__main__":
    unittest.main()
