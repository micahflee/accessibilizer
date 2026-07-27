from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from typing import Any

from accessibilizer.provider import ProviderConfig
from accessibilizer.vision_prototype import (
    reconstruct_prototype_document,
    reconstruct_prototype_page,
    replay_prototype_document,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"
POPPLER = all(shutil.which(tool) is not None for tool in ("pdfinfo", "pdftoppm", "pdftotext"))


def page_response(**overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "nodes": [
            {
                "type": "heading",
                "level": 1,
                "text": "Electric Current, Resistance, and Ohm's Law",
                "boxes": [[0.1, 0.1, 0.9, 0.2]],
            },
            {
                "type": "paragraph",
                "text": "Electric current is the rate at which charge flows.",
                "boxes": [[0.1, 0.1, 0.9, 0.2]],
            },
        ],
        "warnings": [
            {
                "code": "ambiguous-reading-order",
                "message": "Either column could plausibly be read first.",
                "node_indices": [0],
                "boxes": [[0.05, 0.05, 0.95, 0.45]],
            }
        ],
    }
    response.update(overrides)
    return response


class FakeVisionProvider:
    def __init__(
        self,
        response: dict[str, Any] | list[dict[str, Any]],
        *,
        expect_native_context: bool,
    ) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.expect_native_context = expect_native_context
        self.requests: list[dict[str, Any]] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                request: Any = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    self.send_error(400)
                    return
                provider.requests.append(request)
                try:
                    response_format = request["response_format"]
                    contents = request["messages"][1]["content"]
                    text_items = [
                        item["text"]
                        for item in contents
                        if item.get("type") == "text"
                    ]
                    context = next(
                        json.loads(text[text.index("{") :])
                        for text in text_items
                        if "{" in text
                    )
                    image_url = next(
                        item["image_url"]["url"]
                        for item in contents
                        if item.get("type") == "image_url"
                    )
                    image = base64.b64decode(
                        image_url.removeprefix("data:image/png;base64,"), validate=True
                    )
                    valid = (
                        response_format["type"] == "json_schema"
                        and response_format["json_schema"]["strict"] is True
                        and "tools" not in request
                        and "functions" not in request
                        and image.startswith(b"\x89PNG\r\n\x1a\n")
                        and "untrusted" in " ".join(text_items).lower()
                        and "non-authoritative" in " ".join(text_items).lower()
                        and isinstance(context["native_pdf_words"], list)
                        and bool(context["native_pdf_words"])
                        is provider.expect_native_context
                    )
                except (KeyError, StopIteration, TypeError, ValueError):
                    valid = False
                if not valid:
                    self.send_error(400)
                    return
                response_index = len(provider.requests) - 1
                body = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    provider.responses[response_index % len(provider.responses)],
                                    allow_nan=True,
                                )
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100 + response_index,
                        "completion_tokens": 20,
                        "total_tokens": 120 + response_index,
                    },
                }
                encoded = json.dumps(body, allow_nan=True).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def config(self) -> ProviderConfig:
        return ProviderConfig(
            base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
            model="exact-model",
            api_key_env=None,
            data_location="local",
        )

    def __enter__(self) -> FakeVisionProvider:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@unittest.skipUnless(POPPLER, "poppler is required for the gold-page prototype")
