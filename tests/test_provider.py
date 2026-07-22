from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from typing import Any

from accessibilizer.provider import (
    ProviderConfig,
    RequestBudget,
    RequestCeilingExceeded,
    check_capabilities,
    parse_schema_content,
)


class SequencedProvider:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.requests = 0
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                status = provider.statuses[min(provider.requests, len(provider.statuses) - 1)]
                provider.requests += 1
                if status != 200:
                    self.send_response(status)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                body: dict[str, Any] = {
                    "choices": [
                        {"message": {"content": json.dumps({"blue_square_count": 3})}}
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 4,
                        "total_tokens": 15,
                    },
                }
                encoded = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> SequencedProvider:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def config(self) -> ProviderConfig:
        return ProviderConfig(
            f"http://127.0.0.1:{self.server.server_port}/v1",
            "exact-model",
            None,
            "local",
        )


class ProviderRuntimeTest(unittest.TestCase):
    def test_transient_failures_retry_with_a_bound_and_retain_reported_usage(self) -> None:
        with SequencedProvider([429, 503, 200]) as provider:
            budget = RequestBudget(estimated_requests=1, ceiling=3)
            delays: list[float] = []

            check_capabilities(
                provider.config,
                budget=budget,
                max_retries=2,
                retry_base_seconds=0.5,
                sleep=delays.append,
            )

            self.assertEqual(provider.requests, 3)
            self.assertEqual(delays, [0.5, 1.0])
            self.assertEqual(
                budget.as_dict(),
                {
                    "actual_requests": 3,
                    "estimated_requests": 1,
                    "reported_token_usage": {
                        "completion_tokens": 4,
                        "prompt_tokens": 11,
                        "total_tokens": 15,
                    },
                    "request_ceiling": 3,
                },
            )

    def test_request_ceiling_pauses_before_an_excess_retry(self) -> None:
        with SequencedProvider([429, 200]) as provider:
            budget = RequestBudget(estimated_requests=1, ceiling=1)

            with self.assertRaises(RequestCeilingExceeded):
                check_capabilities(
                    provider.config,
                    budget=budget,
                    max_retries=2,
                    retry_base_seconds=0,
                )

            self.assertEqual(provider.requests, 1)
            self.assertEqual(budget.actual_requests, 1)


class SchemaContentTest(unittest.TestCase):
    FAILURE = "region verification returned an invalid schema response"

    def test_valid_content_is_parsed(self) -> None:
        response = {"choices": [{"message": {"content": json.dumps({"ok": True})}}]}
        self.assertEqual(parse_schema_content(response, self.FAILURE), {"ok": True})

    def test_a_length_capped_response_reports_truncation(self) -> None:
        # A reasoning model can exhaust max_completion_tokens on hidden reasoning
        # and return truncated JSON with finish_reason "length".
        response = {
            "choices": [
                {"finish_reason": "length", "message": {"content": '{"transcription": "the tab'}}
            ]
        }
        with self.assertRaises(RuntimeError) as caught:
            parse_schema_content(response, self.FAILURE)
        self.assertEqual(
            str(caught.exception),
            f"{self.FAILURE}; provider response was truncated at the token limit",
        )

    def test_empty_content_reports_truncation(self) -> None:
        response = {"choices": [{"finish_reason": "length", "message": {"content": None}}]}
        with self.assertRaises(RuntimeError) as caught:
            parse_schema_content(response, self.FAILURE)
        self.assertIn("truncated at the token limit", str(caught.exception))

    def test_malformed_json_without_truncation_reports_invalid_json(self) -> None:
        response = {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}
        with self.assertRaises(RuntimeError) as caught:
            parse_schema_content(response, self.FAILURE)
        self.assertEqual(str(caught.exception), f"{self.FAILURE}; provider returned invalid JSON")

    def test_a_missing_choice_reports_the_bare_failure(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            parse_schema_content({"choices": []}, self.FAILURE)
        self.assertEqual(str(caught.exception), self.FAILURE)


if __name__ == "__main__":
    unittest.main()
