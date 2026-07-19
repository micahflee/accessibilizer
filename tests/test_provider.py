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


if __name__ == "__main__":
    unittest.main()
