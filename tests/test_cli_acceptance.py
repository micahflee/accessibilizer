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
from typing import Any

import jsonschema
import yaml

from accessibilizer.events import STAGE_LABELS
from tests.test_provider_acceptance import FakeProvider


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"
CONVERSION_EVENTS_SCHEMA = json.loads(
    (ROOT / "schemas" / "conversion-events-1.0.schema.json").read_text(encoding="utf-8")
)
# Every labelled stage is a named major stage that must emit start and completion.
MAJOR_STAGES = set(STAGE_LABELS)
# Field names that would betray a secret, an encoded image, or model-produced
# Semantic Layer content leaking into the durable log.
FORBIDDEN_IN_EVENTS = (
    "Bearer",
    "Authorization",
    "authorization",
    "data:image",
    "base64",
    "normalized_math",
    "spoken_math_alternative",
    "figure_alternative",
    "detailed_figure_description",
    "transcription",
    "messages",
)


def read_event_lines(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
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


class ConversionTest(unittest.TestCase):
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
        page: str | None = "1",
        replace: bool = False,
        resume: bool = False,
        verbose: bool = False,
        base_url: str | None = None,
    ) -> list[str]:
        replacement_arguments = ["--replace"] if replace else []
        resume_arguments = ["--resume"] if resume else []
        verbose_arguments = ["--verbose"] if verbose else []
        page_arguments = ["--page", page] if page is not None else []
        return [
            str(ROOT / "accessibilizer"),
            "convert",
            str(source),
            *page_arguments,
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
            *verbose_arguments,
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
        page: str | None = "1",
        replace: bool = False,
        resume: bool = False,
        verbose: bool = False,
        base_url: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.conversion_command(
                source,
                bundle,
                page=page,
                replace=replace,
                resume=resume,
                verbose=verbose,
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

    def test_public_report_cli_supports_bundle_and_standalone_modes_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            bundle = temporary / "source.accessibilizer"
            created = self.run_conversion(SOURCE, bundle)
            self.assertEqual(created.returncode, 0, created.stderr + created.stdout)

            (bundle / "reviewer-note.txt").write_text("keep me", encoding="utf-8")
            bundled = self.run_bundle_command("report", bundle)
            self.assertEqual(bundled.returncode, 0, bundled.stderr + bundled.stdout)
            self.assertEqual((bundle / "reviewer-note.txt").read_text(), "keep me")
            self.assertTrue((bundle / "review-report.html").is_file())

            standalone = temporary / "nested" / "standalone-report"
            command = [
                str(ROOT / "accessibilizer"), "report",
                "--source", str(bundle / "source.pdf"),
                "--record", str(bundle / "review-record.yaml"),
                "--output", str(standalone),
                "--json",
            ]
            reported = subprocess.run(
                command, cwd=ROOT, text=True, capture_output=True, check=False,
                env=self.conversion_environment(),
            )

            self.assertEqual(reported.returncode, 0, reported.stderr + reported.stdout)
            self.assertEqual(
                json.loads(reported.stdout)["report"],
                str(standalone / "review-report.html"),
            )
            self.assertTrue((standalone / "regions" / "page-1.png").is_file())

    def test_public_report_cli_reports_container_runtime_failures_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            bundle = temporary / "source.accessibilizer"
            bundle.mkdir()
            binaries = temporary / "bin"
            binaries.mkdir()
            docker = binaries / "docker"
            docker.write_text("#!/bin/sh\nexit 125\n", encoding="utf-8")
            docker.chmod(0o755)
            environment = self.conversion_environment()
            environment["PATH"] = f"{binaries}:{environment['PATH']}"

            result = subprocess.run(
                [
                    str(ROOT / "accessibilizer"),
                    "report",
                    "--bundle",
                    str(bundle),
                    "--json",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )

            self.assertEqual(result.returncode, 125, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "error": "container runtime failed",
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
            self.assertEqual(review_record["schema_version"], "3.0")
            warning_codes = {warning["code"] for warning in review_record["warnings"]}
            self.assertIn("ambiguous-reading-order", warning_codes)
            # A freshly converted record starts with every warning unresolved.
            self.assertTrue(
                all(warning["resolution"] is None for warning in review_record["warnings"])
            )
            self.assertTrue(all("id" in warning for warning in review_record["warnings"]))
            self.assertTrue(
                all(
                    "semantic_nodes" in warning and "source_regions" in warning
                    for warning in review_record["warnings"]
                )
            )
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

            # The internal checks read the authored structure tree back out of
            # output.pdf and confirm it matches the Review Record, so a passing
            # internal check proves the Review Record's Formula reached the tagged
            # PDF verbatim.
            internal = json.loads((bundle / "validation" / "internal.json").read_text())
            self.assertTrue(internal["passed"], internal)
            record = yaml.safe_load((bundle / "review-record.yaml").read_text())
            authored_formula = next(
                node for node in record["semantic_layer"] if node["type"] == "formula"
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

    def _figure_survives(self, figure: dict[str, object]) -> dict[str, object]:
        """Convert with the given reconstructed figure and return the figure node
        the internal check reads back out of the authored output.pdf."""
        with (
            FakeProvider(page_overrides={"figure": figure}) as provider,
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            bundle = Path(temporary_directory) / "figure.accessibilizer"
            result = self.run_conversion(SOURCE, bundle, base_url=provider.base_url)
            self.assertIn(result.returncode, (0, 2), result.stderr)
            self.assertTrue((bundle / "output.pdf").is_file())

            internal = json.loads((bundle / "validation" / "internal.json").read_text())
            self.assertTrue(internal["passed"], internal)
            self.assertIn(
                'isCompliant="true"', (bundle / "validation" / "verapdf.xml").read_text()
            )
            # A passing internal check confirms the authored structure tree matches
            # the Review Record, so the figure semantics survived authoring end to end.
            record = yaml.safe_load((bundle / "review-record.yaml").read_text())
            authored: dict[str, object] = next(
                node for node in record["semantic_layer"] if node["type"] == "figure"
            )
            return authored

    def test_a_simple_figure_survives_the_pdf_ua_authoring_path(self) -> None:
        # A simple Informative Figure reaches assistive technology as a concise
        # Figure Alternative with no Detailed Figure Description.
        authored = self._figure_survives(
            {
                "complexity": "simple",
                "figure_alternative": "A photograph of a copper wire.",
                "detailed_figure_description": None,
            }
        )
        self.assertEqual(authored["complexity"], "simple")
        self.assertEqual(authored["figure_alternative"], "A photograph of a copper wire.")
        self.assertNotIn("detailed_figure_description", authored)

    def test_a_complex_figure_survives_the_pdf_ua_authoring_path(self) -> None:
        # A complex Informative Figure reaches assistive technology with both its
        # concise Alternative and its Detailed Figure Description intact.
        detailed = (
            "A resistor R connects a battery's positive terminal to an ammeter; the "
            "arrow labels conventional current flowing clockwise around the loop."
        )
        authored = self._figure_survives(
            {
                "complexity": "complex",
                "figure_alternative": "A single-loop resistor circuit.",
                "detailed_figure_description": detailed,
            }
        )
        self.assertEqual(authored["complexity"], "complex")
        self.assertEqual(authored["figure_alternative"], "A single-loop resistor circuit.")
        self.assertEqual(authored["detailed_figure_description"], detailed)

    def _table_survives(self, table: dict[str, object]) -> dict[str, object]:
        """Convert with the given reconstructed table and return the table node the
        internal check reads back out of the authored output.pdf."""
        with (
            FakeProvider(page_overrides={"table": table}) as provider,
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            bundle = Path(temporary_directory) / "table.accessibilizer"
            result = self.run_conversion(SOURCE, bundle, base_url=provider.base_url)
            self.assertIn(result.returncode, (0, 2), result.stderr)
            self.assertTrue((bundle / "output.pdf").is_file())

            internal = json.loads((bundle / "validation" / "internal.json").read_text())
            self.assertTrue(internal["passed"], internal)
            # The independent PDF/UA-1 validator agrees the tagged table is conformant.
            self.assertIn(
                'isCompliant="true"', (bundle / "validation" / "verapdf.xml").read_text()
            )
            # A passing internal check confirms the authored structure tree matches
            # the Review Record, so the table semantics survived authoring end to end.
            record = yaml.safe_load((bundle / "review-record.yaml").read_text())
            authored: dict[str, object] = next(
                node for node in record["semantic_layer"] if node["type"] == "table"
            )
            return authored

    def test_a_semantic_table_survives_the_pdf_ua_authoring_path(self) -> None:
        # A Semantic Table reaches assistive technology with its caption, column and
        # row headers (with their associations), merged cells, and data cells intact.
        table: dict[str, object] = {
            "caption": "Resistivity of common materials at 20 degrees Celsius",
            "boundaries_are_uncertain": False,
            "headers_are_uncertain": False,
            "rows": [
                {
                    "cells": [
                        {"kind": "header", "text": "Electrical properties",
                         "scope": "col", "row_span": 1, "col_span": 2},
                    ]
                },
                {
                    "cells": [
                        {"kind": "header", "text": "Material", "scope": "col",
                         "row_span": 1, "col_span": 1},
                        {"kind": "header", "text": "Resistivity (ohm-metre)",
                         "scope": "col", "row_span": 1, "col_span": 1},
                    ]
                },
                {
                    "cells": [
                        {"kind": "header", "text": "Copper", "scope": "row",
                         "row_span": 1, "col_span": 1},
                        {"kind": "data", "text": "1.68e-8", "scope": "none",
                         "row_span": 1, "col_span": 1},
                    ]
                },
            ],
        }
        authored = self._table_survives(table)
        self.assertEqual(
            authored["caption"], "Resistivity of common materials at 20 degrees Celsius"
        )
        rows = authored["rows"]
        assert isinstance(rows, list)
        # The merged header cell keeps its two-column span and column scope.
        self.assertEqual(rows[0]["cells"][0]["col_span"], 2)
        self.assertEqual(rows[0]["cells"][0]["scope"], "col")
        self.assertEqual(rows[0]["cells"][0]["kind"], "header")
        # The row header and its data cell keep their associations and values.
        self.assertEqual(rows[2]["cells"][0]["scope"], "row")
        self.assertEqual(rows[2]["cells"][1]["kind"], "data")
        self.assertEqual(rows[2]["cells"][1]["text"], "1.68e-8")

    def test_a_captionless_table_survives_without_a_caption(self) -> None:
        table: dict[str, object] = {
            "caption": None,
            "boundaries_are_uncertain": False,
            "headers_are_uncertain": False,
            "rows": [
                {
                    "cells": [
                        {"kind": "header", "text": "Material", "scope": "col",
                         "row_span": 1, "col_span": 1},
                        {"kind": "header", "text": "Resistivity", "scope": "col",
                         "row_span": 1, "col_span": 1},
                    ]
                },
                {
                    "cells": [
                        {"kind": "header", "text": "Copper", "scope": "row",
                         "row_span": 1, "col_span": 1},
                        {"kind": "data", "text": "1.68e-8", "scope": "none",
                         "row_span": 1, "col_span": 1},
                    ]
                },
            ],
        }
        authored = self._table_survives(table)
        self.assertNotIn("caption", authored)

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

            failed_replacement = self.run_conversion(SOURCE, bundle, page="999", replace=True)

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
            page_checkpoint = workspace / "checkpoints" / "document.json"
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
                "checkpoints/document.json",
                "checkpoints/page-semantics-page-1.json",
                "checkpoints/provider-capability.json",
                "checkpoints/recognition-page-1.json",
                "checkpoints/region-page-1.json",
                "checkpoints/source.json",
                "checkpoints/validation.json",
                "output.pdf",
                "page-semantics/page-1.json",
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
            self.assertEqual(review_record["schema_version"], "3.0")
            self.assertEqual(review_record["pages"], [1])
            self.assertEqual(review_record["page_dimensions"][0]["page"], 1)
            self.assertEqual(
                review_record["page_dimensions"][0],
                {"page": 1, "width_points": 612.0, "height_points": 803.25},
            )
            self.assertTrue(review_record["source_regions"])
            self.assertEqual(
                [node["type"] for node in review_record["semantic_layer"]],
                ["heading", "paragraph", "formula", "figure", "table"],
            )
            # Every node is tagged with its source page.
            self.assertTrue(all(node["page"] == 1 for node in review_record["semantic_layer"]))
            self.assertTrue(
                all(node["id"].startswith("page-1-s") for node in review_record["semantic_layer"])
            )
            self.assertTrue(
                all(node["source_regions"] for node in review_record["semantic_layer"])
            )
            self.assertIn("spoken_math_alternative", review_record["semantic_layer"][2])
            # The default reconstruction is a complex Informative Figure, so it
            # carries both its concise Alternative and its Detailed Description.
            self.assertEqual(review_record["semantic_layer"][3]["complexity"], "complex")
            self.assertIn("figure_alternative", review_record["semantic_layer"][3])
            self.assertIn("detailed_figure_description", review_record["semantic_layer"][3])
            # The Semantic Table preserves its caption, headers, and cells.
            table = review_record["semantic_layer"][4]
            self.assertEqual(table["type"], "table")
            self.assertTrue(table["caption"])
            self.assertEqual(table["rows"][0]["cells"][0]["scope"], "col")
            self.assertEqual(review_record["warnings"], [])
            # Recognition Candidates retain source context under identities distinct
            # from the canonical Source Regions; crop paths are derived, not stored.
            self.assertTrue(review_record["candidates"])
            self.assertTrue(all("crop" not in c for c in review_record["candidates"]))
            self.assertTrue(
                all(candidate["id"].startswith("page-1-c") for candidate in review_record["candidates"])
            )
            self.assertTrue(all(c["source_region"] for c in review_record["candidates"]))
            self.assertEqual(review_record["reconstruction"]["page_prompt_version"], "1.4")
            self.assertEqual(
                review_record["reconstruction"]["provider_model"],
                "acceptance-model-2026-07-19",
            )
            self.assertEqual(len(review_record["reconstruction"]["pages"]), 1)
            self.assertEqual(review_record["reconstruction"]["pages"][0]["page"], 1)
            self.assertEqual(
                len(review_record["reconstruction"]["pages"][0]["verified_regions"]), 3
            )
            # The baseline mirrors the freshly built record for history tracking.
            baseline = json.loads((bundle / "review-baseline.json").read_text())
            self.assertEqual(baseline["semantic_layer"], review_record["semantic_layer"])

            page_semantics = json.loads((bundle / "page-semantics" / "page-1.json").read_text())
            # Reconstruction selects Source Regions; durable node identity and page
            # metadata are added while assembling the Review Record.
            self.assertEqual(
                page_semantics["semantic_layer"],
                [
                    {
                        key: value
                        for key, value in node.items()
                        if key not in {"id", "page"}
                    }
                    for node in review_record["semantic_layer"]
                ],
            )
            self.assertEqual(page_semantics["title"], review_record["title"])

            provenance = json.loads((bundle / "provenance.json").read_text())
            self.assertTrue(provenance["source_copy_verified"])
            self.assertEqual((bundle / "source.pdf").read_bytes(), SOURCE.read_bytes())
            self.assertEqual(
                provenance["source_sha256"], hashlib.sha256(SOURCE.read_bytes()).hexdigest()
            )
            self.assertEqual(provenance["source_pages"], [1])
            self.assertEqual(provenance["page_prompt_version"], "1.4")
            self.assertEqual(provenance["page_schema_version"], "1.1")
            # capability check plus one page call and one call per crop region.
            self.assertEqual(provenance["provider_usage"]["actual_requests"], 5)
            self.assertEqual(provenance["provider_usage"]["estimated_requests"], 5)
            self.assertEqual(provenance["provider_usage"]["request_ceiling"], 100)
            self.assertIn('<html lang="en-US">', (bundle / "review-report.html").read_text())

            internal = json.loads((bundle / "validation/internal.json").read_text())
            self.assertEqual(internal["checks"], [])
            self.assertTrue(internal["passed"])
            self.assertEqual(internal["source"], "output.pdf structure tree")
            # Every internal semantic-check category passed.
            self.assertTrue(all(internal["categories"].values()), internal["categories"])
            self.assertEqual(
                set(internal["categories"]),
                {
                    "review-record-consistency",
                    "reading-order",
                    "source-region-coverage",
                    "alternatives",
                    "table-relationships",
                    "recognition-agreement",
                },
            )

            visual = json.loads((bundle / "validation/visual.json").read_text())
            self.assertTrue(visual["passed"])
            self.assertLessEqual(visual["max_different_pixel_ratio"], visual["tolerance"])
            self.assertEqual(len(visual["pages"]), 1)
            self.assertEqual(visual["pages"][0]["source_page"], 1)
            self.assertEqual(visual["pages"][0]["source_width"], visual["pages"][0]["output_width"])
            self.assertEqual(
                visual["pages"][0]["source_height"], visual["pages"][0]["output_height"]
            )

            verapdf_report = (bundle / "validation/verapdf.xml").read_text()
            self.assertIn('isCompliant="true"', verapdf_report)

    def test_whole_document_conversion_covers_all_eleven_pages(self) -> None:
        # The acceptance gate for issue #13: convert the entire 11-page sample into
        # one visually faithful, internally verifiable, PDF/UA-1 conformant document.
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "whole-document.accessibilizer"
            # No --page: the whole document is converted by default.
            result = self.run_conversion(SOURCE, bundle, page=None)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], "accessible")

            record = yaml.safe_load((bundle / "review-record.yaml").read_text())
            self.assertEqual(record["pages"], list(range(1, 12)))
            # Each of the 11 pages contributes its five reconstructed nodes, tagged
            # with the source page they belong to.
            self.assertEqual(len(record["semantic_layer"]), 55)
            self.assertEqual(
                sorted({node["page"] for node in record["semantic_layer"]}),
                list(range(1, 12)),
            )
            self.assertEqual(len(record["reconstruction"]["pages"]), 11)

            provenance = json.loads((bundle / "provenance.json").read_text())
            self.assertEqual(provenance["source_pages"], list(range(1, 12)))
            # capability check once, plus one page call and three crop calls per page.
            self.assertEqual(provenance["provider_usage"]["actual_requests"], 1 + 11 * 4)

            # Full-page visual regression covers all 11 pages within tolerance.
            visual = json.loads((bundle / "validation/visual.json").read_text())
            self.assertTrue(visual["passed"])
            self.assertEqual([p["source_page"] for p in visual["pages"]], list(range(1, 12)))

            # Clean internal semantic checks and independent PDF/UA-1 validation.
            internal = json.loads((bundle / "validation/internal.json").read_text())
            self.assertTrue(internal["passed"], internal["checks"])
            self.assertIn(
                'isCompliant="true"', (bundle / "validation/verapdf.xml").read_text()
            )
            # The whole-document output carries a bookmark outline for navigation.
            self.assertIn(b"/Outlines", (bundle / "output.pdf").read_bytes())

    def test_conversion_events_log_is_versioned_secret_free_and_confined_to_stderr(
        self,
    ) -> None:
        usage = {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}
        with (
            FakeProvider(usage=usage) as provider,
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            bundle = Path(temporary_directory) / "observed.accessibilizer"
            result = self.run_conversion(SOURCE, bundle, base_url=provider.base_url)

            self.assertEqual(result.returncode, 0, result.stderr)
            # stdout carries exactly one machine-readable result; all progress
            # (including the request estimate) is confined to stderr.
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            self.assertEqual(json.loads(result.stdout)["status"], "accessible")
            self.assertIn("Estimated provider requests", result.stderr)
            self.assertNotIn("Estimated provider requests", result.stdout)

            events_path = bundle / "conversion-events.jsonl"
            self.assertTrue(events_path.is_file())
            events = read_event_lines(events_path)
            self.assertTrue(events)
            for event in events:
                self.assertEqual(event["schema_version"], "1.0")
                jsonschema.validate(instance=event, schema=CONVERSION_EVENTS_SCHEMA)

            # Every named major stage emits a start and a completion event.
            for stage in MAJOR_STAGES:
                states = {e["state"] for e in events if e["stage"] == stage}
                self.assertIn("started", states, stage)
                self.assertIn("completed", states, stage)

            # Per-page events report the current page and selected-page count.
            per_page = [e for e in events if e["stage"] == "page-recognition"]
            self.assertTrue(per_page)
            self.assertTrue(all(e.get("page") == 1 and e.get("page_count") == 1 for e in per_page))

            # Provider requests identify purpose, count, endpoint, and model
            # immediately before transmission, and report token usage on completion.
            started = [
                e for e in events
                if e["stage"] == "provider-reconstruction" and e["state"] == "started"
            ]
            self.assertTrue(started)
            for key in ("purpose", "request", "request_total", "endpoint", "model"):
                self.assertIn(key, started[0])
            completed = [
                e for e in events
                if e["stage"] == "provider-reconstruction" and e["state"] == "completed"
            ]
            self.assertTrue(any("token_usage" in e for e in completed))

            # The log never carries a secret, an encoded image, or model content.
            raw = events_path.read_text(encoding="utf-8")
            for forbidden in FORBIDDEN_IN_EVENTS:
                self.assertNotIn(forbidden, raw)
            # No Source PDF prose or reconstructed text leaks into the log either.
            self.assertNotIn("Electric current is the rate", raw)

    def test_ctrl_c_interruption_exits_130_preserves_state_and_resumes(self) -> None:
        with (
            FakeProvider(page_response_delay=20) as provider,
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            temporary = Path(temporary_directory)
            bundle = temporary / "interrupted.accessibilizer"
            command = self.conversion_command(SOURCE, bundle, base_url=provider.base_url)
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.conversion_environment(),
            )
            workspace = temporary / ".interrupted.accessibilizer.in-progress"
            events_path = workspace / "conversion-events.jsonl"

            def reconstruction_started() -> bool:
                if not events_path.is_file():
                    return False
                return any(
                    event["stage"] == "provider-reconstruction"
                    and event["state"] == "started"
                    for event in read_event_lines(events_path)
                )

            deadline = time.monotonic() + 60
            while not reconstruction_started() and process.poll() is None:
                if time.monotonic() >= deadline:
                    process.kill()
                    self.fail("a provider request never started before the timeout")
                time.sleep(0.05)
            self.assertIsNone(process.poll(), "conversion finished before interruption")

            cid_directories = list(
                temporary.glob(".interrupted.accessibilizer.container.*")
            )
            self.assertEqual(len(cid_directories), 1)
            container_id = (cid_directories[0] / "id").read_text().strip()
            # A deliberate Ctrl-C-equivalent: SIGTERM to the running container.
            signalled = subprocess.run(
                ["docker", "kill", "--signal=TERM", container_id],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(signalled.returncode, 0, signalled.stderr)
            stdout, stderr = process.communicate(timeout=30)

            # Exit 130, one machine-readable interruption result on stdout, and
            # the resume command / diagnostics only on stderr.
            self.assertEqual(process.returncode, 130, stderr)
            interruption = json.loads(stdout)
            self.assertEqual(interruption["status"], "interrupted")
            self.assertEqual(interruption["stage"], "provider-reconstruction")
            self.assertIn("--resume", interruption["resume_command"])
            self.assertNotIn("Estimated provider requests", stdout)
            self.assertIn("Resume with", stderr)

            # State is preserved: the bundle is not published, and the durable log
            # records the interrupted operation and its resume command.
            self.assertFalse(bundle.exists())
            self.assertTrue(events_path.is_file())
            interrupted_events = [
                e for e in read_event_lines(events_path) if e["state"] == "interrupted"
            ]
            self.assertEqual(len(interrupted_events), 1)
            self.assertEqual(interrupted_events[0]["stage"], "provider-reconstruction")
            self.assertIn("resume_command", interrupted_events[0])

            # Resuming reuses the completed stages (reported as reused) and finishes;
            # the durable log survives the interruption/resume into the final bundle.
            provider.page_response_delay = 0.0
            resumed = self.run_conversion(SOURCE, bundle, resume=True, base_url=provider.base_url)
            self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)

            final_events = read_event_lines(bundle / "conversion-events.jsonl")
            self.assertTrue(any(e["state"] == "interrupted" for e in final_events))
            reused_stages = {e["stage"] for e in final_events if e["state"] == "reused"}
            self.assertIn("provider-capability", reused_stages)
            self.assertIn("page-recognition", reused_stages)

    def test_verbose_flag_is_forwarded_and_adds_technical_detail_on_stderr(self) -> None:
        with (
            FakeProvider() as provider,
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            bundle = Path(temporary_directory) / "verbose.accessibilizer"
            result = self.run_conversion(
                SOURCE, bundle, base_url=provider.base_url, verbose=True
            )

            # The launcher accepts and forwards --verbose (it is not rejected as an
            # unknown argument), and the extra detail appears on stderr only.
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("model=acceptance-model-2026-07-19", result.stderr)
            self.assertNotIn("model=", result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], "accessible")

    def test_provider_retries_are_reported_and_logged(self) -> None:
        with (
            FakeProvider(transient_failures=2) as provider,
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            bundle = Path(temporary_directory) / "retried.accessibilizer"
            result = self.run_conversion(SOURCE, bundle, base_url=provider.base_url)

            self.assertEqual(result.returncode, 0, result.stderr)
            events = read_event_lines(bundle / "conversion-events.jsonl")
            retrying = [e for e in events if e["state"] == "retrying"]
            self.assertGreaterEqual(len(retrying), 2)
            for event in retrying:
                jsonschema.validate(instance=event, schema=CONVERSION_EVENTS_SCHEMA)
                self.assertIn("attempt", event)
                self.assertIn("delay", event)
                # The retry reason is a safe summary, never a raw response body.
                self.assertTrue(
                    event["detail"].startswith("HTTP") or event["detail"] == "connection error",
                    event["detail"],
                )
            self.assertIn("retrying", result.stderr)

    def test_provider_failure_is_recorded_as_a_failed_event(self) -> None:
        with (
            FakeProvider(compatible=False) as provider,
            tempfile.TemporaryDirectory() as temporary_directory,
        ):
            temporary = Path(temporary_directory)
            bundle = temporary / "failed.accessibilizer"
            result = self.run_conversion(SOURCE, bundle, base_url=provider.base_url)

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "operational_failure")
            self.assertFalse(bundle.exists())
            workspace = temporary / ".failed.accessibilizer.in-progress"
            events = read_event_lines(workspace / "conversion-events.jsonl")
            failed = [e for e in events if e["state"] == "failed"]
            self.assertTrue(failed)
            self.assertEqual(failed[0]["stage"], "provider-capability")
            # The error state is recorded without a message that could leak content.
            self.assertNotIn("detail", failed[0])


class AuthoringCapabilityTest(unittest.TestCase):
    """Author supported node types directly and read them back out of the PDF.

    Exercises the PDF/UA-1 representations the reconstruction pipeline does not
    emit for the scanned sample — a heading hierarchy (H1 through H3) and a
    reconstructable link — proving they survive authoring and validate.
    """

    CONTRACT: dict[str, Any] = {
        "schema_version": "2.0",
        "title": "Authoring Capability Fixture",
        "language": "en-US",
        "pages": [
            {
                "source_page": 1,
                "semantic_layer": [
                    {"type": "heading", "level": 1, "text": "Chapter"},
                    {"type": "heading", "level": 2, "text": "Section"},
                    {"type": "heading", "level": 3, "text": "Subsection"},
                    {"type": "paragraph", "text": "A paragraph of body text."},
                    {"type": "link", "text": "Ohm's Law", "href": "https://example.org/ohm"},
                ],
            },
            {
                "source_page": 2,
                "semantic_layer": [
                    {"type": "heading", "level": 1, "text": "Second Chapter"},
                    {"type": "paragraph", "text": "More body text on the next page."},
                ],
            },
        ],
    }

    def test_heading_hierarchy_and_link_survive_authoring_and_validate(self) -> None:
        image = os.environ.get("ACCESSIBILIZER_IMAGE", "accessibilizer:test")
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            (temporary / "contract.json").write_text(json.dumps(self.CONTRACT))
            script = (
                "set -e\n"
                "jar=/opt/accessibilizer/pdf-author.jar\n"
                'java -jar "$jar" /work/contract.json "$SOURCE" /work/out.pdf\n'
                'java -jar "$jar" --inspect /work/out.pdf > /work/inspect.json\n'
                'verapdf --format xml -f ua1 /work/out.pdf > /work/verapdf.xml || true\n'
            )
            result = subprocess.run(
                [
                    "docker", "run", "--rm", "--network", "none",
                    "--volume", f"{temporary}:/work",
                    "--volume", f"{SOURCE}:/source.pdf:ro",
                    "--env", "SOURCE=/source.pdf",
                    image, "bash", "-c", script,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            inspected = json.loads((temporary / "inspect.json").read_text())
            self.assertEqual(len(inspected["pages"]), 2)
            first = inspected["pages"][0]["semantic_layer"]
            self.assertEqual(
                [(node["type"], node.get("level")) for node in first[:3]],
                [("heading", 1), ("heading", 2), ("heading", 3)],
            )
            link = next(node for node in first if node["type"] == "link")
            self.assertEqual(link["text"], "Ohm's Law")
            self.assertEqual(link["href"], "https://example.org/ohm")
            self.assertEqual(inspected["pages"][1]["semantic_layer"][0]["level"], 1)

            self.assertIn(
                'isCompliant="true"', (temporary / "verapdf.xml").read_text()
            )


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
        self.assertIn("usage: accessibilizer {convert,report,review,validate,finalize}", result.stderr)


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
