from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "testdata" / "Chapter 20_ Electric Current Resistance and Ohms Law.pdf"

CAPABILITY_IMAGE_SHA256 = "8640b42788b7bf45cc580582ac9b6b77f05b55512fc290f48040259ee8f03c9e"

# A reconstruction consistent with the fake recognition backend, so the clean
# path produces no Conversion Warnings. Tests inject warnings via page_overrides.
BASE_PAGE_CONTENT: dict[str, Any] = {
    "title": "Electric Current, Resistance, and Ohm's Law",
    "language": "en-US",
    "primary_language_is_english": True,
    "document_class": "stem_instructional",
    "reading_order": ["heading", "paragraph", "formula", "figure", "table"],
    "reading_order_is_unambiguous": True,
    "heading": {"level": 1, "text": "Electric Current, Resistance, and Ohm's Law"},
    "paragraph": {"text": "Electric current is the rate at which charge flows."},
    "formula": {
        "normalized_math": "I = Q / delta t",
        "spoken_math_alternative": "I equals Q divided by delta t.",
    },
    "figure": {
        "complexity": "complex",
        "figure_alternative": "A wire carrying electric current.",
        "detailed_figure_description": (
            "A wire passes through a surface; positive charge moves along it in the "
            "direction of conventional current."
        ),
    },
    "table": {
        "caption": "Resistivity of common materials at 20 degrees Celsius",
        "boundaries_are_uncertain": False,
        "headers_are_uncertain": False,
        "rows": [
            {
                "cells": [
                    {"kind": "header", "text": "Material", "scope": "col",
                     "row_span": 1, "col_span": 1},
                    {"kind": "header", "text": "Resistivity (ohm-metre)", "scope": "col",
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
    },
    "suspected_source_errors": [],
    "suspected_prompt_injection": False,
}


def _find_image_url(request: dict[str, Any]) -> str:
    for message in request.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return str(part["image_url"]["url"])
    raise KeyError("no image_url in request")


class FakeProvider:
    def __init__(
        self,
        *,
        compatible: bool = True,
        transient_failures: int = 0,
        usage: dict[str, int] | None = None,
        page_overrides: dict[str, Any] | None = None,
        region_agrees: bool = True,
    ) -> None:
        self.compatible = compatible
        self.transient_failures = transient_failures
        self.usage = usage
        self.page_overrides = page_overrides or {}
        self.region_agrees = region_agrees
        self.requests: list[dict[str, Any]] = []
        self.authorizations: list[str | None] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                request: Any = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    self.send_error(400)
                    return
                provider.requests.append(request)
                provider.authorizations.append(self.headers.get("Authorization"))
                # Source content is untrusted: a compliant request never exposes tools.
                if "tools" in request or "functions" in request:
                    self.send_error(400)
                    return
                try:
                    response_format = request["response_format"]
                    name = response_format["json_schema"]["name"]
                    image_url = _find_image_url(request)
                    image = base64.b64decode(
                        image_url.removeprefix("data:image/png;base64,"), validate=True
                    )
                    valid_contract = (
                        image_url.startswith("data:image/png;base64,")
                        and response_format["type"] == "json_schema"
                        and response_format["json_schema"]["strict"] is True
                        and isinstance(request["max_completion_tokens"], int)
                        and "max_tokens" not in request
                    )
                    if name == "accessibilizer_capability_check":
                        valid_contract = valid_contract and (
                            hashlib.sha256(image).hexdigest() == CAPABILITY_IMAGE_SHA256
                        )
                except (KeyError, IndexError, TypeError, ValueError):
                    valid_contract = False
                    name = ""
                if not valid_contract:
                    self.send_error(400)
                    return
                if provider.transient_failures:
                    provider.transient_failures -= 1
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                if not provider.compatible:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"response_format unsupported"}}')
                    return
                if name == "accessibilizer_capability_check":
                    body: Any = {"blue_square_count": 3}
                elif name == "accessibilizer_page_semantics":
                    body = json.loads(json.dumps(BASE_PAGE_CONTENT))
                    evidence = request["messages"][1]["content"][1]["text"]
                    evidence_document = json.loads(evidence.split("\n", 1)[1])
                    region_ids = evidence_document.get("source_regions") or [
                        evidence_document["recognition_candidates"][0]["id"]
                    ]
                    for node_name in ("heading", "paragraph", "formula", "figure", "table"):
                        body[node_name]["source_regions"] = [region_ids[1] if len(region_ids) > 1 else region_ids[0]]
                    body.update(provider.page_overrides)
                    for node_name in ("heading", "paragraph", "formula", "figure", "table"):
                        body[node_name].setdefault("source_regions", [region_ids[1] if len(region_ids) > 1 else region_ids[0]])
                elif name == "accessibilizer_region_check":
                    body = {
                        "transcription": "I = Q / delta t",
                        "agrees_with_page": provider.region_agrees,
                        "suspected_prompt_injection": False,
                    }
                else:
                    self.send_error(400)
                    return
                response: dict[str, Any] = {
                    "choices": [{"message": {"content": json.dumps(body)}}]
                }
                if provider.usage is not None:
                    response["usage"] = provider.usage
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://host.docker.internal:{self.server.server_port}/v1"

    @property
    def loopback_base_url(self) -> str:
        return f"http://localhost:{self.server.server_port}/v1"

    def __enter__(self) -> FakeProvider:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ProviderConfigurationTest(unittest.TestCase):
    def run_conversion(
        self,
        project: Path,
        bundle: Path,
        *,
        extra_arguments: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(ROOT / "accessibilizer"),
                "convert",
                str(SOURCE),
                "--page",
                "1",
                "--bundle",
                str(bundle),
                *extra_arguments,
                "--json",
            ],
            cwd=project,
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "ACCESSIBILIZER_IMAGE": "accessibilizer:test",
                "ACCESSIBILIZER_RECOGNITION_BACKEND": "fake",
                # Isolate from any personal ~/.config/accessibilizer/config.toml;
                # tests that exercise configuration override XDG_CONFIG_HOME.
                "XDG_CONFIG_HOME": str(project / "no-user-config"),
                **(environment or {}),
            },
        )

    def test_configuration_precedence_and_secret_free_provenance(self) -> None:
        with FakeProvider() as provider, tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            user_config_home = temporary / "user-config"
            user_config = user_config_home / "accessibilizer" / "config.toml"
            user_config.parent.mkdir(parents=True)
            user_config.write_text(
                "[provider]\n"
                'base_url = "http://invalid.example/v1"\n'
                'model = "user-model"\n'
                'api_key_env = "USER_PROVIDER_KEY"\n'
                'data_location = "remote"\n'
            )
            (temporary / "accessibilizer.toml").write_text(
                "[provider]\n"
                f'base_url = "{provider.base_url}"\n'
                'model = "project-model"\n'
                'api_key_env = "PROJECT_PROVIDER_KEY"\n'
            )
            bundle = temporary / "configured.accessibilizer"

            result = self.run_conversion(
                temporary,
                bundle,
                extra_arguments=(
                    "--provider-model",
                    "cli-model-2026-07-19",
                    "--allow-remote",
                ),
                environment={
                    "XDG_CONFIG_HOME": str(user_config_home),
                    "USER_PROVIDER_KEY": "wrong-secret",
                    "PROJECT_PROVIDER_KEY": "correct-secret",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            # capability check, one page-level call, and one call per crop region.
            self.assertEqual(len(provider.requests), 5)
            schema_names = [
                request["response_format"]["json_schema"]["name"]
                for request in provider.requests
            ]
            self.assertEqual(schema_names[0], "accessibilizer_capability_check")
            self.assertEqual(schema_names[1], "accessibilizer_page_semantics")
            self.assertEqual(
                schema_names[2:], ["accessibilizer_region_check"] * 3
            )
            for request in provider.requests:
                self.assertEqual(request["model"], "cli-model-2026-07-19")
                self.assertEqual(request["response_format"]["type"], "json_schema")
                self.assertNotIn("tools", request)
                self.assertNotIn("functions", request)
            image = provider.requests[0]["messages"][0]["content"][1]["image_url"]["url"]
            self.assertTrue(image.startswith("data:image/png;base64,"))
            self.assertEqual(provider.authorizations, ["Bearer correct-secret"] * 5)

            provenance = json.loads((bundle / "provenance.json").read_text())
            self.assertEqual(provenance["provider_endpoint"], provider.base_url)
            self.assertEqual(provenance["provider_model"], "cli-model-2026-07-19")
            self.assertEqual(provenance["provider_data_location"], "remote")
            serialized = json.dumps(provenance)
            self.assertNotIn("correct-secret", serialized)
            self.assertNotIn("PROJECT_PROVIDER_KEY", serialized)
            self.assertNotIn("reasoning", serialized)

    def test_incompatible_provider_fails_before_bundle_creation(self) -> None:
        with FakeProvider(compatible=False) as provider, tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            bundle = temporary / "incompatible.accessibilizer"

            result = self.run_conversion(
                temporary,
                bundle,
                extra_arguments=(
                    "--provider-base-url",
                    provider.loopback_base_url,
                    "--provider-model",
                    "exact-model",
                    "--provider-data-location",
                    "local",
                ),
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("capability check", json.loads(result.stdout)["error"])
            self.assertEqual(len(provider.requests), 1)
            self.assertFalse(bundle.exists())

    def test_provider_endpoint_rejects_query_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            bundle = temporary / "credentialed-endpoint.accessibilizer"

            result = self.run_conversion(
                temporary,
                bundle,
                extra_arguments=(
                    "--provider-base-url",
                    "https://provider.example/v1?api_key=must-not-be-recorded",
                    "--provider-model",
                    "exact-model",
                    "--provider-data-location",
                    "local",
                ),
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("provider configuration", json.loads(result.stdout)["error"])
            self.assertNotIn("must-not-be-recorded", result.stdout)
            self.assertFalse(bundle.exists())

    def test_uncertain_endpoint_requires_explicit_remote_authorization(self) -> None:
        with FakeProvider() as provider, tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            bundle = temporary / "unauthorized.accessibilizer"

            result = self.run_conversion(
                temporary,
                bundle,
                extra_arguments=(
                    "--provider-base-url",
                    provider.base_url,
                    "--provider-model",
                    "exact-model",
                ),
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("--allow-remote", json.loads(result.stdout)["error"])
            self.assertEqual(provider.requests, [])
            self.assertFalse(bundle.exists())

    def test_page_stage_pauses_at_ceiling_then_resumes_reusing_the_capability(self) -> None:
        usage = {
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "total_tokens": 15,
        }
        with (
            FakeProvider(usage=usage) as provider,
            tempfile.TemporaryDirectory() as directory,
        ):
            temporary = Path(directory)
            bundle = temporary / "resumed.accessibilizer"
            provider_arguments = (
                "--provider-base-url",
                provider.base_url,
                "--provider-model",
                "exact-model",
                "--provider-data-location",
                "local",
                "--provider-retry-base-seconds",
                "0",
            )

            # The ceiling of 1 admits the capability check but pauses the page stage,
            # which needs one page call plus a call per crop region.
            paused = self.run_conversion(
                temporary,
                bundle,
                extra_arguments=(*provider_arguments, "--max-requests", "1"),
            )

            self.assertEqual(paused.returncode, 1, paused.stderr)
            self.assertIn("request ceiling", json.loads(paused.stdout)["error"])
            self.assertFalse(bundle.exists())
            self.assertTrue((temporary / ".resumed.accessibilizer.in-progress").is_dir())
            self.assertEqual(len(provider.requests), 1)

            resumed = self.run_conversion(
                temporary,
                bundle,
                extra_arguments=(
                    *provider_arguments,
                    "--max-requests",
                    "10",
                    "--resume",
                ),
            )

            self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
            # The completed capability checkpoint is reused; only the page stage's
            # four calls are made on resume.
            self.assertEqual(len(provider.requests), 5)
            provenance = json.loads((bundle / "provenance.json").read_text())
            self.assertEqual(
                provenance["provider_usage"],
                {
                    "actual_requests": 5,
                    "estimated_requests": 4,
                    "reported_token_usage": {
                        "prompt_tokens": 55,
                        "completion_tokens": 20,
                        "total_tokens": 75,
                    },
                    "request_ceiling": 10,
                },
            )


if __name__ == "__main__":
    unittest.main()
