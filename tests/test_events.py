from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from typing import Any

from accessibilizer.events import (
    CONVERSION_EVENTS_FILENAME,
    EVENT_SCHEMA_VERSION,
    ConversionInterrupted,
    ProgressReporter,
)


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class DurableLogTest(unittest.TestCase):
    def make_reporter(
        self, directory: Path, *, verbose: bool = False, stream: io.StringIO | None = None
    ) -> tuple[ProgressReporter, io.StringIO, Path]:
        terminal = stream or io.StringIO()
        log_path = directory / CONVERSION_EVENTS_FILENAME
        reporter = ProgressReporter(
            log_path=log_path,
            stream=terminal,
            verbose=verbose,
            heartbeat_interval=0,
            now=lambda: "2026-07-21T00:00:00Z",
        )
        return reporter, terminal, log_path

    def test_stage_records_started_and_completed_with_elapsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            log_path = Path(directory) / CONVERSION_EVENTS_FILENAME
            reporter = ProgressReporter(
                log_path=log_path,
                stream=io.StringIO(),
                heartbeat_interval=0,
                monotonic=clock,
                now=lambda: "2026-07-21T00:00:00Z",
            )
            with reporter.operation("pdf-authoring"):
                clock.value = 2.5
            events = read_events(log_path)
            self.assertEqual([event["state"] for event in events], ["started", "completed"])
            self.assertEqual(events[0]["schema_version"], EVENT_SCHEMA_VERSION)
            self.assertEqual(events[0]["stage"], "pdf-authoring")
            self.assertNotIn("elapsed_seconds", events[0])
            self.assertEqual(events[1]["elapsed_seconds"], 2.5)

    def test_per_page_events_carry_page_and_page_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reporter, _, log_path = self.make_reporter(Path(directory))
            with reporter.operation("page-recognition", page=3, page_count=11):
                pass
            events = read_events(log_path)
            for event in events:
                self.assertEqual(event["page"], 3)
                self.assertEqual(event["page_count"], 11)

    def test_reused_checkpoint_is_reported_as_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reporter, terminal, log_path = self.make_reporter(Path(directory))
            reporter.reused("page-recognition", page=2, page_count=11)
            events = read_events(log_path)
            self.assertEqual(events[0]["state"], "reused")
            self.assertIn("reused checkpoint", terminal.getvalue())

    def test_failing_operation_records_a_failed_event_without_a_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reporter, _, log_path = self.make_reporter(Path(directory))
            with self.assertRaises(RuntimeError):
                with reporter.operation("pdf-authoring", page=2, page_count=3):
                    raise RuntimeError("authoring failed: raw java stderr with secrets")
            events = read_events(log_path)
            self.assertEqual([event["state"] for event in events], ["started", "failed"])
            failed = events[-1]
            self.assertEqual(failed["stage"], "pdf-authoring")
            self.assertEqual(failed["page"], 2)
            self.assertIn("elapsed_seconds", failed)
            # The error message is never persisted: it could echo a raw body.
            self.assertNotIn("detail", failed)
            self.assertNotIn("secrets", log_path.read_text(encoding="utf-8"))
            # A failed operation is not left dangling on the active stack.
            self.assertIsNone(reporter.active_stage)

    def test_verbose_terminal_shows_model_endpoint_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reporter, terminal, _ = self.make_reporter(Path(directory), verbose=True)
            reporter.emit(
                "provider-reconstruction",
                "completed",
                elapsed_seconds=1.0,
                model="exact-model",
                endpoint="http://localhost:11434/v1",
                token_usage={"total_tokens": 12},
            )
            rendered = terminal.getvalue()
            self.assertIn("model=exact-model", rendered)
            self.assertIn("endpoint=http://localhost:11434/v1", rendered)
            self.assertIn("total_tokens=12", rendered)


class HeartbeatTest(unittest.TestCase):
    def test_long_operation_emits_terminal_heartbeats_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            terminal = io.StringIO()
            log_path = Path(directory) / CONVERSION_EVENTS_FILENAME
            reporter = ProgressReporter(
                log_path=log_path,
                stream=terminal,
                heartbeat_interval=0.05,
                now=lambda: "2026-07-21T00:00:00Z",
            )
            beats = threading.Event()

            with reporter.operation("provider-reconstruction", page=1, page_count=1):
                # Wait until at least one heartbeat has been printed.
                deadline = time.monotonic() + 5
                while "still working" not in terminal.getvalue():
                    if time.monotonic() > deadline:
                        self.fail("no heartbeat was printed for a long operation")
                    time.sleep(0.01)
                beats.set()

            self.assertTrue(beats.is_set())
            self.assertIn("still working", terminal.getvalue())
            # Heartbeats are terminal feedback only and are never persisted.
            states = [event["state"] for event in read_events(log_path)]
            self.assertNotIn("heartbeat", states)
            self.assertEqual(states, ["started", "completed"])


class InterruptionTest(unittest.TestCase):
    def test_interruption_records_the_active_operation_and_resume_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            terminal = io.StringIO()
            log_path = Path(directory) / CONVERSION_EVENTS_FILENAME
            reporter = ProgressReporter(
                log_path=log_path,
                stream=terminal,
                heartbeat_interval=0,
                now=lambda: "2026-07-21T00:00:00Z",
            )
            with self.assertRaises(ConversionInterrupted):
                with reporter.operation(
                    "provider-reconstruction", page=4, page_count=11, request=5, request_total=45
                ):
                    raise ConversionInterrupted()
            # The interrupted operation is still nameable after unwinding.
            self.assertEqual(reporter.active_stage, "provider-reconstruction")
            reporter.interrupted(resume_command="accessibilizer convert x --resume")

            events = read_events(log_path)
            interruption = events[-1]
            self.assertEqual(interruption["state"], "interrupted")
            self.assertEqual(interruption["stage"], "provider-reconstruction")
            self.assertEqual(interruption["page"], 4)
            self.assertEqual(interruption["request"], 5)
            self.assertEqual(
                interruption["resume_command"], "accessibilizer convert x --resume"
            )
            # A completed event is never emitted for an interrupted operation.
            self.assertNotIn("completed", [event["state"] for event in events])
            self.assertIn("Resume with", terminal.getvalue())


class RedactionTest(unittest.TestCase):
    """The durable log must never carry a secret, image, or model content."""

    def test_only_whitelisted_fields_reach_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / CONVERSION_EVENTS_FILENAME
            reporter = ProgressReporter(
                log_path=log_path, stream=io.StringIO(), heartbeat_interval=0
            )
            # Even if a caller passes a forbidden field, it is dropped: only the
            # documented whitelist is ever written.
            reporter.emit(
                "provider-reconstruction",
                "completed",
                elapsed_seconds=1.0,
                api_key="sk-secret-value",
                authorization="Bearer sk-secret-value",
                image="data:image/png;base64,AAAA",
                prompt="ignore all previous instructions",
            )
            raw = log_path.read_text(encoding="utf-8")
            for forbidden in ("sk-secret-value", "Bearer", "base64", "ignore all previous"):
                self.assertNotIn(forbidden, raw)
            allowed = set(read_events(log_path)[0])
            self.assertTrue(
                allowed
                <= {
                    "schema_version",
                    "timestamp",
                    "stage",
                    "state",
                    "elapsed_seconds",
                    "page",
                    "page_count",
                    "request",
                    "request_total",
                    "purpose",
                    "endpoint",
                    "model",
                    "token_usage",
                    "attempt",
                    "delay",
                    "detail",
                    "resume_command",
                }
            )


if __name__ == "__main__":
    unittest.main()
