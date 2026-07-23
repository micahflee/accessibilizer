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
import json
import math
import re
from typing import Any, NoReturn, Sequence

import yaml
from jsonschema import Draft202012Validator


REVIEW_RECORD_SCHEMA_VERSION = "3.0"
REVIEW_REPORT_VERSION = "4.0"

REVIEW_REPORT_STYLESHEET = "review-report.css"
REVIEW_REPORT_SCRIPT = "review-report.js"

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
            "recognition": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "backend", "backend_version", "proposal_generation", "weights_version"
                ],
                "properties": {
                    "backend": {"type": "string", "minLength": 1},
                    "backend_version": {"type": "string", "minLength": 1},
                    "weights_version": {"type": "string", "minLength": 1},
                    "proposal_generation": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "algorithm", "algorithm_version",
                            "deduplication_pixels", "max_nonfallback_area_ratio", "sources",
                        ],
                        "properties": {
                            "algorithm": {"type": "string", "minLength": 1},
                            "algorithm_version": {"type": "string", "minLength": 1},
                            "deduplication_pixels": {"type": "integer", "minimum": 1},
                            "max_nonfallback_area_ratio": {
                                "type": "number", "exclusiveMinimum": 0, "maximum": 1,
                            },
                            "model_binding_deduplication_pixels": {
                                "type": "integer", "minimum": 1,
                            },
                            "model_binding_overlay_grid": {
                                "type": "array",
                                "prefixItems": [
                                    {"type": "integer", "minimum": 1},
                                    {"type": "integer", "minimum": 1},
                                ],
                                "items": False,
                                "minItems": 2,
                                "maxItems": 2,
                            },
                            "sources": {
                                "type": "array", "minItems": 1, "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
            "verified_regions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["agrees_with_page", "source_region", "type"],
                    "properties": {
                        "agrees_with_page": {"type": "boolean"},
                        "source_region": {"type": "string", "pattern": _SOURCE_REGION_ID},
                        "type": {
                            "enum": [
                                "heading",
                                "paragraph",
                                "formula",
                                "table",
                                "figure",
                            ]
                        },
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
                    "backend": {"type": "string", "minLength": 1},
                    "raw_class": {"type": "string", "minLength": 1},
                    "layout_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "ocr_text_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "verification": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["eligible", "reason_codes"],
                        "properties": {
                            "eligible": {"type": "boolean"},
                            "reason_codes": {
                                "type": "array", "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1},
                            },
                        },
                    },
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
            retained = copy.deepcopy(candidate)
            retained.setdefault("text", None)
            candidates.append(retained)
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
        page_reconstruction = {
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
        if "recognition" in reconstruction:
            page_reconstruction["recognition"] = copy.deepcopy(
                reconstruction["recognition"]
            )
        page_reconstructions.append(page_reconstruction)
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
    for page_number, (width, height) in dimensions.items():
        fallback_id = f"page-{page_number}-r0000"
        fallback = regions.get(fallback_id)
        if fallback is not None and [
            float(value) for value in fallback["bbox_points"]
        ] != [
            0.0, 0.0, width, height
        ]:
            fail(
                f"page {page_number} whole-page fallback Source Region "
                f"{fallback_id} with exact page bounds"
            )

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

    reconstruction_pages = [
        reconstruction["page"] for reconstruction in record["reconstruction"]["pages"]
    ]
    if len(reconstruction_pages) != len(set(reconstruction_pages)) or set(
        reconstruction_pages
    ) != page_set:
        fail("reconstruction must describe every converted page exactly once")
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


def _region_warning_ids(warnings: list[dict[str, Any]], region_id: str) -> str:
    """Return explicit region warning associations; never infer from list position."""
    return " ".join(
        str(warning["id"])
        for warning in warnings
        if region_id in warning.get("source_regions", [])
    )


def _string_regions(node: dict[str, Any]) -> list[str]:
    """Return a node's Source Region references, ignoring any malformed entries."""
    return [region for region in node.get("source_regions", []) if isinstance(region, str)]


def _warnings_for_node(
    warnings: list[dict[str, Any]], node: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return warnings explicitly attached to a node or one of its regions."""
    node_id = node.get("id")
    region_ids = set(_string_regions(node))
    return [
        warning
        for warning in warnings
        if node_id in warning.get("semantic_nodes", [])
        or bool(region_ids.intersection(warning.get("source_regions", [])))
    ]


def _warning_link(warning: dict[str, Any]) -> str:
    identifier = _escape(warning.get("id", ""))
    code = _escape(warning.get("code", ""))
    message = _escape(warning.get("message", ""))
    status = _status_label(warning.get("resolution"))
    return (
        f'<a href="#warning-{identifier}">{identifier}: {code}</a> — '
        f"{message} (Status: {status})"
    )


def _node_content(node: dict[str, Any]) -> str:
    node_type = str(node.get("type", ""))
    if node_type == "heading":
        return (
            f"<dl><dt>Heading level</dt><dd>{_escape(node.get('level', ''))}</dd>"
            f"<dt>Text</dt><dd>{_escape(node.get('text', ''))}</dd></dl>"
        )
    if node_type == "paragraph":
        return f"<p>{_escape(node.get('text', ''))}</p>"
    if node_type == "link":
        return (
            f"<dl><dt>Link text</dt><dd>{_escape(node.get('text', ''))}</dd>"
            f"<dt>Destination</dt><dd><code>{_escape(node.get('href', ''))}</code></dd></dl>"
        )
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
                row_span = f' rowspan="{_escape(cell.get("row_span", 1))}"'
                col_span = f' colspan="{_escape(cell.get("col_span", 1))}"'
                cells.append(
                    f"<{tag}{scope}{row_span}{col_span}>"
                    f"{_escape(cell.get('text', ''))}</{tag}>"
                )
            rows.append(f"<tr>{''.join(cells)}</tr>")
        caption = f"<caption>{_escape(node.get('caption', ''))}</caption>" if node.get("caption") else ""
        return f"<table>{caption}<tbody>{''.join(rows)}</tbody></table>"
    return f"<pre>{_escape(node)}</pre>"


def _region_evidence(
    region_id: str, candidates_by_region: dict[str, list[dict[str, Any]]], warnings: list[dict[str, Any]]
) -> str:
    warning_ids = _region_warning_ids(warnings, region_id)
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
    source_regions = [
        region for region in warning.get("source_regions", []) if isinstance(region, str)
    ]
    if source_regions:
        rendered_regions: list[str] = []
        for region in source_regions:
            candidates = candidates_by_region.get(region, [])
            candidate = candidates[0] if candidates else {}
            source = html.escape(f"regions/{region}.png", quote=True)
            region_type = html.escape(str(candidate.get("type", "region")))
            recognized = candidate.get("text")
            recognized_cell = (
                f"<span>Recognized text: {html.escape(str(recognized))}</span>"
                if recognized
                else ""
            )
            rendered_regions.append(
                f'<div><img src="{source}" '
                f'alt="Source region {html.escape(region)} ({region_type})">'
                f"<span> Region {html.escape(region)} ({region_type})</span>"
                f"{recognized_cell}</div>"
            )
        region_cell = "".join(rendered_regions)
    else:
        region_cell = "No Source Region (page or document scope)"
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
        f'<tr id="warning-{identifier}"><th scope="row">{identifier}: {code}</th>'
        f"<td>{page_cell}</td>"
        f"<td>{message}</td><td>{region_cell}</td>"
        f"<td>Status: {_status_label(resolution)}</td>"
        f"<td>{resolution_cell}</td></tr>"
    )


def _primary_at_content(node: dict[str, Any]) -> str:
    """Return the primary assistive-technology content shown in the concise view."""
    node_type = str(node.get("type", ""))
    if node_type in {"heading", "paragraph", "link"}:
        return str(node.get("text", ""))
    if node_type == "formula":
        return str(node.get("spoken_math_alternative", ""))
    if node_type == "figure":
        return str(node.get("figure_alternative", ""))
    if node_type == "table":
        caption = node.get("caption")
        return str(caption) if caption else "Semantic Table"
    return ""


def _type_label(node: dict[str, Any]) -> str:
    return str(node.get("type", "")).replace("_", " ").title() or "Component"


def _warning_status_text(attached: list[dict[str, Any]]) -> str:
    if not attached:
        return "No attached Conversion Warnings"
    unresolved = sum(1 for warning in attached if warning.get("resolution") is None)
    return f"{len(attached)} attached Conversion Warning(s); {unresolved} unresolved"


def _component_article(
    node: dict[str, Any],
    index: int,
    warnings: list[dict[str, Any]],
    candidates_by_region: dict[str, list[dict[str, Any]]],
    regions_by_id: dict[str, dict[str, Any]],
) -> str:
    """Render one Semantic Layer node as a hidden, escaped Component panel.

    The concise fields (type, primary content, warning status, region count) sit
    above a native ``All details`` disclosure. Every source-derived value is
    escaped; the panel never carries source content as executable markup.
    """
    node_id = str(node.get("id", ""))
    page = node.get("page")
    type_label = _type_label(node)
    regions = _string_regions(node)
    attached = _warnings_for_node(warnings, node)
    warning_ids = " ".join(str(warning.get("id", "")) for warning in attached)

    region_controls: list[str] = []
    coord_rows: list[str] = []
    for number, region in enumerate(regions, start=1):
        geometry = regions_by_id.get(region, {})
        bbox = geometry.get("bbox_points", [])
        coords = ", ".join(_escape(value) for value in bbox) if bbox else "unknown"
        region_controls.append(
            f'<li class="region-control"><span class="region-number">Region {number} of {len(regions)}</span> '
            f'<span class="region-id"><code>{_escape(region)}</code></span> '
            f'<button type="button" data-emphasize="{_escape(region)}">Emphasize box {number}</button> '
            f'<button type="button" data-zoom-region="{_escape(region)}">Zoom to box {number}</button></li>'
        )
        coord_rows.append(
            f"<dt>Box {number} — <code>{_escape(region)}</code></dt>"
            f"<dd>Bounding box (PDF points): {coords}</dd>"
        )
    region_controls_html = (
        f'<ul class="region-controls" aria-label="Source Region boxes">{"".join(region_controls)}</ul>'
        if region_controls
        else '<p class="region-controls-empty">This Component references no Source Regions.</p>'
    )

    attached_warning_list = "".join(
        f"<li>{_warning_link(warning)}</li>" for warning in attached
    ) or "<li>No Conversion Warnings attached to this Component.</li>"
    evidence = "".join(
        _region_evidence(region, candidates_by_region, warnings) for region in regions
    ) or "<p>No Source Region crops for this Component.</p>"

    return (
        f'<article id="{_escape(node_id)}" class="component" data-index="{index}" '
        f'data-page="{_escape(page)}" data-regions="{_escape(" ".join(regions))}" '
        f'data-warning-ids="{_escape(warning_ids)}" tabindex="-1" hidden>'
        f'<h3 class="component-type">{_escape(type_label)}</h3>'
        f'<p class="component-page">Source PDF page {_escape(page)}</p>'
        f'<p class="component-at-content"><span class="field-label">Assistive-technology content:</span> '
        f'{_escape(_primary_at_content(node))}</p>'
        f'<p class="component-warning-status">{_escape(_warning_status_text(attached))}</p>'
        f'<p class="component-region-count">{len(regions)} referenced Source Region(s)</p>'
        f'{region_controls_html}'
        f'<details class="all-details">'
        f'<summary>All details</summary>'
        f'<dl class="component-identity"><dt>Semantic Layer node ID</dt>'
        f'<dd><code>{_escape(node_id)}</code></dd>'
        f'<dt>Node type (canonical)</dt><dd>{_escape(str(node.get("type", "")))}</dd></dl>'
        f'<section aria-label="Component fields"><h4>Component fields</h4>{_node_content(node)}</section>'
        f'<section aria-label="Exact Source Region coordinates"><h4>Source Region coordinates</h4>'
        f'<dl class="region-coordinates">{"".join(coord_rows) or "<dt>None</dt><dd>No Source Regions.</dd>"}</dl></section>'
        f'<section aria-label="Attached Conversion Warnings"><h4>Attached Conversion Warnings</h4>'
        f'<ul>{attached_warning_list}</ul></section>'
        f'<section aria-label="Source Region evidence"><h4>Source Region evidence and Recognition Candidates</h4>'
        f'{evidence}</section>'
        f'</details></article>'
    )


def _review_report_data(
    record: dict[str, Any],
    components: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    regions_by_id: dict[str, dict[str, Any]],
) -> str:
    """Serialize the numeric geometry and identity the client script needs.

    The payload carries stable IDs, page numbers, page dimensions, and Source
    Region bounding boxes (numbers), plus display strings the script inserts with
    ``textContent`` only. Angle brackets and ampersands are escaped so no
    source-derived value can break out of the embedding script tag.
    """
    dimensions: dict[str, dict[str, float]] = {}
    for entry in record.get("page_dimensions", []):
        if isinstance(entry, dict) and isinstance(entry.get("page"), int):
            dimensions[str(entry["page"])] = {
                "width": float(entry.get("width_points", 0.0)),
                "height": float(entry.get("height_points", 0.0)),
            }
    regions: dict[str, dict[str, Any]] = {}
    for region_id, geometry in regions_by_id.items():
        bbox = [float(value) for value in geometry.get("bbox_points", [])]
        regions[region_id] = {"page": geometry.get("page"), "bbox": bbox}

    component_data: list[dict[str, Any]] = []
    for index, node in enumerate(components):
        attached = _warnings_for_node(warnings, node)
        component_data.append(
            {
                "id": str(node.get("id", "")),
                "index": index,
                "page": node.get("page"),
                "type": str(node.get("type", "")),
                "typeLabel": _type_label(node),
                "atContent": _primary_at_content(node),
                "regions": _string_regions(node),
                "warningIds": [str(warning.get("id", "")) for warning in attached],
                "hasWarning": bool(attached),
            }
        )

    payload = {
        "reportVersion": REVIEW_REPORT_VERSION,
        "components": component_data,
        "pageDimensions": dimensions,
        "regions": regions,
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    # Neutralize any source-derived string that could otherwise close the
    # embedding <script> element or introduce markup.
    return (
        serialized.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def render_review_report(record: dict[str, Any]) -> str:
    """Render the interactive, split-screen HTML Review Report of a Review Record.

    The report is a JavaScript-required Component navigator: source-derived values
    are escaped and never become executable markup; the interactive shell,
    styling, and behavior load only through the relative local ``review-report.css``
    and ``review-report.js`` files this module also emits.
    """
    language = html.escape(str(record.get("language", "en")), quote=True)
    title = html.escape(str(record.get("title", "Untitled")))

    candidates_by_region: dict[str, list[dict[str, Any]]] = {}
    for candidate in record.get("candidates", []):
        if isinstance(candidate, dict) and isinstance(candidate.get("source_region"), str):
            candidates_by_region.setdefault(candidate["source_region"], []).append(candidate)

    regions_by_id: dict[str, dict[str, Any]] = {
        str(region["id"]): region
        for region in record.get("source_regions", [])
        if isinstance(region, dict) and isinstance(region.get("id"), str)
    }

    warnings = [warning for warning in record.get("warnings", []) if isinstance(warning, dict)]
    unresolved = sum(1 for warning in warnings if warning.get("resolution") is None)

    pages = [page for page in record.get("pages", []) if isinstance(page, int)]
    page_set = set(pages)
    components = [
        node
        for node in record.get("semantic_layer", [])
        if isinstance(node, dict) and node.get("page") in page_set
    ]
    articles = "".join(
        _component_article(node, index, warnings, candidates_by_region, regions_by_id)
        for index, node in enumerate(components)
    )

    if warnings:
        rows = "".join(_warning_row(warning, candidates_by_region) for warning in warnings)
        warnings_table = (
            "<table><caption>Conversion Warnings and their resolutions</caption>"
            "<thead><tr>"
            '<th scope="col">Warning</th><th scope="col">Page</th>'
            '<th scope="col">Concern</th>'
            '<th scope="col">Source regions</th><th scope="col">Status</th>'
            '<th scope="col">Resolution</th>'
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        warnings_table = "<p>No Conversion Warnings remain.</p>"

    # Page- and document-level warnings — those attached to no node or region —
    # live in a separate always-reachable summary and are never Components.
    scope_items: list[str] = []
    for warning in warnings:
        if warning.get("semantic_nodes") or warning.get("source_regions"):
            continue
        page = warning.get("page")
        label = "Document" if page is None else f"Page {_escape(page)}"
        scope_items.append(f"<li>{label}: {_warning_link(warning)}</li>")
    scope_warnings = "".join(scope_items) or (
        "<li>No page- or document-level Conversion Warnings.</li>"
    )

    data_script = _review_report_data(record, components, warnings, regions_by_id)
    pages_text = html.escape(", ".join(str(page) for page in record.get("pages", [])))
    source_sha = html.escape(str(record.get("source_sha256", "")))

    return f"""<!doctype html>
<html lang="{language}" class="no-js">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Review Report</title>
<link rel="stylesheet" href="{REVIEW_REPORT_STYLESHEET}"></head>
<body>
<noscript><p class="noscript-message">The interactive Review Report requires JavaScript. \
Enable JavaScript in your browser to step through Components, view Source Region boxes, \
and filter Conversion Warnings.</p></noscript>
<main>
<h1>{title} — Review Report</h1>
<p class="unresolved-count" role="status">{unresolved} unresolved Conversion Warning(s) of {len(warnings)} total.</p>
<details class="doc-metadata"><summary>Document details</summary>
<dl><dt>Pages</dt><dd>{pages_text}</dd>
<dt>Language</dt><dd>{language}</dd>
<dt>Source SHA-256</dt><dd><code>{source_sha}</code></dd></dl></details>
<details id="all-warnings" class="all-warnings"><summary>Conversion Warnings ({unresolved} unresolved)</summary>
<section aria-label="Page- and document-level Conversion Warnings"><h2>Page- and document-level Conversion Warnings</h2>
<ul>{scope_warnings}</ul></section>
{warnings_table}</details>
<section class="report-app" aria-labelledby="component-navigator-heading" hidden>
<h2 id="component-navigator-heading">Component navigator</h2>
<div id="component-toolbar" role="toolbar" aria-label="Component navigation" aria-controls="component-host">
<button type="button" data-nav="prev">Previous</button>
<button type="button" data-nav="next">Next</button>
<span id="component-position" class="component-position" aria-hidden="true">Component 0 of 0</span>
<button type="button" data-action="focus-details">Focus component details</button>
<button type="button" id="warnings-only" aria-pressed="false">Warnings only</button>
</div>
<div id="live-region" class="visually-hidden" aria-live="polite" role="status"></div>
<div class="panes">
<section class="pane pane-page" aria-label="Source PDF page">
<div class="page-controls" role="group" aria-label="Page zoom">
<button type="button" data-zoom="component">Zoom to component</button>
<button type="button" data-zoom="fit">Fit page</button>
<button type="button" data-zoom="out">Zoom out</button>
<button type="button" data-zoom="in">Zoom in</button>
</div>
<div id="page-viewport" class="page-viewport">
<div id="page-stage" class="page-stage">
<img id="page-image" class="page-image" alt="">
<svg id="overlay" class="overlay" aria-hidden="true" focusable="false" preserveAspectRatio="none"></svg>
</div>
</div>
</section>
<div id="splitter" class="splitter" role="separator" aria-orientation="vertical" \
aria-label="Resize panes" tabindex="0" aria-valuemin="20" aria-valuemax="80" aria-valuenow="50">
<button type="button" id="reset-panes">Reset pane sizes</button>
</div>
<section class="pane pane-details" aria-label="Component details">
<div id="component-host">{articles}</div>
<p id="filter-empty" class="filter-empty" hidden>No Components have attached or region-associated Conversion Warnings.</p>
</section>
</div>
</section>
</main>
<script type="application/json" id="review-data">{data_script}</script>
<script src="{REVIEW_REPORT_SCRIPT}"></script>
</body></html>
"""


def review_report_css() -> str:
    """Return the stylesheet emitted beside the report as ``review-report.css``."""
    return _REVIEW_REPORT_CSS


def review_report_javascript() -> str:
    """Return the client script emitted beside the report as ``review-report.js``."""
    return _REVIEW_REPORT_JS


_REVIEW_REPORT_CSS = r""":root { color-scheme: light dark; }

* { box-sizing: border-box; }

body {
  font-family: system-ui, sans-serif;
  margin: 0;
  padding: 1rem;
  line-height: 1.4;
}

h1 { font-size: 1.5rem; }

img { max-width: 100%; height: auto; }

:focus-visible {
  outline: 3px solid;
  outline-offset: 2px;
}

[hidden] { display: none !important; }

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  border: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  white-space: nowrap;
}

.noscript-message {
  border: 2px solid;
  padding: 1rem;
  font-weight: bold;
}

.unresolved-count {
  font-weight: bold;
  margin: 0.5rem 0;
}

.doc-metadata,
.all-warnings {
  margin: 0.5rem 0;
}

.all-warnings table {
  border-collapse: collapse;
  width: 100%;
}

.all-warnings th,
.all-warnings td {
  border: 1px solid;
  padding: 0.4rem;
  text-align: left;
  vertical-align: top;
}

button {
  font: inherit;
  padding: 0.4rem 0.7rem;
  cursor: pointer;
}

button[aria-pressed="true"] {
  outline: 2px solid;
  font-weight: bold;
}

.report-app {
  margin-top: 1rem;
}

#component-toolbar,
.page-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
  margin-bottom: 0.5rem;
}

.component-position {
  font-weight: bold;
}

.panes {
  --left-pane: 50%;
  display: flex;
  align-items: stretch;
  gap: 0;
  min-height: 60vh;
}

.pane {
  min-width: 0;
  overflow: auto;
}

.pane-page {
  flex: 0 0 var(--left-pane);
  display: flex;
  flex-direction: column;
}

.pane-details {
  flex: 1 1 0;
  padding-left: 1rem;
}

.page-viewport {
  flex: 1 1 auto;
  height: 70vh;
  overflow: auto;
  border: 1px solid;
  position: relative;
}

.page-stage {
  position: relative;
  transform-origin: top left;
}

.page-image {
  display: block;
  width: 100%;
  height: 100%;
}

.overlay {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.region-box {
  fill: none;
  stroke: #000;
  stroke-width: 3;
  vector-effect: non-scaling-stroke;
}

.region-box.emphasized {
  stroke-width: 6;
  stroke-dasharray: 8 4;
}

.region-box-label {
  font-size: 16px;
  font-weight: bold;
  fill: #000;
  paint-order: stroke;
  stroke: #fff;
  stroke-width: 3px;
}

.splitter {
  flex: 0 0 auto;
  width: 0.75rem;
  cursor: col-resize;
  border: 1px solid;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  background: transparent;
}

.splitter #reset-panes {
  writing-mode: vertical-rl;
  font-size: 0.75rem;
  padding: 0.3rem 0.1rem;
  cursor: pointer;
}

.component {
  border: 1px solid;
  padding: 1rem;
}

.component-type {
  margin-top: 0;
}

.field-label {
  font-weight: bold;
}

.component-warning-status {
  font-weight: bold;
}

.region-controls {
  list-style: none;
  padding: 0;
}

.region-control {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: center;
  margin: 0.3rem 0;
  padding: 0.3rem;
  border: 1px solid;
}

.region-number {
  font-weight: bold;
}

.all-details {
  margin-top: 1rem;
}

.source-region {
  border: 1px solid;
  padding: 0.5rem;
  margin: 0.5rem 0;
}

.filter-empty {
  border: 1px solid;
  padding: 1rem;
  font-weight: bold;
}

@media (max-width: 60rem) {
  .panes {
    display: block;
  }
  .pane-page {
    flex: none;
    width: 100%;
  }
  .pane-details {
    padding-left: 0;
    padding-top: 1rem;
  }
  .splitter {
    display: none;
  }
  .page-viewport {
    height: 60vh;
  }
}

@media (prefers-reduced-motion: reduce) {
  * {
    animation-duration: 0.001ms !important;
    transition-duration: 0.001ms !important;
    scroll-behavior: auto !important;
  }
}
"""


_REVIEW_REPORT_JS = r'''"use strict";
(function () {
  var root = document.querySelector(".report-app");
  if (!root) return;
  var dataEl = document.getElementById("review-data");
  var data;
  try {
    data = JSON.parse(dataEl.textContent);
  } catch (e) {
    return;
  }
  var components = data.components || [];

  document.documentElement.classList.remove("no-js");
  root.hidden = false;

  var host = document.getElementById("component-host");
  var img = document.getElementById("page-image");
  var overlay = document.getElementById("overlay");
  var viewport = document.getElementById("page-viewport");
  var stage = document.getElementById("page-stage");
  var positionEl = document.getElementById("component-position");
  var live = document.getElementById("live-region");
  var toolbar = document.getElementById("component-toolbar");
  var warningsOnlyBtn = document.getElementById("warnings-only");
  var filterEmpty = document.getElementById("filter-empty");
  var allWarnings = document.getElementById("all-warnings");
  var splitter = document.getElementById("splitter");
  var resetBtn = document.getElementById("reset-panes");
  var panes = root.querySelector(".panes");
  var pageControls = root.querySelector(".page-controls");
  var SVGNS = "http://www.w3.org/2000/svg";

  var articles = {};
  components.forEach(function (c) {
    articles[c.id] = document.getElementById(c.id);
  });

  var state = {
    activeId: null,
    zoomMode: "fit",
    manualScale: 1,
    warningsOnly: false,
    detailsOpen: false,
    emphasized: null,
    zoomRegion: null
  };

  function componentById(id) {
    for (var i = 0; i < components.length; i++) {
      if (components[i].id === id) return components[i];
    }
    return null;
  }

  function traversal() {
    if (!state.warningsOnly) return components;
    return components.filter(function (c) {
      return c.hasWarning;
    });
  }

  // ---- overlay ----------------------------------------------------------
  function buildOverlay(component) {
    while (overlay.firstChild) overlay.removeChild(overlay.firstChild);
    var dim = data.pageDimensions[String(component.page)];
    if (!dim) return;
    overlay.setAttribute("viewBox", "0 0 " + dim.width + " " + dim.height);
    component.regions.forEach(function (rid, i) {
      var region = data.regions[rid];
      if (!region || !region.bbox || region.bbox.length < 4) return;
      var x0 = region.bbox[0], y0 = region.bbox[1], x1 = region.bbox[2], y1 = region.bbox[3];
      var rect = document.createElementNS(SVGNS, "rect");
      rect.setAttribute("x", x0);
      rect.setAttribute("y", y0);
      rect.setAttribute("width", Math.max(0, x1 - x0));
      rect.setAttribute("height", Math.max(0, y1 - y0));
      rect.setAttribute("class", "region-box");
      rect.setAttribute("data-region", rid);
      overlay.appendChild(rect);
      var label = document.createElementNS(SVGNS, "text");
      label.setAttribute("x", x0 + 4);
      label.setAttribute("y", y0 + 18);
      label.setAttribute("class", "region-box-label");
      label.textContent = String(i + 1) + " of " + component.regions.length;
      overlay.appendChild(label);
    });
    applyEmphasis();
  }

  function applyEmphasis() {
    var boxes = overlay.querySelectorAll(".region-box");
    for (var i = 0; i < boxes.length; i++) {
      if (state.emphasized && boxes[i].getAttribute("data-region") === state.emphasized) {
        boxes[i].classList.add("emphasized");
      } else {
        boxes[i].classList.remove("emphasized");
      }
    }
  }

  // ---- zoom -------------------------------------------------------------
  function viewportSize() {
    return { w: viewport.clientWidth, h: viewport.clientHeight };
  }

  function fitScale(dim) {
    var vs = viewportSize();
    if (!dim.width || !dim.height || !vs.w || !vs.h) return 1;
    return Math.min(vs.w / dim.width, vs.h / dim.height) || 1;
  }

  function unionBox(component) {
    var box = null;
    component.regions.forEach(function (rid) {
      var r = data.regions[rid];
      if (!r || !r.bbox || r.bbox.length < 4) return;
      if (!box) {
        box = { x0: r.bbox[0], y0: r.bbox[1], x1: r.bbox[2], y1: r.bbox[3] };
      } else {
        box.x0 = Math.min(box.x0, r.bbox[0]);
        box.y0 = Math.min(box.y0, r.bbox[1]);
        box.x1 = Math.max(box.x1, r.bbox[2]);
        box.y1 = Math.max(box.y1, r.bbox[3]);
      }
    });
    return box;
  }

  function activeZoomBox(component) {
    if (state.zoomRegion && component.regions.indexOf(state.zoomRegion) >= 0) {
      var r = data.regions[state.zoomRegion];
      if (r && r.bbox && r.bbox.length >= 4) {
        return { x0: r.bbox[0], y0: r.bbox[1], x1: r.bbox[2], y1: r.bbox[3] };
      }
    }
    return unionBox(component);
  }

  function componentScale(component, dim) {
    var box = activeZoomBox(component);
    if (!box) return fitScale(dim);
    var vs = viewportSize();
    var bw = Math.max(1, box.x1 - box.x0);
    var bh = Math.max(1, box.y1 - box.y0);
    var pad = 1.15;
    var s = Math.min(vs.w / (bw * pad), vs.h / (bh * pad));
    return Math.max(fitScale(dim), Math.min(s, 8));
  }

  function currentScale(component) {
    var dim = data.pageDimensions[String(component.page)];
    if (!dim) return 1;
    if (state.zoomMode === "fit") return fitScale(dim);
    if (state.zoomMode === "component") return componentScale(component, dim);
    return state.manualScale;
  }

  function applyZoom(component) {
    var dim = data.pageDimensions[String(component.page)];
    if (!dim) return;
    var scale = currentScale(component) || 1;
    stage.style.width = dim.width * scale + "px";
    stage.style.height = dim.height * scale + "px";
    if (state.zoomMode === "component") {
      var box = activeZoomBox(component);
      if (box) {
        var vs = viewportSize();
        var cx = ((box.x0 + box.x1) / 2) * scale;
        var cy = ((box.y0 + box.y1) / 2) * scale;
        viewport.scrollLeft = Math.max(0, cx - vs.w / 2);
        viewport.scrollTop = Math.max(0, cy - vs.h / 2);
      }
    }
  }

  function zoomBy(factor) {
    var c = componentById(state.activeId);
    if (!c) return;
    state.manualScale = Math.max(0.1, Math.min(8, currentScale(c) * factor));
    state.zoomMode = "manual";
    state.zoomRegion = null;
    applyZoom(c);
  }

  // ---- activation -------------------------------------------------------
  function updatePosition(component) {
    var list = traversal();
    var pos = list.indexOf(component) + 1;
    positionEl.textContent = "Component " + pos + " of " + list.length;
  }

  function announce(component) {
    var list = traversal();
    var pos = list.indexOf(component) + 1;
    live.textContent =
      "Component " + pos + " of " + list.length + ", " + component.typeLabel + ", page " + component.page;
  }

  function setActive(id, opts) {
    opts = opts || {};
    var component = componentById(id);
    if (!component) return;
    if (state.activeId && articles[state.activeId]) {
      articles[state.activeId].hidden = true;
    }
    state.activeId = id;
    var el = articles[id];
    var details = el.querySelector("details.all-details");
    if (details) details.open = state.detailsOpen;
    el.hidden = false;
    filterEmpty.hidden = true;

    img.setAttribute("src", "regions/page-" + component.page + ".png");
    img.setAttribute("alt", "Full Source PDF page " + component.page);

    if (!opts.keepZoomRegion) state.zoomRegion = null;
    state.emphasized = null;
    buildOverlay(component);
    applyZoom(component);
    updatePosition(component);
    announce(component);

    var hash = "#" + id;
    if (opts.push === false) {
      history.replaceState(null, "", hash);
    } else if (location.hash !== hash) {
      history.pushState(null, "", hash);
    }
  }

  function step(delta) {
    var list = traversal();
    if (!list.length) return;
    var current = componentById(state.activeId);
    var idx = list.indexOf(current);
    if (idx < 0) idx = 0;
    else idx = Math.min(list.length - 1, Math.max(0, idx + delta));
    setActive(list[idx].id);
  }

  function focusDetails() {
    var el = articles[state.activeId];
    if (el) el.focus();
  }

  function setWarningsOnly(on) {
    state.warningsOnly = on;
    warningsOnlyBtn.setAttribute("aria-pressed", String(on));
    var list = traversal();
    if (!list.length) {
      if (state.activeId && articles[state.activeId]) articles[state.activeId].hidden = true;
      filterEmpty.hidden = false;
      positionEl.textContent = "Component 0 of 0";
      live.textContent = "No Components match the warnings filter.";
      return;
    }
    var current = componentById(state.activeId);
    var target = current && list.indexOf(current) >= 0 ? current.id : list[0].id;
    setActive(target);
  }

  // ---- routing ----------------------------------------------------------
  function route(hash) {
    if (!hash) return false;
    var id = hash.replace(/^#/, "");
    if (articles[id]) {
      setActive(id, { push: false });
      return true;
    }
    var pageMatch = /^page-(\d+)$/.exec(id);
    if (pageMatch) {
      var p = parseInt(pageMatch[1], 10);
      var list = traversal().filter(function (c) { return c.page === p; });
      if (!list.length) list = components.filter(function (c) { return c.page === p; });
      if (list.length) {
        setActive(list[0].id, { push: false });
        return true;
      }
    }
    if (/^warning-/.test(id)) {
      if (allWarnings) allWarnings.open = true;
      var row = document.getElementById(id);
      if (row) {
        row.setAttribute("tabindex", "-1");
        row.focus();
      }
      return true;
    }
    return false;
  }

  // ---- events -----------------------------------------------------------
  toolbar.addEventListener("click", function (e) {
    var nav = e.target.closest("[data-nav]");
    if (nav) {
      step(nav.getAttribute("data-nav") === "next" ? 1 : -1);
      return;
    }
    if (e.target.closest('[data-action="focus-details"]')) {
      focusDetails();
      return;
    }
    if (e.target.closest("#warnings-only")) {
      setWarningsOnly(warningsOnlyBtn.getAttribute("aria-pressed") !== "true");
    }
  });

  toolbar.addEventListener("keydown", function (e) {
    if (e.key === "ArrowRight") {
      e.preventDefault();
      step(1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      step(-1);
    }
  });

  pageControls.addEventListener("click", function (e) {
    var b = e.target.closest("[data-zoom]");
    if (!b) return;
    var c = componentById(state.activeId);
    if (!c) return;
    var mode = b.getAttribute("data-zoom");
    if (mode === "fit") {
      state.zoomMode = "fit";
      state.zoomRegion = null;
      applyZoom(c);
    } else if (mode === "component") {
      state.zoomMode = "component";
      state.zoomRegion = null;
      applyZoom(c);
    } else if (mode === "in") {
      zoomBy(1.25);
    } else if (mode === "out") {
      zoomBy(0.8);
    }
  });

  host.addEventListener("click", function (e) {
    var emph = e.target.closest("[data-emphasize]");
    if (emph) {
      var rid = emph.getAttribute("data-emphasize");
      state.emphasized = state.emphasized === rid ? null : rid;
      applyEmphasis();
      return;
    }
    var zoom = e.target.closest("[data-zoom-region]");
    if (zoom) {
      var rid2 = zoom.getAttribute("data-zoom-region");
      state.emphasized = rid2;
      state.zoomRegion = rid2;
      state.zoomMode = "component";
      applyEmphasis();
      applyZoom(componentById(state.activeId));
    }
  });

  host.addEventListener(
    "toggle",
    function (e) {
      if (e.target.matches("details.all-details")) {
        state.detailsOpen = e.target.open;
      }
    },
    true
  );

  document.addEventListener("click", function (e) {
    var a = e.target.closest('a[href^="#"]');
    if (!a) return;
    var hash = a.getAttribute("href");
    if (route(hash)) {
      e.preventDefault();
      if (location.hash !== hash) history.pushState(null, "", hash);
    }
  });

  window.addEventListener("popstate", function () {
    route(location.hash);
  });

  window.addEventListener("resize", function () {
    if (state.activeId) applyZoom(componentById(state.activeId));
  });

  // ---- splitter ---------------------------------------------------------
  var paneWidth = 50;
  function setPaneWidth(pct) {
    paneWidth = Math.max(20, Math.min(80, pct));
    panes.style.setProperty("--left-pane", paneWidth + "%");
    splitter.setAttribute("aria-valuenow", String(Math.round(paneWidth)));
    if (state.activeId) applyZoom(componentById(state.activeId));
  }

  splitter.addEventListener("keydown", function (e) {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      setPaneWidth(paneWidth - 2);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      setPaneWidth(paneWidth + 2);
    } else if (e.key === "Home") {
      e.preventDefault();
      setPaneWidth(20);
    } else if (e.key === "End") {
      e.preventDefault();
      setPaneWidth(80);
    }
  });

  var dragging = false;
  splitter.addEventListener("pointerdown", function (e) {
    if (e.target.closest("#reset-panes")) return;
    dragging = true;
    try {
      splitter.setPointerCapture(e.pointerId);
    } catch (_) {}
    e.preventDefault();
  });
  splitter.addEventListener("pointermove", function (e) {
    if (!dragging) return;
    var rect = panes.getBoundingClientRect();
    if (rect.width) setPaneWidth(((e.clientX - rect.left) / rect.width) * 100);
  });
  function endDrag(e) {
    dragging = false;
    try {
      splitter.releasePointerCapture(e.pointerId);
    } catch (_) {}
  }
  splitter.addEventListener("pointerup", endDrag);
  splitter.addEventListener("pointercancel", endDrag);

  resetBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    setPaneWidth(50);
  });

  // ---- init -------------------------------------------------------------
  setPaneWidth(50);
  if (!components.length) {
    positionEl.textContent = "Component 0 of 0";
    filterEmpty.hidden = false;
    filterEmpty.textContent = "This Review Record has no Semantic Layer Components.";
    return;
  }
  var handled = location.hash && route(location.hash);
  if (!handled) setActive(components[0].id, { push: false });
})();
'''
