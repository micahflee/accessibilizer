"""Produce reproducible, source-linked recognition evidence for one page.

The candidates and existing PDF text produced here are deliberately
*non-authoritative*: they are inputs for later reconciliation and review, never
final Semantic Layer content. Recognition runs CPU-only and offline; the real
PaddleOCR backend uses only weights baked into the canonical image.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import os
from pathlib import Path
import re
import shutil
import struct
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable
import zlib

from accessibilizer.checkpoint import atomic_write_json
from accessibilizer.process import run as _run


RECOGNITION_CONTRACT_VERSION = "2.0"
RECOGNITION_DPI = 300
PROPOSAL_ALGORITHM = "hybrid-source-regions"
PROPOSAL_ALGORITHM_VERSION = "1.0"
MAX_NONFALLBACK_AREA_RATIO = 0.8
PROPOSAL_DEDUPLICATION_PIXELS = 112
MODEL_BINDING_DEDUPLICATION_PIXELS = 200
MODEL_BINDING_OVERLAY_COLUMNS = 4
MODEL_BINDING_OVERLAY_ROWS = 4
CONFIDENCE_KINDS = ("layout_confidence", "ocr_text_confidence")
VERIFICATION_INELIGIBILITY_REASON_CODES = (
    "missing-layout-confidence",
    "missing-ocr-text-confidence",
    "missing-recognized-content",
    "source-region-too-large",
    "whole-page-fallback",
)

CANDIDATE_TYPES = frozenset(
    {"text", "handwriting", "formula", "table", "figure", "document_structure"}
)

Bbox = tuple[int, int, int, int]


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


def pixels_to_points(bbox: Sequence[float], dpi: int) -> list[float]:
    return [round(value * 72.0 / dpi, 2) for value in bbox]


def points_to_pixels(bbox: Sequence[float], dpi: int) -> Bbox:
    return tuple(round(float(value) * dpi / 72.0) for value in bbox)  # type: ignore[return-value]


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
    width, height = page_size
    fallback_id = f"page-{page}-r0000"
    proposal_sources: dict[Bbox, set[str]] = {}
    normalized_candidate_boxes: list[Bbox] = []
    for _, candidate in candidates:
        normalized = clamp_bbox_to_page(candidate.bbox_pixels, page_size)
        normalized_candidate_boxes.append(normalized)
        if _bbox_area(normalized) < width * height * MAX_NONFALLBACK_AREA_RATIO:
            proposal_sources.setdefault(normalized, set()).add("recognition")
    for word in words:
        bbox_points = word.get("bbox_points")
        if isinstance(bbox_points, list) and len(bbox_points) == 4:
            normalized = clamp_bbox_to_page(points_to_pixels(bbox_points, dpi), page_size)
            if _bbox_area(normalized) < width * height * MAX_NONFALLBACK_AREA_RATIO:
                proposal_sources.setdefault(normalized, set()).add("native-pdf-word")
    for proposal in raster_proposals:
        normalized = clamp_bbox_to_page(proposal, page_size)
        if _bbox_area(normalized) < width * height * MAX_NONFALLBACK_AREA_RATIO:
            proposal_sources.setdefault(normalized, set()).add("raster-ink")

    ordered_proposals = sorted(
        proposal_sources, key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2])
    )
    region_id_by_bbox = {
        bbox: f"page-{page}-r{index:04d}"
        for index, bbox in enumerate(ordered_proposals, start=1)
    }
    model_visible_boxes = select_model_binding_boxes(
        ordered_proposals,
        candidate_boxes=(
            bbox for bbox in normalized_candidate_boxes if bbox in region_id_by_bbox
        ),
    )
    source_regions: list[dict[str, object]] = [
        {
            "bbox_pixels": [0, 0, width, height],
            "bbox_points": pixels_to_points((0, 0, width, height), dpi),
            "crop": f"regions/{fallback_id}.png",
            "id": fallback_id,
            "model_visible": True,
            "proposal_sources": ["whole-page-fallback"],
        }
    ]
    for bbox in ordered_proposals:
        identifier = region_id_by_bbox[bbox]
        source_regions.append(
            {
                "bbox_pixels": list(bbox),
                "bbox_points": pixels_to_points(bbox, dpi),
                "crop": f"regions/{identifier}.png",
                "id": identifier,
                "model_visible": bbox in model_visible_boxes,
                "proposal_sources": sorted(proposal_sources[bbox]),
            }
        )

    candidate_documents: list[dict[str, object]] = []
    ordered_candidates = sorted(
        zip(normalized_candidate_boxes, (candidate for _, candidate in candidates)),
        key=lambda item: (item[0][1], item[0][0]),
    )
    for index, (bbox, candidate) in enumerate(ordered_candidates, start=1):
        source_region = region_id_by_bbox.get(bbox, fallback_id)
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
        "source_regions": source_regions,
        "source_sha256": source_sha256,
    }


def _bbox_area(bbox: Bbox) -> int:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def select_model_binding_boxes(
    proposals: Sequence[Bbox], *, candidate_boxes: Iterable[Bbox] = (),
) -> set[Bbox]:
    """Choose a compact, deterministic geometry subset for partitioned overlays."""
    binding_clusters: dict[tuple[int, int, int, int], list[Bbox]] = {}
    for bbox in proposals:
        key = (
            round(bbox[0] / MODEL_BINDING_DEDUPLICATION_PIXELS),
            round(bbox[1] / MODEL_BINDING_DEDUPLICATION_PIXELS),
            round(bbox[2] / MODEL_BINDING_DEDUPLICATION_PIXELS),
            round(bbox[3] / MODEL_BINDING_DEDUPLICATION_PIXELS),
        )
        binding_clusters.setdefault(key, []).append(bbox)
    selected = {
        min(
            cluster,
            key=lambda bbox: sum(
                abs(bbox[index] - key[index] * MODEL_BINDING_DEDUPLICATION_PIXELS)
                for index in range(4)
            ),
        )
        for key, cluster in binding_clusters.items()
    }
    selected.update(candidate_boxes)
    return selected


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
    if candidate.layout_confidence is None:
        reasons.append("missing-layout-confidence")
    if candidate.type in {"text", "handwriting", "formula"}:
        if candidate.confidence is None:
            reasons.append("missing-ocr-text-confidence")
        if not (candidate.text or "").strip():
            reasons.append("missing-recognized-content")
    return reasons


_REGION_ID_PATTERN = re.compile(r"^page-[0-9]+-r[0-9]{4,}$")
_CANDIDATE_ID_PATTERN = re.compile(r"^page-[0-9]+-c[0-9]{4,}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate_bbox(value: object, message: str) -> None:
    _require(
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(number, (int, float)) and not isinstance(number, bool) for number in value),
        message,
    )


def validate_recognition_document(document: object) -> None:
    """Validate a recognition document against the recognition-2.0 contract."""
    _require(isinstance(document, dict), "recognition document must be an object")
    assert isinstance(document, dict)
    _require(document.get("schema_version") == "2.0", "recognition schema_version must be 2.0")
    page = document.get("page")
    _require(isinstance(page, int) and not isinstance(page, bool) and page >= 1, "page must be a positive integer")
    _require(
        isinstance(document.get("source_sha256"), str)
        and bool(_SHA256_PATTERN.match(str(document["source_sha256"]))),
        "source_sha256 must be a sha-256 hex digest",
    )

    rendering = document.get("rendering")
    _require(isinstance(rendering, dict), "rendering must be an object")
    assert isinstance(rendering, dict)
    _require(
        isinstance(rendering.get("dpi"), int) and not isinstance(rendering["dpi"], bool) and rendering["dpi"] >= 1,
        "rendering.dpi must be a positive integer",
    )
    for field in ("renderer", "renderer_version"):
        _require(
            isinstance(rendering.get(field), str) and bool(str(rendering[field]).strip()),
            f"rendering.{field} must be a non-empty string",
        )

    recognition = document.get("recognition")
    _require(isinstance(recognition, dict), "recognition must be an object")
    assert isinstance(recognition, dict)
    for field in ("backend", "backend_version", "weights_version"):
        _require(
            isinstance(recognition.get(field), str) and bool(str(recognition[field]).strip()),
            f"recognition.{field} must be a non-empty string",
        )

    source_regions = document.get("source_regions")
    _require(isinstance(source_regions, list) and bool(source_regions), "source_regions must be a nonempty array")
    assert isinstance(source_regions, list)
    region_ids: set[str] = set()
    for region in source_regions:
        _require(isinstance(region, dict), "each Source Region must be an object")
        assert isinstance(region, dict)
        identifier = region.get("id")
        _require(isinstance(identifier, str) and bool(_REGION_ID_PATTERN.match(identifier)), "Source Region id is invalid")
        assert isinstance(identifier, str)
        region_ids.add(identifier)
        _validate_bbox(region.get("bbox_pixels"), "Source Region bbox_pixels must be four numbers")
        _validate_bbox(region.get("bbox_points"), "Source Region bbox_points must be four numbers")
        _require(isinstance(region.get("crop"), str), "Source Region crop must be a string")
        _require(
            isinstance(region.get("model_visible"), bool),
            "Source Region model_visible must be boolean",
        )
        _require(isinstance(region.get("proposal_sources"), list), "proposal_sources must be an array")

    proposal_generation = document.get("proposal_generation")
    _require(isinstance(proposal_generation, dict), "proposal_generation must be an object")
    assert isinstance(proposal_generation, dict)
    for field in ("algorithm", "algorithm_version"):
        _require(isinstance(proposal_generation.get(field), str) and bool(str(proposal_generation[field]).strip()), f"proposal_generation.{field} must be a non-empty string")
    _require(
        isinstance(proposal_generation.get("model_binding_deduplication_pixels"), int),
        "proposal_generation.model_binding_deduplication_pixels must be an integer",
    )

    candidates = document.get("candidates")
    _require(isinstance(candidates, list), "candidates must be an array")
    assert isinstance(candidates, list)
    for candidate in candidates:
        _require(isinstance(candidate, dict), "each candidate must be an object")
        assert isinstance(candidate, dict)
        _require(
            isinstance(candidate.get("id"), str) and bool(_CANDIDATE_ID_PATTERN.match(str(candidate["id"]))),
            "candidate id must match page-<n>-c<index>",
        )
        _require(candidate.get("type") in CANDIDATE_TYPES, f"unsupported candidate type: {candidate.get('type')}")
        _require(
            isinstance(candidate.get("source_region"), str) and candidate["source_region"] in region_ids,
            "candidate source_region must resolve",
        )
        _require(
            isinstance(candidate.get("backend"), str) and bool(str(candidate["backend"]).strip()),
            "candidate backend must be a non-empty string",
        )
        if "text" in candidate:
            _require(isinstance(candidate["text"], str), "candidate text must be a string")
        _require(isinstance(candidate.get("raw_class"), str), "candidate raw_class must be a string")
        verification = candidate.get("verification")
        _require(isinstance(verification, dict), "candidate verification must be an object")
        assert isinstance(verification, dict)
        _require(isinstance(verification.get("eligible"), bool), "candidate verification eligibility must be boolean")
        _require(isinstance(verification.get("reason_codes"), list), "candidate verification reason_codes must be an array")
        for confidence_field in ("layout_confidence", "ocr_text_confidence"):
            if confidence_field not in candidate:
                continue
            confidence = candidate[confidence_field]
            _require(
                isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and 0.0 <= float(confidence) <= 1.0,
                f"candidate {confidence_field} must be between 0 and 1",
            )

    evidence = document.get("pdf_text_evidence")
    _require(isinstance(evidence, dict), "pdf_text_evidence must be an object")
    assert isinstance(evidence, dict)
    _require(evidence.get("authoritative") is False, "existing PDF text must remain non-authoritative")
    for field in ("extractor", "extractor_version", "source"):
        _require(
            isinstance(evidence.get(field), str) and bool(str(evidence[field]).strip()),
            f"pdf_text_evidence.{field} must be a non-empty string",
        )
    evidence_words = evidence.get("words")
    _require(isinstance(evidence_words, list), "pdf_text_evidence.words must be an array")
    assert isinstance(evidence_words, list)
    for word in evidence_words:
        _require(isinstance(word, dict), "each evidence word must be an object")
        assert isinstance(word, dict)
        _require(isinstance(word.get("text"), str), "evidence word text must be a string")
        _validate_bbox(word.get("bbox_points"), "evidence word bbox_points must be four numbers")


def png_size(path: Path) -> tuple[int, int]:
    """Return a PNG's displayed pixel dimensions from its IHDR chunk."""
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"not a PNG render: {path}")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    return width, height


