#!/usr/bin/env python3
"""Render a small, deterministic interactive Review Report fixture.

The browser suite drives the *actual* renderer output (never a hand-written
copy), so this script builds a valid Review Record exercising multi-region and
shared-region Components plus node-, region-, page-, and document-scoped
Conversion Warnings, renders ``review-report.{html,css,js}`` into the given
directory, and writes solid-colour placeholder PNGs for every referenced page
and Source Region so ``<img>`` loads succeed offline.

Usage: build_fixture.py <output-dir>
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from accessibilizer import review  # noqa: E402

PAGE_WIDTH = 600.0
PAGE_HEIGHT = 800.0


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Return a minimal solid-colour PNG so the report's <img> tags resolve."""
    line = b"\x00" + bytes(rgb) * width
    raw = line * height
    compressed = zlib.compress(raw)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def _region(region_id: str, bbox: list[float]) -> dict[str, Any]:
    return {"id": region_id, "bbox_points": bbox}


def _reconstruction(order: list[str]) -> dict[str, Any]:
    return {
        "document_class": "stem_instructional",
        "page_prompt_version": "1.0",
        "page_schema_version": "1.0",
        "primary_language_is_english": True,
        "provider_endpoint": "http://localhost:11434/v1",
        "provider_model": "fixture-model",
        "reading_order": order,
        "reading_order_is_unambiguous": True,
        "region_prompt_version": "1.0",
        "region_schema_version": "1.0",
        "verified_regions": [],
    }


def _page_document(
    page: int,
    source_regions: list[dict[str, Any]],
    semantic_layer: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "page": page,
        "source_sha256": "a" * 64,
        "title": "Interactive Report Fixture",
        "language": "en-US",
        "page_dimensions": {"width_points": PAGE_WIDTH, "height_points": PAGE_HEIGHT},
        "source_regions": [{**r, "page": page} for r in source_regions],
        "semantic_layer": semantic_layer,
        "warnings": warnings,
        "candidates": [
            {
                "id": f"page-{page}-c0001",
                "type": "text",
                "text": "recognized sample text",
                "source_region": f"page-{page}-r0001",
            }
        ],
        "reconstruction": _reconstruction([n["type"] for n in semantic_layer]),
    }


def build_record() -> dict[str, Any]:
    page1_regions = [
        _region("page-1-r0001", [60, 40, 540, 100]),
        _region("page-1-r0002", [60, 120, 540, 200]),
        _region("page-1-r0003", [60, 240, 300, 320]),
        _region("page-1-r0004", [320, 240, 540, 320]),
    ]
    page1_nodes: list[dict[str, Any]] = [
        {
            "id": "page-1-s0001",
            "type": "heading",
            "level": 1,
            "text": "Interactive Report Fixture",
            "source_regions": ["page-1-r0001"],
        },
        {
            "id": "page-1-s0002",
            "type": "paragraph",
            "text": "A paragraph describing electric current on the first page.",
            "source_regions": ["page-1-r0002"],
        },
        {
            "id": "page-1-s0003",
            "type": "formula",
            "normalized_math": "I = Q / t",
            "spoken_math_alternative": "I equals Q divided by t.",
            "source_regions": ["page-1-r0003", "page-1-r0004"],
        },
        {
            "id": "page-1-s0004",
            "type": "figure",
            "complexity": "simple",
            "figure_alternative": "A wire crossing a boundary; shares the fourth region.",
            "source_regions": ["page-1-r0004"],
        },
    ]
    page1_warnings: list[dict[str, Any]] = [
        {
            "code": "recognition-disagreement",
            "message": "The formula region disagrees with the candidate.",
            "status": "unresolved",
            "semantic_nodes": ["page-1-s0003"],
            "source_regions": ["page-1-r0003"],
        },
        {
            "code": "region-only",
            "message": "Inspect the second region on page one.",
            "status": "unresolved",
            "semantic_nodes": [],
            "source_regions": ["page-1-r0002"],
        },
        {
            "code": "ambiguous-reading-order",
            "message": "More than one order is plausible on page one.",
            "status": "unresolved",
        },
    ]

    page2_regions = [
        _region("page-2-r0001", [60, 40, 540, 120]),
        _region("page-2-r0002", [60, 160, 540, 400]),
    ]
    page2_nodes: list[dict[str, Any]] = [
        {
            "id": "page-2-s0001",
            "type": "paragraph",
            "text": "A second-page paragraph with no attached warning.",
            "source_regions": ["page-2-r0001"],
        },
        {
            "id": "page-2-s0002",
            "type": "table",
            "caption": "Resistivity of common materials",
            "rows": [
                {
                    "cells": [
                        {"kind": "header", "text": "Material", "scope": "col", "row_span": 1, "col_span": 1},
                        {"kind": "header", "text": "Resistivity", "scope": "col", "row_span": 1, "col_span": 1},
                    ]
                },
                {
                    "cells": [
                        {"kind": "header", "text": "Copper", "scope": "row", "row_span": 1, "col_span": 1},
                        {"kind": "data", "text": "1.68e-8", "scope": "none", "row_span": 1, "col_span": 1},
                    ]
                },
            ],
            "source_regions": ["page-2-r0002"],
        },
    ]

    page3_regions = [_region("page-3-r0001", [60, 40, 540, 200])]
    page3_nodes: list[dict[str, Any]] = [
        {
            "id": "page-3-s0001",
            "type": "paragraph",
            "text": "A third-page paragraph so stepping crosses several pages.",
            "source_regions": ["page-3-r0001"],
        }
    ]

    record = review.build_review_record(
        source_sha256="a" * 64,
        title="Interactive Report Fixture",
        language="en-US",
        provider_endpoint="http://localhost:11434/v1",
        provider_model="fixture-model",
        page_prompt_version="1.0",
        page_schema_version="1.0",
        region_prompt_version="1.0",
        region_schema_version="1.0",
        pages=[
            _page_document(1, page1_regions, page1_nodes, page1_warnings),
            _page_document(2, page2_regions, page2_nodes, []),
            _page_document(3, page3_regions, page3_nodes, []),
        ],
    )
    # Promote the page-one ambiguity warning to a document-wide warning so the
    # report exercises the always-reachable, never-a-fake-Component path.
    record["warnings"][2]["page"] = None
    review.validate_review_record(record)
    return record


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: build_fixture.py <output-dir>", file=sys.stderr)
        return 2
    out = Path(sys.argv[1])
    regions = out / "regions"
    regions.mkdir(parents=True, exist_ok=True)

    record = build_record()
    (out / "review-report.html").write_text(review.render_review_report(record), encoding="utf-8")
    (out / review.REVIEW_REPORT_STYLESHEET).write_text(review.review_report_css(), encoding="utf-8")
    (out / review.REVIEW_REPORT_SCRIPT).write_text(review.review_report_javascript(), encoding="utf-8")

    for entry in record["page_dimensions"]:
        page = entry["page"]
        (regions / f"page-{page}.png").write_bytes(
            _png(int(PAGE_WIDTH) // 2, int(PAGE_HEIGHT) // 2, (235, 235, 235))
        )
    for region in record["source_regions"]:
        (regions / f"{region['id']}.png").write_bytes(_png(40, 20, (200, 200, 255)))

    print(str(out / "review-report.html"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
