import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"
SEMANTIC_INPUT = ROOT / "testdata" / "one-page-semantic.json"


class OnePageConversionTest(unittest.TestCase):
    def test_public_cli_produces_accessible_visual_preserving_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "electric-current.accessibilizer"
            result = subprocess.run(
                [
                    str(ROOT / "accessibilizer"),
                    "convert",
                    str(SOURCE),
                    "--page",
                    "1",
                    "--semantic-input",
                    str(SEMANTIC_INPUT),
                    "--bundle",
                    str(bundle),
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "ACCESSIBILIZER_IMAGE": "accessibilizer:test"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "bundle": str(bundle),
                    "output": str(bundle / "output.pdf"),
                    "status": "accessible",
                },
            )
            self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((bundle / "source.pdf").stat().st_mode), 0o400)

            expected_files = {
                "authoring.json",
                "output.pdf",
                "provenance.json",
                "regions/page-1.png",
                "review-record.json",
                "review-report.html",
                "source.pdf",
                "validation/internal.json",
                "validation/verapdf.xml",
                "validation/visual.json",
            }
            actual_files = {
                str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file()
            }
            self.assertTrue(expected_files.issubset(actual_files))

            review_record = json.loads((bundle / "review-record.json").read_text())
            self.assertEqual(review_record["schema_version"], "1.0")
            self.assertEqual(
                [node["type"] for node in review_record["semantic_layer"]],
                ["heading", "paragraph", "formula", "figure"],
            )
            self.assertIn("spoken_math_alternative", review_record["semantic_layer"][2])
            self.assertIn("figure_alternative", review_record["semantic_layer"][3])
            self.assertIn("detailed_figure_description", review_record["semantic_layer"][3])

            provenance = json.loads((bundle / "provenance.json").read_text())
            self.assertTrue(provenance["source_copy_verified"])
            self.assertIn('<html lang="en-US">', (bundle / "review-report.html").read_text())

            internal = json.loads((bundle / "validation/internal.json").read_text())
            self.assertEqual(internal["checks"], [])
            self.assertTrue(internal["passed"])
            self.assertEqual(internal["source"], "output.pdf structure tree")
            self.assertEqual(internal["semantic_layer"], review_record["semantic_layer"])

            visual = json.loads((bundle / "validation/visual.json").read_text())
            self.assertLessEqual(visual["different_pixel_ratio"], visual["tolerance"])
            self.assertEqual(visual["source_width"], visual["output_width"])
            self.assertEqual(visual["source_height"], visual["output_height"])

            verapdf_report = (bundle / "validation/verapdf.xml").read_text()
            self.assertIn('isCompliant="true"', verapdf_report)


if __name__ == "__main__":
    unittest.main()
