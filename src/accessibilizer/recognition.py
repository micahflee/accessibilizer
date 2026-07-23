"""Produce reproducible, source-linked recognition evidence for one page.

The candidates and existing PDF text produced here are deliberately
*non-authoritative*: they are inputs for later reconciliation and review, never
final Semantic Layer content. Recognition runs CPU-only and offline; the real
PaddleOCR backend uses only weights baked into the canonical image.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from jsonschema import Draft202012Validator

from accessibilizer.checkpoint import atomic_write_json
from accessibilizer.process import run as _run

from accessibilizer.source_regions import (
    Bbox as Bbox,
    MAX_NONFALLBACK_AREA_RATIO as MAX_NONFALLBACK_AREA_RATIO,
    MODEL_BINDING_DEDUPLICATION_PIXELS as MODEL_BINDING_DEDUPLICATION_PIXELS,
    MODEL_BINDING_OVERLAY_COLUMNS as MODEL_BINDING_OVERLAY_COLUMNS,
    MODEL_BINDING_OVERLAY_ROWS as MODEL_BINDING_OVERLAY_ROWS,
    PROPOSAL_ALGORITHM as PROPOSAL_ALGORITHM,
    PROPOSAL_ALGORITHM_VERSION as PROPOSAL_ALGORITHM_VERSION,
    PROPOSAL_DEDUPLICATION_PIXELS as PROPOSAL_DEDUPLICATION_PIXELS,
    _bbox_area,
    _create_region_overlay,
    _create_region_overlay_partitions,
    _render_page_rgb as _render_page_rgb,
    build_source_region_proposals,
    clamp_bbox_to_page,
    pixels_to_points as pixels_to_points,
    png_size,
    points_to_pixels as points_to_pixels,
    raster_region_proposals as raster_region_proposals,
    select_model_binding_boxes as select_model_binding_boxes,
)


RECOGNITION_CONTRACT_VERSION = "2.0"
RECOGNITION_DPI = 300
CONFIDENCE_KINDS = ("layout_confidence", "ocr_text_confidence")
VERIFICATION_INELIGIBILITY_REASON_CODES = (
    "missing-ocr-text-confidence",
    "missing-recognized-content",
    "source-region-too-large",
    "whole-page-fallback",
)

CANDIDATE_TYPES = frozenset(
    {"text", "handwriting", "formula", "table", "figure", "document_structure"}
)


@dataclass(frozen=True)
class RawCandidate:
    """Recognition evidence before it receives a stable Candidate identity."""

    type: str
    bbox_pixels: Bbox
    text: str | None
    confidence: float | None
    backend: str
    raw_class: str | None = None
    layout_confidence: float | None = None


@dataclass(frozen=True)
class RecognitionResult:
    document_path: Path
    artifacts: list[Path]


@runtime_checkable
class RecognitionBackend(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def weights_version(self) -> str: ...

    def detect(self, page_image: Path, size: tuple[int, int]) -> list[RawCandidate]:
        ...


class FakeBackend:
    """A deterministic backend so the CLI seam can be tested without OCR.

    It fabricates one region per candidate type from the page dimensions. It
    performs no recognition and must never be used for a real conversion.
    """

    name = "fake"
    version = "1.0"
    weights_version = "fake-weights-1.0"

    _BANDS: tuple[tuple[str, float, float, str | None, float], ...] = (
        (
            "document_structure", 0.03, 0.12,
            "Electric Current, Resistance, and Ohm's Law", 0.99,
        ),
        ("text", 0.14, 0.30, "Electric current is the rate at which charge flows.", 0.95),
        ("handwriting", 0.32, 0.44, "annotated note", 0.55),
        ("formula", 0.46, 0.56, "I = Q / delta t", 0.80),
        ("table", 0.58, 0.74, None, 0.70),
        ("figure", 0.76, 0.96, None, 0.65),
    )

    def detect(self, page_image: Path, size: tuple[int, int]) -> list[RawCandidate]:
        width, height = size
        left = int(0.05 * width)
        right = int(0.95 * width)
        candidates: list[RawCandidate] = []
        for kind, top, bottom, text, confidence in self._BANDS:
            candidates.append(
                RawCandidate(
                    type=kind,
                    bbox_pixels=(left, int(top * height), right, int(bottom * height)),
                    text=text,
                    confidence=confidence,
                    backend=f"fake-{kind}",
                    raw_class=kind,
                    layout_confidence=confidence,
                )
            )
        return candidates


class PaddleBackend:
    """Pinned, CPU-only PaddleOCR recognition using image-baked weights.

    PaddleOCR and its weights are pinned in the canonical Docker image; nothing
    is downloaded at runtime. This backend is exercised by the opt-in real-OCR
    test and manual runs over the sample; the fast CLI seam uses FakeBackend.
    """

    name = "paddleocr"

    # PP-Structure layout labels mapped to Accessibilizer candidate types.
    _LABEL_TYPES: dict[str, str] = {
        "text": "text",
        "title": "document_structure",
        "list": "text",
        "table": "table",
        "table_caption": "text",
        "figure": "figure",
        "figure_caption": "text",
        "image": "figure",
        "formula": "formula",
        "equation": "formula",
        "isolate_formula": "formula",
        "reference": "text",
        "header": "document_structure",
        "footer": "document_structure",
    }

    def __init__(self) -> None:
        self.weights_version = os.environ.get(
            "ACCESSIBILIZER_PADDLE_WEIGHTS_VERSION", "PP-Structure"
        )
        self._structure: object | None = None

    @property
    def version(self) -> str:
        import paddleocr  # type: ignore[import-not-found]

        return str(getattr(paddleocr, "__version__", "unknown"))

    def _pipeline(self) -> object:
        if self._structure is None:
            from paddleocr import PPStructure

            # CPU-only is enforced by the pinned wheel. Disable the optional IR
            # optimizer because PaddlePaddle 2.6.x's self-attention fusion pass
            # can execute an unsupported instruction on valid x86-64 hosts.
            self._structure = PPStructure(show_log=False, ir_optim=False)
        return self._structure

    def detect(self, page_image: Path, size: tuple[int, int]) -> list[RawCandidate]:
        pipeline = self._pipeline()
        regions = pipeline(str(page_image))  # type: ignore[operator]
        candidates: list[RawCandidate] = []
        for region in regions:
            label = str(region.get("type", "")).lower()
            candidate_type = self._LABEL_TYPES.get(label, "text")
            box = region.get("bbox")
            if not box or len(box) != 4:
                continue
            bbox: Bbox = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
            text, confidence = _paddle_region_text(region.get("res"))
            layout_confidence_value = region.get("score")
            layout_confidence = (
                float(layout_confidence_value)
                if isinstance(layout_confidence_value, (int, float))
                and not isinstance(layout_confidence_value, bool)
                else None
            )
            candidates.append(
                RawCandidate(
                    type=candidate_type,
                    bbox_pixels=bbox,
                    text=text,
                    confidence=confidence,
                    backend=f"paddleocr-{label or 'region'}",
                    raw_class=label or "region",
                    layout_confidence=layout_confidence,
                )
            )
        return candidates


def _paddle_region_text(res: object) -> tuple[str | None, float | None]:
    """Extract concatenated text and mean confidence from a PP-Structure region."""
    if not isinstance(res, list):
        return None, None
    lines: list[str] = []
    scores: list[float] = []
    for line in res:
        if not isinstance(line, dict):
            continue
        text = line.get("text")
        if isinstance(text, str) and text:
            lines.append(text)
        score = line.get("confidence")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            scores.append(float(score))
    if not lines:
        return None, None
    confidence = round(sum(scores) / len(scores), 4) if scores else None
    return " ".join(lines), confidence


def select_backend(environment: Mapping[str, str]) -> RecognitionBackend:
    name = environment.get("ACCESSIBILIZER_RECOGNITION_BACKEND", "paddleocr")
    if name == "fake":
        return FakeBackend()
    if name == "paddleocr":
        return PaddleBackend()
    raise ValueError(f"unknown recognition backend: {name}")


def assign_region_ids(
    page: int, candidates: Sequence[RawCandidate]
) -> list[tuple[str, RawCandidate]]:
    """Order candidates top-to-bottom, left-to-right and give each a stable id.

    Ordering is independent of backend output order so identifiers are
    reproducible; ties keep their input order via a stable sort.
    """
    ordered = sorted(
        candidates, key=lambda candidate: (candidate.bbox_pixels[1], candidate.bbox_pixels[0])
    )
    return [
        (f"page-{page}-r{index:04d}", candidate)
        for index, candidate in enumerate(ordered, start=1)
    ]


_WORD_PATTERN = re.compile(
    r'<word\s+xMin="(?P<x_min>[\d.]+)"\s+yMin="(?P<y_min>[\d.]+)"'
    r'\s+xMax="(?P<x_max>[\d.]+)"\s+yMax="(?P<y_max>[\d.]+)"\s*>'
    r"(?P<text>.*?)</word>",
    re.DOTALL,
)


def parse_pdf_text_bbox(xhtml: str) -> list[dict[str, object]]:
    """Parse pdftotext ``-bbox`` output into non-authoritative word geometry."""
    words: list[dict[str, object]] = []
    for match in _WORD_PATTERN.finditer(xhtml):
        words.append(
            {
                "text": html.unescape(match.group("text")),
                "bbox_points": [
                    round(float(match.group("x_min")), 2),
                    round(float(match.group("y_min")), 2),
                    round(float(match.group("x_max")), 2),
                    round(float(match.group("y_max")), 2),
                ],
            }
        )
    return words


def build_recognition_document(
    *,
    page: int,
    source_sha256: str,
    dpi: int,
    renderer: str,
    renderer_version: str,
    backend: RecognitionBackend,
    page_size: tuple[int, int],
    raster_proposals: Sequence[Bbox] = (),
    candidates: Sequence[tuple[str, RawCandidate]],
    words: Sequence[dict[str, object]],
    extractor: str,
    extractor_version: str,
) -> dict[str, Any]:
    fallback_id = f"page-{page}-r0000"
    proposals = build_source_region_proposals(
        page=page,
        dpi=dpi,
        page_size=page_size,
        candidate_boxes=[candidate.bbox_pixels for _, candidate in candidates],
        words=words,
        raster_proposals=raster_proposals,
    )

    candidate_documents: list[dict[str, object]] = []
    ordered_candidates = sorted(
        zip(
            proposals.normalized_candidate_boxes,
            (candidate for _, candidate in candidates),
        ),
        key=lambda item: (item[0][1], item[0][0]),
    )
    for index, (bbox, candidate) in enumerate(ordered_candidates, start=1):
        representative = proposals.representative_by_bbox.get(bbox)
        source_region = (
            proposals.region_id_by_bbox[representative]
            if representative is not None
            else fallback_id
        )
        reasons = _candidate_ineligibility_reasons(
            candidate, bbox=bbox, page_size=page_size, source_region=source_region
        )
        entry: dict[str, object] = {
            "backend": candidate.backend,
            "id": f"page-{page}-c{index:04d}",
            "raw_class": candidate.raw_class or candidate.type,
            "source_region": source_region,
            "type": candidate.type,
            "verification": {"eligible": not reasons, "reason_codes": reasons},
        }
        if candidate.text is not None:
            entry["text"] = candidate.text
        if candidate.confidence is not None:
            entry["ocr_text_confidence"] = candidate.confidence
        if candidate.layout_confidence is not None:
            entry["layout_confidence"] = candidate.layout_confidence
        candidate_documents.append(entry)
    return {
        "candidates": candidate_documents,
        "page": page,
        "pdf_text_evidence": {
            "authoritative": False,
            "extractor": extractor,
            "extractor_version": extractor_version,
            "source": "existing Source PDF text layer",
            "words": list(words),
        },
        "recognition": {
            "backend": backend.name,
            "backend_version": backend.version,
            "weights_version": backend.weights_version,
        },
        "proposal_generation": {
            "algorithm": PROPOSAL_ALGORITHM,
            "algorithm_version": PROPOSAL_ALGORITHM_VERSION,
            "deduplication_pixels": PROPOSAL_DEDUPLICATION_PIXELS,
            "model_binding_deduplication_pixels": MODEL_BINDING_DEDUPLICATION_PIXELS,
            "model_binding_overlay_grid": [
                MODEL_BINDING_OVERLAY_COLUMNS,
                MODEL_BINDING_OVERLAY_ROWS,
            ],
            "max_nonfallback_area_ratio": MAX_NONFALLBACK_AREA_RATIO,
            "sources": ["native-pdf-word", "raster-ink", "recognition"],
        },
        "rendering": {
            "dpi": dpi,
            "renderer": renderer,
            "renderer_version": renderer_version,
        },
        "schema_version": RECOGNITION_CONTRACT_VERSION,
        "source_regions": proposals.source_regions,
        "source_sha256": source_sha256,
    }


def _candidate_ineligibility_reasons(
    candidate: RawCandidate,
    *,
    bbox: Bbox,
    page_size: tuple[int, int],
    source_region: str,
) -> list[str]:
    reasons: list[str] = []
    if source_region.endswith("-r0000"):
        reasons.append("whole-page-fallback")
    if _bbox_area(bbox) >= page_size[0] * page_size[1] * MAX_NONFALLBACK_AREA_RATIO:
        reasons.append("source-region-too-large")
    if candidate.type in {"text", "handwriting", "formula"}:
        if candidate.confidence is None:
            reasons.append("missing-ocr-text-confidence")
        if not (candidate.text or "").strip():
            reasons.append("missing-recognized-content")
    return reasons


def recognition_document_schema() -> dict[str, Any]:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "recognition-2.0.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise RuntimeError("recognition schema must be a JSON object")
    return schema


_RECOGNITION_VALIDATOR = Draft202012Validator(recognition_document_schema())


def validate_recognition_document(document: object) -> None:
    """Validate canonical Recognition 2.0 shape plus relational invariants."""
    errors = sorted(
        _RECOGNITION_VALIDATOR.iter_errors(document),
        key=lambda error: list(error.path),
    )
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.path) or "(root)"
        raise ValueError(
            f"invalid recognition document at {location}: {error.message}"
        )
    assert isinstance(document, dict)
    page = document["page"]
    page_prefix = f"page-{page}-"

    region_ids: set[str] = set()
    for region in document["source_regions"]:
        identifier = region["id"]
        if identifier in region_ids:
            raise ValueError(
                f"invalid recognition document: duplicate Source Region id {identifier}"
            )
        if not identifier.startswith(f"{page_prefix}r"):
            raise ValueError(
                f"invalid recognition document: Source Region {identifier} is not on page {page}"
            )
        region_ids.add(identifier)
        for field in ("bbox_pixels", "bbox_points"):
            bbox = region[field]
            if (
                not all(math.isfinite(float(number)) for number in bbox)
                or bbox[0] < 0
                or bbox[1] < 0
                or bbox[2] <= bbox[0]
                or bbox[3] <= bbox[1]
            ):
                raise ValueError(
                    f"invalid recognition document: Source Region {identifier} "
                    f"has invalid {field}"
                )

    candidate_ids: set[str] = set()
    for candidate in document["candidates"]:
        identifier = candidate["id"]
        if identifier in candidate_ids:
            raise ValueError(
                f"invalid recognition document: duplicate Candidate id {identifier}"
            )
        if not identifier.startswith(f"{page_prefix}c"):
            raise ValueError(
                f"invalid recognition document: Candidate {identifier} is not on page {page}"
            )
        candidate_ids.add(identifier)
        source_region = candidate["source_region"]
        if source_region not in region_ids:
            raise ValueError(
                f"invalid recognition document: Candidate {identifier} references "
                f"unknown Source Region {source_region}"
            )
        verification = candidate["verification"]
        if verification["eligible"] != (not verification["reason_codes"]):
            raise ValueError(
                f"invalid recognition document: Candidate {identifier} eligibility "
                "does not match its reason codes"
            )


def recognize_page(
    *,
    source_pdf: Path,
    page: int,
    dpi: int,
    regions_dir: Path,
    recognition_dir: Path,
    backend: RecognitionBackend,
    source_sha256: str,
    renderer_version: str,
    extractor_version: str,
) -> RecognitionResult:
    """Render, recognize, crop, and record non-authoritative evidence for a page."""
    page_render = regions_dir / f"page-{page}-recognition.png"
    rendered = _run([
        "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-r", str(dpi),
        "-png", str(source_pdf), str(regions_dir / f"page-{page}-recognition"),
    ])
    if rendered.returncode:
        raise RuntimeError(f"recognition render failed: {rendered.stderr.strip()}")
    width, height = png_size(page_render)

    detected = backend.detect(page_render, (width, height))

    extracted = _run([
        "pdftotext", "-f", str(page), "-l", str(page), "-bbox", str(source_pdf), "-",
    ])
    if extracted.returncode:
        raise RuntimeError(f"PDF text evidence extraction failed: {extracted.stderr.strip()}")
    words = parse_pdf_text_bbox(extracted.stdout)
    overlay_width, overlay_height, overlay_pixels = _render_page_rgb(
        source_pdf=source_pdf,
        page=page,
        dpi=dpi,
        temporary_base=recognition_dir / f".page-{page}-overlay-base",
    )
    raster_proposals = raster_region_proposals(
        overlay_width, overlay_height, overlay_pixels
    )

    document = build_recognition_document(
        page=page,
        source_sha256=source_sha256,
        dpi=dpi,
        renderer="pdftoppm",
        renderer_version=renderer_version,
        backend=backend,
        page_size=(width, height),
        raster_proposals=raster_proposals,
        candidates=assign_region_ids(page, detected),
        words=words,
        extractor="pdftotext",
        extractor_version=extractor_version,
    )
    source_regions_value = document["source_regions"]
    assert isinstance(source_regions_value, list)
    overlays = _create_region_overlay_partitions(
        destination_prefix=regions_dir / f"page-{page}-overlay",
        width=overlay_width,
        height=overlay_height,
        pixels=overlay_pixels,
        regions=source_regions_value,
    )
    if not overlays:
        overlay = regions_dir / f"page-{page}-overlay.png"
        _create_region_overlay(
            destination=overlay, width=overlay_width, height=overlay_height,
            pixels=overlay_pixels, regions=source_regions_value,
        )
        overlays = [overlay]
    document["overlay"] = f"regions/{overlays[0].name}"
    document["overlays"] = [f"regions/{overlay.name}" for overlay in overlays]
    validate_recognition_document(document)
    artifacts: list[Path] = [page_render, *overlays]
    source_regions = document["source_regions"]
    assert isinstance(source_regions, list)
    candidate_regions = {
        str(candidate["source_region"])
        for candidate in document["candidates"]
        if isinstance(candidate, dict)
    }
    for region in source_regions:
        assert isinstance(region, dict)
        identifier = str(region["id"])
        if identifier not in candidate_regions and not identifier.endswith("-r0000"):
            continue
        if identifier.endswith("-r0000"):
            destination = regions_dir / f"{identifier}.png"
            shutil.copyfile(page_render, destination)
            artifacts.append(destination)
            continue
        bbox_pixels = region["bbox_pixels"]
        assert isinstance(bbox_pixels, list)
        crop_x, crop_y, crop_right, crop_bottom = (int(value) for value in bbox_pixels)
        cropped = _run([
            "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-r", str(dpi),
            "-png", "-x", str(crop_x), "-y", str(crop_y), "-W", str(crop_right - crop_x),
            "-H", str(crop_bottom - crop_y), str(source_pdf), str(regions_dir / identifier),
        ])
        if cropped.returncode:
            raise RuntimeError(f"source-region crop failed for {identifier}: {cropped.stderr.strip()}")
        artifacts.append(regions_dir / f"{identifier}.png")
    document_path = recognition_dir / f"page-{page}.json"
    atomic_write_json(document_path, document)
    artifacts.append(document_path)
    return RecognitionResult(document_path=document_path, artifacts=artifacts)
