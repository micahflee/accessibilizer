"""The human-editable Review Record, its accessible report, and finalize gating.

A Review Record is the durable, editable account of a whole document's
reconstructed Semantic Layer, the original recognition candidates, the Conversion
Warnings, and their resolution history. It spans every converted page: the
Semantic Layer is a single flat list in document Logical Reading Order whose nodes
each carry a stable identity, their source ``page``, and explicit Source Region
references. Recognition Candidates have distinct identities and each reference
exactly one Source Region. Every warning carries the page it concerns and may
reference affected nodes and regions. It is serialized as human-editable YAML,
validated against the canonical versioned JSON Schema in
``schemas/review-record-3.0.schema.json`` (mirrored by
:func:`review_record_schema`), and finalized only once every warning is resolved.

A warning is resolved exactly one of three ways — ``corrected``, ``accepted``, or
``not_applicable`` (which requires a reason) — and every resolution carries the
Reviewer's non-secret identifier. Superseded resolutions are moved into each
warning's history so later reviewers can see what changed.
"""

from __future__ import annotations

import copy
import html
import math
import re
from typing import Any, NoReturn, Sequence

import yaml
from jsonschema import Draft202012Validator


REVIEW_RECORD_SCHEMA_VERSION = "3.0"
REVIEW_REPORT_VERSION = "3.0"

RESOLUTION_STATUSES: tuple[str, ...] = ("corrected", "accepted", "not_applicable")
_STATUS_LABELS = {
    "corrected": "Corrected",
    "accepted": "Accepted",
    "not_applicable": "Not applicable",
}
CANDIDATE_TYPES: tuple[str, ...] = (
    "text",
    "handwriting",
    "formula",
    "table",
    "figure",
    "document_structure",
)


class ReviewRecordError(Exception):
    """A Review Record is not valid or cannot be finalized as written."""


# --- semantic-layer node sub-schemas (shared with page semantics) ------------


# A node's source page is required on every Semantic Layer node so a whole-document
# record stays flat while remaining unambiguous about where each node belongs.
_PAGE_PROPERTY = {"type": "integer", "minimum": 1}
_SOURCE_REGION_ID = "^page-[0-9]+-r[0-9]{4,}$"
_SEMANTIC_NODE_ID = "^page-[0-9]+-s[0-9]{4,}$"
_CANDIDATE_ID = "^page-[0-9]+-c[0-9]{4,}$"


def _node_properties() -> dict[str, Any]:
    return {
        "id": {"type": "string", "pattern": _SEMANTIC_NODE_ID},
        "page": _PAGE_PROPERTY,
        "source_regions": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": _SOURCE_REGION_ID},
        },
    }


_NODE_REQUIRED = ["id", "page", "source_regions"]


