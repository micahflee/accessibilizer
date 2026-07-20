from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, TypedDict

from accessibilizer import __version__, page, recognition, review
from accessibilizer.checkpoint import (
    CheckpointStore,
    atomic_write_json,
    atomic_write_text,
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
from accessibilizer.runtime import (
    ConversionLimits,
    resolve_conversion_limits,
    resolve_reviewer,
)

PDF_AUTHOR_JAR = "/opt/accessibilizer/pdf-author.jar"


class VisualReport(TypedDict):
    different_pixel_ratio: float
    output_height: int
    output_width: int
    source_height: int
    source_width: int
    tolerance: float


CONVERT_EPILOG = """\
examples:
  accessibilizer convert source.pdf --page 1 \\
      --bundle output.accessibilizer \\
      --provider-base-url http://localhost:11434/v1 \\
      --provider-model exact-model-identifier \\
      --provider-data-location local --json

  accessibilizer convert source.pdf --page 1 \\
      --bundle output.accessibilizer \\
      --provider-base-url http://localhost:11434/v1 \\
      --provider-model exact-model-identifier \\
      --provider-data-location local --resume
"""


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider-base-url",
        help="base URL of the OpenAI-compatible vision provider (e.g. http://localhost:11434/v1)",
    )
    parser.add_argument(
        "--provider-model",
        help="exact model identifier to use with the provider (aliases like 'latest' are rejected)",
    )
    parser.add_argument(
        "--provider-api-key-env",
        help="name of the environment variable holding the provider API key",
    )
    parser.add_argument(
        "--provider-data-location",
        choices=("local", "remote"),
        help=(
            "whether the provider processes data locally or remotely; defaults to "
            "'local' for localhost providers and 'remote' otherwise"
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accessibilizer",
        description=(
            "Turn a visually readable Source PDF into a Conversion Bundle whose Visual "
            "Layer is preserved and whose Semantic Layer can be consumed by assistive "
            "technology."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, metavar="{convert,review,validate,finalize}"
    )
    convert = subparsers.add_parser(
        "convert",
        description=(
            "Convert a single page of a Source PDF into an accessible Conversion "
            "Bundle, gated on internal checks, visual comparison, and veraPDF's "
            "PDF/UA-1 profile."
        ),
        epilog=CONVERT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    convert.add_argument("source", type=Path, help="path to the Source PDF to convert")
    convert.add_argument(
        "--page",
        type=int,
        required=True,
        help=(
            "single, required page number (1-indexed) to convert; whole-document "
            "conversion is tracked by issue #1"
        ),
    )
    convert.add_argument(
        "--bundle",
        type=Path,
        required=True,
        help="path to the Conversion Bundle directory to create or update",
    )
    _add_provider_arguments(convert)
    convert.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "authorize sending data to a remote or uncertain provider without an "
            "interactive confirmation prompt"
        ),
    )
    convert.add_argument(
        "--max-requests",
        type=int,
        help="maximum provider requests to allow before pausing the conversion (default: 100)",
    )
    convert.add_argument(
        "--provider-max-retries",
        type=int,
        help="maximum retries for a failed provider request (default: 3)",
    )
    convert.add_argument(
        "--provider-retry-base-seconds",
        type=float,
        help="base delay in seconds before the first provider retry (default: 0.5)",
    )
    convert.add_argument(
        "--provider-retry-max-seconds",
        type=float,
        help="maximum delay in seconds between provider retries (default: 8.0)",
    )
    convert.add_argument(
        "--replace",
        action="store_true",
        help=(
            "replace an existing Conversion Bundle; Accessibilizer builds the "
            "replacement in a protected staging directory and leaves the existing "
            "bundle untouched if conversion fails"
        ),
    )
    convert.add_argument(
        "--resume",
        action="store_true",
        help=(
            "resume an interrupted or paused conversion, reusing stages whose "
            "dependency key and artifact hashes are still valid"
        ),
    )
    convert.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of human-readable text",
    )
    validate = subparsers.add_parser(
        "validate",
        description=(
            "Validate a Conversion Bundle's Review Record against the canonical "
            "schema and report whether it is ready to finalize. Read-only."
        ),
    )
    validate.add_argument(
        "--bundle", type=Path, required=True, help="path to the Conversion Bundle to check"
    )
    validate.add_argument(
        "--json", action="store_true", help="print machine-readable JSON instead of text"
    )

    review_parser = subparsers.add_parser(
        "review",
        description=(
            "Record the resolutions a Reviewer edited into the YAML Review Record: "
            "stamp them with the Reviewer identifier and timestamp, move superseded "
            "resolutions into history, and regenerate the accessible Review Report."
        ),
    )
    review_parser.add_argument(
        "--bundle", type=Path, required=True, help="path to the Conversion Bundle to review"
    )
    review_parser.add_argument(
        "--reviewer",
        help="non-secret Reviewer identifier attached to resolutions (overrides config)",
    )
    review_parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON instead of text"
    )

    finalize = subparsers.add_parser(
        "finalize",
        description=(
            "Rebuild the Accessible PDF from the corrected Review Record without any "
            "OCR or provider calls. Blocked while any Conversion Warning is unresolved; "
            "verifies the immutable Source PDF hash and preserves reviewer edits."
        ),
    )
    finalize.add_argument(
        "--bundle", type=Path, required=True, help="path to the Conversion Bundle to finalize"
    )
    finalize.add_argument(
        "--reviewer",
        help="non-secret Reviewer identifier attached to resolutions (overrides config)",
    )
    finalize.add_argument(
        "--json", action="store_true", help="print machine-readable JSON instead of text"
    )

    provider_key_env = subparsers.add_parser("provider-key-env", help=argparse.SUPPRESS)
    _add_provider_arguments(provider_key_env)
    # argparse.SUPPRESS on add_parser() hides the description but not the
    # subcommand's own list entry; drop its pseudo-action to hide it fully.
    subparsers._choices_actions[:] = [
        action for action in subparsers._choices_actions if action.dest != "provider-key-env"
    ]
    return parser


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


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _host_bundle(bundle: Path) -> Path:
    return Path(os.environ.get("ACCESSIBILIZER_HOST_BUNDLE", str(bundle)))


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