_DIGITS: dict[str, tuple[str, ...]] = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


def _read_ppm(path: Path) -> tuple[int, int, bytearray]:
    data = path.read_bytes()
    tokens: list[bytes] = []
    offset = 0
    while len(tokens) < 4:
        while offset < len(data) and data[offset] in b" \t\r\n":
            offset += 1
        if offset < len(data) and data[offset] == ord("#"):
            offset = data.find(b"\n", offset) + 1
            continue
        end = offset
        while end < len(data) and data[end] not in b" \t\r\n":
            end += 1
        tokens.append(data[offset:end])
        offset = end
    if tokens[0] != b"P6" or tokens[3] != b"255":
        raise ValueError(f"unsupported overlay render: {path}")
    while offset < len(data) and data[offset] in b" \t\r\n":
        offset += 1
    width, height = int(tokens[1]), int(tokens[2])
    pixels = bytearray(data[offset:])
    if len(pixels) != width * height * 3:
        raise ValueError(f"truncated overlay render: {path}")
    return width, height, pixels


def _set_pixel(
    pixels: bytearray, width: int, height: int, x: int, y: int,
    color: tuple[int, int, int],
) -> None:
    if not (0 <= x < width and 0 <= y < height):
        return
    offset = (y * width + x) * 3
    pixels[offset:offset + 3] = bytes(color)


