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
SEMANTIC_INPUT = ROOT / "testdata" / "one-page-semantic.json"


class FakeProvider:
    def __init__(self, *, compatible: bool = True) -> None:
        self.compatible = compatible
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
                try:
                    content = request["messages"][0]["content"]
                    image_url = content[1]["image_url"]["url"]
                    image = base64.b64decode(image_url.removeprefix("data:image/png;base64,"), validate=True)
                    response_format = request["response_format"]
                    schema = response_format["json_schema"]["schema"]
                    valid_contract = (
                        image_url.startswith("data:image/png;base64,")
                        and hashlib.sha256(image).hexdigest()
                        == "8640b42788b7bf45cc580582ac9b6b77f05b55512fc290f48040259ee8f03c9e"
                        and response_format["type"] == "json_schema"
                        and response_format["json_schema"]["strict"] is True
                        and schema["properties"]["blue_square_count"]["type"] == "integer"
                    )
                except (KeyError, IndexError, TypeError, ValueError):
                    valid_contract = False
                if not valid_contract:
                    self.send_error(400)
                    return
                if not provider.compatible:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"response_format unsupported"}}')
                    return
                content = json.dumps({"blue_square_count": 3})
                response = {"choices": [{"message": {"content": content}}]}
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
                "--semantic-input",
                str(SEMANTIC_INPUT),
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
            self.assertEqual(len(provider.requests), 1)
            request = provider.requests[0]
            self.assertEqual(request["model"], "cli-model-2026-07-19")
            self.assertEqual(request["response_format"]["type"], "json_schema")
            self.assertNotIn("const", json.dumps(request["response_format"]))
            image = request["messages"][0]["content"][1]["image_url"]["url"]
            self.assertTrue(image.startswith("data:image/png;base64,"))
            self.assertEqual(provider.authorizations, ["Bearer correct-secret"])

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


if __name__ == "__main__":
    unittest.main()
