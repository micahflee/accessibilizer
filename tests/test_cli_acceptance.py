import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest

from tests.test_provider_acceptance import FakeProvider


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"
SEMANTIC_INPUT = ROOT / "testdata" / "one-page-semantic.json"
EMPTY_PASSWORD_ENCRYPTED_PDF = (
    "JVBERi0xLjcKJb/3ov4KMSAwIG9iago8PCAvRXh0ZW5zaW9ucyA8PCAvQURCRSA8PCAvQmFz"
    "ZVZlcnNpb24gLzEuNyAvRXh0ZW5zaW9uTGV2ZWwgOCA+PiA+PiAvUGFnZXMgMiAwIFIgL1R5"
    "cGUgL0NhdGFsb2cgPj4KZW5kb2JqCjIgMCBvYmoKPDwgL0NvdW50IDEgL0tpZHMgWyAzIDAg"
    "UiBdIC9UeXBlIC9QYWdlcyA+PgplbmRvYmoKMyAwIG9iago8PCAvQ29udGVudHMgNCAwIFIg"
    "L01lZGlhQm94IFsgMCAwIDcyIDcyIF0gL1BhcmVudCAyIDAgUiAvVHlwZSAvUGFnZSA+Pgpl"
    "bmRvYmoKNCAwIG9iago8PCAvTGVuZ3RoIDMyID4+CnN0cmVhbQqDfn/Anqsocr9HxLDEzulN"
    "7KLBBVzakwH4PEkCIuNTo2VuZHN0cmVhbQplbmRvYmoKNSAwIG9iago8PCAvQ0YgPDwgL1N0"
    "ZENGIDw8IC9BdXRoRXZlbnQgL0RvY09wZW4gL0NGTSAvQUVTVjMgL0xlbmd0aCAzMiA+PiA+"
    "PiAvRmlsdGVyIC9TdGFuZGFyZCAvTGVuZ3RoIDI1NiAvTyA8NmRiYWQzMDkzZjcwMGNlMDY0"
    "NWI0ODllNDA4ZjhjZWY4ZmZkMDhhNGE5OWUwNTVhZDUxYzcyOWU0ODdiY2U4YmFhYzg3MzA3"
    "NTQ4OTk2NmU2ZDZhMzhlNzBmODY0NDYzPiAvT0UgPDAwNDc5YWY5N2I4OTRmMTcwZDZjNDAy"
    "MjgzYjg2ZjYwMGY2YTZhOTdkZjQzNGIwZWQ2NDczMzgyMDc1MzkzOGI+IC9QIC00IC9QZXJt"
    "cyA8OWM0YTk5NGJlNjM4ODIxMmRkNjgxOTc5ZWY4MDQ1N2E+IC9SIDYgL1N0bUYgL1N0ZENG"
    "IC9TdHJGIC9TdGRDRiAvVSA8ZGJiNTZiNjI4NmVhYjdiZTk5OTRhNTA5YWNmYWI5MzYwYzdl"
    "YzQwNzhhZDI4MGM1NWM2ZDJmYTdmYzBhMDFjODFkNjg2M2UyZDFiMjg1Njk2NTMyMDkyYmY5"
    "ZmQ1YTY1PiAvVUUgPGIyMjc5NjZiOTliNTk0YTQ1NGFiMDYxNmQyYjJiN2YyNmNhNzlhNTNi"
    "YzQ1MjY5MDM5ZjJmMWQyNDgxYmY4N2E+IC9WIDUgPj4KZW5kb2JqCnhyZWYKMCA2CjAwMDAw"
    "MDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMTMwIDAwMDAwIG4g"
    "CjAwMDAwMDAxODkgMDAwMDAgbiAKMDAwMDAwMDI3NiAwMDAwIG4gCjAwMDAwMDAzNTcgMDAw"
    "MDAgbiAKdHJhaWxlciA8PCAvUm9vdCAxIDAgUiAvU2l6ZSA2IC9JRCBbPDlhOWVjMzNmYWNiZTRl"
    "N2MzNDcyZjY4NzkwM2M1YzA5Pjw5YTllYzMzZmFjYmU0ZTdjMzQ3MmY2ODc5MDNjNWMwOT5d"
    "IC9FbmNyeXB0IDUgMCBSID4+CnN0YXJ0eHJlZgo5MDQKJSVFT0YK"
)