def _draw_region_label(
    pixels: bytearray, width: int, height: int, bbox: Bbox, label: str,
) -> None:
    x0, y0, x1, y1 = bbox
    color = (220, 0, 80)
    for thickness in range(3):
        for x in range(x0, x1):
            _set_pixel(pixels, width, height, x, y0 + thickness, color)
            _set_pixel(pixels, width, height, x, y1 - 1 - thickness, color)
        for y in range(y0, y1):
            _set_pixel(pixels, width, height, x0 + thickness, y, color)
            _set_pixel(pixels, width, height, x1 - 1 - thickness, y, color)
    digits = label[-4:]
    label_width = len(digits) * 8 + 4
    label_height = 14
    for y in range(y0, min(y0 + label_height, height)):
        for x in range(x0, min(x0 + label_width, width)):
            _set_pixel(pixels, width, height, x, y, color)
    for digit_index, digit in enumerate(digits):
        glyph = _DIGITS[digit]
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit == "1":
                    for dy in range(2):
                        for dx in range(2):
                            _set_pixel(
                                pixels, width, height,
                                x0 + 3 + digit_index * 8 + column * 2 + dx,
                                y0 + 2 + row * 2 + dy,
                                (255, 255, 255),
                            )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _write_rgb_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    rows = b"".join(
        b"\x00" + bytes(pixels[y * width * 3:(y + 1) * width * 3])
        for y in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(rows, 6))
        + _png_chunk(b"IEND", b"")
    )