def _semantic_layer_defs() -> dict[str, Any]:
    return {
        "heading": {
            "type": "object",
            "additionalProperties": False,
            "required": [*_NODE_REQUIRED, "level", "text", "type"],
            "properties": {
                **_node_properties(),
                # Heading hierarchy: H1 through H6 so section structure survives.
                "level": {"type": "integer", "minimum": 1, "maximum": 6},
                "text": {"type": "string", "minLength": 1},
                "type": {"const": "heading"},
            },
        },
        "paragraph": {
            "type": "object",
            "additionalProperties": False,
            "required": [*_NODE_REQUIRED, "text", "type"],
            "properties": {
                **_node_properties(),
                "text": {"type": "string", "minLength": 1},
                "type": {"const": "paragraph"},
            },
        },
        "formula": {
            "type": "object",
            "additionalProperties": False,
            "required": [*_NODE_REQUIRED, "normalized_math", "spoken_math_alternative", "type"],
            "properties": {
                **_node_properties(),
                "normalized_math": {"type": "string", "minLength": 1},
                "spoken_math_alternative": {"type": "string", "minLength": 1},
                "type": {"const": "formula"},
            },
        },
        "figure": {
            "type": "object",
            "additionalProperties": False,
            "required": [*_NODE_REQUIRED, "complexity", "figure_alternative", "type"],
            "properties": {
                **_node_properties(),
                "complexity": {"enum": ["simple", "complex"]},
                "detailed_figure_description": {"type": "string", "minLength": 1},
                "figure_alternative": {"type": "string", "minLength": 1},
                "type": {"const": "figure"},
            },
            # A complex Informative Figure carries a Detailed Figure Description; a
            # simple one carries only its concise Figure Alternative.
            "if": {"properties": {"complexity": {"const": "complex"}}},
            "then": {"required": ["detailed_figure_description"]},
            "else": {"not": {"required": ["detailed_figure_description"]}},
        },
        "link": {
            "type": "object",
            "additionalProperties": False,
            "required": [*_NODE_REQUIRED, "href", "text", "type"],
            "properties": {
                **_node_properties(),
                # A reconstructable link exposes its announced text and its
                # destination so assistive technology can follow it.
                "href": {"type": "string", "minLength": 1},
                "text": {"type": "string", "minLength": 1},
                "type": {"const": "link"},
            },
        },
        "table": {
            "type": "object",
            "additionalProperties": False,
            "required": [*_NODE_REQUIRED, "rows", "type"],
            "properties": {
                **_node_properties(),
                # Present only when the table has a caption.
                "caption": {"type": "string", "minLength": 1},
                "rows": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["cells"],
                        "properties": {
                            "cells": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"$ref": "#/$defs/table_cell"},
                            }
                        },
                    },
                },
                "type": {"const": "table"},
            },
        },
        "table_cell": {
            "type": "object",
            "additionalProperties": False,
            "required": ["col_span", "kind", "row_span", "scope", "text"],
            "properties": {
                "kind": {"enum": ["header", "data"]},
                "text": {"type": "string"},
                "scope": {"enum": ["col", "row", "both", "none"]},
                "row_span": {"type": "integer", "minimum": 1},
                "col_span": {"type": "integer", "minimum": 1},
            },
            # A header cell associates the cells it labels through a scope; a data
            # cell governs nothing and carries scope "none".
            "if": {"properties": {"kind": {"const": "data"}}},
            "then": {"properties": {"scope": {"const": "none"}}},
            "else": {"properties": {"scope": {"enum": ["col", "row", "both"]}}},
        },
    }


def _page_reconstruction_schema() -> dict[str, Any]:
    """One page's reconstruction provenance within a whole-document record."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "document_class",
            "page",
            "primary_language_is_english",
            "reading_order",
            "reading_order_is_unambiguous",
            "verified_regions",
        ],
        "properties": {
            "document_class": {"enum": ["stem_instructional", "other"]},
            "page": _PAGE_PROPERTY,
            "primary_language_is_english": {"type": "boolean"},
            "reading_order": {
                "type": "array",
                "items": {"enum": ["heading", "paragraph", "formula", "figure", "table"]},
            },
            "reading_order_is_unambiguous": {"type": "boolean"},
            "verified_regions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["agrees_with_page", "source_region", "type"],
                    "properties": {
                        "agrees_with_page": {"type": "boolean"},
                        "source_region": {"type": "string", "pattern": _SOURCE_REGION_ID},
                        "type": {"enum": ["formula", "table", "figure"]},
                    },
                },
            },
        },
    }


def _reconstruction_schema() -> dict[str, Any]:
    """Document-level reconstruction provenance shared across every page."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "page_prompt_version",
            "page_schema_version",
            "pages",
            "provider_endpoint",
            "provider_model",
            "region_prompt_version",
            "region_schema_version",
        ],
        "properties": {
            "page_prompt_version": {"type": "string", "minLength": 1},
            "page_schema_version": {"type": "string", "minLength": 1},
            "provider_endpoint": {"type": "string", "minLength": 1},
            "provider_model": {"type": "string", "minLength": 1},
            "region_prompt_version": {"type": "string", "minLength": 1},
            "region_schema_version": {"type": "string", "minLength": 1},
            "pages": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/page_reconstruction"},
            },
        },
    }


