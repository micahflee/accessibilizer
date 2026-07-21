"""The human-editable Review Record, its accessible report, and finalize gating.

A Review Record is the durable, editable account of one page's reconstructed
Semantic Layer, the original recognition candidates, the Conversion Warnings, and
their resolution history. It is serialized as human-editable YAML, validated
against the canonical versioned JSON Schema in
``schemas/review-record-1.0.schema.json`` (mirrored by :func:`review_record_schema`),
and finalized only once every warning is resolved.

A warning is resolved exactly one of three ways — ``corrected``, ``accepted``, or
``not_applicable`` (which requires a reason) — and every resolution carries the
Reviewer's non-secret identifier. Superseded resolutions are moved into each
warning's history so later reviewers can see what changed.
"""

from __future__ import annotations

import copy
import html
from typing import Any, Sequence

import yaml
from jsonschema import Draft202012Validator


REVIEW_RECORD_SCHEMA_VERSION = "1.0"
REVIEW_REPORT_VERSION = "2.0"

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


def _semantic_layer_defs() -> dict[str, Any]:
    return {
        "heading": {
            "type": "object",
            "additionalProperties": False,
            "required": ["level", "text", "type"],
            "properties": {
                "level": {"const": 1},
                "text": {"type": "string", "minLength": 1},
                "type": {"const": "heading"},
            },
        },
        "paragraph": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "type"],
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "type": {"const": "paragraph"},
            },
        },
        "formula": {
            "type": "object",
            "additionalProperties": False,
            "required": ["normalized_math", "spoken_math_alternative", "type"],
            "properties": {
                "normalized_math": {"type": "string", "minLength": 1},
                "spoken_math_alternative": {"type": "string", "minLength": 1},
                "type": {"const": "formula"},
            },
        },
        "figure": {
            "type": "object",
            "additionalProperties": False,
            "required": ["complexity", "figure_alternative", "type"],
            "properties": {
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
        "table": {
            "type": "object",
            "additionalProperties": False,
            "required": ["rows", "type"],
            "properties": {
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


def _reconstruction_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "document_class",
            "page_prompt_version",
            "page_schema_version",
            "primary_language_is_english",
            "provider_endpoint",
            "provider_model",
            "reading_order",
            "reading_order_is_unambiguous",
            "region_prompt_version",
            "region_schema_version",
            "verified_regions",
        ],
        "properties": {
            "document_class": {"enum": ["stem_instructional", "other"]},
            "page_prompt_version": {"type": "string", "minLength": 1},
            "page_schema_version": {"type": "string", "minLength": 1},
            "primary_language_is_english": {"type": "boolean"},
            "provider_endpoint": {"type": "string", "minLength": 1},
            "provider_model": {"type": "string", "minLength": 1},
            "reading_order": {
                "type": "array",
                "items": {"enum": ["heading", "paragraph", "formula", "figure", "table"]},
            },
            "reading_order_is_unambiguous": {"type": "boolean"},
            "region_prompt_version": {"type": "string", "minLength": 1},
            "region_schema_version": {"type": "string", "minLength": 1},
            "verified_regions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["agrees_with_page", "id", "type"],
                    "properties": {
                        "agrees_with_page": {"type": "boolean"},
                        "id": {"type": "string", "pattern": "^page-[0-9]+-r[0-9]{4,}$"},
                        "type": {"enum": ["formula", "table", "figure"]},
                    },
                },
            },
        },
    }


