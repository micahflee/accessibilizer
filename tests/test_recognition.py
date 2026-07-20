from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from accessibilizer import recognition
from accessibilizer.recognition import (
    FakeBackend,
    RawCandidate,
    assign_region_ids,
    build_recognition_document,
    parse_pdf_text_bbox,
    pixels_to_points,
    recognize_page,
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
    def valid_document(self) -> dict[str, object]:
        return build_recognition_document(
            page=1,
            source_sha256="a" * 64,
            dpi=300,
            renderer="pdftoppm",
            renderer_version="pdftoppm version 24.0",
            backend=FakeBackend(),
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

        self.assertEqual(document["schema_version"], "1.0")
        self.assertFalse(document["pdf_text_evidence"]["authoritative"])  # type: ignore[index]
        candidate = document["candidates"][0]  # type: ignore[index]
        self.assertEqual(candidate["id"], "page-1-r0001")
        self.assertEqual(candidate["crop"], "regions/page-1-r0001.png")
        self.assertEqual(candidate["bbox_points"], [2.4, 4.8, 19.2, 9.6])

    def test_unknown_candidate_type_is_rejected(self) -> None:
        document = self.valid_document()
        document["candidates"][0]["type"] = "sidebar"  # type: ignore[index]

        with self.assertRaises(ValueError):
            validate_recognition_document(document)

    def test_authoritative_evidence_is_rejected(self) -> None:
        document = self.valid_document()
        document["pdf_text_evidence"]["authoritative"] = True  # type: ignore[index]

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
                crop = workspace / candidate["crop"]
                self.assertTrue(crop.is_file(), candidate["crop"])
                self.assertGreater(crop.stat().st_size, 0)
                self.assertEqual(candidate["crop"], f"regions/{candidate['id']}.png")
            self.assertIn(result.document_path, result.artifacts)
            self.assertFalse(document["pdf_text_evidence"]["authoritative"])


if __name__ == "__main__":
    unittest.main()