def review_record_schema() -> dict[str, Any]:
    """Return the canonical Review Record JSON Schema as a dict.

    This is the runtime source of truth; ``schemas/review-record-3.0.schema.json``
    mirrors it, and a drift test keeps the two in sync.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://accessibilizer.org/schemas/review-record-3.0.schema.json",
        "title": "Accessibilizer Review Record 3.0",
        "description": (
            "The human-editable, durable account of a whole document's reconstructed "
            "Semantic Layer, canonical Source Regions, non-authoritative Recognition "
            "Candidates, and Conversion Warnings with their resolution history. "
            "Semantic Layer nodes, Candidates, and Source Regions have distinct stable "
            "identities joined by explicit same-page references. Source Region geometry "
            "uses displayed-page PDF points; crops are derived and are not stored. "
            "Review-only identity and evidence fields do not cross the PDF-authoring "
            "boundary. Finalization is blocked while any warning is unresolved."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidates",
            "language",
            "page_dimensions",
            "pages",
            "reconstruction",
            "schema_version",
            "semantic_layer",
            "source_sha256",
            "source_regions",
            "title",
            "warnings",
        ],
        "properties": {
            "schema_version": {"const": "3.0"},
            "pages": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "integer", "minimum": 1},
            },
            "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "page_dimensions": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/page_dimensions"},
            },
            "source_regions": {
                "type": "array",
                "items": {"$ref": "#/$defs/source_region"},
            },
            "title": {"type": "string", "minLength": 1},
            "language": {"type": "string", "minLength": 1},
            "semantic_layer": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {"$ref": "#/$defs/heading"},
                        {"$ref": "#/$defs/paragraph"},
                        {"$ref": "#/$defs/formula"},
                        {"$ref": "#/$defs/figure"},
                        {"$ref": "#/$defs/link"},
                        {"$ref": "#/$defs/table"},
                    ]
                },
            },
            "candidates": {"type": "array", "items": {"$ref": "#/$defs/candidate"}},
            "warnings": {"type": "array", "items": {"$ref": "#/$defs/warning"}},
            "reconstruction": {"$ref": "#/$defs/reconstruction"},
        },
        "$defs": {
            **_semantic_layer_defs(),
            "page_dimensions": {
                "type": "object",
                "additionalProperties": False,
                "required": ["height_points", "page", "width_points"],
                "properties": {
                    "page": _PAGE_PROPERTY,
                    "width_points": {"type": "number", "exclusiveMinimum": 0},
                    "height_points": {"type": "number", "exclusiveMinimum": 0},
                },
            },
            "source_region": {
                "type": "object",
                "additionalProperties": False,
                "required": ["bbox_points", "id", "page"],
                "properties": {
                    "id": {"type": "string", "pattern": _SOURCE_REGION_ID},
                    "page": _PAGE_PROPERTY,
                    "bbox_points": {
                        "type": "array",
                        "prefixItems": [
                            {"type": "number", "minimum": 0},
                            {"type": "number", "minimum": 0},
                            {"type": "number", "minimum": 0},
                            {"type": "number", "minimum": 0},
                        ],
                        "items": False,
                        "minItems": 4,
                    },
                },
            },
            "page_reconstruction": _page_reconstruction_schema(),
            "reconstruction": _reconstruction_schema(),
            "candidate": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "source_region", "text", "type"],
                "properties": {
                    "id": {"type": "string", "pattern": _CANDIDATE_ID},
                    "source_region": {"type": "string", "pattern": _SOURCE_REGION_ID},
                    "type": {"enum": list(CANDIDATE_TYPES)},
                    "text": {"type": ["string", "null"]},
                },
            },
            "resolution": {
                "type": "object",
                "additionalProperties": False,
                "required": ["reviewer", "status"],
                "properties": {
                    "status": {"enum": list(RESOLUTION_STATUSES)},
                    "reason": {"type": ["string", "null"]},
                    "reviewer": {"type": "string", "minLength": 1},
                    "timestamp": {"type": "string"},
                },
                "if": {"properties": {"status": {"const": "not_applicable"}}},
                "then": {
                    "required": ["reason"],
                    "properties": {"reason": {"type": "string", "minLength": 1}},
                },
            },
            "warning": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "code", "history", "id", "message", "page", "resolution",
                    "semantic_nodes", "source_regions"
                ],
                "properties": {
                    "id": {"type": "string", "pattern": "^w[0-9]{4,}$"},
                    "code": {"type": "string", "minLength": 1},
                    "message": {"type": "string", "minLength": 1},
                    "page": {"type": ["integer", "null"], "minimum": 1},
                    "semantic_nodes": {
                        "type": "array", "uniqueItems": True,
                        "items": {"type": "string", "pattern": _SEMANTIC_NODE_ID},
                    },
                    "source_regions": {
                        "type": "array", "uniqueItems": True,
                        "items": {"type": "string", "pattern": _SOURCE_REGION_ID},
                    },
                    "resolution": {
                        "oneOf": [{"type": "null"}, {"$ref": "#/$defs/resolution"}]
                    },
                    "history": {"type": "array", "items": {"$ref": "#/$defs/resolution"}},
                },
            },
        },
    }


_VALIDATOR = Draft202012Validator(review_record_schema())


# --- construction ------------------------------------------------------------


def build_review_record(
    *,
    source_sha256: str,
    title: str,
    language: str,
    provider_endpoint: str,
    provider_model: str,
    page_prompt_version: str,
    page_schema_version: str,
    region_prompt_version: str,
    region_schema_version: str,
    pages: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble a whole-document Review Record from per-page reconstructions.

    ``pages`` is an ordered sequence of per-page reconstruction documents (as
    produced by :func:`accessibilizer.page.build_page_semantics_document`), each
    already carrying its own Semantic Layer, warnings, candidates, and page-level
    reconstruction provenance. This flattens them into one document-scoped record:
    the Semantic Layer is concatenated in page order with each node tagged with its
    source page, every Conversion Warning gets a document-stable identifier and its
    page, and the original recognition candidates are retained so each warning can
    be checked against the exact source region. Warnings start ``unresolved`` with
    an empty history.
    """
    semantic_layer: list[dict[str, Any]] = []
    page_dimensions: list[dict[str, Any]] = []
    source_regions: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    page_reconstructions: list[dict[str, Any]] = []
    page_numbers: list[int] = []
    warning_index = 0
    for page_document in pages:
        page_number = page_document["page"]
        page_numbers.append(page_number)
        page_dimensions.append(
            {"page": page_number, **copy.deepcopy(page_document["page_dimensions"])}
        )
        source_regions.extend(copy.deepcopy(page_document["source_regions"]))
        for node in page_document["semantic_layer"]:
            tagged = {"page": page_number, **copy.deepcopy(node)}
            semantic_layer.append(tagged)
        for candidate in page_document.get("candidates", []):
            candidates.append(
                {
                    "id": candidate["id"],
                    "type": candidate["type"],
                    "text": candidate.get("text"),
                    "source_region": candidate["source_region"],
                }
            )
        for warning in page_document.get("warnings", []):
            warning_index += 1
            warnings.append(
                {
                    "id": f"w{warning_index:04d}",
                    "code": warning["code"],
                    "message": warning["message"],
                    "page": page_number,
                    "semantic_nodes": copy.deepcopy(warning.get("semantic_nodes", [])),
                    "source_regions": copy.deepcopy(warning.get("source_regions", [])),
                    "resolution": None,
                    "history": [],
                }
            )
        reconstruction = page_document["reconstruction"]
        page_reconstructions.append(
            {
                "document_class": reconstruction["document_class"],
                "page": page_number,
                "primary_language_is_english": reconstruction[
                    "primary_language_is_english"
                ],
                "reading_order": copy.deepcopy(reconstruction["reading_order"]),
                "reading_order_is_unambiguous": reconstruction[
                    "reading_order_is_unambiguous"
                ],
                "verified_regions": copy.deepcopy(reconstruction["verified_regions"]),
            }
        )
    return {
        "schema_version": REVIEW_RECORD_SCHEMA_VERSION,
        "pages": page_numbers,
        "page_dimensions": page_dimensions,
        "source_sha256": source_sha256,
        "source_regions": source_regions,
        "title": title,
        "language": language,
        "semantic_layer": semantic_layer,
        "candidates": candidates,
        "warnings": warnings,
        "reconstruction": {
            "page_prompt_version": page_prompt_version,
            "page_schema_version": page_schema_version,
            "provider_endpoint": provider_endpoint,
            "provider_model": provider_model,
            "region_prompt_version": region_prompt_version,
            "region_schema_version": region_schema_version,
            "pages": page_reconstructions,
        },
    }


