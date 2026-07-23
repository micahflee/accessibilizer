"""Deterministic, type-neutral Source Region geometry and model-binding visuals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Any, Iterable, Mapping, Sequence
import zlib

from accessibilizer.process import run as _run


PROPOSAL_ALGORITHM = "hybrid-source-regions"
PROPOSAL_ALGORITHM_VERSION = "1.1"
MAX_NONFALLBACK_AREA_RATIO = 0.8
PROPOSAL_DEDUPLICATION_PIXELS = 112
MODEL_BINDING_DEDUPLICATION_PIXELS = 200
MODEL_BINDING_SCALE_VARIANT_AREA_RATIO = 1.4
MODEL_BINDING_OVERLAY_COLUMNS = 4
MODEL_BINDING_OVERLAY_ROWS = 4

Bbox = tuple[int, int, int, int]


@dataclass(frozen=True)
class SourceRegionProposalSet:
    """Canonical regions plus the geometry mapping needed to bind Candidates."""

    source_regions: list[dict[str, object]]
    normalized_candidate_boxes: list[Bbox]
    representative_by_bbox: dict[Bbox, Bbox]
    region_id_by_bbox: dict[Bbox, str]


def pixels_to_points(bbox: Sequence[float], dpi: int) -> list[float]:
    return [round(value * 72.0 / dpi, 2) for value in bbox]


def points_to_pixels(bbox: Sequence[float], dpi: int) -> Bbox:
    return tuple(round(float(value) * dpi / 72.0) for value in bbox)  # type: ignore[return-value]


def _bbox_area(bbox: Bbox) -> int:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def _deduplicate_proposals(
    proposal_sources: Mapping[Bbox, set[str]],
) -> tuple[dict[Bbox, set[str]], dict[Bbox, Bbox]]:
    """Merge near-identical geometry across every proposal source.

    Stable representatives are chosen only from normalized input boxes. Substantially
    larger parents survive even when all four edges fall in the same coarse bucket.
    """
    merged: dict[Bbox, set[str]] = {}
    representative_by_bbox: dict[Bbox, Bbox] = {}
    ordered = sorted(
        proposal_sources,
        key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]),
    )
    for bbox in ordered:
        area = _bbox_area(bbox)
        representative = next(
            (
                existing
                for existing in merged
                if max(abs(bbox[index] - existing[index]) for index in range(4))
                <= PROPOSAL_DEDUPLICATION_PIXELS
                and _bbox_intersection_area(bbox, existing)
                / max(area, _bbox_area(existing))
                >= 0.8
            ),
            bbox,
        )
        merged.setdefault(representative, set()).update(proposal_sources[bbox])
        representative_by_bbox[bbox] = representative
    return merged, representative_by_bbox


def _bbox_intersection_area(first: Bbox, second: Bbox) -> int:
    return max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1])
    )


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
    selected: set[Bbox] = set()
    for key, cluster in binding_clusters.items():
        primary = min(
            cluster,
            key=lambda bbox: sum(
                abs(bbox[index] - key[index] * MODEL_BINDING_DEDUPLICATION_PIXELS)
                for index in range(4)
            ),
        )
        selected.add(primary)
        compact = min(cluster, key=_bbox_area)
        if _bbox_area(primary) >= (
            _bbox_area(compact) * MODEL_BINDING_SCALE_VARIANT_AREA_RATIO
        ):
            selected.add(compact)
    selected.update(candidate_boxes)
    return selected


def build_source_region_proposals(
    *,
    page: int,
    dpi: int,
    page_size: tuple[int, int],
    candidate_boxes: Sequence[Bbox],
    words: Sequence[dict[str, object]],
    raster_proposals: Sequence[Bbox],
) -> SourceRegionProposalSet:
    """Normalize, merge, identify, and select model-visible Source Regions."""
    width, height = page_size
    fallback_id = f"page-{page}-r0000"
    raw_proposal_sources: dict[Bbox, set[str]] = {}
    normalized_candidate_boxes = [
        clamp_bbox_to_page(bbox, page_size) for bbox in candidate_boxes
    ]
    for normalized in normalized_candidate_boxes:
        if _bbox_area(normalized) < width * height * MAX_NONFALLBACK_AREA_RATIO:
            raw_proposal_sources.setdefault(normalized, set()).add("recognition")
    for word in words:
        bbox_points = word.get("bbox_points")
        if isinstance(bbox_points, list) and len(bbox_points) == 4:
            normalized = clamp_bbox_to_page(
                points_to_pixels(bbox_points, dpi), page_size
            )
            if _bbox_area(normalized) < width * height * MAX_NONFALLBACK_AREA_RATIO:
                raw_proposal_sources.setdefault(normalized, set()).add(
                    "native-pdf-word"
                )
    for proposal in raster_proposals:
        normalized = clamp_bbox_to_page(proposal, page_size)
        if _bbox_area(normalized) < width * height * MAX_NONFALLBACK_AREA_RATIO:
            raw_proposal_sources.setdefault(normalized, set()).add("raster-ink")

    proposal_sources, representative_by_bbox = _deduplicate_proposals(
        raw_proposal_sources
    )
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
            representative_by_bbox[bbox]
            for bbox in normalized_candidate_boxes
            if bbox in representative_by_bbox
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
    return SourceRegionProposalSet(
        source_regions=source_regions,
        normalized_candidate_boxes=normalized_candidate_boxes,
        representative_by_bbox=representative_by_bbox,
        region_id_by_bbox=region_id_by_bbox,
    )


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
