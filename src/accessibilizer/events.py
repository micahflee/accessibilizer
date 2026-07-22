"""Accessible live progress and a durable, metadata-only conversion event log.

A single :class:`ProgressReporter` owns both faces of conversion observability:

* **Terminal feedback** — concise, assistive-technology-friendly lines written
  to a stream (``stderr`` in the CLI) so a long conversion never looks hung. Lines
  are stable and meaningful: no spinners, animation, color, or carriage-return
  rewriting. ``--verbose`` adds safe technical detail (endpoint, model, token
  usage, retry specifics). When a single indivisible operation (page recognition
  or a provider request) has produced no finer-grained event, a heartbeat line
  is printed every :data:`HEARTBEAT_INTERVAL_SECONDS` seconds.

* **Durable log** — a versioned JSON Lines artifact, ``conversion-events.jsonl``,
  appended to the in-progress Conversion Bundle. It survives ``--resume`` (it is
  simply reopened for append) and is published with the finished bundle. Each
  line carries a documented, stable core contract and only safe operational
  metadata. Heartbeats are terminal-only and are never persisted.

The log is deliberately metadata-only. Nothing that flows through it can carry a
credential, API key, authorization header, base64 image, Source PDF text,
prompt, raw request or response body, model-produced Semantic Layer content, or
hidden reasoning: callers pass structured fields drawn from the whitelist below,
and only those fields are ever written.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping, TextIO


EVENT_SCHEMA_VERSION = "1.0"
HEARTBEAT_INTERVAL_SECONDS = 10.0

CONVERSION_EVENTS_FILENAME = "conversion-events.jsonl"

# Lifecycle states a durable event may report.
STATE_STARTED = "started"
STATE_COMPLETED = "completed"
STATE_REUSED = "reused"
STATE_RETRYING = "retrying"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"
# Heartbeats are terminal feedback only; this state never reaches the log.
STATE_HEARTBEAT = "heartbeat"

# Human labels for stage identifiers. A stage the map does not know falls back to
# its identifier, so an unmapped stage still produces a stable, meaningful line.
STAGE_LABELS: dict[str, str] = {
    "provider-capability": "Provider capability check",
    "source-preflight": "Source PDF preflight",
    "page-recognition": "Page rendering and recognition",
    "provider-reconstruction": "Provider reconstruction request",
    "review-record": "Review Record assembly",
    "pdf-authoring": "PDF authoring",
    "internal-checks": "Internal semantic checks",
    "visual-comparison": "Visual comparison",
    "verapdf-validation": "veraPDF PDF/UA-1 validation",
    "bundle-publication": "Conversion Bundle publication",
}

# The three output-gate substeps, in the order they run. Shared so the reused
# path and the gate runner report the same stage identities.
VALIDATION_STAGES = ("internal-checks", "visual-comparison", "verapdf-validation")

# The only fields a durable event may carry beyond the core envelope. Keeping the
# set explicit is the mechanism that keeps secrets and content out of the log.
_OPTIONAL_FIELDS = (
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
)


class ConversionInterrupted(KeyboardInterrupt):
    """An intentional interruption (Ctrl-C or SIGTERM) of a conversion.

    Subclasses :class:`KeyboardInterrupt` so it is a ``BaseException`` that the
    pipeline's ``except Exception`` handlers do not swallow, and so existing
    keyboard-interrupt handling treats a delivered SIGTERM identically.
    """


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _Operation:
    """An in-flight operation, retained so an interruption can name it."""

    stage: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class OperationHandle:
    """Yielded by :meth:`ProgressReporter.operation`.

    A caller enriches the completion event with fields known only once the work
    is done — for example a provider request's reported token usage — by storing
    them in :attr:`extra`.
    """

    extra: dict[str, Any] = field(default_factory=dict)


class ProgressReporter:
    def __init__(
        self,
        *,
        log_path: Path | None = None,
        stream: TextIO | None = None,
        verbose: bool = False,
        emit_terminal: bool = True,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        monotonic: Callable[[], float] | None = None,
        now: Callable[[], str] = iso_now,
    ) -> None:
        self._log_path = log_path
        self._stream: TextIO = stream if stream is not None else sys.stderr
        self._verbose = verbose
        self._emit_terminal = emit_terminal
        self._heartbeat_interval = heartbeat_interval
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._now = now
        self._write_lock = threading.Lock()
        self._active: list[_Operation] = []

    # -- durable + terminal emission ------------------------------------------

    def _record(self, stage: str, state: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "timestamp": self._now(),
            "stage": stage,
            "state": state,
        }
        for name in _OPTIONAL_FIELDS:
            value = fields.get(name)
            if value is not None:
                record[name] = value
        return record

    def retarget(self, log_path: Path | None) -> None:
        """Point the durable log at a new path.

        Publishing the Conversion Bundle atomically renames the in-progress
        workspace onto the bundle, moving the log with it; retargeting lets the
        reporter keep appending to the published log afterwards.
        """
        self._log_path = log_path

    def _append(self, record: Mapping[str, Any]) -> None:
        if self._log_path is None:
            return
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._write_lock:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as stream:
                stream.write(line)

    def _print(self, text: str) -> None:
        if not self._emit_terminal:
            return
        with self._write_lock:
            self._stream.write(text + "\n")
            self._stream.flush()

    def emit(
        self, stage: str, state: str, *, durable: bool = True, **fields: Any
    ) -> dict[str, Any]:
        """Record one event durably (unless suppressed) and to the terminal."""
        record = self._record(stage, state, fields)
        if durable and state != STATE_HEARTBEAT:
            self._append(record)
        self._print(self._terminal_text(stage, state, fields))
        return record

    # -- terminal formatting --------------------------------------------------

    def _label(self, stage: str) -> str:
        return STAGE_LABELS.get(stage, stage)

    def _context(self, fields: Mapping[str, Any]) -> str:
        parts: list[str] = []
        page = fields.get("page")
        if page is not None:
            page_count = fields.get("page_count")
            parts.append(f"page {page}/{page_count}" if page_count else f"page {page}")
        purpose = fields.get("purpose")
        if purpose is not None:
            parts.append(str(purpose))
        request = fields.get("request")
        if request is not None:
            request_total = fields.get("request_total")
            parts.append(
                f"request {request}/{request_total}" if request_total else f"request {request}"
            )
        return f" ({'; '.join(parts)})" if parts else ""

    def _verbose_context(self, fields: Mapping[str, Any]) -> str:
        if not self._verbose:
            return ""
        parts: list[str] = []
        for name in ("model", "endpoint"):
            value = fields.get(name)
            if value is not None:
                parts.append(f"{name}={value}")
        usage = fields.get("token_usage")
        if isinstance(usage, Mapping) and usage:
            rendered = ", ".join(f"{key}={usage[key]}" for key in sorted(usage))
            parts.append(f"tokens: {rendered}")
        return f" [{'; '.join(parts)}]" if parts else ""

    def _terminal_text(self, stage: str, state: str, fields: Mapping[str, Any]) -> str:
        label = self._label(stage)
        context = self._context(fields)
        if state == STATE_STARTED:
            return f"{label}: started{context}{self._verbose_context(fields)}"
        if state == STATE_COMPLETED:
            elapsed = fields.get("elapsed_seconds")
            timing = f" in {elapsed:.1f}s" if isinstance(elapsed, (int, float)) else ""
            return f"{label}: completed{timing}{context}{self._verbose_context(fields)}"
        if state == STATE_REUSED:
            return f"{label}: reused checkpoint{context}"
        if state == STATE_RETRYING:
            attempt = fields.get("attempt")
            delay = fields.get("delay")
            detail = fields.get("detail")
            delay_text = f", waiting {delay:.1f}s" if isinstance(delay, (int, float)) else ""
            detail_text = f": {detail}" if detail else ""
            return f"{label}: retrying{context} (attempt {attempt}{delay_text}{detail_text})"
        if state == STATE_HEARTBEAT:
            elapsed = fields.get("elapsed_seconds")
            elapsed_text = f"{elapsed:.0f}s" if isinstance(elapsed, (int, float)) else "?"
            return f"{label}: still working ({elapsed_text} elapsed){context}"
        if state == STATE_FAILED:
            detail = fields.get("detail")
            detail_text = f": {detail}" if detail else ""
            return f"{label}: failed{context}{detail_text}"
        if state == STATE_INTERRUPTED:
            command = fields.get("resume_command")
            resume = f"\nResume with:\n  {command}" if command else ""
            return f"Interrupted during {label}{context}.{resume}"
        return f"{label}: {state}{context}"

    # -- one-shot events ------------------------------------------------------

    def reused(self, stage: str, **fields: Any) -> None:
        self.emit(stage, STATE_REUSED, **fields)

    def retrying(self, stage: str, **fields: Any) -> None:
        self.emit(stage, STATE_RETRYING, **fields)

    def failed(self, stage: str, **fields: Any) -> None:
        self.emit(stage, STATE_FAILED, **fields)

    # -- scoped operations ----------------------------------------------------

    @contextmanager
    def operation(
        self, stage: str, *, heartbeat: bool = True, durable: bool = True, **fields: Any
    ) -> Iterator[OperationHandle]:
        """Scope a named operation, emitting start and completion with elapsed.

        A heartbeat thread prints an elapsed-time line every heartbeat interval
        while the block runs, so a long indivisible operation stays observable;
        it never fires for a block that finishes before the first interval. On an
        exception the operation is left on the active stack so an interruption
        can report exactly what was running, and the completion event is skipped.
        The yielded handle's ``extra`` fields are merged into the completion
        event, letting a caller add detail known only once the work has finished.
        """
        start = self._monotonic()
        handle = OperationHandle()
        self.emit(stage, STATE_STARTED, durable=durable, **fields)
        operation = _Operation(stage=stage, fields={k: v for k, v in fields.items() if v is not None})
        self._active.append(operation)
        stop = threading.Event()
        thread: threading.Thread | None = None
        if heartbeat and self._heartbeat_interval > 0:
            thread = threading.Thread(
                target=self._beat, args=(stage, start, dict(fields), stop), daemon=True
            )
            thread.start()
        try:
            yield handle
        except ConversionInterrupted:
            # Leave the operation on the active stack so the interruption can
            # name exactly what was running; the top-level handler logs it.
            stop.set()
            if thread is not None:
                thread.join()
            raise
        except BaseException:
            stop.set()
            if thread is not None:
                thread.join()
            self._active.pop()
            elapsed = round(self._monotonic() - start, 3)
            # Record error state without a message: an arbitrary exception string
            # could echo a raw body or model content, which the log must exclude.
            self.emit(stage, STATE_FAILED, durable=durable, elapsed_seconds=elapsed, **fields)
            raise
        stop.set()
        if thread is not None:
            thread.join()
        self._active.pop()
        elapsed = round(self._monotonic() - start, 3)
        self.emit(
            stage, STATE_COMPLETED, durable=durable, elapsed_seconds=elapsed,
            **{**fields, **handle.extra},
        )

    @contextmanager
    def tracked(self, stage: str, **fields: Any) -> Iterator[None]:
        """Mark a stage active for interruption reporting without emitting events.

        Used for a step whose own start/completion events are emitted by the
        caller (Conversion Bundle publication, whose completion must be written
        after the workspace has been renamed onto the bundle). The operation is
        left on the active stack on an interruption so it can still be named.
        """
        self._active.append(_Operation(stage=stage, fields={k: v for k, v in fields.items() if v is not None}))
        try:
            yield
        except ConversionInterrupted:
            raise
        except BaseException:
            self._active.pop()
            raise
        self._active.pop()

    def _beat(
        self, stage: str, start: float, fields: dict[str, Any], stop: threading.Event
    ) -> None:
        while not stop.wait(self._heartbeat_interval):
            elapsed = self._monotonic() - start
            self.emit(stage, STATE_HEARTBEAT, durable=False, elapsed_seconds=elapsed, **fields)

    # -- interruption ---------------------------------------------------------

    def interrupted(self, *, resume_command: str | None = None) -> dict[str, Any]:
        """Record the interruption of whatever operation was most recently active."""
        active = self._active[-1] if self._active else None
        stage = active.stage if active is not None else "conversion"
        fields = dict(active.fields) if active is not None else {}
        fields.pop("elapsed_seconds", None)
        if resume_command is not None:
            fields["resume_command"] = resume_command
        return self.emit(stage, STATE_INTERRUPTED, **fields)

    @property
    def active_stage(self) -> str | None:
        return self._active[-1].stage if self._active else None