# --- YAML (de)serialization --------------------------------------------------


class _RecordDumper(yaml.SafeDumper):
    """A SafeDumper that renders multi-line strings as readable block scalars."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_RecordDumper.add_representer(str, _represent_str)


def dump_yaml(record: dict[str, Any]) -> str:
    return yaml.dump(
        record,
        Dumper=_RecordDumper,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )


def load_yaml(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ReviewRecordError(f"Review Record is not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ReviewRecordError("Review Record must be a YAML mapping")
    return data


# --- validation --------------------------------------------------------------


def validate_review_record(record: dict[str, Any]) -> None:
    """Validate a record against the canonical schema and the resolution rules."""
    errors = sorted(_VALIDATOR.iter_errors(record), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        location = "/".join(str(part) for part in error.path) or "(root)"
        raise ReviewRecordError(f"invalid Review Record at {location}: {error.message}")

    def fail(message: str) -> NoReturn:
        raise ReviewRecordError(f"invalid Review Record: {message}")

    def unique_by_id(collection: str) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for item in record[collection]:
            identifier = item["id"]
            if identifier in indexed:
                fail(f"duplicate {collection} id {identifier}")
            indexed[identifier] = item
        return indexed

    def id_page(identifier: str) -> int:
        match = re.match(r"^page-([0-9]+)-[rsc][0-9]{4,}$", identifier)
        if match is None:
            fail(f"invalid page-scoped id {identifier}")
        return int(match.group(1))

    pages = record["pages"]
    if len(pages) != len(set(pages)):
        fail("pages must be unique")
    page_set = set(pages)
    dimensions: dict[int, tuple[float, float]] = {}
    for entry in record["page_dimensions"]:
        page_number = entry["page"]
        if page_number in dimensions:
            fail(f"duplicate page dimensions for page {page_number}")
        width = float(entry["width_points"])
        height = float(entry["height_points"])
        if not math.isfinite(width) or not math.isfinite(height):
            fail(f"page {page_number} dimensions must be finite")
        dimensions[page_number] = (width, height)
    if set(dimensions) != page_set:
        fail("page_dimensions must describe every converted page exactly once")

    regions = unique_by_id("source_regions")
    for identifier, region in regions.items():
        page_number = region["page"]
        if page_number not in page_set:
            fail(f"Source Region {identifier} names an unconverted page")
        if id_page(identifier) != page_number:
            fail(f"Source Region {identifier} id does not agree with page {page_number}")
        x0, y0, x1, y1 = (float(value) for value in region["bbox_points"])
        if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
            fail(f"Source Region {identifier} bounds must be finite")
        width, height = dimensions[page_number]
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            fail(f"Source Region {identifier} bounds must be ordered and contained by page")

    nodes = unique_by_id("semantic_layer")
    for identifier, node in nodes.items():
        page_number = node["page"]
        if page_number not in page_set or id_page(identifier) != page_number:
            fail(f"Semantic Layer node {identifier} does not agree with its page")
        for reference in node["source_regions"]:
            referenced_region = regions.get(reference)
            if referenced_region is None:
                fail(f"Semantic Layer node {identifier} references unknown Source Region {reference}")
            if referenced_region["page"] != page_number:
                fail(f"Semantic Layer node {identifier} references a different-page Source Region")

    candidates = unique_by_id("candidates")
    for identifier, candidate in candidates.items():
        reference = candidate["source_region"]
        candidate_region = regions.get(reference)
        if candidate_region is None:
            fail(f"Recognition Candidate {identifier} references unknown Source Region {reference}")
        if id_page(identifier) != candidate_region["page"]:
            fail(f"Recognition Candidate {identifier} and its Source Region disagree on page")

    unique_by_id("warnings")
    for warning in record["warnings"]:
        warning_page = warning["page"]
        if warning_page is not None and warning_page not in page_set:
            fail(f"warning {warning['id']} names an unconverted page")
        for reference in warning["semantic_nodes"]:
            warning_node = nodes.get(reference)
            if warning_node is None:
                fail(f"warning {warning['id']} references unknown Semantic Layer node {reference}")
            if warning_page is not None and warning_node["page"] != warning_page:
                fail(f"warning {warning['id']} references a different-page Semantic Layer node")
        for reference in warning["source_regions"]:
            warning_region = regions.get(reference)
            if warning_region is None:
                fail(f"warning {warning['id']} references unknown Source Region {reference}")
            if warning_page is not None and warning_region["page"] != warning_page:
                fail(f"warning {warning['id']} references a different-page Source Region")

    for reconstruction in record["reconstruction"]["pages"]:
        page_number = reconstruction["page"]
        for verified in reconstruction["verified_regions"]:
            reference = verified["source_region"]
            verified_region = regions.get(reference)
            if verified_region is None or verified_region["page"] != page_number:
                fail(
                    f"page {page_number} verification references an unknown or different-page "
                    f"Source Region {reference}"
                )
    for warning in record["warnings"]:
        resolution = warning["resolution"]
        if resolution is None:
            continue
        if not str(resolution.get("reviewer") or "").strip():
            raise ReviewRecordError(
                f"resolution for warning {warning['id']} requires a reviewer identifier"
            )
        if resolution["status"] == "not_applicable" and not str(
            resolution.get("reason") or ""
        ).strip():
            raise ReviewRecordError(
                f"a not_applicable resolution for warning {warning['id']} requires a reason"
            )


# --- resolution and history --------------------------------------------------


def unresolved_warnings(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        warning
        for warning in record.get("warnings", [])
        if warning.get("resolution") is None
    ]


def is_finalizable(record: dict[str, Any]) -> bool:
    """A record finalizes only once every Conversion Warning is resolved."""
    return not unresolved_warnings(record)


def _resolution_core(resolution: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        resolution.get("status"),
        (resolution.get("reason") or None),
        (resolution.get("reviewer") or None),
    )


def commit_resolutions(
    record: dict[str, Any],
    *,
    baseline: dict[str, Any] | None,
    reviewer: str | None,
    now: str,
) -> dict[str, Any]:
    """Stamp resolutions and record any superseded ones in history.

    Each resolution missing a Reviewer inherits the configured ``reviewer``. A
    resolution that differs from the ``baseline`` (the last committed record) is
    stamped with ``now`` and the baseline's prior resolution is appended to that
    warning's history, so a changed decision never silently overwrites the record.
    """
    record = copy.deepcopy(record)
    warnings = record.get("warnings")
    if not isinstance(warnings, list):
        return record
    baseline_resolutions: dict[str, Any] = {}
    if isinstance(baseline, dict):
        for warning in baseline.get("warnings", []):
            if isinstance(warning, dict) and isinstance(warning.get("id"), str):
                baseline_resolutions[warning["id"]] = warning.get("resolution")
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        history = warning.get("history")
        if not isinstance(history, list):
            history = []
            warning["history"] = history
        resolution = warning.get("resolution")
        if not isinstance(resolution, dict):
            continue
        if not str(resolution.get("reviewer") or "").strip() and reviewer:
            resolution["reviewer"] = reviewer
        warning_id = warning.get("id")
        prior = baseline_resolutions.get(warning_id) if isinstance(warning_id, str) else None
        if isinstance(prior, dict):
            changed = _resolution_core(prior) != _resolution_core(resolution)
            if changed and (
                not history or _resolution_core(history[-1]) != _resolution_core(prior)
            ):
                history.append(copy.deepcopy(prior))
            stamp = changed
        else:
            stamp = True
        if stamp or not resolution.get("timestamp"):
            resolution["timestamp"] = now
    return record


# --- accessible review report ------------------------------------------------


def _status_label(resolution: dict[str, Any] | None) -> str:
    if resolution is None:
        return "Unresolved"
    return _STATUS_LABELS.get(str(resolution.get("status")), "Unresolved")


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _warning_ids(
    warnings: list[dict[str, Any]], *, node_id: str | None = None, region_id: str | None = None,
    page: int | None = None,
) -> str:
    """Return explicit warning associations; never infer them from list position."""
    return " ".join(
        str(warning["id"])
        for warning in warnings
        if (node_id is not None and node_id in warning.get("semantic_nodes", []))
        or (region_id is not None and region_id in warning.get("source_regions", []))
        or (page is not None and warning.get("page") == page
            and not warning.get("semantic_nodes") and not warning.get("source_regions"))
        or (page is None and warning.get("page") is None
            and not warning.get("semantic_nodes") and not warning.get("source_regions"))
    )


def _node_content(node: dict[str, Any]) -> str:
    node_type = str(node.get("type", ""))
    if node_type in {"heading", "paragraph", "link"}:
        return f"<p>{_escape(node.get('text', ''))}</p>"
    if node_type == "formula":
        return (f"<dl><dt>Normalized math</dt><dd>{_escape(node.get('normalized_math', ''))}</dd>"
                f"<dt>Spoken Math Alternative</dt><dd>{_escape(node.get('spoken_math_alternative', ''))}</dd></dl>")
    if node_type == "figure":
        details = ""
        if node.get("detailed_figure_description"):
            details = f"<dt>Detailed Figure Description</dt><dd>{_escape(node['detailed_figure_description'])}</dd>"
        return f"<dl><dt>Figure Alternative</dt><dd>{_escape(node.get('figure_alternative', ''))}</dd>{details}</dl>"
    if node_type == "table":
        rows: list[str] = []
        for row in node.get("rows", []):
            cells: list[str] = []
            for cell in row.get("cells", []):
                tag = "th" if cell.get("kind") == "header" else "td"
                scope = f' scope="{_escape(cell.get("scope"))}"' if tag == "th" else ""
                cells.append(f"<{tag}{scope}>{_escape(cell.get('text', ''))}</{tag}>")
            rows.append(f"<tr>{''.join(cells)}</tr>")
        caption = f"<caption>{_escape(node.get('caption', ''))}</caption>" if node.get("caption") else ""
        return f"<table>{caption}<tbody>{''.join(rows)}</tbody></table>"
    return f"<pre>{_escape(node)}</pre>"


def _region_evidence(
    region_id: str, candidates_by_region: dict[str, list[dict[str, Any]]], warnings: list[dict[str, Any]]
) -> str:
    warning_ids = _warning_ids(warnings, region_id=region_id)
    data_warning = f' data-warning-ids="{_escape(warning_ids)}"' if warning_ids else ""
    candidates = "".join(
        f"<li><strong>{_escape(candidate.get('type', 'Candidate'))}</strong>: {_escape(candidate.get('text') or 'No retained text')}</li>"
        for candidate in candidates_by_region.get(region_id, [])
    ) or "<li>No Recognition Candidates retained.</li>"
    return (
        f'<figure class="source-region"{data_warning}><img src="regions/{_escape(region_id)}.png" '
        f'alt="Source Region {_escape(region_id)}"><figcaption>Source Region {_escape(region_id)}</figcaption>'
        f"<details><summary>Recognition Candidates (non-authoritative)</summary><ul>{candidates}</ul></details></figure>"
    )


def _warning_row(
    warning: dict[str, Any], candidates_by_region: dict[str, list[dict[str, Any]]]
) -> str:
    identifier = html.escape(str(warning.get("id", "")))
    code = html.escape(str(warning.get("code", "")))
    message = html.escape(str(warning.get("message", "")))
    page = warning.get("page")
    page_cell = f"Page {html.escape(str(page))}" if page is not None else "Document"
    source_regions = warning.get("source_regions", [])
    region = source_regions[0] if source_regions else None
    if region:
        candidates = candidates_by_region.get(str(region), [])
        candidate = candidates[0] if candidates else {}
        source = html.escape(f"regions/{region}.png", quote=True)
        region_type = html.escape(str(candidate.get("type", "region")))
        recognized = candidate.get("text")
        recognized_cell = (
            f"<span>Recognized text: {html.escape(str(recognized))}</span>"
            if recognized
            else ""
        )
        region_cell = (
            f'<img src="{source}" '
            f'alt="Source region {html.escape(str(region))} ({region_type})">'
            f"<span> Region {html.escape(str(region))} ({region_type})</span>"
            f"{recognized_cell}"
        )
    else:
        region_cell = "Whole page"
    resolution = warning.get("resolution")
    if resolution is None:
        resolution_cell = "Awaiting reviewer resolution"
    else:
        reviewer = html.escape(str(resolution.get("reviewer", "")))
        timestamp = html.escape(str(resolution.get("timestamp", "")))
        reason = resolution.get("reason")
        detail = f" {html.escape(str(reason))}" if reason else ""
        resolution_cell = f"By {reviewer} on {timestamp}.{detail}"
    return (
        f'<tr><th scope="row">{identifier}: {code}</th>'
        f"<td>{page_cell}</td>"
        f"<td>{message}</td><td>{region_cell}</td>"
        f"<td>Status: {_status_label(resolution)}</td>"
        f"<td>{resolution_cell}</td></tr>"
    )


def render_review_report(record: dict[str, Any]) -> str:
    """Render a WCAG 2.2 AA HTML Review Report of a Review Record.

    Warning state is carried by explicit text (never color alone), each
    source-region warning shows a labelled crop, and warnings and resolutions are
    presented as a semantic table with row and column headers.
    """
    language = html.escape(str(record.get("language", "en")), quote=True)
    title = html.escape(str(record.get("title", "Untitled")))
    candidates_by_region: dict[str, list[dict[str, Any]]] = {}
    for candidate in record.get("candidates", []):
        if isinstance(candidate, dict) and isinstance(candidate.get("source_region"), str):
            candidates_by_region.setdefault(candidate["source_region"], []).append(candidate)
    warnings = [warning for warning in record.get("warnings", []) if isinstance(warning, dict)]
    if warnings:
        unresolved = sum(1 for warning in warnings if warning.get("resolution") is None)
        rows = "".join(
            _warning_row(warning, candidates_by_region) for warning in warnings
        )
        warnings_body = (
            f"<p>{len(warnings)} Conversion Warning(s); {unresolved} not yet resolved.</p>"
            "<table><caption>Conversion Warnings and their resolutions</caption>"
            "<thead><tr>"
            '<th scope="col">Warning</th><th scope="col">Page</th>'
            '<th scope="col">Concern</th>'
            '<th scope="col">Source region</th><th scope="col">Status</th>'
            '<th scope="col">Resolution</th>'
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        warnings_body = "<p>No Conversion Warnings remain.</p>"
    page_sections: list[str] = []
    pages = [page for page in record.get("pages", []) if isinstance(page, int)]
    for index, page in enumerate(pages):
        previous = f'<a href="#page-{pages[index - 1]}">Previous page</a>' if index else ""
        following = f'<a href="#page-{pages[index + 1]}">Next page</a>' if index + 1 < len(pages) else ""
        page_warning_ids = _warning_ids(warnings, page=page)
        page_warnings = [warning for warning in warnings if warning.get("page") in {None, page}]
        warning_list = "".join(
            f"<li><strong>{_escape(warning.get('id'))}: {_escape(warning.get('code'))}</strong> — {_escape(warning.get('message'))} (Status: {_status_label(warning.get('resolution'))})</li>"
            for warning in page_warnings
        ) or "<li>No Conversion Warnings for this page.</li>"
        cards: list[str] = []
        for node in record.get("semantic_layer", []):
            if not isinstance(node, dict) or node.get("page") != page:
                continue
            node_id = str(node.get("id", ""))
            node_warning_ids = _warning_ids(warnings, node_id=node_id)
            all_warning_ids = " ".join(filter(None, [page_warning_ids, node_warning_ids]))
            regions = "".join(_region_evidence(str(region), candidates_by_region, warnings)
                              for region in node.get("source_regions", []) if isinstance(region, str))
            cards.append(
                f'<article class="semantic-node" data-warning-ids="{_escape(all_warning_ids)}">'
                f"<h3>{_escape(str(node.get('type', '')).replace('_', ' ').title())}</h3>"
                f"{_node_content(node)}<section aria-label=\"Source Regions\">{regions}</section></article>"
            )
        page_sections.append(
            f'<section id="page-{page}" class="review-page"><h2>Source PDF page {page}</h2>'
            f'<nav aria-label="Page navigation">{previous} {following}</nav>'
            f'<figure class="source-page"><img src="regions/page-{page}.png" alt="Full Source PDF page {page}"><figcaption>Full Source PDF page {page}</figcaption></figure>'
            f'<section aria-label="Warnings for page {page}"><h3>Conversion Warnings</h3><ul>{warning_list}</ul></section>'
            f"{''.join(cards)}</section>"
        )
    return f"""<!doctype html>
