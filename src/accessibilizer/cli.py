from __future__ import annotations

import argparse
import ctypes
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, TypedDict

from accessibilizer.provider import authorize_remote, check_capabilities, resolve_provider


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
    convert.add_argument("--replace", action="store_true")
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


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


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
    if bundle.exists() and not args.replace:
        raise FileExistsError(f"Conversion Bundle already exists: {bundle}")
    provider = resolve_provider(args)
    authorize_remote(provider, allow_remote=args.allow_remote)
    check_capabilities(provider)
    semantics = _load_semantics(args.semantic_input)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{bundle.name}.", dir=bundle.parent))
    os.chmod(staging, 0o700)
    try:
        validation = staging / "validation"
        validation.mkdir(mode=0o700)
        source_copy = staging / "source.pdf"
        source_sha256 = _sha256(source)
        shutil.copyfile(source, source_copy)
        if _sha256(source_copy) != source_sha256:
            raise RuntimeError("immutable Source PDF copy failed SHA-256 verification")
        os.chmod(source_copy, stat.S_IRUSR)
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
        _write_json(staging / "review-record.json", semantics)
        (staging / "review-report.html").write_text(
            _review_report(semantics), encoding="utf-8"
        )
        regions = staging / "regions"
        regions.mkdir(mode=0o700)
        crop = _run([
            "pdftoppm", "-f", str(args.page), "-l", str(args.page), "-singlefile",
            "-r", "144", "-png", str(source_copy), str(regions / "page-1"),
        ])
        if crop.returncode:
            raise RuntimeError(f"source-region crop failed: {crop.stderr}")
        contract = {
            "language": semantics["language"],
            "page": args.page,
            "schema_version": "1.0",
            "semantic_layer": semantics["semantic_layer"],
            "title": semantics["title"],
        }
        _write_json(staging / "authoring.json", contract)
        output = staging / "output.pdf"
        authored = _run([
            "java", "-jar", "/opt/accessibilizer/pdf-author.jar",
            str(staging / "authoring.json"), str(source_copy), str(output),
        ])
        if authored.returncode:
            raise RuntimeError(f"PDF authoring failed: {authored.stderr}")

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
        _write_json(validation / "internal.json", internal)
        visual = _visual_report(source_copy, output, args.page, staging)
        _write_json(validation / "visual.json", visual)
        for render in staging.glob("*-render.ppm"):
            render.unlink()

        verapdf = _run(["verapdf", "--format", "xml", "-f", "ua1", str(output)])
        (validation / "verapdf.xml").write_text(verapdf.stdout, encoding="utf-8")
        compliant = 'isCompliant="true"' in verapdf.stdout
        visual_failed = visual["different_pixel_ratio"] > visual["tolerance"]
        if not internal["passed"] or visual_failed or not compliant:
            raise RuntimeError("conversion failed its accessibility or visual-preservation gates")
        _write_json(staging / "provenance.json", {
            "accessibilizer_version": "0.1.0",
            "authoring_contract_version": "1.0",
            "source_copy_verified": True,
            "source_sha256": source_sha256,
            "source_page": args.page,
            "provider_data_location": provider.data_location,
            "provider_endpoint": provider.base_url,
            "provider_model": provider.model,
        })
        _publish_bundle(staging, bundle, args.replace)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

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