def raster_region_proposals(
    width: int, height: int, pixels: bytearray, *, cell_size: int = 4
) -> list[Bbox]:
    """Derive deterministic type-neutral line and block geometry from page ink."""
    columns = (width + cell_size - 1) // cell_size
    rows = (height + cell_size - 1) // cell_size
    occupied: list[set[int]] = [set() for _ in range(rows)]
    for cell_y in range(rows):
        y = min(cell_y * cell_size + cell_size // 2, height - 1)
        for cell_x in range(columns):
            x = min(cell_x * cell_size + cell_size // 2, width - 1)
            offset = (y * width + x) * 3
            if min(pixels[offset:offset + 3]) < 210:
                occupied[cell_y].add(cell_x)

    bands: list[tuple[int, int]] = []
    start: int | None = None
    last_ink = -1
    for row, ink_columns in enumerate(occupied):
        if len(ink_columns) >= 3:
            if start is None:
                start = row
            last_ink = row
        elif start is not None and row - last_ink > 2:
            bands.append((start, last_ink + 1))
            start = None
    if start is not None:
        bands.append((start, last_ink + 1))

    lines: list[Bbox] = []
    band_lines: list[Bbox] = []
    for top, bottom in bands:
        band_columns = sorted({column for row in range(top, bottom) for column in occupied[row]})
        if not band_columns:
            continue
        band_lines.append(
            (
                band_columns[0] * cell_size,
                top * cell_size,
                min((band_columns[-1] + 1) * cell_size, width),
                min(bottom * cell_size, height),
            )
        )
        run_start = band_columns[0]
        previous = band_columns[0]
        for column in band_columns[1:] + [columns + 20]:
            if column - previous <= 10:
                previous = column
                continue
            if previous - run_start >= 2:
                lines.append(
                    (
                        run_start * cell_size,
                        top * cell_size,
                        min((previous + 1) * cell_size, width),
                        min(bottom * cell_size, height),
                    )
                )
            run_start = column
            previous = column

    proposals: set[Bbox] = set()

    def padded(box: Bbox, padding: int) -> Bbox:
        return clamp_bbox_to_page(
            (box[0] - padding, box[1] - padding, box[2] + padding, box[3] + padding),
            (width, height),
        )

    def add_aspect_variants(box: Bbox) -> None:
        box_width = box[2] - box[0]
        center = (box[0] + box[2]) // 2
        horizontal_variants = {box}
        for numerator, denominator in ((3, 2), (2, 1), (3, 1), (4, 1)):
            expanded_width = box_width * numerator // denominator
            horizontal_variants.add((box[0], box[1], box[0] + expanded_width, box[3]))
            horizontal_variants.add((box[2] - expanded_width, box[1], box[2], box[3]))
            horizontal_variants.add(
                (center - expanded_width // 2, box[1], center + expanded_width // 2, box[3])
            )
        for variant in horizontal_variants:
            for vertical_padding in (8, 24, 48, 72):
                proposals.add(
                    clamp_bbox_to_page(
                        (
                            variant[0] - 8, variant[1] - vertical_padding,
                            variant[2] + 8, variant[3] + vertical_padding,
                        ),
                        (width, height),
                    )
                )

    def morphology_components(horizontal: int, vertical: int) -> list[Bbox]:
        horizontal_rows: list[set[int]] = []
        for ink in occupied:
            difference = [0] * (columns + 1)
            for column in ink:
                left = max(0, column - horizontal)
                right = min(columns, column + horizontal + 1)
                difference[left] += 1
                difference[right] -= 1
            active = 0
            expanded_row: set[int] = set()
            for column, delta in enumerate(difference[:-1]):
                active += delta
                if active:
                    expanded_row.add(column)
            horizontal_rows.append(expanded_row)

        expanded: set[int] = set()
        for row in range(rows):
            columns_on_row: set[int] = set()
            for source_row in range(max(0, row - vertical), min(rows, row + vertical + 1)):
                columns_on_row.update(horizontal_rows[source_row])
            expanded.update(row * columns + column for column in columns_on_row)

        components: list[Bbox] = []
        while expanded:
            seed = expanded.pop()
            stack = [seed]
            min_row = max_row = seed // columns
            min_column = max_column = seed % columns
            while stack:
                current = stack.pop()
                row, column = divmod(current, columns)
                neighbors = []
                if column:
                    neighbors.append(current - 1)
                if column + 1 < columns:
                    neighbors.append(current + 1)
                if row:
                    neighbors.append(current - columns)
                if row + 1 < rows:
                    neighbors.append(current + columns)
                for neighbor in neighbors:
                    if neighbor not in expanded:
                        continue
                    expanded.remove(neighbor)
                    stack.append(neighbor)
                    neighbor_row, neighbor_column = divmod(neighbor, columns)
                    min_row = min(min_row, neighbor_row)
                    max_row = max(max_row, neighbor_row)
                    min_column = min(min_column, neighbor_column)
                    max_column = max(max_column, neighbor_column)
            ink_width = max_column - min_column + 1 - 2 * horizontal
            ink_height = max_row - min_row + 1 - 2 * vertical
            if ink_width < 3 or ink_height < 2:
                continue
            components.append(
                clamp_bbox_to_page(
                    (
                        (min_column + horizontal) * cell_size,
                        (min_row + vertical) * cell_size,
                        (max_column - horizontal + 1) * cell_size,
                        (max_row - vertical + 1) * cell_size,
                    ),
                    (width, height),
                )
            )
        return components

    for horizontal, vertical in (
        (3, 1), (8, 2), (16, 4),
        (28, 1), (40, 1), (55, 1), (70, 2),
        (28, 8), (44, 14),
    ):
        for component in morphology_components(horizontal, vertical):
            add_aspect_variants(component)
            for padding in (8, 24, 48, 72):
                proposals.add(padded(component, padding))

    for line in [*lines, *band_lines]:
        add_aspect_variants(line)
        for padding in (8, 24, 48):
            proposals.add(padded(line, padding))

    for sequence in (lines, band_lines):
        ordered = sorted(sequence, key=lambda box: (box[1], box[0], -box[2]))
        for start_index, first in enumerate(ordered):
            union = first
            included = 1
            for following in ordered[start_index + 1:start_index + 14]:
                vertical_gap = following[1] - union[3]
                horizontal_overlap = min(union[2], following[2]) - max(union[0], following[0])
                horizontal_gap = max(following[0] - union[2], union[0] - following[2], 0)
                if vertical_gap > 140:
                    break
                if horizontal_overlap <= 0 and horizontal_gap > 72:
                    continue
                union = (
                    min(union[0], following[0]), min(union[1], following[1]),
                    max(union[2], following[2]), max(union[3], following[3]),
                )
                included += 1
                for padding in (8, 24, 48, 72, 96):
                    proposals.add(padded(union, padding))
                if included == 8:
                    break

    # Same-column content can be interleaved with another column in reading-order
    # geometry. Pair nearby components directly so the intervening column does not
    # prevent a useful multi-line parent proposal.
    for index, first in enumerate(lines):
        first_width = first[2] - first[0]
        first_center = (first[0] + first[2]) / 2
        for following in lines[index + 1:]:
            if following[1] - first[3] > 520:
                continue
            following_width = following[2] - following[0]
            following_center = (following[0] + following[2]) / 2
            overlap = min(first[2], following[2]) - max(first[0], following[0])
            if (
                overlap < min(first_width, following_width) * 0.2
                and abs(first_center - following_center) > max(first_width, following_width) * 0.65
            ):
                continue
            union = (
                min(first[0], following[0]), min(first[1], following[1]),
                max(first[2], following[2]), max(first[3], following[3]),
            )
            for padding in (8, 24, 48, 72, 96):
                proposals.add(padded(union, padding))

    ordered_proposals = sorted(
        (
            proposal for proposal in proposals
            if _bbox_area(proposal) < width * height * MAX_NONFALLBACK_AREA_RATIO
        ),
        key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]),
    )
    deduplicated: dict[tuple[int, int, int, int], Bbox] = {}
    for proposal in ordered_proposals:
        key = (
            round(proposal[0] / PROPOSAL_DEDUPLICATION_PIXELS),
            round(proposal[1] / PROPOSAL_DEDUPLICATION_PIXELS),
            round(proposal[2] / PROPOSAL_DEDUPLICATION_PIXELS),
            round(proposal[3] / PROPOSAL_DEDUPLICATION_PIXELS),
        )
        deduplicated.setdefault(key, proposal)
    return sorted(
        deduplicated.values(), key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2])
    )