def review_record_schema() -> dict[str, Any]:
    """Return the canonical Review Record JSON Schema as a dict.

    This is the runtime source of truth; ``schemas/review-record-1.0.schema.json``
    mirrors it, and a drift test keeps the two in sync.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://accessibilizer.org/schemas/review-record-1.0.schema.json",
        "title": "Accessibilizer Review Record 1.0",
        "description": (
            "The human-editable, durable account of one page's reconstructed Semantic "
            "Layer, the original recognition candidates, and the Conversion Warnings "
            "with their resolution history. A warning is resolved only as corrected, "
            "accepted, or not_applicable (with a reason), attributed to a Reviewer. "
            "Finalization is blocked while any warning is unresolved."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidates",
            "language",
            "page",
            "reconstruction",
            "schema_version",
            "semantic_layer",
            "source_sha256",
            "title",
            "warnings",
        ],
        "properties": {
            "schema_version": {"const": "1.0"},
            "page": {"type": "integer", "minimum": 1},
            "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "title": {"type": "string", "minLength": 1},
            "language": {"type": "string", "minLength": 1},
            "semantic_layer": {
                "type": "array",
                "prefixItems": [
                    {"$ref": "#/$defs/heading"},
                    {"$ref": "#/$defs/paragraph"},
                    {"$ref": "#/$defs/formula"},
                    {"$ref": "#/$defs/figure"},
                    {"$ref": "#/$defs/table"},
                ],
                "items": False,
            },
            "candidates": {"type": "array", "items": {"$ref": "#/$defs/candidate"}},
            "warnings": {"type": "array", "items": {"$ref": "#/$defs/warning"}},
            "reconstruction": {"$ref": "#/$defs/reconstruction"},
        },
        "$defs": {
            **_semantic_layer_defs(),
            "reconstruction": _reconstruction_schema(),
            "candidate": {
                "type": "object",
                "additionalProperties": False,
                "required": ["crop", "id", "text", "type"],
                "properties": {
                    "id": {"type": "string", "pattern": "^page-[0-9]+-r[0-9]{4,}$"},
                    "type": {"enum": list(CANDIDATE_TYPES)},
                    "text": {"type": ["string", "null"]},
                    "crop": {"type": "string", "minLength": 1},
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
                "required": ["code", "history", "id", "message", "region", "resolution"],
                "properties": {
                    "id": {"type": "string", "pattern": "^w[0-9]{4,}$"},
                    "code": {"type": "string", "minLength": 1},
                    "message": {"type": "string", "minLength": 1},
                    "region": {"type": ["string", "null"]},
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
    *, page_semantics: dict[str, Any], candidates: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Assemble a Review Record from the generation output and its candidates.

    The reconstructed Semantic Layer and the immutable reconstruction provenance
    come straight from ``page_semantics``. Every Conversion Warning gets a stable
    identifier, starts ``unresolved`` with an empty history, and the original
    recognition candidates are retained so each warning can be checked against the
    exact source region.
    """
    warnings: list[dict[str, Any]] = []
    for index, warning in enumerate(page_semantics.get("warnings", []), start=1):
        warnings.append(
            {
                "id": f"w{index:04d}",
                "code": warning["code"],
                "message": warning["message"],
                "region": warning.get("region"),
                "resolution": None,
                "history": [],
            }
        )
    return {
        "schema_version": REVIEW_RECORD_SCHEMA_VERSION,
        "page": page_semantics["page"],
        "source_sha256": page_semantics["source_sha256"],
        "title": page_semantics["title"],
        "language": page_semantics["language"],
        "semantic_layer": copy.deepcopy(page_semantics["semantic_layer"]),
        "candidates": [
            {
                "id": candidate["id"],
                "type": candidate["type"],
                "text": candidate.get("text"),
                "crop": candidate["crop"],
            }
            for candidate in candidates
        ],
        "warnings": warnings,
        "reconstruction": copy.deepcopy(page_semantics["reconstruction"]),
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


def _semantic_layer_items(record: dict[str, Any]) -> str:
    items: list[str] = []
    for node in record.get("semantic_layer", []):
        node_type = html.escape(str(node.get("type", "")).replace("_", " ").title())
        rows = "".join(
            f"<dt>{html.escape(str(key).replace('_', ' ').title())}</dt>"
            f"<dd>{html.escape(str(value))}</dd>"
            for key, value in node.items()
            if key != "type"
        )
        items.append(f"<li><h3>{node_type}</h3><dl>{rows}</dl></li>")
    return "".join(items)


def _warning_row(warning: dict[str, Any], crops: dict[str, dict[str, Any]]) -> str:
    identifier = html.escape(str(warning.get("id", "")))
    code = html.escape(str(warning.get("code", "")))
    message = html.escape(str(warning.get("message", "")))
    region = warning.get("region")
    if region and region in crops:
        crop = crops[region]
        source = html.escape(str(crop.get("crop", "")), quote=True)
        region_type = html.escape(str(crop.get("type", "region")))
        recognized = crop.get("text")
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
    elif region:
        region_cell = f"Region {html.escape(str(region))}"
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
    crops = {
        candidate["id"]: candidate
        for candidate in record.get("candidates", [])
        if isinstance(candidate, dict) and "id" in candidate
    }
    warnings = record.get("warnings", [])
    if warnings:
        unresolved = sum(1 for warning in warnings if warning.get("resolution") is None)
        rows = "".join(_warning_row(warning, crops) for warning in warnings)
        warnings_body = (
            f"<p>{len(warnings)} Conversion Warning(s); {unresolved} not yet resolved.</p>"
            "<table><caption>Conversion Warnings and their resolutions</caption>"
            "<thead><tr>"
            '<th scope="col">Warning</th><th scope="col">Concern</th>'
            '<th scope="col">Source region</th><th scope="col">Status</th>'
            '<th scope="col">Resolution</th>'
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        warnings_body = "<p>No Conversion Warnings remain.</p>"
    return f"""<!doctype html>
<html lang="{language}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Review Report</title></head>
<body><main>
<h1>{title} — Review Report</h1>
<section aria-labelledby="document-identity"><h2 id="document-identity">Document</h2>
<dl><dt>Page</dt><dd>{html.escape(str(record.get("page", "")))}</dd>
<dt>Language</dt><dd>{language}</dd>
<dt>Source SHA-256</dt>\
<dd><code>{html.escape(str(record.get("source_sha256", "")))}</code></dd></dl></section>
<section aria-labelledby="semantic-layer"><h2 id="semantic-layer">Semantic Layer</h2>
<ol>{_semantic_layer_items(record)}</ol></section>
<section aria-labelledby="conversion-warnings">\
<h2 id="conversion-warnings">Conversion Warnings</h2>
{warnings_body}</section>
</main></body></html>
"""