class VisionOnlyPagePrototypeTest(unittest.TestCase):
    def reconstruct(
        self,
        provider: FakeVisionProvider,
        directory: str,
        *,
        include_native_pdf_context: bool = True,
    ) -> dict[str, Any]:
        return reconstruct_prototype_page(
            provider.config,
            source_pdf=SOURCE,
            page=1,
            artifacts_dir=Path(directory),
            include_native_pdf_context=include_native_pdf_context,
            max_retries=0,
        )

    def test_gold_page_is_reconstructed_in_one_request_with_deterministic_identity(
        self,
    ) -> None:
        with (
            FakeVisionProvider(page_response(), expect_native_context=True) as provider,
            tempfile.TemporaryDirectory() as directory,
        ):
            result = self.reconstruct(provider, directory)

        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(result["page"], 1)
        self.assertEqual(
            result["page_dimensions"],
            {"width_points": 612.0, "height_points": 803.25},
        )
        self.assertEqual(
            result["source_regions"],
            [
                {
                    "id": "page-1-r0001",
                    "page": 1,
                    "bbox_points": [30.6, 40.1625, 581.4, 361.4625],
                },
                {
                    "id": "page-1-r0002",
                    "page": 1,
                    "bbox_points": [61.2, 80.325, 550.8, 160.65],
                },
            ],
        )
        self.assertEqual(
            [node["id"] for node in result["semantic_layer"]],
            ["page-1-s0001", "page-1-s0002"],
        )
        self.assertEqual(
            [node["source_regions"] for node in result["semantic_layer"]],
            [["page-1-r0002"], ["page-1-r0002"]],
        )
        self.assertEqual(
            result["warnings"],
            [
                {
                    "id": "page-1-w0001",
                    "page": 1,
                    "code": "ambiguous-reading-order",
                    "message": "Either column could plausibly be read first.",
                    "semantic_nodes": ["page-1-s0001"],
                    "source_regions": ["page-1-r0001"],
                }
            ],
        )

    def test_gold_page_can_be_reconstructed_without_native_pdf_context(self) -> None:
        with (
            FakeVisionProvider(page_response(), expect_native_context=False) as provider,
            tempfile.TemporaryDirectory() as directory,
        ):
            result = self.reconstruct(
                provider, directory, include_native_pdf_context=False
            )

        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(len(result["semantic_layer"]), 2)
        self.assertEqual(result["candidates"], [])

    def test_malformed_model_geometry_is_rejected_before_normalized_output(self) -> None:
        malformed_boxes: list[Any] = [
            [],
            [[0.1, 0.2, 0.3]],
            [[math.nan, 0.1, 0.2, 0.3]],
            [[0.5, 0.1, 0.5, 0.2]],
            [[0.8, 0.1, 0.2, 0.3]],
            [[-0.1, 0.1, 0.2, 0.3]],
            [[0.1, 0.1, 1.1, 0.3]],
        ]
        for boxes in malformed_boxes:
            with self.subTest(boxes=boxes):
                response = page_response()
                response["nodes"][0]["boxes"] = boxes
                with (
                    FakeVisionProvider(response, expect_native_context=False) as provider,
                    tempfile.TemporaryDirectory() as directory,
                    self.assertRaisesRegex(ValueError, "prototype schema|normalized box"),
                ):
                    self.reconstruct(
                        provider, directory, include_native_pdf_context=False
                    )

    def test_gold_document_runs_are_independent_replayable_and_auditable(self) -> None:
        responses = []
        for page in range(1, 12):
            response = page_response()
            response["nodes"][0]["text"] = f"Gold page {page}"
            response["warnings"] = []
            responses.append(response)

        with (
            FakeVisionProvider(responses, expect_native_context=True) as provider,
            tempfile.TemporaryDirectory() as directory,
        ):
            artifacts_root = Path(directory)
            first = reconstruct_prototype_document(
                provider.config,
                source_pdf=SOURCE,
                artifacts_root=artifacts_root,
                run_id="run-alpha",
                max_retries=0,
            )
            second = reconstruct_prototype_document(
                provider.config,
                source_pdf=SOURCE,
                artifacts_root=artifacts_root,
                run_id="run-beta",
                max_retries=0,
            )

            self.assertEqual(len(provider.requests), 22)
            self.assertEqual(first["run_id"], "run-alpha")
            self.assertEqual(second["run_id"], "run-beta")
            self.assertEqual([page["page"] for page in first["pages"]], list(range(1, 12)))
            self.assertEqual(
                [page["semantic_layer"][0]["text"] for page in first["pages"]],
                [f"Gold page {page}" for page in range(1, 12)],
            )
            self.assertEqual(first["request_usage"]["actual_requests"], 11)
            self.assertEqual(first["request_usage"]["request_ceiling"], 22)
            self.assertEqual(first["model"], "exact-model")

            for run_id in ("run-alpha", "run-beta"):
                run_dir = artifacts_root / run_id
                manifest = json.loads((run_dir / "manifest.json").read_text())
                self.assertEqual(manifest["run_id"], run_id)
                self.assertEqual(manifest["page_count"], 11)
                self.assertEqual(manifest["model"], "exact-model")
                self.assertEqual(manifest["request_usage"]["actual_requests"], 11)
                self.assertNotIn("authorization", json.dumps(manifest).lower())
                replayed = replay_prototype_document(run_dir)
                self.assertEqual(replayed["run_id"], run_id)
                self.assertEqual(replayed["pages"], manifest["pages"])
                self.assertTrue((run_dir / "prompt" / "system.txt").is_file())
                self.assertTrue((run_dir / "prompt" / "page.txt").is_file())
                self.assertTrue((run_dir / "prompt" / "schema.json").is_file())
                for page in range(1, 12):
                    page_dir = run_dir / "pages" / f"page-{page}"
                    self.assertTrue((page_dir / "page.png").is_file())
                    self.assertTrue((page_dir / "input.json").is_file())
                    self.assertTrue((page_dir / "response.json").is_file())
                    normalized = json.loads(
                        (page_dir / "normalized.json").read_text()
                    )
                    self.assertEqual(normalized["page"], page)

            tampered_manifest = json.loads(
                (artifacts_root / "run-alpha" / "manifest.json").read_text()
            )
            tampered_manifest["page_artifacts"][0]["input"] = "../outside.json"
            (artifacts_root / "run-alpha" / "manifest.json").write_text(
                json.dumps(tampered_manifest)
            )
            with self.assertRaisesRegex(ValueError, "inside the run directory"):
                replay_prototype_document(artifacts_root / "run-alpha")


if __name__ == "__main__":
    unittest.main()
