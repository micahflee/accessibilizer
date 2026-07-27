from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from accessibilizer.provider import ProviderConfig
from accessibilizer.vision_only_run import PageInput, run_source_pdf_vision_only


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQ"
    "DJ/pLvAAAAAElFTkSuQmCC"
)


class FullDocumentVisionOnlyRunTest(unittest.TestCase):
    def test_replay_processes_all_eleven_pages_into_an_auditable_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path("testdata/Chapter 20_ Electric Current Resistance and Ohms Law.pdf")
            pages: list[PageInput] = []
            for page_number in range(1, 12):
                image = root / f"replay-page-{page_number}.png"
                image.write_bytes(PNG_BYTES)
                words: list[dict[str, Any]] = []
                if page_number != 7:
                    words = [{
                        "text": f"Native page {page_number}",
                        "bbox_points": [10.0, 20.0, 100.0, 30.0],
                    }]
                pages.append(PageInput(page_number, image, words, 612.0, 792.0))

            calls: list[dict[str, Any]] = []

            def replay(payload: dict[str, Any]) -> dict[str, Any]:
                calls.append(payload)
                page_number = len(calls)
                response = {
                    "title": f"Gold page {page_number}",
                    "language": "en-US",
                    "primary_language_is_english": True,
                    "document_class": "stem_instructional",
                    "reading_order_is_unambiguous": True,
                    "nodes": [{
                        "type": "paragraph",
                        "text": f"Semantic content for page {page_number}",
                        "boxes": [[0.1, 0.1, 0.9, 0.2]],
                    }],
                    "suspected_source_errors": [],
                    "suspected_prompt_injection": page_number == 3,
                }
                return {
                    "choices": [{"message": {"content": json.dumps(response)}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }

            config = ProviderConfig("https://example.test/v1", "gpt-5.6-sol", None, "remote")
            with patch(
                "accessibilizer.vision_only_run._prepare_page_inputs",
                return_value=pages,
            ) as prepare:
                first = run_source_pdf_vision_only(
                    config,
                    source_pdf=source,
                    runs_directory=root / "runs",
                    requester=replay,
                )
                second = run_source_pdf_vision_only(
                    config,
                    source_pdf=source,
                    runs_directory=root / "runs",
                    requester=replay,
                )

            self.assertEqual(prepare.call_count, 2)

            self.assertNotEqual(first.run_id, second.run_id)
            self.assertEqual(len(calls), 22)
            manifest = json.loads(first.manifest_path.read_text())
            self.assertEqual(manifest["run_id"], first.run_id)
            self.assertEqual(manifest["model"], "gpt-5.6-sol")
            self.assertEqual(manifest["page_count"], 11)
            self.assertEqual(manifest["logical_request_count"], 11)
            self.assertEqual(
                manifest["logical_reading_order"],
                [f"page-{page}-s0001" for page in range(1, 12)],
            )
            self.assertEqual([page["page"] for page in manifest["pages"]], list(range(1, 12)))

            page_seven = first.run_directory / "pages" / "page-7"
            request = json.loads((page_seven / "request.json").read_text())
            response = json.loads((page_seven / "response.json").read_text())
            normalized = json.loads((page_seven / "normalized.json").read_text())
            inputs = json.loads((page_seven / "inputs.json").read_text())
            self.assertIsInstance(inputs["native_pdf_words"], list)
            self.assertEqual(inputs["native_pdf_words"], [])
            self.assertFalse(inputs["native_pdf_evidence_authoritative"])
            self.assertEqual(request["model"], "gpt-5.6-sol")
            self.assertEqual(request["response_format"]["schema"], manifest["schema"])
            self.assertEqual(response["nodes"][0]["text"], "Semantic content for page 7")
            self.assertEqual(normalized["page"], 7)
            self.assertEqual(normalized["semantic_layer"][0]["id"], "page-7-s0001")
            self.assertEqual(normalized["source_regions"][0]["page"], 7)
            self.assertEqual(
                normalized["semantic_layer"][0]["source_regions"],
                ["page-7-r0001"],
            )
            self.assertNotIn("authorization", json.dumps(manifest).lower())
            page_three = json.loads(
                (first.run_directory / "pages" / "page-3" / "normalized.json").read_text()
            )
            self.assertEqual(page_three["warnings"][0]["code"], "prompt-injection")


if __name__ == "__main__":
    unittest.main()