def write_pdf(
    path: Path,
    *,
    catalog: str = "",
    page: str = "",
    extra_objects: tuple[str, ...] = (),
    trailer: str = "",
) -> None:
    objects = [
        f"<< /Type /Catalog /Pages 2 0 R {catalog} >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] {page} "
        "/Contents 4 0 R >>",
        "<< /Length 0 >>\nstream\n\nendstream",
        *extra_objects,
    ]
    contents = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, pdf_object in enumerate(objects, start=1):
        offsets.append(len(contents))
        contents.extend(f"{number} 0 obj\n{pdf_object}\nendobj\n".encode("ascii"))
    xref_offset = len(contents)
    contents.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    contents.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        contents.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    contents.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R {trailer} >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(contents)


class OnePageConversionTest(unittest.TestCase):
    provider: FakeProvider

    @classmethod
    def setUpClass(cls) -> None:
        cls.provider = FakeProvider()
        cls.provider.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.provider.__exit__()

    def conversion_command(
        self,
        source: Path,
        bundle: Path,
        *,
        page: int = 1,
        replace: bool = False,
        resume: bool = False,
        semantic_input: Path = SEMANTIC_INPUT,
    ) -> list[str]:
        replacement_arguments = ["--replace"] if replace else []
        resume_arguments = ["--resume"] if resume else []
        return [
            str(ROOT / "accessibilizer"),
            "convert",
            str(source),
            "--page",
            str(page),
            "--semantic-input",
            str(semantic_input),
            "--bundle",
            str(bundle),
            "--provider-base-url",
            self.provider.base_url,
            "--provider-model",
            "acceptance-model-2026-07-19",
            "--provider-data-location",
            "local",
            *replacement_arguments,
            *resume_arguments,
            "--json",
        ]

    @staticmethod
    def conversion_environment() -> dict[str, str]:
        return {
            **os.environ,
            "ACCESSIBILIZER_IMAGE": "accessibilizer:test",
            "ACCESSIBILIZER_RECOGNITION_BACKEND": "fake",
        }

    def run_conversion(
        self,
        source: Path,
        bundle: Path,
        *,
        page: int = 1,
        replace: bool = False,
        resume: bool = False,
        semantic_input: Path = SEMANTIC_INPUT,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.conversion_command(
                source,
                bundle,
                page=page,
                replace=replace,
                resume=resume,
                semantic_input=semantic_input,
            ),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=self.conversion_environment(),
        )

    def test_public_cli_reports_launcher_failures_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            result = self.run_conversion(temporary / "missing.pdf", temporary / "bundle")

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "error": "source and semantic input must be files",
                    "status": "operational_failure",
                },
            )

    def test_public_cli_returns_review_required_status_and_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            semantic_input = temporary / "warning-semantic.json"
            semantics = json.loads(SEMANTIC_INPUT.read_text())
            semantics["warnings"] = [
                {
                    "code": "ambiguous-reading-order",
                    "message": "Two reading orders remain plausible.",
                    "status": "unresolved",
                }
            ]
            semantic_input.write_text(json.dumps(semantics))
            bundle = temporary / "review-required.accessibilizer"

            result = self.run_conversion(SOURCE, bundle, semantic_input=semantic_input)

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "bundle": str(bundle),
                    "output": str(bundle / "output.pdf"),
                    "status": "review_required",
                },
            )
            self.assertTrue((bundle / "output.pdf").is_file())
            self.assertEqual(stat.S_IMODE((bundle / "source.pdf").stat().st_mode), 0o400)

    def test_public_cli_requires_authorization_to_replace_a_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "protected.accessibilizer"
            created = self.run_conversion(SOURCE, bundle)
            self.assertEqual(created.returncode, 0, created.stderr)
            reviewer_edit = '{"reviewer_edit": "keep me"}\n'
            (bundle / "review-record.json").write_text(reviewer_edit)

            refused = self.run_conversion(SOURCE, bundle)

            self.assertEqual(refused.returncode, 1, refused.stderr)
            self.assertEqual(json.loads(refused.stdout)["status"], "operational_failure")
            self.assertEqual((bundle / "review-record.json").read_text(), reviewer_edit)

            failed_replacement = self.run_conversion(SOURCE, bundle, page=999, replace=True)

            self.assertEqual(failed_replacement.returncode, 1, failed_replacement.stderr)
            self.assertEqual((bundle / "review-record.json").read_text(), reviewer_edit)

            replaced = self.run_conversion(SOURCE, bundle, replace=True)

            self.assertEqual(replaced.returncode, 0, replaced.stderr + replaced.stdout)
            self.assertNotEqual((bundle / "review-record.json").read_text(), reviewer_edit)

    def test_replacement_reuses_unaffected_provider_and_region_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            bundle = temporary / "cached.accessibilizer"
            created = self.run_conversion(SOURCE, bundle)
            self.assertEqual(created.returncode, 0, created.stderr)
            provider_requests = len(self.provider.requests)
            crop = bundle / "regions" / "page-1.png"
            os.utime(crop, ns=(1_000_000_000, 1_000_000_000))
            changed_semantics = temporary / "changed-semantic.json"
            semantics = json.loads(SEMANTIC_INPUT.read_text())
            semantics["title"] = "Changed title"
            changed_semantics.write_text(json.dumps(semantics))

            replaced = self.run_conversion(
                SOURCE,
                bundle,
                replace=True,
                semantic_input=changed_semantics,
            )

            self.assertEqual(replaced.returncode, 0, replaced.stderr + replaced.stdout)
            self.assertEqual(len(self.provider.requests), provider_requests)
            self.assertEqual(crop.stat().st_mtime_ns, 1_000_000_000)
            review_record = json.loads((bundle / "review-record.json").read_text())
            self.assertEqual(review_record["title"], "Changed title")
            provenance = json.loads((bundle / "provenance.json").read_text())
            self.assertEqual(provenance["provider_usage"]["estimated_requests"], 0)
            self.assertEqual(provenance["provider_usage"]["actual_requests"], 0)

    def test_interrupted_conversion_reuses_completed_page_and_region_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            bundle = temporary / "interrupted.accessibilizer"
            command = self.conversion_command(SOURCE, bundle)
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.conversion_environment(),
            )
            workspace = temporary / ".interrupted.accessibilizer.in-progress"
            page_checkpoint = workspace / "checkpoints" / "page-1.json"
            deadline = time.monotonic() + 30
            while not page_checkpoint.is_file() and process.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail("page checkpoint was not completed before the timeout")
                time.sleep(0.01)
            self.assertIsNone(process.poll(), "conversion finished before interruption")

            cid_directories = list(
                temporary.glob(".interrupted.accessibilizer.container.*")
            )
            self.assertEqual(len(cid_directories), 1)
            container_id = (cid_directories[0] / "id").read_text().strip()
            stopped = subprocess.run(
                ["docker", "stop", "--time", "0", container_id],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            process.communicate(timeout=10)

            self.assertFalse(bundle.exists())
            crop = workspace / "regions" / "page-1.png"
            output = workspace / "output.pdf"
            fixed_time = 1_000_000_000
            os.utime(crop, ns=(fixed_time, fixed_time))
            os.utime(output, ns=(fixed_time, fixed_time))
            provider_requests = len(self.provider.requests)

            resumed = self.run_conversion(SOURCE, bundle, resume=True)

            self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
            self.assertEqual(len(self.provider.requests), provider_requests)
            self.assertEqual((bundle / "regions" / "page-1.png").stat().st_mtime_ns, fixed_time)
            self.assertEqual((bundle / "output.pdf").stat().st_mtime_ns, fixed_time)

    def test_public_cli_safely_rejects_protected_and_interactive_pdfs(self) -> None:
        unsafe_pdfs = {
            "encrypted": {
                "extra_objects": (
                    "<< /Filter /Standard /V 1 /R 2 /O <00> /U <00> /P -4 >>",
                ),
                "trailer": "/Encrypt 5 0 R",
            },
            "signed": {
                "catalog": "/AcroForm 5 0 R",
                "extra_objects": (
                    "<< /Fields [6 0 R] >>",
                    "<< /FT /Sig /T (Signature) /V 7 0 R >>",
                    "<< /Type /Sig /Filter /Adobe.PPKLite /Contents <00> >>",
                ),
            },
            "scripted": {
                "catalog": "/OpenAction 5 0 R",
                "extra_objects": ("<< /S /JavaScript /JS (app.alert\\(1\\)) >>",),
            },
            "form-based": {
                "catalog": "/AcroForm 5 0 R",
                "extra_objects": ("<< /Fields [] >>",),
            },
            "embedded-file": {
                "catalog": "/Names << /EmbeddedFiles 5 0 R >>",
                "extra_objects": ("<< /Names [(attachment) 6 0 R] >>", "<< /Type /Filespec >>"),
            },
            "embedded-media": {
                "page": "/Annots [5 0 R]",
                "extra_objects": ("<< /Type /Annot /Subtype /RichMedia /Rect [0 0 10 10] >>",),
            },
            "otherwise-interactive": {
                "catalog": "/OpenAction 5 0 R",
                "extra_objects": ("<< /S /Launch /F (program) >>",),
            },
            "ordinary-link": {
                "page": "/Annots [5 0 R]",
                "extra_objects": (
                    "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] "
                    "/A << /S /URI /URI (https://example.com) >> >>",
                ),
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            for unsafe_kind, pdf_parts in unsafe_pdfs.items():
                with self.subTest(unsafe_kind=unsafe_kind):
                    source = temporary / f"{unsafe_kind}.pdf"
                    bundle = temporary / f"{unsafe_kind}.accessibilizer"
                    write_pdf(source, **pdf_parts)  # type: ignore[arg-type]

                    result = self.run_conversion(source, bundle)

                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["status"], "operational_failure")
                    self.assertIn("Unsupported Source PDF", json.loads(result.stdout)["error"])
                    self.assertFalse(bundle.exists())

    def test_public_cli_rejects_encryption_that_uses_an_empty_password(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            source = temporary / "empty-password-encrypted.pdf"
            source.write_bytes(base64.b64decode(EMPTY_PASSWORD_ENCRYPTED_PDF))
            bundle = temporary / "encrypted.accessibilizer"

            result = self.run_conversion(source, bundle)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("encryption", json.loads(result.stdout)["error"])
            self.assertFalse(bundle.exists())

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
                    "--provider-base-url",
                    self.provider.base_url,
                    "--provider-model",
                    "acceptance-model-2026-07-19",
                    "--provider-data-location",
                    "local",
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=self.conversion_environment(),
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
                "checkpoints/page-1.json",
                "checkpoints/provider-capability.json",
                "checkpoints/recognition-page-1.json",
                "checkpoints/region-page-1.json",
                "checkpoints/source.json",
                "checkpoints/validation.json",
                "output.pdf",
                "provenance.json",
                "recognition/page-1.json",
                "regions/page-1.png",
                "regions/page-1-recognition.png",
                "request-usage.json",
                "review-record.json",
                "review-report.html",
                "source.pdf",
                "validation/internal.json",
                "validation/preflight.json",
                "validation/verapdf.xml",
                "validation/visual.json",
            }
            actual_files = {
                str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file()
            }
            self.assertTrue(expected_files.issubset(actual_files))

            recognition = json.loads((bundle / "recognition/page-1.json").read_text())
            self.assertEqual(recognition["schema_version"], "1.0")
            self.assertEqual(recognition["page"], 1)
            self.assertEqual(recognition["recognition"]["backend"], "fake")
            self.assertEqual(recognition["rendering"]["dpi"], 300)
            candidate_types = {candidate["type"] for candidate in recognition["candidates"]}
            self.assertEqual(
                candidate_types,
                {
                    "text",
                    "handwriting",
                    "formula",
                    "table",
                    "figure",
                    "document_structure",
                },
            )
            for candidate in recognition["candidates"]:
                crop = bundle / candidate["crop"]
                self.assertTrue(crop.is_file(), candidate["crop"])
                self.assertEqual(candidate["crop"], f"regions/{candidate['id']}.png")
            self.assertFalse(recognition["pdf_text_evidence"]["authoritative"])

            recognition_provenance = json.loads((bundle / "provenance.json").read_text())
            self.assertEqual(recognition_provenance["recognition_backend"], "fake")
            self.assertEqual(recognition_provenance["recognition_dpi"], 300)

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
            self.assertEqual((bundle / "source.pdf").read_bytes(), SOURCE.read_bytes())
            self.assertEqual(
                provenance["source_sha256"], hashlib.sha256(SOURCE.read_bytes()).hexdigest()
            )
            self.assertEqual(provenance["provider_usage"]["actual_requests"], 1)
            self.assertEqual(provenance["provider_usage"]["estimated_requests"], 1)
            self.assertEqual(provenance["provider_usage"]["request_ceiling"], 100)
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


REAL_OCR_PROBE = """
import json
import subprocess
from pathlib import Path

from accessibilizer import recognition
from accessibilizer.recognition import (
    recognize_page,
    select_backend,
    validate_recognition_document,
)

source = Path("/probe/source.pdf")
info = subprocess.run(
    ["pdfinfo", str(source)], text=True, capture_output=True, check=True
)
pages = next(
    int(line.split(":", 1)[1]) for line in info.stdout.splitlines()
    if line.startswith("Pages:")
)
backend = select_backend({})
regions = Path("/probe/out/regions")
recognition_directory = Path("/probe/out/recognition")
regions.mkdir(parents=True, exist_ok=True)
recognition_directory.mkdir(parents=True, exist_ok=True)

types: set[str] = set()
validated = 0
for page in range(1, pages + 1):
    result = recognize_page(
        source_pdf=source,
        page=page,
        dpi=recognition.RECOGNITION_DPI,
        regions_dir=regions,
        recognition_dir=recognition_directory,
        backend=backend,
        source_sha256="0" * 64,
        renderer_version="probe",
        extractor_version="probe",
    )
    document = json.loads(result.document_path.read_text())
    validate_recognition_document(document)
    types.update(candidate["type"] for candidate in document["candidates"])
    validated += 1

print(json.dumps({"pages": pages, "validated": validated, "types": sorted(types)}))
"""


class RealPaddleOcrRecognitionTest(unittest.TestCase):
    """Opt-in check that pinned PaddleOCR produces evidence for the whole sample.

    Enable with ``ACCESSIBILIZER_RUN_REAL_OCR=1``. It runs inside the canonical
    image with networking disabled so any attempt to download model artifacts at
    runtime would fail, proving the weights are baked in and CPU-only.
    """

    @unittest.skipUnless(
        os.environ.get("ACCESSIBILIZER_RUN_REAL_OCR") == "1",
        "set ACCESSIBILIZER_RUN_REAL_OCR=1 to run the pinned PaddleOCR sample check",
    )
    def test_pinned_paddleocr_produces_schema_valid_candidates_offline(self) -> None:
        image = os.environ.get("ACCESSIBILIZER_IMAGE", "accessibilizer:test")
        with tempfile.TemporaryDirectory() as temporary_directory:
            probe = Path(temporary_directory) / "probe.py"
            probe.write_text(REAL_OCR_PROBE)

            result = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none",
                    "--volume", f"{probe}:/probe/probe.py:ro",
                    "--volume", f"{SOURCE}:/probe/source.pdf:ro",
                    image, "python3", "/probe/probe.py",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(summary["pages"], 11)
            self.assertEqual(summary["validated"], 11)
            # Criterion 2: the sample yields candidates for text (or handwriting),
            # Formulas, tables, figures, and Document Structure.
            self.assertLessEqual(
                {"text", "formula", "table", "figure", "document_structure"},
                set(summary["types"]),
            )


if __name__ == "__main__":
    unittest.main()