def _author_pdf(
    *,
    source_pdf: Path,
    output: Path,
    authoring_contract: Path,
    language: str,
    page_number: int,
    semantic_layer: list[dict[str, Any]],
    title: str,
) -> None:
    """Author output.pdf from a Semantic Layer through the Java authoring boundary."""
    atomic_write_json(authoring_contract, {
        "language": language,
        "page": page_number,
        "schema_version": "1.0",
        "semantic_layer": semantic_layer,
        "title": title,
    })
    authored = _run([
        "java", "-jar", PDF_AUTHOR_JAR,
        str(authoring_contract), str(source_pdf), str(output),
    ])
    if authored.returncode:
        raise RuntimeError(f"PDF authoring failed: {authored.stderr}")


def _run_output_gates(
    *,
    workspace: Path,
    source_pdf: Path,
    output: Path,
    page_number: int,
    semantics: dict[str, Any],
    internal_artifact: Path,
    visual_artifact: Path,
    verapdf_artifact: Path,
) -> None:
    """Run internal semantic checks, the visual comparison, and veraPDF PDF/UA-1.

    Writes each report as a bundle artifact and raises if any gate fails.
    """
    inspected = _run(["java", "-jar", PDF_AUTHOR_JAR, "--inspect", str(output)])
    if inspected.returncode:
        raise RuntimeError(f"PDF semantic inspection failed: {inspected.stderr}")
    try:
        extracted_semantics: dict[str, Any] = json.loads(inspected.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("PDF semantic inspection returned invalid JSON") from error
    internal = _internal_checks(semantics, extracted_semantics)
    atomic_write_json(internal_artifact, internal)
    visual = _visual_report(source_pdf, output, page_number, workspace)
    atomic_write_json(visual_artifact, visual)
    for render in workspace.glob("*-render.ppm"):
        render.unlink()
    verapdf = _run(["verapdf", "--format", "xml", "-f", "ua1", str(output)])
    atomic_write_text(verapdf_artifact, verapdf.stdout)
    compliant = 'isCompliant="true"' in verapdf.stdout
    visual_failed = visual["different_pixel_ratio"] > visual["tolerance"]
    if not internal["passed"] or visual_failed or not compliant:
        raise RuntimeError("conversion failed its accessibility or visual-preservation gates")


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
    capability_estimate = 0 if capability_reusable else 1
    budget = _request_budget(
        workspace, limits, estimated_requests=capability_estimate
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

    validation = workspace / "validation"
    validation.mkdir(mode=0o700, exist_ok=True)
    source_copy = workspace / "source.pdf"
    source_sha256 = file_sha256(source)
    source_key = dependency_key({
        "pdf_author_sha256": file_sha256(Path(PDF_AUTHOR_JAR)),
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
            "java", "-jar", PDF_AUTHOR_JAR, "--preflight",
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

    recognition_document: dict[str, Any] = json.loads(
        (recognition_directory / f"page-{args.page}.json").read_text(encoding="utf-8")
    )
    candidates = recognition_document["candidates"]
    pdf_words = recognition_document["pdf_text_evidence"]["words"]

    page_semantics_path = workspace / "page-semantics.json"
    page_semantics_key = dependency_key({
        "base_url": provider.base_url,
        "model": provider.model,
        "page": args.page,
        "page_prompt_version": page.PAGE_PROMPT_VERSION,
        "page_schema_version": page.PAGE_SCHEMA_VERSION,
        "page_semantics_contract_version": page.PAGE_SEMANTICS_CONTRACT_VERSION,
        "recognition_stage": recognition_key,
        "region_prompt_version": page.REGION_PROMPT_VERSION,
        "region_schema_version": page.REGION_SCHEMA_VERSION,
        "source_sha256": source_sha256,
    })
    page_semantics_stage = f"page-semantics-page-{args.page}"
    page_semantics_reusable = checkpoints.is_reusable(page_semantics_stage, page_semantics_key)
    page_requests = 0 if page_semantics_reusable else page.expected_request_count(candidates)
    budget.update_estimate(capability_estimate + page_requests)
    if not args.json:
        print(
            f"Estimated provider requests: {budget.estimated_requests} "
            f"(ceiling: {budget.ceiling})"
        )
    if not page_semantics_reusable:
        if budget.actual_requests + page_requests > budget.ceiling:
            raise RequestCeilingExceeded(
                f"conversion paused before exceeding request ceiling {budget.ceiling}; "
                "resume with a higher --max-requests value"
            )
        semantics_document = page.reconstruct_page(
            provider,
            page=args.page,
            source_sha256=source_sha256,
            page_image=crop_artifact,
            regions_dir=regions,
            candidates=candidates,
            pdf_words=pdf_words,
            budget=budget,
            max_retries=limits.provider_max_retries,
            retry_base_seconds=limits.provider_retry_base_seconds,
            retry_max_seconds=limits.provider_retry_max_seconds,
        )
        atomic_write_json(page_semantics_path, semantics_document)
        checkpoints.complete(page_semantics_stage, page_semantics_key, [page_semantics_path])

    semantics: dict[str, Any] = json.loads(
        page_semantics_path.read_text(encoding="utf-8")
    )
    page_key = dependency_key({
        "authoring_contract_version": "1.0",
        "page_semantics_stage": page_semantics_key,
        "page_stage_version": "3.0",
        "pdf_author_sha256": file_sha256(Path(PDF_AUTHOR_JAR)),
        "page": args.page,
        "review_record_schema_version": review.REVIEW_RECORD_SCHEMA_VERSION,
        "review_report_version": review.REVIEW_REPORT_VERSION,
        "source_sha256": source_sha256,
    })
    review_record = workspace / "review-record.yaml"
    review_baseline = workspace / "review-baseline.json"
    review_report = workspace / "review-report.html"
    authoring_contract = workspace / "authoring.json"
    output = workspace / "output.pdf"
    page_stage = f"page-{args.page}"
    if not checkpoints.is_reusable(page_stage, page_key):
        record = review.build_review_record(page_semantics=semantics, candidates=candidates)
        review.validate_review_record(record)
        atomic_write_text(review_record, review.dump_yaml(record))
        atomic_write_json(review_baseline, record)
        atomic_write_text(review_report, review.render_review_report(record))
        _author_pdf(
            source_pdf=source_copy,
            output=output,
            authoring_contract=authoring_contract,
            language=semantics["language"],
            page_number=args.page,
            semantic_layer=semantics["semantic_layer"],
            title=semantics["title"],
        )
        checkpoints.complete(
            page_stage,
            page_key,
            [review_record, review_baseline, review_report, authoring_contract, output],
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
        _run_output_gates(
            workspace=workspace,
            source_pdf=source_copy,
            output=output,
            page_number=args.page,
            semantics=semantics,
            internal_artifact=internal_artifact,
            visual_artifact=visual_artifact,
            verapdf_artifact=verapdf_artifact,
        )
        checkpoints.complete(
            "validation",
            validation_key,
            [internal_artifact, visual_artifact, verapdf_artifact],
        )

    atomic_write_json(workspace / "provenance.json", {
        "accessibilizer_version": __version__,
        "authoring_contract_version": "1.0",
        "page_prompt_version": page.PAGE_PROMPT_VERSION,
        "page_schema_version": page.PAGE_SCHEMA_VERSION,
        "page_semantics_contract_version": page.PAGE_SEMANTICS_CONTRACT_VERSION,
        "region_prompt_version": page.REGION_PROMPT_VERSION,
        "region_schema_version": page.REGION_SCHEMA_VERSION,
        "review_record_schema_version": review.REVIEW_RECORD_SCHEMA_VERSION,
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

    host_bundle = _host_bundle(bundle)
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


def _load_bundle_record(bundle: Path) -> tuple[dict[str, Any], Path]:
    if not bundle.is_dir():
        raise RuntimeError(f"no Conversion Bundle found: {bundle}")
    record_path = bundle / "review-record.yaml"
    if not record_path.is_file():
        raise RuntimeError(f"no Review Record found in bundle: {bundle}")
    record = review.load_yaml(record_path.read_text(encoding="utf-8"))
    return record, record_path


def _load_baseline(bundle: Path) -> dict[str, Any] | None:
    baseline_path = bundle / "review-baseline.json"
    if not baseline_path.is_file():
        return None
    loaded: Any = json.loads(baseline_path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _emit(args: argparse.Namespace, result: dict[str, Any], human_line: str) -> None:
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(human_line)


def _validate(args: argparse.Namespace) -> int:
    record, _ = _load_bundle_record(args.bundle)
    review.validate_review_record(record)
    unresolved = review.unresolved_warnings(record)
    finalizable = not unresolved
    result = {
        "bundle": str(_host_bundle(args.bundle)),
        "finalizable": finalizable,
        "status": "finalizable" if finalizable else "review_required",
        "unresolved_warnings": len(unresolved),
        "warnings": len(record.get("warnings", [])),
    }
    _emit(
        args,
        result,
        f"Review Record is valid and ready to finalize: {result['bundle']}"
        if finalizable
        else f"Review Record is valid; {len(unresolved)} warning(s) still need resolution.",
    )
    return 0 if finalizable else 2


def _persist_review(bundle: Path, record: dict[str, Any]) -> None:
    atomic_write_text(bundle / "review-record.yaml", review.dump_yaml(record))
    atomic_write_json(bundle / "review-baseline.json", record)
    atomic_write_text(bundle / "review-report.html", review.render_review_report(record))


def _review(args: argparse.Namespace) -> int:
    reviewer = resolve_reviewer(args)
    record, _ = _load_bundle_record(args.bundle)
    committed = review.commit_resolutions(
        record, baseline=_load_baseline(args.bundle), reviewer=reviewer, now=_iso_now()
    )
    review.validate_review_record(committed)
    _persist_review(args.bundle, committed)
    unresolved = review.unresolved_warnings(committed)
    result = {
        "bundle": str(_host_bundle(args.bundle)),
        "report": str(_host_bundle(args.bundle) / "review-report.html"),
        "status": "finalizable" if not unresolved else "review_required",
        "unresolved_warnings": len(unresolved),
    }
    _emit(
        args,
        result,
        f"Review Record updated; ready to finalize: {result['report']}"
        if not unresolved
        else f"Review Record updated; {len(unresolved)} warning(s) still need resolution.",
    )
    return 0 if not unresolved else 2


def _rebuild_and_validate(workspace: Path, record: dict[str, Any]) -> None:
    """Author and validate the PDF from a resolved record; no OCR or provider calls."""
    source_copy = workspace / "source.pdf"
    output = workspace / "output.pdf"
    validation = workspace / "validation"
    validation.mkdir(mode=0o700, exist_ok=True)
    _author_pdf(
        source_pdf=source_copy,
        output=output,
        authoring_contract=workspace / "authoring.json",
        language=record["language"],
        page_number=record["page"],
        semantic_layer=record["semantic_layer"],
        title=record["title"],
    )
    _run_output_gates(
        workspace=workspace,
        source_pdf=source_copy,
        output=output,
        page_number=record["page"],
        semantics=record,
        internal_artifact=validation / "internal.json",
        visual_artifact=validation / "visual.json",
        verapdf_artifact=validation / "verapdf.xml",
    )


def _finalize_provenance(
    bundle: Path, record: dict[str, Any], reviewer: str | None, *, finalized: bool
) -> dict[str, Any]:
    provenance: dict[str, Any] = {}
    original = bundle / "provenance.json"
    if original.is_file():
        loaded: Any = json.loads(original.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            provenance = loaded
    resolutions: list[dict[str, Any]] = []
    for warning in record.get("warnings", []):
        resolution = warning.get("resolution") or {}
        resolutions.append({
            "reviewer": resolution.get("reviewer"),
            "status": resolution.get("status"),
            "warning": warning.get("id"),
        })
    provenance.update({
        "accessibilizer_version": __version__,
        "finalized": finalized,
        "review_record_schema_version": record["schema_version"],
        "reviewer": reviewer,
        "resolutions": resolutions,
    })
    return provenance


def _finalize(args: argparse.Namespace) -> int:
    bundle: Path = args.bundle
    reviewer = resolve_reviewer(args)
    # Fail fast before touching the bundle: load, stamp, validate, verify source.
    record, _ = _load_bundle_record(bundle)
    committed = review.commit_resolutions(
        record, baseline=_load_baseline(bundle), reviewer=reviewer, now=_iso_now()
    )
    review.validate_review_record(committed)
    source_copy = bundle / "source.pdf"
    if not source_copy.is_file():
        raise RuntimeError("bundle is missing its immutable Source PDF copy")
    if file_sha256(source_copy) != committed["source_sha256"]:
        raise RuntimeError(
            "the Source PDF copy does not match the Review Record; refusing to finalize"
        )

    host_bundle = _host_bundle(bundle)
    unresolved = review.unresolved_warnings(committed)
    if unresolved:
        # Unresolved warnings block finalization: refuse without touching the
        # bundle. The Reviewer resolves them and records them with `review`.
        result = {
            "bundle": str(host_bundle),
            "output": str(host_bundle / "output.pdf"),
            "status": "review_required",
            "unresolved_warnings": len(unresolved),
        }
        _emit(
            args,
            result,
            f"Finalization blocked; {len(unresolved)} warning(s) still need resolution.",
        )
        return 2

    workspace = _prepare_workspace(bundle, replace=True, resume=False)
    try:
        # _prepare_workspace omits request-usage.json; finalize makes no provider
        # calls, so carry the existing usage record forward unchanged.
        usage = bundle / "request-usage.json"
        if usage.is_file():
            _atomic_copy(usage, workspace / "request-usage.json")
        atomic_write_text(workspace / "review-record.yaml", review.dump_yaml(committed))
        atomic_write_json(workspace / "review-baseline.json", committed)
        atomic_write_text(
            workspace / "review-report.html", review.render_review_report(committed)
        )
        _rebuild_and_validate(workspace, committed)
        atomic_write_json(
            workspace / "provenance.json",
            _finalize_provenance(bundle, committed, reviewer, finalized=True),
        )
        _publish_bundle(workspace, bundle, True)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise

    result = {
        "bundle": str(host_bundle),
        "output": str(host_bundle / "output.pdf"),
        "status": "accessible",
        "unresolved_warnings": 0,
    }
    _emit(args, result, f"Accessible PDF: {result['output']}")
    return 0


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "convert":
            return _convert(args)
        if args.command == "validate":
            return _validate(args)
        if args.command == "review":
            return _review(args)
        if args.command == "finalize":
            return _finalize(args)
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