<html lang="{language}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Review Report</title><style>
body{{font-family:system-ui,sans-serif;max-width:76rem;margin:auto;padding:1rem}} img{{max-width:100%;height:auto}} .semantic-node,.source-region{{border:1px solid;padding:1rem;margin:1rem 0}} :focus-visible{{outline:3px solid;outline-offset:3px}} [hidden]{{display:none}}</style></head>
<body><main>
<h1>{title} — Review Report</h1>
<section aria-labelledby="document-identity"><h2 id="document-identity">Document</h2>
<dl><dt>Pages</dt><dd>{html.escape(", ".join(str(page) for page in record.get("pages", [])))}</dd>
<dt>Language</dt><dd>{language}</dd>
<dt>Source SHA-256</dt>\
<dd><code>{html.escape(str(record.get("source_sha256", "")))}</code></dd></dl></section>
<button type="button" id="warnings-only" aria-pressed="false">Show warnings only</button>
<section aria-labelledby="semantic-layer"><h2 id="semantic-layer">Semantic Layer by Source PDF page</h2>
{''.join(page_sections)}</section>
<section aria-labelledby="conversion-warnings">\
<h2 id="conversion-warnings">Conversion Warnings</h2>
{warnings_body}</section>
</main><script>document.getElementById('warnings-only').addEventListener('click',function(){{var on=this.getAttribute('aria-pressed')!=='true';this.setAttribute('aria-pressed',String(on));this.textContent=on?'Show all content':'Show warnings only';document.querySelectorAll('.semantic-node').forEach(function(node){{node.hidden=on&&!node.dataset.warningIds;}});}});</script></body></html>
"""
