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
from typing import Mapping, Protocol, Sequence, runtime_checkable

from accessibilizer.checkpoint import atomic_write_json
from accessibilizer.process import run as _run


RECOGNITION_CONTRACT_VERSION = "1.0"
RECOGNITION_DPI = 300

CANDIDATE_TYPES = frozenset(
    {"text", "handwriting", "formula", "table", "figure", "document_structure"}
)

Bbox = tuple[int, int, int, int]


@dataclass(frozen=True)
class RawCandidate:
    """A recognized region before it receives a stable identifier or a crop."""

    type: str
    bbox_pixels: Bbox
    text: str | None
    confidence: float | None
    backend: str


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
        ("document_structure", 0.03, 0.12, "Chapter 20", 0.99),
        ("text", 0.14, 0.30, "Electric current is the rate at which charge flows.", 0.95),
        ("handwriting", 0.32, 0.44, "annotated note", 0.55),
        ("formula", 0.46, 0.56, "I = Q / t", 0.80),
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

            # show_log/ocr defaults; CPU-only is enforced by the CPU paddlepaddle
            # wheel pinned in the image. No network access is used.
            self._structure = PPStructure(show_log=False)
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
            candidates.append(
                RawCandidate(
                    type=candidate_type,
                    bbox_pixels=bbox,
                    text=text,
                    confidence=confidence,
                    backend=f"paddleocr-{label or 'region'}",
                )
            )
        structure = _page_structure_candidate(candidates)
        if structure is not None:
            candidates.append(structure)
        return candidates


def _page_structure_candidate(regions: Sequence[RawCandidate]) -> RawCandidate | None:
    """Derive a Document Structure candidate from the detected page layout.

    The layout model rarely emits an explicit title/header region, so the page's
    reading order (the detected regions ordered top-to-bottom, left-to-right) is
    itself the Document Structure evidence. This is derived from real detections,
    not fabricated, and is preserved for later reconciliation of reading order.
    """
    if not regions:
        return None
    ordered = sorted(regions, key=lambda region: (region.bbox_pixels[1], region.bbox_pixels[0]))
    bbox: Bbox = (
        min(region.bbox_pixels[0] for region in regions),
        min(region.bbox_pixels[1] for region in regions),
        max(region.bbox_pixels[2] for region in regions),
        max(region.bbox_pixels[3] for region in regions),
    )
    return RawCandidate(
        type="document_structure",
        bbox_pixels=bbox,
        text="; ".join(region.type for region in ordered),
        confidence=None,
        backend="paddleocr-layout",
    )


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
    candidates: Sequence[tuple[str, RawCandidate]],
    words: Sequence[dict[str, object]],
    extractor: str,
    extractor_version: str,
) -> dict[str, object]:
    candidate_documents: list[dict[str, object]] = []
    for identifier, candidate in candidates:
        entry: dict[str, object] = {
            "backend": candidate.backend,
            "bbox_pixels": [int(value) for value in candidate.bbox_pixels],
            "bbox_points": pixels_to_points(candidate.bbox_pixels, dpi),
            "crop": f"regions/{identifier}.png",
            "id": identifier,
            "type": candidate.type,
        }
        if candidate.text is not None:
            entry["text"] = candidate.text
        if candidate.confidence is not None:
            entry["confidence"] = candidate.confidence
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
        "rendering": {
            "dpi": dpi,
            "renderer": renderer,
            "renderer_version": renderer_version,
        },
        "schema_version": RECOGNITION_CONTRACT_VERSION,
        "source_sha256": source_sha256,
    }


_ID_PATTERN = re.compile(r"^page-[0-9]+-r[0-9]{4,}$")
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
    """Validate a recognition document against the recognition-1.0 contract."""
    _require(isinstance(document, dict), "recognition document must be an object")
    assert isinstance(document, dict)
    _require(document.get("schema_version") == "1.0", "recognition schema_version must be 1.0")
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

    candidates = document.get("candidates")
    _require(isinstance(candidates, list), "candidates must be an array")
    assert isinstance(candidates, list)
    for candidate in candidates:
        _require(isinstance(candidate, dict), "each candidate must be an object")
        assert isinstance(candidate, dict)
        _require(
            isinstance(candidate.get("id"), str) and bool(_ID_PATTERN.match(str(candidate["id"]))),
            "candidate id must match page-<n>-r<index>",
        )
        _require(candidate.get("type") in CANDIDATE_TYPES, f"unsupported candidate type: {candidate.get('type')}")
        _validate_bbox(candidate.get("bbox_pixels"), "candidate bbox_pixels must be four numbers")
        _validate_bbox(candidate.get("bbox_points"), "candidate bbox_points must be four numbers")
        _require(
            isinstance(candidate.get("crop"), str) and bool(str(candidate["crop"]).strip()),
            "candidate crop must be a non-empty string",
        )
        _require(
            isinstance(candidate.get("backend"), str) and bool(str(candidate["backend"]).strip()),
            "candidate backend must be a non-empty string",
        )
        if "text" in candidate:
            _require(isinstance(candidate["text"], str), "candidate text must be a string")
        if "confidence" in candidate:
            confidence = candidate["confidence"]
            _require(
                isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and 0.0 <= float(confidence) <= 1.0,
                "candidate confidence must be between 0 and 1",
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

    ordered = assign_region_ids(page, backend.detect(page_render, (width, height)))
    artifacts: list[Path] = [page_render]
    for identifier, candidate in ordered:
        crop_x, crop_y, crop_right, crop_bottom = clamp_bbox_to_page(
            candidate.bbox_pixels, (width, height)
        )
        crop_width = crop_right - crop_x
        crop_height = crop_bottom - crop_y
        cropped = _run([
            "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-r", str(dpi),
            "-png", "-x", str(crop_x), "-y", str(crop_y), "-W", str(crop_width),
            "-H", str(crop_height), str(source_pdf), str(regions_dir / identifier),
        ])
        if cropped.returncode:
            raise RuntimeError(f"source-region crop failed for {identifier}: {cropped.stderr.strip()}")
        artifacts.append(regions_dir / f"{identifier}.png")

    extracted = _run([
        "pdftotext", "-f", str(page), "-l", str(page), "-bbox", str(source_pdf), "-",
    ])
    if extracted.returncode:
        raise RuntimeError(f"PDF text evidence extraction failed: {extracted.stderr.strip()}")
    words = parse_pdf_text_bbox(extracted.stdout)

    document = build_recognition_document(
        page=page,
        source_sha256=source_sha256,
        dpi=dpi,
        renderer="pdftoppm",
        renderer_version=renderer_version,
        backend=backend,
        candidates=ordered,
        words=words,
        extractor="pdftotext",
        extractor_version=extractor_version,
    )
    validate_recognition_document(document)
    document_path = recognition_dir / f"page-{page}.json"
    atomic_write_json(document_path, document)
    artifacts.append(document_path)
    return RecognitionResult(document_path=document_path, artifacts=artifacts)
