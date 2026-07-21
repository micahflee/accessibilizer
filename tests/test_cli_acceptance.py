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

import yaml

from tests.test_provider_acceptance import FakeProvider


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"
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
        base_url: str | None = None,
    ) -> list[str]:
        replacement_arguments = ["--replace"] if replace else []
        resume_arguments = ["--resume"] if resume else []
        return [
            str(ROOT / "accessibilizer"),
            "convert",
            str(source),
            "--page",
            str(page),
            "--bundle",
            str(bundle),
            "--provider-base-url",
            base_url or self.provider.base_url,
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
        # Point XDG_CONFIG_HOME at a directory with no config so a developer's
        # personal ~/.config/accessibilizer/config.toml cannot leak into the run.
        return {
            **os.environ,
            "ACCESSIBILIZER_IMAGE": "accessibilizer:test",
            "ACCESSIBILIZER_RECOGNITION_BACKEND": "fake",
            "XDG_CONFIG_HOME": str(ROOT / ".no-user-config"),
        }

    def run_conversion(
        self,
        source: Path,
        bundle: Path,
        *,
        page: int = 1,
        replace: bool = False,
        resume: bool = False,
        base_url: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.conversion_command(
                source,
                bundle,
                page=page,
                replace=replace,
                resume=resume,
                base_url=base_url,
            ),
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=self.conversion_environment(),
        )

    def run_bundle_command(
        self, command: str, bundle: Path, *, reviewer: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        arguments = [str(ROOT / "accessibilizer"), command, "--bundle", str(bundle)]
        if reviewer is not None:
            arguments += ["--reviewer", reviewer]
        arguments += ["--json"]
        return subprocess.run(
            arguments,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=self.conversion_environment(),
        )

    def make_review_required_bundle(self, bundle: Path) -> None:
        with FakeProvider(page_overrides={"reading_order_is_unambiguous": False}) as provider:
            result = self.run_conversion(SOURCE, bundle, base_url=provider.base_url)
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_public_cli_reports_launcher_failures_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            result = self.run_conversion(temporary / "missing.pdf", temporary / "bundle")

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "error": "source must be a file",
                    "status": "operational_failure",
                },
            )

    def test_reconstruction_warnings_yield_review_required_status_and_exit_two(self) -> None:
        # A page the model reports as ambiguous produces a non-bypassable warning.
        with (
            FakeProvider(page_overrides={"reading_order_is_unambiguous": False}) as provider,
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            temporary = Path(temporary_directory)
            bundle = temporary / "review-required.accessibilizer"

            result = self.run_conversion(SOURCE, bundle, base_url=provider.base_url)

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
            review_record = yaml.safe_load((bundle / "review-record.yaml").read_text())
            self.assertEqual(review_record["schema_version"], "1.0")
            warning_codes = {warning["code"] for warning in review_record["warnings"]}
            self.assertIn("ambiguous-reading-order", warning_codes)
            # A freshly converted record starts with every warning unresolved.
            self.assertTrue(
                all(warning["resolution"] is None for warning in review_record["warnings"])
            )
            self.assertTrue(all("id" in warning for warning in review_record["warnings"]))
            self.assertIn(
                "Conversion Warnings", (bundle / "review-report.html").read_text()
            )

    def test_formula_notation_survives_the_pdf_ua_authoring_path(self) -> None:
        # Fractions, superscripts, subscripts, symbols, and units must reach the
        # tagged PDF/UA structure verbatim. The internal check reads the authored
        # structure tree back out of output.pdf, so comparing its Formula node to
        # the reconstructed one proves the notation survived authoring end to end.
        rich_formula = {
            "normalized_math": "v₀ = √(2·g·h), a = 9.8 m/s² × ¾ ± Δx ⇒ x⁻¹, m₁/m₂",
            "spoken_math_alternative": (
                "v naught equals the square root of two g h; a is about 9.8 meters "
                "per second squared."
            ),
        }
        with (
            FakeProvider(page_overrides={"formula": rich_formula}) as provider,
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            bundle = Path(temporary_directory) / "formula.accessibilizer"
            result = self.run_conversion(SOURCE, bundle, base_url=provider.base_url)

            # A Formula that diverges from the fake specialized candidate raises a
            # reviewable warning (exit 2); authoring and the PDF/UA gates still run.
            self.assertIn(result.returncode, (0, 2), result.stderr)
            self.assertTrue((bundle / "output.pdf").is_file())

            internal = json.loads((bundle / "validation" / "internal.json").read_text())
            self.assertTrue(internal["passed"], internal)
            authored_formula = next(
                node for node in internal["semantic_layer"] if node["type"] == "formula"
            )
            self.assertEqual(
                authored_formula["normalized_math"], rich_formula["normalized_math"]
            )
            self.assertEqual(
                authored_formula["spoken_math_alternative"],
                rich_formula["spoken_math_alternative"],
            )
            # The independent PDF/UA-1 validator agrees the output is conformant,
            # so the exotic glyphs never produced a forbidden .notdef.
            self.assertIn(
                'isCompliant="true"', (bundle / "validation" / "verapdf.xml").read_text()
            )
            record = yaml.safe_load((bundle / "review-record.yaml").read_text())
            self.assertEqual(
                record["semantic_layer"][2]["normalized_math"],
                rich_formula["normalized_math"],
            )

    def test_finalize_is_blocked_until_every_warning_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "review-required.accessibilizer"
            self.make_review_required_bundle(bundle)

            # A Review-Required bundle cannot be finalized while warnings remain.
            blocked = self.run_bundle_command("finalize", bundle, reviewer="tester")
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            blocked_result = json.loads(blocked.stdout)
            self.assertEqual(blocked_result["status"], "review_required")
            self.assertGreaterEqual(blocked_result["unresolved_warnings"], 1)

            # validate agrees the record is valid but not yet finalizable.
            checked = self.run_bundle_command("validate", bundle)
            self.assertEqual(checked.returncode, 2, checked.stderr)
            self.assertFalse(json.loads(checked.stdout)["finalizable"])

            # The Reviewer resolves the warning by hand-editing the YAML record,
            # then corrects the heading text to prove reviewer edits survive.
            record_path = bundle / "review-record.yaml"
            record = yaml.safe_load(record_path.read_text())
            record["warnings"][0]["resolution"] = {"status": "accepted"}
            record["semantic_layer"][0]["text"] = "Reviewer Corrected Heading"
            record_path.write_text(yaml.safe_dump(record, sort_keys=True))

            finalized = self.run_bundle_command("finalize", bundle, reviewer="tester")
            self.assertEqual(finalized.returncode, 0, finalized.stderr + finalized.stdout)
            self.assertEqual(json.loads(finalized.stdout)["status"], "accessible")

            # The corrected heading survived, the resolution is attributed, and no
            # provider requests were made (finalize runs with the network disabled).
            finalized_record = yaml.safe_load(record_path.read_text())
            self.assertEqual(
                finalized_record["semantic_layer"][0]["text"], "Reviewer Corrected Heading"
            )
            resolution = finalized_record["warnings"][0]["resolution"]
            self.assertEqual(resolution["reviewer"], "tester")
            self.assertEqual(resolution["status"], "accepted")
            self.assertTrue(resolution["timestamp"])
            provenance = json.loads((bundle / "provenance.json").read_text())
            self.assertTrue(provenance["finalized"])
            self.assertEqual(provenance["reviewer"], "tester")
            # Finalization merges its audit fields onto the conversion provenance
            # and preserves the request-usage record rather than discarding them.
            self.assertEqual(provenance["recognition_backend"], "fake")
            self.assertTrue((bundle / "request-usage.json").is_file())
            self.assertTrue((bundle / "output.pdf").is_file())
            self.assertNotIn(
                "Unresolved", (bundle / "review-report.html").read_text()
            )
            self.assertEqual(stat.S_IMODE((bundle / "source.pdf").stat().st_mode), 0o400)

    def test_review_records_resolutions_and_keeps_history_across_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "reviewed.accessibilizer"
            self.make_review_required_bundle(bundle)
            record_path = bundle / "review-record.yaml"

            record = yaml.safe_load(record_path.read_text())
            record["warnings"][0]["resolution"] = {"status": "accepted"}
            record_path.write_text(yaml.safe_dump(record, sort_keys=True))
            first = self.run_bundle_command("review", bundle, reviewer="alice")
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            self.assertEqual(json.loads(first.stdout)["unresolved_warnings"], 0)

            # A second reviewer changes the resolution; the prior one is preserved.
            record = yaml.safe_load(record_path.read_text())
            record["warnings"][0]["resolution"] = {
                "status": "not_applicable", "reason": "Order does not affect meaning."
            }
            record_path.write_text(yaml.safe_dump(record, sort_keys=True))
            second = self.run_bundle_command("review", bundle, reviewer="bob")
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)

            reviewed = yaml.safe_load(record_path.read_text())
            warning = reviewed["warnings"][0]
            self.assertEqual(warning["resolution"]["status"], "not_applicable")
            self.assertEqual(warning["resolution"]["reviewer"], "bob")
            self.assertEqual(len(warning["history"]), 1)
            self.assertEqual(warning["history"][0]["status"], "accepted")
            self.assertEqual(warning["history"][0]["reviewer"], "alice")

    def test_finalize_refuses_when_the_source_copy_no_longer_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "tampered.accessibilizer"
            self.make_review_required_bundle(bundle)
            record_path = bundle / "review-record.yaml"
            record = yaml.safe_load(record_path.read_text())
            record["warnings"][0]["resolution"] = {"status": "accepted", "reviewer": "tester"}
            record_path.write_text(yaml.safe_dump(record, sort_keys=True))

            # Corrupt the immutable Source PDF copy so its hash no longer matches.
            source_copy = bundle / "source.pdf"
            os.chmod(source_copy, 0o600)
            source_copy.write_bytes(b"%PDF-1.7\nnot the original\n")

            refused = self.run_bundle_command("finalize", bundle, reviewer="tester")
            self.assertEqual(refused.returncode, 1, refused.stderr)
            self.assertEqual(json.loads(refused.stdout)["status"], "operational_failure")
            self.assertIn("does not match", json.loads(refused.stdout)["error"])

    def test_public_cli_requires_authorization_to_replace_a_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "protected.accessibilizer"
            created = self.run_conversion(SOURCE, bundle)
            self.assertEqual(created.returncode, 0, created.stderr)
            reviewer_edit = "reviewer_edit: keep me\n"
            (bundle / "review-record.yaml").write_text(reviewer_edit)

            refused = self.run_conversion(SOURCE, bundle)

            self.assertEqual(refused.returncode, 1, refused.stderr)
            self.assertEqual(json.loads(refused.stdout)["status"], "operational_failure")
            self.assertEqual((bundle / "review-record.yaml").read_text(), reviewer_edit)

            failed_replacement = self.run_conversion(SOURCE, bundle, page=999, replace=True)

            self.assertEqual(failed_replacement.returncode, 1, failed_replacement.stderr)
            self.assertEqual((bundle / "review-record.yaml").read_text(), reviewer_edit)

            replaced = self.run_conversion(SOURCE, bundle, replace=True)

            self.assertEqual(replaced.returncode, 0, replaced.stderr + replaced.stdout)
            self.assertNotEqual((bundle / "review-record.yaml").read_text(), reviewer_edit)

    def test_replacement_reuses_all_valid_stages_without_new_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            bundle = temporary / "cached.accessibilizer"
            created = self.run_conversion(SOURCE, bundle)
            self.assertEqual(created.returncode, 0, created.stderr)
            provider_requests = len(self.provider.requests)
            crop = bundle / "regions" / "page-1.png"
            os.utime(crop, ns=(1_000_000_000, 1_000_000_000))

            replaced = self.run_conversion(SOURCE, bundle, replace=True)

            self.assertEqual(replaced.returncode, 0, replaced.stderr + replaced.stdout)
            # Every stage's dependency key and artifact hashes are still valid, so
            # the replacement reuses the recognition, page-semantics, and page
            # stages and makes no new provider calls.
            self.assertEqual(len(self.provider.requests), provider_requests)
            self.assertEqual(crop.stat().st_mtime_ns, 1_000_000_000)
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
                self.conversion_command(SOURCE, bundle),
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
                "checkpoints/page-semantics-page-1.json",
                "checkpoints/provider-capability.json",
                "checkpoints/recognition-page-1.json",
                "checkpoints/region-page-1.json",
                "checkpoints/source.json",
                "checkpoints/validation.json",
                "output.pdf",
                "page-semantics.json",
                "provenance.json",
                "recognition/page-1.json",
                "regions/page-1.png",
                "regions/page-1-recognition.png",
                "request-usage.json",
                "review-baseline.json",
                "review-record.yaml",
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

            # The Semantic Layer is reconstructed by the provider, not supplied.
            review_record = yaml.safe_load((bundle / "review-record.yaml").read_text())
            self.assertEqual(review_record["schema_version"], "1.0")
            self.assertEqual(
                [node["type"] for node in review_record["semantic_layer"]],
                ["heading", "paragraph", "formula", "figure"],
            )
            self.assertIn("spoken_math_alternative", review_record["semantic_layer"][2])
            self.assertIn("figure_alternative", review_record["semantic_layer"][3])
            self.assertIn("detailed_figure_description", review_record["semantic_layer"][3])
            self.assertEqual(review_record["warnings"], [])
            # The original recognition candidates are retained for source context.
            self.assertTrue(review_record["candidates"])
            self.assertTrue(all("crop" in c for c in review_record["candidates"]))
            self.assertEqual(review_record["reconstruction"]["page_prompt_version"], "1.1")
            self.assertEqual(
                review_record["reconstruction"]["provider_model"],
                "acceptance-model-2026-07-19",
            )
            self.assertEqual(len(review_record["reconstruction"]["verified_regions"]), 3)
            # The baseline mirrors the freshly built record for history tracking.
            baseline = json.loads((bundle / "review-baseline.json").read_text())
            self.assertEqual(baseline["semantic_layer"], review_record["semantic_layer"])

            page_semantics = json.loads((bundle / "page-semantics.json").read_text())
            self.assertEqual(page_semantics["semantic_layer"], review_record["semantic_layer"])
            self.assertEqual(page_semantics["title"], review_record["title"])

            provenance = json.loads((bundle / "provenance.json").read_text())
            self.assertTrue(provenance["source_copy_verified"])
            self.assertEqual((bundle / "source.pdf").read_bytes(), SOURCE.read_bytes())
            self.assertEqual(
                provenance["source_sha256"], hashlib.sha256(SOURCE.read_bytes()).hexdigest()
            )
            self.assertEqual(provenance["page_prompt_version"], "1.1")
            self.assertEqual(provenance["page_schema_version"], "1.0")
            # capability check plus one page call and one call per crop region.
            self.assertEqual(provenance["provider_usage"]["actual_requests"], 5)
            self.assertEqual(provenance["provider_usage"]["estimated_requests"], 5)
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


class LauncherHelpTest(unittest.TestCase):
    @staticmethod
    def run_launcher(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "accessibilizer"), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "ACCESSIBILIZER_IMAGE": "accessibilizer:test"},
        )

    def test_top_level_help_delegates_to_the_argparse_parser(self) -> None:
        result = self.run_launcher("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("assistive technology", " ".join(result.stdout.split()))

    def test_convert_help_delegates_to_the_argparse_parser(self) -> None:
        result = self.run_launcher("convert", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--bundle", result.stdout)
        self.assertIn("examples:", result.stdout)

    def test_help_anywhere_in_the_arguments_still_shows_help(self) -> None:
        result = self.run_launcher("convert", "some.pdf", "--page", "1", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--bundle", result.stdout)

    def test_unknown_command_without_help_keeps_the_short_usage_failure(self) -> None:
        result = self.run_launcher("bogus")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("usage: accessibilizer {convert,review,validate,finalize}", result.stderr)


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
