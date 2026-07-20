from __future__ import annotations

import argparse
import ctypes
import html
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, TypedDict

from accessibilizer import __version__, recognition
from accessibilizer.checkpoint import (
    CheckpointStore,
    atomic_write_json,
    dependency_key,
    file_sha256,
)
from accessibilizer.process import run as _run, tool_version as _tool_version
from accessibilizer.provider import (
    RequestBudget,
    RequestCeilingExceeded,
    authorize_remote,
    check_capabilities,
    resolve_provider,
)
from accessibilizer.runtime import ConversionLimits, resolve_conversion_limits


REQUIRED_NODE_FIELDS = {
    "heading": {"level", "text"},
    "paragraph": {"text"},
    "formula": {"normalized_math", "spoken_math_alternative"},
    "figure": {"figure_alternative", "detailed_figure_description"},
}


class VisualReport(TypedDict):
    different_pixel_ratio: float
    output_height: int
    output_width: int
    source_height: int
    source_width: int
    tolerance: float


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider-base-url")
    parser.add_argument("--provider-model")
    parser.add_argument("--provider-api-key-env")
    parser.add_argument("--provider-data-location", choices=("local", "remote"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accessibilizer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    convert = subparsers.add_parser("convert")
    convert.add_argument("source", type=Path)
    convert.add_argument("--page", type=int, required=True)
    convert.add_argument("--semantic-input", type=Path, required=True)
    convert.add_argument("--bundle", type=Path, required=True)
    _add_provider_arguments(convert)
    convert.add_argument("--allow-remote", action="store_true")
    convert.add_argument("--max-requests", type=int)
    convert.add_argument("--provider-max-retries", type=int)
    convert.add_argument("--provider-retry-base-seconds", type=float)
    convert.add_argument("--provider-retry-max-seconds", type=float)
    convert.add_argument("--replace", action="store_true")
    convert.add_argument("--resume", action="store_true")
    convert.add_argument("--json", action="store_true")
    provider_key_env = subparsers.add_parser("provider-key-env", help=argparse.SUPPRESS)
    _add_provider_arguments(provider_key_env)
    return parser


def _load_semantics(path: Path) -> dict[str, Any]:
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != "1.0":
        raise ValueError("semantic input must use schema_version 1.0")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError("semantic input requires a non-empty title")
    if not isinstance(data.get("language"), str) or not data["language"].strip():
        raise ValueError("semantic input requires title and language")
    nodes = data.get("semantic_layer")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("semantic_layer must be a non-empty array")
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") not in REQUIRED_NODE_FIELDS:
            raise ValueError("semantic_layer contains an unsupported node")
        missing = REQUIRED_NODE_FIELDS[str(node["type"])] - node.keys()
        if missing:
            raise ValueError(f"{node['type']} is missing {', '.join(sorted(missing))}")
        for field in REQUIRED_NODE_FIELDS[str(node["type"])]:
            if field == "level":
                if node[field] != 1:
                    raise ValueError("representative heading must use level 1")
            elif not isinstance(node[field], str) or not node[field].strip():
                raise ValueError(f"{node['type']}.{field} must be a non-empty string")
    if [node["type"] for node in nodes] != ["heading", "paragraph", "formula", "figure"]:
        raise ValueError("semantic_layer is not in the required Logical Reading Order")
    warnings = data.get("warnings", [])
    if not isinstance(warnings, list):
        raise ValueError("warnings must be an array")
    for warning in warnings:
        if not isinstance(warning, dict) or warning.get("status") not in {
            "unresolved", "corrected", "accepted", "not_applicable",
        }:
            raise ValueError("each warning requires a valid status")
    return data


def _atomic_copy(source: Path, destination: Path, mode: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _workspace_for(bundle: Path) -> Path:
    return bundle.parent / f".{bundle.name}.in-progress"


def _prepare_workspace(bundle: Path, *, replace: bool, resume: bool) -> Path:
    workspace = _workspace_for(bundle)
    if bundle.exists() and not replace:
        raise FileExistsError(f"Conversion Bundle already exists: {bundle}")
    if workspace.exists():
        if resume:
            return workspace
        if not replace:
            raise FileExistsError(
                f"incomplete conversion exists: {workspace}; pass --resume to continue "
                "or --replace to start over"
            )
        shutil.rmtree(workspace)
    elif resume:
        raise FileNotFoundError(f"no incomplete conversion exists for: {bundle}")
    if bundle.exists():
        if not bundle.is_dir():
            raise FileExistsError(f"Conversion Bundle path is not a directory: {bundle}")
        shutil.copytree(
            bundle,
            workspace,
            copy_function=shutil.copy2,
            ignore=shutil.ignore_patterns("provenance.json", "request-usage.json"),
        )
        os.chmod(workspace, 0o700)
    else:
        workspace.mkdir(mode=0o700)
    return workspace


def _request_budget(
    workspace: Path, limits: ConversionLimits, *, estimated_requests: int
) -> RequestBudget:
    usage_path = workspace / "request-usage.json"
    actual_requests = 0
    reported_usage: dict[str, int] = {}
    if usage_path.is_file():
        try:
            stored: Any = json.loads(usage_path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise ValueError("request usage must be an object")
            actual_requests = int(stored.get("actual_requests", 0))
            raw_usage = stored.get("reported_token_usage", {})
            if isinstance(raw_usage, dict):
                reported_usage = {
                    str(name): value
                    for name, value in raw_usage.items()
                    if isinstance(value, int) and not isinstance(value, bool)
                }
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("incomplete conversion has invalid request usage") from error

    def persist(budget: RequestBudget) -> None:
        atomic_write_json(usage_path, budget.as_dict())

    budget = RequestBudget(
        estimated_requests=estimated_requests,
        ceiling=limits.max_requests,
        actual_requests=actual_requests,
        reported_token_usage=reported_usage,
        on_change=persist,
    )
    persist(budget)
    return budget


def _publish_bundle(staging: Path, bundle: Path, replace: bool) -> None:
    if not bundle.exists():
        os.replace(staging, bundle)
        return
    if not replace:
        raise FileExistsError(f"Conversion Bundle already exists: {bundle}")
    if not bundle.is_dir():
        raise FileExistsError(f"Conversion Bundle path is not a directory: {bundle}")

    rename_exchange = ctypes.CDLL(None, use_errno=True).renameat2
    rename_exchange.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    ]
    rename_exchange.restype = ctypes.c_int
    at_current_working_directory = -100
    exchange = 2
    result = rename_exchange(
        at_current_working_directory,
        os.fsencode(staging),
        at_current_working_directory,
        os.fsencode(bundle),
        exchange,
    )
    if result == 0:
        shutil.rmtree(staging, ignore_errors=True)
        return

    backup = Path(tempfile.mkdtemp(prefix=f".{bundle.name}.replaced.", dir=bundle.parent))
    backup.rmdir()
    os.replace(bundle, backup)
    try:
        os.replace(staging, bundle)
    except Exception:
        os.replace(backup, bundle)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _review_report(semantics: dict[str, Any]) -> str:
    items: list[str] = []
    for node in semantics["semantic_layer"]:
        node_type = html.escape(str(node["type"]).replace("_", " ").title())
        values = [
            f"<dt>{html.escape(str(key).replace('_', ' ').title())}</dt>"
            f"<dd>{html.escape(str(value))}</dd>"
            for key, value in node.items()
            if key != "type"
        ]
        items.append(f"<li><h3>{node_type}</h3><dl>{''.join(values)}</dl></li>")
    return """<!doctype html>
<html lang="{language}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title} — Review Report</title></head>
<body><main><h1>{title} — Review Report</h1>
<section aria-labelledby="semantic-layer"><h2 id="semantic-layer">Semantic Layer</h2>
<ol>{items}</ol></section></main></body></html>
""".format(
        language=html.escape(str(semantics["language"]), quote=True),
        title=html.escape(str(semantics["title"])),
        items="".join(items),
    )


def _read_ppm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    position = 0
    tokens: list[bytes] = []
    while len(tokens) < 4:
        while position < len(data) and data[position] in b" \t\r\n":
            position += 1
        if position < len(data) and data[position] == ord("#"):
            position = data.index(b"\n", position) + 1
            continue
        end = position
        while end < len(data) and data[end] not in b" \t\r\n":
            end += 1
        tokens.append(data[position:end])
        position = end
    if tokens[0] != b"P6" or tokens[3] != b"255":
        raise ValueError("pdftoppm returned an unsupported image")
    while position < len(data) and data[position] in b" \t\r\n":
        position += 1
    return int(tokens[1]), int(tokens[2]), data[position:]


def _visual_report(source: Path, output: Path, page: int, directory: Path) -> VisualReport:
    source_prefix = directory / "source-render"
    output_prefix = directory / "output-render"
    source_render = _run([
        "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-r", "144",
        str(source), str(source_prefix),
    ])
    output_render = _run([
        "pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-r", "144",
        str(output), str(output_prefix),
    ])
    if source_render.returncode or output_render.returncode:
        raise RuntimeError(source_render.stderr + output_render.stderr)
    source_width, source_height, source_pixels = _read_ppm(source_prefix.with_suffix(".ppm"))
    output_width, output_height, output_pixels = _read_ppm(output_prefix.with_suffix(".ppm"))
    if (source_width, source_height) != (output_width, output_height):
        different_ratio = 1.0
    else:
        pixels = source_width * source_height
        different = sum(
            source_pixels[offset : offset + 3] != output_pixels[offset : offset + 3]
            for offset in range(0, min(len(source_pixels), len(output_pixels)), 3)
        )
        different_ratio = different / pixels
    return {
        "different_pixel_ratio": different_ratio,
        "output_height": output_height,
        "output_width": output_width,
        "source_height": source_height,
        "source_width": source_width,
        "tolerance": 0.0001,
    }


def _internal_checks(
    semantics: dict[str, Any], extracted_semantics: dict[str, Any]
) -> dict[str, object]:
    expected = semantics["semantic_layer"]
    actual = extracted_semantics.get("semantic_layer")
    failures: list[str] = []
    if actual != expected:
        failures.append(
            "output PDF Semantic Layer does not match the required Logical Reading Order"
        )
    return {
        "checks": failures,
        "passed": not failures,
        "semantic_layer": actual,
        "source": "output.pdf structure tree",
    }


def _convert(args: argparse.Namespace) -> int:
    source: Path = args.source
    bundle: Path = args.bundle
    if args.page < 1:
        raise ValueError("--page must be positive")
    provider = resolve_provider(args)
    limits = resolve_conversion_limits(args)
    authorize_remote(provider, allow_remote=args.allow_remote)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    workspace = _prepare_workspace(
        bundle, replace=args.replace, resume=args.resume
    )
    checkpoints = CheckpointStore(workspace)

    capability_key = dependency_key({
        "base_url": provider.base_url,
        "capability_contract_version": "1.0",
        "capability_prompt_version": "1.0",
        "capability_schema_version": "1.0",
        "model": provider.model,
    })
    capability_reusable = checkpoints.is_reusable("provider-capability", capability_key)
    estimated_requests = 0 if capability_reusable else 1
    budget = _request_budget(
        workspace, limits, estimated_requests=estimated_requests
    )
    if not args.json:
        print(
            f"Estimated provider requests: {budget.estimated_requests} "
            f"(ceiling: {budget.ceiling})"
        )
    if not capability_reusable:
        if budget.actual_requests + 1 > budget.ceiling:
            raise RequestCeilingExceeded(
                f"conversion paused before exceeding request ceiling {budget.ceiling}; "
                "resume with a higher --max-requests value"
            )
        check_capabilities(
            provider,
            budget=budget,
            max_retries=limits.provider_max_retries,
            retry_base_seconds=limits.provider_retry_base_seconds,
            retry_max_seconds=limits.provider_retry_max_seconds,
        )
        checkpoints.complete("provider-capability", capability_key, [])

    semantics = _load_semantics(args.semantic_input)
    validation = workspace / "validation"
    validation.mkdir(mode=0o700, exist_ok=True)
    source_copy = workspace / "source.pdf"
    source_sha256 = file_sha256(source)
    source_key = dependency_key({
        "pdf_author_sha256": file_sha256(Path("/opt/accessibilizer/pdf-author.jar")),
        "preflight_contract_version": "1.0",
        "preflight_stage_version": "1.0",
        "source_sha256": source_sha256,
    })
    preflight_artifact = validation / "preflight.json"
    if not checkpoints.is_reusable("source", source_key):
        _atomic_copy(source, source_copy, stat.S_IRUSR)
        if file_sha256(source_copy) != source_sha256:
            raise RuntimeError("immutable Source PDF copy failed SHA-256 verification")
        preflight = _run([
            "java", "-jar", "/opt/accessibilizer/pdf-author.jar", "--preflight",
            str(source_copy),
        ])
        if preflight.returncode:
            raise RuntimeError(f"Source PDF preflight failed: {preflight.stderr.strip()}")
        try:
            preflight_result: dict[str, Any] = json.loads(preflight.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Source PDF preflight returned invalid JSON") from error
        unsupported_features = preflight_result.get("unsupported_features")
        if not isinstance(unsupported_features, list):
            raise RuntimeError("Source PDF preflight returned an invalid result")
        if unsupported_features:
            features = ", ".join(str(feature) for feature in unsupported_features)
            raise RuntimeError(f"Unsupported Source PDF: {features}")
        atomic_write_json(preflight_artifact, preflight_result)
        checkpoints.complete("source", source_key, [source_copy, preflight_artifact])

    regions = workspace / "regions"
    regions.mkdir(mode=0o700, exist_ok=True)
    crop_artifact = regions / f"page-{args.page}.png"
    region_key = dependency_key({
        "format": "png",
        "page": args.page,
        "renderer": "pdftoppm",
        "renderer_version": _tool_version(["pdftoppm", "-v"]),
        "rendering_dpi": 144,
        "source_sha256": source_sha256,
    })
    if not checkpoints.is_reusable(f"region-page-{args.page}", region_key):
        crop = _run([
            "pdftoppm", "-f", str(args.page), "-l", str(args.page), "-singlefile",
            "-r", "144", "-png", str(source_copy),
            str(regions / f"page-{args.page}"),
        ])
        if crop.returncode:
            raise RuntimeError(f"source-region crop failed: {crop.stderr}")
        checkpoints.complete(
            f"region-page-{args.page}", region_key, [crop_artifact]
        )

    recognition_directory = workspace / "recognition"
    recognition_directory.mkdir(mode=0o700, exist_ok=True)
    backend = recognition.select_backend(os.environ)
    pdftoppm_version = _tool_version(["pdftoppm", "-v"])
    pdftotext_version = _tool_version(["pdftotext", "-v"])
    recognition_key = dependency_key({
        "backend": backend.name,
        "backend_version": backend.version,
        "page": args.page,
        "pdf_text_extractor": "pdftotext",
        "pdf_text_extractor_version": pdftotext_version,
        "recognition_contract_version": recognition.RECOGNITION_CONTRACT_VERSION,
        "recognition_dpi": recognition.RECOGNITION_DPI,
        "renderer": "pdftoppm",
        "renderer_version": pdftoppm_version,
        "source_sha256": source_sha256,
        "weights_version": backend.weights_version,
    })
    recognition_stage = f"recognition-page-{args.page}"
    if not checkpoints.is_reusable(recognition_stage, recognition_key):
        recognized = recognition.recognize_page(
            source_pdf=source_copy,
            page=args.page,
            dpi=recognition.RECOGNITION_DPI,
            regions_dir=regions,
            recognition_dir=recognition_directory,
            backend=backend,
            source_sha256=source_sha256,
            renderer_version=pdftoppm_version,
            extractor_version=pdftotext_version,
        )
        checkpoints.complete(recognition_stage, recognition_key, recognized.artifacts)

    semantic_input_sha256 = file_sha256(args.semantic_input)
    page_key = dependency_key({
        "authoring_contract_version": "1.0",
        "page_stage_version": "1.0",
        "pdf_author_sha256": file_sha256(Path("/opt/accessibilizer/pdf-author.jar")),
        "page": args.page,
        "review_report_version": "1.0",
        "semantic_input_sha256": semantic_input_sha256,
        "source_sha256": source_sha256,
    })
    review_record = workspace / "review-record.json"
    review_report = workspace / "review-report.html"
    authoring_contract = workspace / "authoring.json"
    output = workspace / "output.pdf"
    page_stage = f"page-{args.page}"
    if not checkpoints.is_reusable(page_stage, page_key):
        atomic_write_json(review_record, semantics)
        review_report.write_text(_review_report(semantics), encoding="utf-8")
        contract = {
            "language": semantics["language"],
            "page": args.page,
            "schema_version": "1.0",
            "semantic_layer": semantics["semantic_layer"],
            "title": semantics["title"],
        }
        atomic_write_json(authoring_contract, contract)
        authored = _run([
            "java", "-jar", "/opt/accessibilizer/pdf-author.jar",
            str(authoring_contract), str(source_copy), str(output),
        ])
        if authored.returncode:
            raise RuntimeError(f"PDF authoring failed: {authored.stderr}")
        checkpoints.complete(
            page_stage,
            page_key,
            [review_record, review_report, authoring_contract, output],
        )

    validation_key = dependency_key({
        "internal_checks_version": "1.0",
        "page_stage": page_key,
        "region_stage": region_key,
        "verapdf_profile": "ua1",
        "verapdf_version": _tool_version(["verapdf", "--version"]),
        "visual_dpi": 144,
        "visual_tolerance": 0.0001,
    })
    internal_artifact = validation / "internal.json"
    visual_artifact = validation / "visual.json"
    verapdf_artifact = validation / "verapdf.xml"
    if not checkpoints.is_reusable("validation", validation_key):
        inspected = _run([
            "java", "-jar", "/opt/accessibilizer/pdf-author.jar", "--inspect", str(output),
        ])
        if inspected.returncode:
            raise RuntimeError(f"PDF semantic inspection failed: {inspected.stderr}")
        try:
            extracted_semantics: dict[str, Any] = json.loads(inspected.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("PDF semantic inspection returned invalid JSON") from error
        internal = _internal_checks(semantics, extracted_semantics)
        atomic_write_json(internal_artifact, internal)
        visual = _visual_report(source_copy, output, args.page, workspace)
        atomic_write_json(visual_artifact, visual)
        for render in workspace.glob("*-render.ppm"):
            render.unlink()

        verapdf = _run(["verapdf", "--format", "xml", "-f", "ua1", str(output)])
        verapdf_artifact.write_text(verapdf.stdout, encoding="utf-8")
        compliant = 'isCompliant="true"' in verapdf.stdout
        visual_failed = visual["different_pixel_ratio"] > visual["tolerance"]
        if not internal["passed"] or visual_failed or not compliant:
            raise RuntimeError("conversion failed its accessibility or visual-preservation gates")
        checkpoints.complete(
            "validation",
            validation_key,
            [internal_artifact, visual_artifact, verapdf_artifact],
        )

    atomic_write_json(workspace / "provenance.json", {
        "accessibilizer_version": __version__,
        "authoring_contract_version": "1.0",
        "recognition_backend": backend.name,
        "recognition_backend_version": backend.version,
        "recognition_contract_version": recognition.RECOGNITION_CONTRACT_VERSION,
        "recognition_dpi": recognition.RECOGNITION_DPI,
        "recognition_weights_version": backend.weights_version,
        "source_copy_verified": True,
        "source_sha256": source_sha256,
        "source_page": args.page,
        "provider_data_location": provider.data_location,
        "provider_endpoint": provider.base_url,
        "provider_model": provider.model,
        "provider_usage": budget.as_dict(),
    })
    _publish_bundle(workspace, bundle, args.replace)

    host_bundle = Path(os.environ.get("ACCESSIBILIZER_HOST_BUNDLE", str(bundle)))
    review_required = any(
        warning.get("status") == "unresolved" for warning in semantics.get("warnings", [])
    )
    result = {
        "bundle": str(host_bundle),
        "output": str(host_bundle / "output.pdf"),
        "status": "review_required" if review_required else "accessible",
    }
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif review_required:
        print(f"Review-Required PDF: {result['output']}")
    else:
        print(f"Accessible PDF: {result['output']}")
    return 2 if review_required else 0


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "convert":
            return _convert(args)
        if args.command == "provider-key-env":
            provider = resolve_provider(args)
            if provider.api_key_env is not None:
                print(provider.api_key_env)
            return 0
    except Exception as error:
        if args.json:
            print(json.dumps({"error": str(error), "status": "operational_failure"}, sort_keys=True))
        else:
            print(f"accessibilizer: {error}", file=sys.stderr)
        return 1
    return 1
