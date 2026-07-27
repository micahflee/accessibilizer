"""Durable full-document runs for the isolated vision-only prototype."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Sequence, cast
import uuid

from accessibilizer.checkpoint import atomic_write_json
from accessibilizer.provider import ProviderConfig, RequestBudget
from accessibilizer.vision_only import (
    PAGE_SEMANTICS_12_SCHEMA_VERSION,
    build_page_request,
    page_response_schema,
    reconstruct_page_vision_only,
)


@dataclass(frozen=True)
class PageInput:
    page: int
    image: Path
    native_pdf_words: Sequence[dict[str, Any]]
    width_points: float
    height_points: float


@dataclass(frozen=True)
class VisionOnlyRun:
    run_id: str
    run_directory: Path
    manifest_path: Path


Requester = Callable[[dict[str, Any]], dict[str, Any]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _with_node_ids(document: dict[str, Any], page: int) -> dict[str, Any]:
    copied = cast(dict[str, Any], json.loads(json.dumps(document)))
    for index, node in enumerate(copied["semantic_layer"], start=1):
        node["id"] = f"page-{page}-s{index:04d}"
    return copied


def run_document_vision_only(
    config: ProviderConfig,
    *,
    source_pdf: Path,
    pages: Sequence[PageInput],
    runs_directory: Path,
    requester: Requester | None = None,
) -> VisionOnlyRun:
    """Process a Source PDF as one independently identified prototype run.

    ``pages`` is the narrow preparation seam: callers provide a full-page image,
    displayed-page dimensions, and any native PDF text and geometry for each page.
    The function makes exactly one semantic request per page and records only the
    request contract and schema-valid content, never transport headers or traces.
    """
    if not pages:
        raise ValueError("a vision-only document run requires at least one page")
    page_numbers = [page.page for page in pages]
    if page_numbers != list(range(1, len(pages) + 1)):
        raise ValueError("page inputs must be complete and ordered from page 1")

    run_id = str(uuid.uuid4())
    run_directory = runs_directory / run_id
    pages_directory = run_directory / "pages"
    pages_directory.mkdir(parents=True, exist_ok=False)
    source_sha256 = _sha256(source_pdf)
    budget = RequestBudget(estimated_requests=len(pages), ceiling=len(pages))
    page_entries: list[dict[str, Any]] = []
    logical_reading_order: list[str] = []

    for page_input in pages:
        page_directory = pages_directory / f"page-{page_input.page}"
        page_directory.mkdir()
        captured: dict[str, dict[str, Any]] = {}

        def record_exchange(request: dict[str, Any], response: dict[str, Any]) -> None:
            captured["request"] = request
            captured["response"] = response

        normalized = reconstruct_page_vision_only(
            config,
            page=page_input.page,
            source_sha256=source_sha256,
            page_image=page_input.image,
            pdf_words=page_input.native_pdf_words,
            page_width_points=page_input.width_points,
            page_height_points=page_input.height_points,
            budget=budget,
            requester=requester,
            exchange_recorder=record_exchange,
        )
        normalized = _with_node_ids(normalized, page_input.page)
        logical_reading_order.extend(
            cast(str, node["id"]) for node in normalized["semantic_layer"]
        )
        inputs = {
            "page": page_input.page,
            "image_sha256": _sha256(page_input.image),
            "image_media_type": "image/png",
            "width_points": page_input.width_points,
            "height_points": page_input.height_points,
            "native_pdf_words": list(page_input.native_pdf_words),
            "native_pdf_evidence_authoritative": False,
        }
        atomic_write_json(page_directory / "inputs.json", inputs)
        atomic_write_json(page_directory / "request.json", captured["request"])
        atomic_write_json(page_directory / "response.json", captured["response"])
        atomic_write_json(page_directory / "normalized.json", normalized)
        page_entries.append({
            "page": page_input.page,
            "inputs": f"pages/page-{page_input.page}/inputs.json",
            "request": f"pages/page-{page_input.page}/request.json",
            "response": f"pages/page-{page_input.page}/response.json",
            "normalized": f"pages/page-{page_input.page}/normalized.json",
        })

    example_request = build_page_request(
        model=config.model,
        page_image=pages[0].image,
        pdf_words=pages[0].native_pdf_words,
    )
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_pdf_sha256": source_sha256,
        "model": config.model,
        "provider_endpoint": config.base_url,
        "page_count": len(pages),
        "logical_request_count": budget.actual_requests,
        "logical_reading_order": logical_reading_order,
        "prompt": {
            "system": example_request["messages"][0]["content"],
            "page": example_request["messages"][1]["content"][0]["text"],
        },
        "schema_version": PAGE_SEMANTICS_12_SCHEMA_VERSION,
        "schema": page_response_schema(),
        "pages": page_entries,
    }
    manifest_path = run_directory / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    return VisionOnlyRun(run_id, run_directory, manifest_path)