def _render_page_rgb(
    *, source_pdf: Path, page: int, dpi: int, temporary_base: Path,
) -> tuple[int, int, bytearray]:
    rendered = _run([
        "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-r", str(dpi),
        str(source_pdf), str(temporary_base),
    ])
    ppm = temporary_base.with_suffix(".ppm")
    if rendered.returncode:
        raise RuntimeError(f"Source Region overlay render failed: {rendered.stderr.strip()}")
    try:
        return _read_ppm(ppm)
    finally:
        ppm.unlink(missing_ok=True)


def _create_region_overlay(
    *, destination: Path, width: int, height: int, pixels: bytearray,
    regions: Sequence[dict[str, Any]],
) -> None:
    for region in regions:
        identifier = str(region["id"])
        if identifier.endswith("-r0000"):
            continue
        bbox_value = region["bbox_pixels"]
        assert isinstance(bbox_value, list)
        bbox: Bbox = tuple(int(value) for value in bbox_value)  # type: ignore[assignment]
        _draw_region_label(pixels, width, height, bbox, identifier)
    _write_rgb_png(destination, width, height, pixels)


def _create_region_overlay_partitions(
    *, destination_prefix: Path, width: int, height: int, pixels: bytearray,
    regions: Sequence[dict[str, Any]],
) -> list[Path]:
    """Render bounded-density labeled page partitions for reliable ID binding."""
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for region in regions:
        if not region.get("model_visible") or str(region["id"]).endswith("-r0000"):
            continue
        bbox = region["bbox_pixels"]
        assert isinstance(bbox, list)
        center_x = (int(bbox[0]) + int(bbox[2])) // 2
        center_y = (int(bbox[1]) + int(bbox[3])) // 2
        column = min(
            center_x * MODEL_BINDING_OVERLAY_COLUMNS // width,
            MODEL_BINDING_OVERLAY_COLUMNS - 1,
        )
        row = min(
            center_y * MODEL_BINDING_OVERLAY_ROWS // height,
            MODEL_BINDING_OVERLAY_ROWS - 1,
        )
        grouped.setdefault((row, column), []).append(region)

    overlays: list[Path] = []
    for row in range(MODEL_BINDING_OVERLAY_ROWS):
        for column in range(MODEL_BINDING_OVERLAY_COLUMNS):
            partition_regions = grouped.get((row, column), [])
            if not partition_regions:
                continue
            x0 = column * width // MODEL_BINDING_OVERLAY_COLUMNS
            x1 = (column + 1) * width // MODEL_BINDING_OVERLAY_COLUMNS
            y0 = row * height // MODEL_BINDING_OVERLAY_ROWS
            y1 = (row + 1) * height // MODEL_BINDING_OVERLAY_ROWS
            partition_width = x1 - x0
            partition_height = y1 - y0
            partition_pixels = bytearray()
            for y in range(y0, y1):
                start = (y * width + x0) * 3
                end = start + partition_width * 3
                partition_pixels.extend(pixels[start:end])
            for region in partition_regions:
                bbox_value = region["bbox_pixels"]
                assert isinstance(bbox_value, list)
                bbox = clamp_bbox_to_page(
                    (
                        int(bbox_value[0]) - x0,
                        int(bbox_value[1]) - y0,
                        int(bbox_value[2]) - x0,
                        int(bbox_value[3]) - y0,
                    ),
                    (partition_width, partition_height),
                )
                _draw_region_label(
                    partition_pixels,
                    partition_width,
                    partition_height,
                    bbox,
                    str(region["id"]),
                )
            destination = destination_prefix.with_name(
                f"{destination_prefix.name}-{row + 1:02d}-{column + 1:02d}.png"
            )
            _write_rgb_png(
                destination,
                partition_width,
                partition_height,
                partition_pixels,
            )
            overlays.append(destination)
    return overlays


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def clamp_bbox_to_page(bbox: Sequence[float], size: tuple[int, int]) -> Bbox:
    """Normalize a detected box to the nonempty page area used for its crop."""
    width, height = size
    x0, y0, x1, y1 = (int(value) for value in bbox)
    crop_x = _clamp(x0, 0, max(width - 1, 0))
    crop_y = _clamp(y0, 0, max(height - 1, 0))
    crop_right = _clamp(x1, crop_x + 1, width)
    crop_bottom = _clamp(y1, crop_y + 1, height)
    return crop_x, crop_y, crop_right, crop_bottom


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
