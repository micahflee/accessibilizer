from __future__ import annotations

import argparse
import copy
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import time
from types import FrameType
from typing import Any, TypedDict

from accessibilizer import __version__, page, recognition, review
from accessibilizer.checkpoint import (
    CheckpointStore,
    atomic_write_json,
    atomic_write_text,
    dependency_key,
    file_sha256,
)
from accessibilizer.events import (
    CONVERSION_EVENTS_FILENAME,
    STATE_COMPLETED,
    STATE_STARTED,
    VALIDATION_STAGES,
    ConversionInterrupted,
    ProgressReporter,
)
from accessibilizer.process import run as _run, tool_version as _tool_version
from accessibilizer.provider import (
    ProviderConfig,
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


class PageVisualReport(TypedDict):
    source_page: int
    output_page: int
    different_pixel_ratio: float
    output_height: int
    output_width: int
    source_height: int
    source_width: int


class VisualReport(TypedDict):
    pages: list[PageVisualReport]
    max_different_pixel_ratio: float
    tolerance: float
    passed: bool


VISUAL_TOLERANCE = 0.0001
SOURCE_EVIDENCE_CONTRACT_VERSION = "1.0"


def _whole_page_region_id(page: int) -> str:
    """Return the deterministic whole-page Source Region identity."""
    return f"page-{page}-r0000"


CONVERT_EPILOG = """\
examples:
  accessibilizer convert source.pdf \\
      --bundle output.accessibilizer \\
      --provider-base-url http://localhost:11434/v1 \\
      --provider-model exact-model-identifier \\
      --provider-data-location local --json

  accessibilizer convert source.pdf --page 1-3 \\
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
        dest="command", required=True, metavar="{convert,report,review,validate,finalize}"
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
        help=(
            "optional subset of pages to convert (1-indexed); accepts a single page "
            "(3), a range (1-11), or a comma list (1,3,5). Defaults to the whole "
            "document."
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
    report = subparsers.add_parser(
        "report", description="Regenerate the read-only, page-oriented Review Report offline."
    )
    report.add_argument("--bundle", type=Path, help="existing Conversion Bundle")
    report.add_argument("--source", type=Path, help="Source PDF for standalone report generation")
    report.add_argument("--record", type=Path, help="Review Record 3.0 YAML for standalone generation")
    report.add_argument("--output", type=Path, help="standalone report directory")
    report.add_argument("--replace", action="store_true", help="replace an existing standalone report")
    report.add_argument("--json", action="store_true", help="print machine-readable JSON")
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
    convert.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "emit finer-grained technical progress to stderr (backend and model "
            "versions, checkpoint identifiers, provider retry detail, and timings); "
            "progress goes only to stderr, never to the stdout result"
        ),
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


def _exchange_directories(staging: Path, published: Path) -> bool:
    """Atomically exchange two directories when the host exposes that operation."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except (AttributeError, OSError):
        return False
    exchange = 2
    implementations = (
        ("renameat2", -100),  # Linux AT_FDCWD
        ("renameatx_np", -2),  # macOS AT_FDCWD
    )
    for function_name, at_current_working_directory in implementations:
        try:
            rename_exchange = getattr(libc, function_name)
        except AttributeError:
            continue
        rename_exchange.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
        ]
        rename_exchange.restype = ctypes.c_int
        if rename_exchange(
            at_current_working_directory,
            os.fsencode(staging),
            at_current_working_directory,
            os.fsencode(published),
            exchange,
        ) == 0:
            return True
    return False


def _publish_protected_directory(staging: Path, published: Path, replace: bool) -> None:
    transaction = published.parent / f".{published.name}.replacement"
    marker = transaction / "accessibilizer-transaction"
    previous = transaction / "previous"
    marker_text = "accessibilizer protected directory replacement 1\n"
    if transaction.exists():
        expected_entries = {"accessibilizer-transaction", "previous"}
        if (
            not transaction.is_dir()
            or not marker.is_file()
            or marker.read_text(encoding="utf-8") != marker_text
            or not {entry.name for entry in transaction.iterdir()} <= expected_entries
            or (previous.exists() and not previous.is_dir())
        ):
            raise RuntimeError(f"ambiguous protected-publication state: {transaction}")
        # Recover a fallback publication interrupted between its two renames. If
        # the destination exists, publication completed and only cleanup remains.
        if published.exists():
            shutil.rmtree(transaction)
        elif previous.is_dir():
            os.replace(previous, published)
            shutil.rmtree(transaction)
        else:
            raise RuntimeError(f"incomplete protected-publication state: {transaction}")
    if not published.exists():
        os.replace(staging, published)
        return
    if not replace:
        raise FileExistsError(f"published directory already exists: {published}")
    if not published.is_dir():
        raise FileExistsError(f"published path is not a directory: {published}")

    if _exchange_directories(staging, published):
        shutil.rmtree(staging, ignore_errors=True)
        return

    transaction.mkdir(mode=0o700)
    atomic_write_text(marker, marker_text)
    os.replace(published, previous)
    try:
        os.replace(staging, published)
    except BaseException:
        os.replace(previous, published)
        shutil.rmtree(transaction)
        raise
    shutil.rmtree(transaction)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _host_bundle(bundle: Path) -> Path:
    return Path(os.environ.get("ACCESSIBILIZER_HOST_BUNDLE", str(bundle)))


def _host_source(source: Path) -> Path:
    """The Source PDF path as the user typed it, for a copy-pasteable resume command.

    The conversion runs inside the container against ``/work/source.pdf``; the
    launcher exports the original host path so the printed resume command names
    the file the user actually passed.
    """
    return Path(os.environ.get("ACCESSIBILIZER_HOST_SOURCE", str(source)))


def _resume_command(args: argparse.Namespace) -> str:
    """Build the exact command that resumes this interrupted conversion."""
    parts = [
        "accessibilizer",
        "convert",
        str(_host_source(args.source)),
        "--bundle",
        str(_host_bundle(args.bundle)),
    ]
    if args.page:
        parts += ["--page", args.page]
    provider_flags = (
        ("--provider-base-url", args.provider_base_url),
        ("--provider-model", args.provider_model),
        ("--provider-api-key-env", args.provider_api_key_env),
        ("--provider-data-location", args.provider_data_location),
    )
    for flag, value in provider_flags:
        if value:
            parts += [flag, str(value)]
    if args.allow_remote:
        parts.append("--allow-remote")
    if args.max_requests is not None:
        parts += ["--max-requests", str(args.max_requests)]
    if args.json:
        parts.append("--json")
    if getattr(args, "verbose", False):
        parts.append("--verbose")
    parts.append("--resume")
    return " ".join(shlex.quote(part) for part in parts)


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    raise ConversionInterrupted()


def _pdf_page_count(pdf: Path) -> int:
    info = _run(["pdfinfo", str(pdf)])
    if info.returncode:
        raise RuntimeError(f"could not read the Source PDF page count: {info.stderr.strip()}")
    for line in info.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1])
    raise RuntimeError("could not read the Source PDF page count")


def _displayed_page_dimensions(pdf: Path, page_number: int) -> tuple[float, float]:
    """Read the cropped, rotated displayed-page size in canonical PDF points."""
    info = _run(
        ["pdfinfo", "-f", str(page_number), "-l", str(page_number), "-box", str(pdf)]
    )
    if info.returncode:
        raise RuntimeError(
            f"could not read Source PDF page {page_number} dimensions: "
            f"{info.stderr.strip()}"
        )
    for line in info.stdout.splitlines():
        fields = line.split()
        if (
            len(fields) == 7
            and fields[0] == "Page"
            and fields[1] == str(page_number)
            and fields[2] == "size:"
            and fields[4] == "x"
            and fields[6] == "pts"
        ):
            return float(fields[3]), float(fields[5])
    raise RuntimeError(f"could not read Source PDF page {page_number} dimensions")


def _parse_page_selection(spec: str | None, total: int) -> list[int]:
    """Resolve a --page subset spec against a document's page count.

    Accepts a single page (``3``), a range (``1-11``), or a comma list
    (``1,3,5``). Defaults to the whole document. Every selected page must be
    within the document.
    """
    if spec is None:
        return list(range(1, total + 1))
    pages: set[int] = set()
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        try:
            if "-" in part:
                low_text, high_text = part.split("-", 1)
                low, high = int(low_text), int(high_text)
                if low > high:
                    raise ValueError
                pages.update(range(low, high + 1))
            else:
                pages.add(int(part))
        except ValueError:
            raise ValueError(f"invalid --page selection: {spec}") from None
    selected = sorted(pages)
    if not selected:
        raise ValueError(f"invalid --page selection: {spec}")
    for page in selected:
        if page < 1 or page > total:
            raise ValueError(
                f"page {page} is outside the document (1-{total})"
            )
    return selected


def _authoring_contract(record: dict[str, Any]) -> dict[str, Any]:
    """Group a flat whole-document Review Record into the Java authoring contract.

    The Review Record's Semantic Layer is one flat list whose nodes each carry a
    ``page``; the authoring boundary wants the nodes grouped per source page, in
    document Logical Reading Order, without the ``page`` tag on each node.
    """
    grouped: dict[int, list[dict[str, Any]]] = {page: [] for page in record["pages"]}
    for node in record["semantic_layer"]:
        page = node["page"]
        grouped.setdefault(page, []).append(
            {
                key: value
                for key, value in node.items()
                if key not in {"id", "page", "source_regions"}
            }
        )
    return {
        "schema_version": "2.0",
        "title": record["title"],
        "language": record["language"],
        "pages": [
            {"source_page": page, "semantic_layer": grouped[page]}
            for page in record["pages"]
        ],
    }


def _review_page_document(
    page_document: dict[str, Any],
    recognition_document: dict[str, Any],
    page_render: Path,
    page_dimensions: tuple[float, float],
) -> dict[str, Any]:
    """Project deterministic recognition evidence into Review Record 3.0 inputs.

    This adapter assigns durable Review Record identities after reconstruction while
    preserving the model's selection from the deterministic Source Region set.
    """
    document = copy.deepcopy(page_document)
    page_number = document["page"]
    dpi = recognition_document["rendering"]["dpi"]
    width_pixels, height_pixels = recognition.png_size(page_render)
    width_points, height_points = page_dimensions
    document["page_dimensions"] = {
        "width_points": width_points,
        "height_points": height_points,
    }

    raw_candidates = recognition_document["candidates"]
    source_regions: list[dict[str, Any]] = [{
        "id": _whole_page_region_id(page_number), "page": page_number,
        "bbox_points": [0.0, 0.0, width_points, height_points],
    }]
    candidates: list[dict[str, Any]] = []
    for index, candidate in enumerate(raw_candidates, start=1):
        region_id = candidate["id"]
        bbox_pixels = recognition.clamp_bbox_to_page(
            candidate["bbox_pixels"], (width_pixels, height_pixels)
        )
        point_x0, point_y0, point_x1, point_y1 = recognition.pixels_to_points(
            bbox_pixels, dpi
        )
        bbox_points = [
            max(0.0, min(point_x0, width_points)),
            max(0.0, min(point_y0, height_points)),
            max(0.0, min(point_x1, width_points)),
            max(0.0, min(point_y1, height_points)),
        ]
        source_regions.append(
            {
                "id": region_id,
                "page": page_number,
                "bbox_points": bbox_points,
            }
        )
        candidates.append(
            {
                "id": f"page-{page_number}-c{index:04d}",
                "source_region": region_id,
                "type": candidate["type"],
                "text": candidate.get("text"),
            }
        )
    document["source_regions"] = source_regions
    document["candidates"] = candidates
    node_type_for_candidate_type = {
        "document_structure": "heading",
        "figure": "figure",
        "formula": "formula",
        "handwriting": "paragraph",
        "table": "table",
        "text": "paragraph",
    }
    candidate_type_by_region = {
        candidate["source_region"]: candidate["type"] for candidate in candidates
    }

    for index, node in enumerate(document["semantic_layer"], start=1):
        node["id"] = f"page-{page_number}-s{index:04d}"
    for warning in document["warnings"]:
        region = warning.pop("region", None)
        semantic_types = {
            semantic_type
            for semantic_type in warning.pop("semantic_types", [])
            if isinstance(semantic_type, str)
        }
        warning_regions = [
            reference
            for reference in warning.get("source_regions", [])
            if isinstance(reference, str)
        ]
        if isinstance(region, str) and region not in warning_regions:
            warning_regions.append(region)
        semantic_nodes = [
            node["id"]
            for node in document["semantic_layer"]
            if node["type"] in semantic_types
            or any(reference in node["source_regions"] for reference in warning_regions)
        ]
        if isinstance(region, str) and not semantic_nodes:
            candidate_type = candidate_type_by_region.get(region)
            node_type = (
                node_type_for_candidate_type.get(candidate_type)
                if candidate_type is not None else None
            )
            semantic_nodes = [
                node["id"] for node in document["semantic_layer"]
                if node["type"] == node_type
            ]
        warning["semantic_nodes"] = semantic_nodes
        warning["source_regions"] = warning_regions
    fallback = _whole_page_region_id(page_number)
    for node in document["semantic_layer"]:
        if fallback in node["source_regions"]:
            document["warnings"].append({
                "code": "imprecise-source-grounding",
                "message": "No tighter deterministic Source Region supports this Semantic Layer node; the whole-page fallback is used.",
                "status": "unresolved",
                "semantic_nodes": [node["id"]],
                "source_regions": [fallback],
            })
    for verified in document["reconstruction"]["verified_regions"]:
        verified["source_region"] = verified.pop("id")
    return document


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


def _page_visual_report(
    source: Path, output: Path, source_page: int, output_page: int, directory: Path
) -> PageVisualReport:
    source_prefix = directory / "source-render"
    output_prefix = directory / "output-render"
    source_render = _run([
        "pdftoppm", "-f", str(source_page), "-l", str(source_page), "-singlefile",
        "-r", "144", str(source), str(source_prefix),
    ])
    output_render = _run([
        "pdftoppm", "-f", str(output_page), "-l", str(output_page), "-singlefile",
        "-r", "144", str(output), str(output_prefix),
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
        "source_page": source_page,
        "output_page": output_page,
        "different_pixel_ratio": different_ratio,
        "output_height": output_height,
        "output_width": output_width,
        "source_height": source_height,
        "source_width": source_width,
    }


def _visual_report(
    source: Path, output: Path, source_pages: list[int], directory: Path
) -> VisualReport:
    """Full-page visual regression: compare every authored page to its source page.

    The authored ``output.pdf`` holds the selected source pages in order, so output
    page ``i`` corresponds to ``source_pages[i - 1]``.
    """
    pages: list[PageVisualReport] = []
    for output_page, source_page in enumerate(source_pages, start=1):
        pages.append(
            _page_visual_report(source, output, source_page, output_page, directory)
        )
    for render in directory.glob("*-render.ppm"):
        render.unlink()
    max_ratio = max((page["different_pixel_ratio"] for page in pages), default=0.0)
    return {
        "pages": pages,
        "max_different_pixel_ratio": max_ratio,
        "tolerance": VISUAL_TOLERANCE,
        "passed": max_ratio <= VISUAL_TOLERANCE,
    }


def _internal_checks(
    record: dict[str, Any], extracted: dict[str, Any]
) -> dict[str, Any]:
    """Run the internal semantic checks over the authored whole-document output.

    The checks read the authored structure tree back out of ``output.pdf`` and
    confirm the Semantic Layer that reaches assistive technology is exactly the one
    the Review Record describes. Each named category covers one acceptance concern.
    """
    categories: dict[str, bool] = {}
    failures: list[str] = []

    def check(name: str, ok: bool, message: str) -> None:
        categories[name] = ok
        if not ok:
            failures.append(f"{name}: {message}")

    # Review Record consistency: the record still validates and its pages agree.
    consistent = True
    try:
        review.validate_review_record(record)
    except review.ReviewRecordError:
        consistent = False
    node_pages = {node["page"] for node in record["semantic_layer"]}
    consistent = consistent and node_pages.issubset(set(record["pages"]))
    check("review-record-consistency", consistent,
          "the Review Record does not validate against its schema, or a node names a "
          "page outside the converted set")

    # Reading order: the authored structure tree matches the record's Semantic
    # Layer grouped per page, in document Logical Reading Order. The output PDF
    # does not echo the source page numbers, so the authored pages are compared in
    # order against the record's per-page node lists.
    expected_layers = [
        page["semantic_layer"] for page in _authoring_contract(record)["pages"]
    ]
    actual_layers = [
        page.get("semantic_layer") for page in extracted.get("pages", [])
    ]
    check("reading-order", actual_layers == expected_layers,
          "the output PDF structure tree does not match the Logical Reading Order")

    # Source-region coverage: every node, candidate, and warning reference resolves
    # to canonical visual evidence. Crops are derived from these Source Regions;
    # Recognition Candidates are not required for a crop to exist.
    region_ids = {region["id"] for region in record.get("source_regions", [])}
    references = [
        reference
        for node in record.get("semantic_layer", [])
        for reference in node.get("source_regions", [])
    ]
    references.extend(
        candidate["source_region"]
        for candidate in record.get("candidates", [])
        if candidate.get("source_region")
    )
    references.extend(
        reference
        for warning in record.get("warnings", [])
        for reference in warning.get("source_regions", [])
    )
    uncovered = [reference for reference in references if reference not in region_ids]
    check("source-region-coverage", not uncovered,
          f"Review Record references unknown Source Regions: {uncovered}")

    # Alternatives: every Formula, Informative Figure, and link exposes its
    # required alternative text.
    missing_alternatives: list[str] = []
    for node in record["semantic_layer"]:
        if node["type"] == "formula" and not node.get("spoken_math_alternative"):
            missing_alternatives.append("formula spoken_math_alternative")
        if node["type"] == "figure":
            if not node.get("figure_alternative"):
                missing_alternatives.append("figure_alternative")
            if node.get("complexity") == "complex" and not node.get(
                "detailed_figure_description"
            ):
                missing_alternatives.append("detailed_figure_description")
        if node["type"] == "link" and not node.get("text"):
            missing_alternatives.append("link text")
    check("alternatives", not missing_alternatives,
          f"semantic nodes are missing alternatives: {missing_alternatives}")

    # Table relationships: every Semantic Table has at least one header cell and
    # every header cell associates the cells it labels through a scope.
    table_problems: list[str] = []
    for node in record["semantic_layer"]:
        if node["type"] != "table":
            continue
        cells = [cell for row in node["rows"] for cell in row["cells"]]
        if not any(cell["kind"] == "header" for cell in cells):
            table_problems.append("a table has no header cell")
        for cell in cells:
            if cell["kind"] == "header" and cell["scope"] == "none":
                table_problems.append("a header cell has no scope")
    check("table-relationships", not table_problems,
          f"table header relationships are incomplete: {table_problems}")

    # Recognition agreement: every Formula, table, and Figure node sits on a page
    # whose reconstruction was cross-checked against an independent crop of that
    # type. This is a structural coverage check (a missing cross-check is an
    # operational failure); a crop that was checked and *disagreed* is a semantic
    # concern surfaced as a recognition-disagreement Conversion Warning (exit 2),
    # never a gate failure here (exit 1).
    verified: dict[int, set[str]] = {}
    for page in record["reconstruction"]["pages"]:
        verified[page["page"]] = {
            region["type"] for region in page["verified_regions"]
        }
    ungrounded: list[str] = []
    for node in record["semantic_layer"]:
        if node["type"] in {"formula", "figure", "table"}:
            if node["type"] not in verified.get(node["page"], set()):
                ungrounded.append(f"{node['type']} on page {node['page']}")
    check("recognition-agreement", not ungrounded,
          f"reconstructed regions were not verified against a crop: {ungrounded}")

    return {
        "categories": categories,
        "checks": failures,
        "passed": not failures,
        "source": "output.pdf structure tree",
    }


def _author_pdf(*, source_pdf: Path, output: Path, authoring_contract: Path,
                record: dict[str, Any]) -> None:
    """Author output.pdf from a whole-document Review Record through Java."""
    atomic_write_json(authoring_contract, _authoring_contract(record))
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
    record: dict[str, Any],
    internal_artifact: Path,
    visual_artifact: Path,
    verapdf_artifact: Path,
    reporter: ProgressReporter | None = None,
) -> None:
    """Run internal semantic checks, the visual comparison, and veraPDF PDF/UA-1.

    Writes each report as a bundle artifact and raises if any gate fails.
    """
    quiet = reporter if reporter is not None else ProgressReporter(
        log_path=None, emit_terminal=False, heartbeat_interval=0
    )
    with quiet.operation("internal-checks"):
        inspected = _run(["java", "-jar", PDF_AUTHOR_JAR, "--inspect", str(output)])
        if inspected.returncode:
            raise RuntimeError(f"PDF semantic inspection failed: {inspected.stderr}")
        try:
            extracted: dict[str, Any] = json.loads(inspected.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("PDF semantic inspection returned invalid JSON") from error
        internal = _internal_checks(record, extracted)
        atomic_write_json(internal_artifact, internal)
    with quiet.operation("visual-comparison"):
        visual = _visual_report(source_pdf, output, list(record["pages"]), workspace)
        atomic_write_json(visual_artifact, visual)
    with quiet.operation("verapdf-validation"):
        verapdf = _run(["verapdf", "--format", "xml", "-f", "ua1", str(output)])
        atomic_write_text(verapdf_artifact, verapdf.stdout)
        compliant = 'isCompliant="true"' in verapdf.stdout
    if not internal["passed"] or not visual["passed"] or not compliant:
        raise RuntimeError("conversion failed its accessibility or visual-preservation gates")


def _source_stage(
    *, checkpoints: CheckpointStore, source: Path,
    source_copy: Path, source_sha256: str, preflight_artifact: Path,
    reporter: ProgressReporter,
) -> None:
    """Copy, verify, and preflight the immutable Source PDF (once per bundle)."""
    source_key = dependency_key({
        "pdf_author_sha256": file_sha256(Path(PDF_AUTHOR_JAR)),
        "preflight_contract_version": "1.0",
        "preflight_stage_version": "1.0",
        "source_sha256": source_sha256,
    })
    if checkpoints.is_reusable("source", source_key):
        reporter.reused("source-preflight")
        return
    with reporter.operation("source-preflight"):
        _atomic_copy(source, source_copy, stat.S_IRUSR)
        if file_sha256(source_copy) != source_sha256:
            raise RuntimeError("immutable Source PDF copy failed SHA-256 verification")
        preflight = _run(["java", "-jar", PDF_AUTHOR_JAR, "--preflight", str(source_copy)])
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


def _recognize_page(
    *, checkpoints: CheckpointStore, source_copy: Path,
    source_sha256: str, page_number: int, backend: recognition.RecognitionBackend,
    regions: Path, recognition_directory: Path,
    pdftoppm_version: str, pdftotext_version: str,
    reporter: ProgressReporter, page_count: int,
) -> str:
    """Run the region-crop and specialized-recognition stages for one page.

    Returns the recognition stage's dependency key so downstream keys can depend
    on it.
    """
    crop_artifact = regions / f"page-{page_number}.png"
    region_key = dependency_key({
        "format": "png",
        "page": page_number,
        "renderer": "pdftoppm",
        "renderer_version": pdftoppm_version,
        "rendering_dpi": 144,
        "source_sha256": source_sha256,
    })
    region_stage = f"region-page-{page_number}"
    region_reusable = checkpoints.is_reusable(region_stage, region_key)
    recognition_key = dependency_key({
        "backend": backend.name,
        "backend_version": backend.version,
        "page": page_number,
        "pdf_text_extractor": "pdftotext",
        "pdf_text_extractor_version": pdftotext_version,
        "recognition_contract_version": recognition.RECOGNITION_CONTRACT_VERSION,
        "recognition_dpi": recognition.RECOGNITION_DPI,
        "renderer": "pdftoppm",
        "renderer_version": pdftoppm_version,
        "source_sha256": source_sha256,
        "weights_version": backend.weights_version,
    })
    recognition_stage = f"recognition-page-{page_number}"
    recognition_reusable = checkpoints.is_reusable(recognition_stage, recognition_key)
    if region_reusable and recognition_reusable:
        reporter.reused("page-recognition", page=page_number, page_count=page_count)
        return recognition_key
    with reporter.operation("page-recognition", page=page_number, page_count=page_count):
        if not region_reusable:
            crop = _run([
                "pdftoppm", "-f", str(page_number), "-l", str(page_number), "-singlefile",
                "-r", "144", "-png", str(source_copy), str(regions / f"page-{page_number}"),
            ])
            if crop.returncode:
                raise RuntimeError(f"source-region crop failed: {crop.stderr}")
            checkpoints.complete(region_stage, region_key, [crop_artifact])
        if not recognition_reusable:
            recognized = recognition.recognize_page(
                source_pdf=source_copy,
                page=page_number,
                dpi=recognition.RECOGNITION_DPI,
                regions_dir=regions,
                recognition_dir=recognition_directory,
                backend=backend,
                source_sha256=source_sha256,
                renderer_version=pdftoppm_version,
                extractor_version=pdftotext_version,
            )
            checkpoints.complete(recognition_stage, recognition_key, recognized.artifacts)
    return recognition_key


def _convert(args: argparse.Namespace) -> int:
    provider = resolve_provider(args)
    limits = resolve_conversion_limits(args)
    authorize_remote(provider, allow_remote=args.allow_remote)
    args.bundle.parent.mkdir(parents=True, exist_ok=True)
    workspace = _prepare_workspace(args.bundle, replace=args.replace, resume=args.resume)
    reporter = ProgressReporter(
        log_path=workspace / CONVERSION_EVENTS_FILENAME,
        verbose=getattr(args, "verbose", False),
    )
    # Handle Ctrl-C (SIGINT) and the launcher's SIGTERM as an intentional
    # interruption rather than a traceback: the in-progress bundle and its
    # checkpoints/events are already on disk, so recording the interruption and
    # printing the resume command is all that remains.
    previous_int = signal.signal(signal.SIGINT, _raise_interrupt)
    previous_term = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        return _run_conversion(args, provider, limits, workspace, reporter)
    except ConversionInterrupted:
        return _handle_interruption(args, reporter)
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


def _handle_interruption(args: argparse.Namespace, reporter: ProgressReporter) -> int:
    command = _resume_command(args)
    stage = reporter.active_stage or "conversion"
    reporter.interrupted(resume_command=command)
    if args.json:
        print(
            json.dumps(
                {
                    "bundle": str(_host_bundle(args.bundle)),
                    "resume_command": command,
                    "stage": stage,
                    "status": "interrupted",
                },
                sort_keys=True,
            )
        )
    return 130


def _run_conversion(
    args: argparse.Namespace,
    provider: ProviderConfig,
    limits: ConversionLimits,
    workspace: Path,
    reporter: ProgressReporter,
) -> int:
    source: Path = args.source
    bundle: Path = args.bundle
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
    budget = _request_budget(workspace, limits, estimated_requests=capability_estimate)
    if capability_reusable:
        reporter.reused("provider-capability")
    else:
        if budget.actual_requests + 1 > budget.ceiling:
            raise RequestCeilingExceeded(
                f"conversion paused before exceeding request ceiling {budget.ceiling}; "
                "resume with a higher --max-requests value"
            )
        before_capability = dict(budget.reported_token_usage)

        def capability_retry(attempt: int, delay: float, reason: str) -> None:
            reporter.retrying(
                "provider-capability", purpose="capability-check",
                request=budget.actual_requests, attempt=attempt, delay=delay, detail=reason,
            )

        with reporter.operation(
            "provider-capability", purpose="capability-check",
            request=budget.actual_requests + 1, request_total=budget.estimated_requests,
            endpoint=provider.base_url, model=provider.model,
        ) as capability_handle:
            check_capabilities(
                provider,
                budget=budget,
                max_retries=limits.provider_max_retries,
                retry_base_seconds=limits.provider_retry_base_seconds,
                retry_max_seconds=limits.provider_retry_max_seconds,
                on_retry=capability_retry,
            )
            capability_usage = budget.usage_since(before_capability)
            if capability_usage:
                capability_handle.extra["token_usage"] = capability_usage
        checkpoints.complete("provider-capability", capability_key, [])

    validation = workspace / "validation"
    validation.mkdir(mode=0o700, exist_ok=True)
    source_copy = workspace / "source.pdf"
    source_sha256 = file_sha256(source)
    _source_stage(
        checkpoints=checkpoints, source=source,
        source_copy=source_copy, source_sha256=source_sha256,
        preflight_artifact=validation / "preflight.json",
        reporter=reporter,
    )

    pages = _parse_page_selection(args.page, _pdf_page_count(source_copy))
    page_count = len(pages)

    regions = workspace / "regions"
    regions.mkdir(mode=0o700, exist_ok=True)
    recognition_directory = workspace / "recognition"
    recognition_directory.mkdir(mode=0o700, exist_ok=True)
    page_semantics_dir = workspace / "page-semantics"
    page_semantics_dir.mkdir(mode=0o700, exist_ok=True)
    backend = recognition.select_backend(os.environ)
    pdftoppm_version = _tool_version(["pdftoppm", "-v"])
    pdftotext_version = _tool_version(["pdftotext", "-v"])

    # First pass over every selected page: crop, recognize, and work out the
    # page-semantics dependency key so the whole conversion's request estimate is
    # known before any reconstruction call is made.
    recognition_keys: dict[int, str] = {}
    recognition_documents: dict[int, dict[str, Any]] = {}
    page_candidates: dict[int, list[dict[str, Any]]] = {}
    page_words: dict[int, list[dict[str, Any]]] = {}
    page_semantics_keys: dict[int, str] = {}
    page_semantics_reusable: dict[int, bool] = {}
    total_estimate = capability_estimate
    for page_number in pages:
        recognition_keys[page_number] = _recognize_page(
            checkpoints=checkpoints, source_copy=source_copy,
            source_sha256=source_sha256, page_number=page_number, backend=backend,
            regions=regions, recognition_directory=recognition_directory,
            pdftoppm_version=pdftoppm_version, pdftotext_version=pdftotext_version,
            reporter=reporter, page_count=page_count,
        )
        recognition_document: dict[str, Any] = json.loads(
            (recognition_directory / f"page-{page_number}.json").read_text(encoding="utf-8")
        )
        recognition_documents[page_number] = recognition_document
        candidates = recognition_document["candidates"]
        page_candidates[page_number] = candidates
        page_words[page_number] = recognition_document["pdf_text_evidence"]["words"]
        page_semantics_keys[page_number] = dependency_key({
            "base_url": provider.base_url,
            "model": provider.model,
            "page": page_number,
            "page_prompt_version": page.PAGE_PROMPT_VERSION,
            "page_schema_version": page.PAGE_SCHEMA_VERSION,
            "page_semantics_contract_version": page.PAGE_SEMANTICS_CONTRACT_VERSION,
            "reconstruction_orchestration_version": page.RECONSTRUCTION_ORCHESTRATION_VERSION,
            "recognition_stage": recognition_keys[page_number],
            "region_prompt_version": page.REGION_PROMPT_VERSION,
            "region_schema_version": page.REGION_SCHEMA_VERSION,
            "source_sha256": source_sha256,
        })
        reusable = checkpoints.is_reusable(
            f"page-semantics-page-{page_number}", page_semantics_keys[page_number]
        )
        page_semantics_reusable[page_number] = reusable
        if not reusable:
            total_estimate += page.expected_request_count(candidates)

    budget.update_estimate(total_estimate)
    # Progress belongs on stderr so a --json run still keeps a single result on
    # stdout; the estimate is useful default output, not verbose-only.
    print(
        f"Estimated provider requests: {budget.estimated_requests} "
        f"(ceiling: {budget.ceiling})",
        file=sys.stderr,
    )

    # Second pass: reconstruct each page that cannot be reused, enforcing the
    # request ceiling against the running total before every page.
    for page_number in pages:
        # The whole-page fallback source region (...-r0000) can anchor a
        # reconstructed formula/table/figure node, so region verification reads
        # its crop — the full page render — before this loop reaches the
        # review-record stage. Materialize it up front for every page.
        _atomic_copy(
            regions / f"page-{page_number}.png",
            regions / f"page-{page_number}-r0000.png",
        )
        if page_semantics_reusable[page_number]:
            reporter.reused(
                "provider-reconstruction", page=page_number, page_count=page_count
            )
            continue
        page_requests = page.expected_request_count(page_candidates[page_number])
        if budget.actual_requests + page_requests > budget.ceiling:
            raise RequestCeilingExceeded(
                f"conversion paused before exceeding request ceiling {budget.ceiling}; "
                "resume with a higher --max-requests value"
            )
        semantics_document = page.reconstruct_page(
            provider,
            page=page_number,
            source_sha256=source_sha256,
            page_image=regions / f"page-{page_number}.png",
            regions_dir=regions,
            candidates=page_candidates[page_number],
            pdf_words=page_words[page_number],
            source_region_ids=[
                _whole_page_region_id(page_number),
                *(str(candidate["id"]) for candidate in page_candidates[page_number]),
            ],
            budget=budget,
            max_retries=limits.provider_max_retries,
            retry_base_seconds=limits.provider_retry_base_seconds,
            retry_max_seconds=limits.provider_retry_max_seconds,
            reporter=reporter,
            page_count=page_count,
        )
        atomic_write_json(
            page_semantics_dir / f"page-{page_number}.json", semantics_document
        )
        checkpoints.complete(
            f"page-semantics-page-{page_number}",
            page_semantics_keys[page_number],
            [page_semantics_dir / f"page-{page_number}.json"],
        )

    # Assemble the whole-document Review Record from every page's reconstruction,
    # injecting each page's retained recognition candidates.
    with reporter.operation("review-record"):
        page_documents: list[dict[str, Any]] = []
        for page_number in pages:
            document = json.loads(
                (page_semantics_dir / f"page-{page_number}.json").read_text(encoding="utf-8")
            )
            page_documents.append(
                _review_page_document(
                    document,
                    recognition_documents[page_number],
                    regions / f"page-{page_number}-recognition.png",
                    _displayed_page_dimensions(source_copy, page_number),
                )
            )
        first = page_documents[0]
        record = review.build_review_record(
            source_sha256=source_sha256,
            title=first["title"],
            language=first["language"],
            provider_endpoint=provider.base_url,
            provider_model=provider.model,
            page_prompt_version=page.PAGE_PROMPT_VERSION,
            page_schema_version=page.PAGE_SCHEMA_VERSION,
            region_prompt_version=page.REGION_PROMPT_VERSION,
            region_schema_version=page.REGION_SCHEMA_VERSION,
            pages=page_documents,
        )
        review.validate_review_record(record)

    document_key = dependency_key({
        "authoring_contract_version": "2.0",
        "document_stage_version": "1.0",
        "page_semantics_stages": [page_semantics_keys[p] for p in pages],
        "pdf_author_sha256": file_sha256(Path(PDF_AUTHOR_JAR)),
        "review_record_schema_version": review.REVIEW_RECORD_SCHEMA_VERSION,
        "review_report_version": review.REVIEW_REPORT_VERSION,
        "source_evidence_contract_version": SOURCE_EVIDENCE_CONTRACT_VERSION,
        "source_sha256": source_sha256,
    })
    review_record = workspace / "review-record.yaml"
    review_baseline = workspace / "review-baseline.json"
    review_report = workspace / "review-report.html"
    authoring_contract = workspace / "authoring.json"
    output = workspace / "output.pdf"
    if checkpoints.is_reusable("document", document_key):
        reporter.reused("pdf-authoring")
    else:
        with reporter.operation("pdf-authoring"):
            atomic_write_text(review_record, review.dump_yaml(record))
            atomic_write_json(review_baseline, record)
            _generate_report(source_copy, record, workspace)
            _author_pdf(
                source_pdf=source_copy,
                output=output,
                authoring_contract=authoring_contract,
                record=record,
            )
            checkpoints.complete(
                "document",
                document_key,
                [review_record, review_baseline, review_report, authoring_contract, output],
            )

    validation_key = dependency_key({
        "document_stage": document_key,
        "internal_checks_version": "2.0",
        "region_stages": [recognition_keys[p] for p in pages],
        "verapdf_profile": "ua1",
        "verapdf_version": _tool_version(["verapdf", "--version"]),
        "visual_dpi": 144,
        "visual_tolerance": VISUAL_TOLERANCE,
    })
    internal_artifact = validation / "internal.json"
    visual_artifact = validation / "visual.json"
    verapdf_artifact = validation / "verapdf.xml"
    if checkpoints.is_reusable("validation", validation_key):
        for validation_stage in VALIDATION_STAGES:
            reporter.reused(validation_stage)
    else:
        _run_output_gates(
            workspace=workspace,
            source_pdf=source_copy,
            output=output,
            record=record,
            internal_artifact=internal_artifact,
            visual_artifact=visual_artifact,
            verapdf_artifact=verapdf_artifact,
            reporter=reporter,
        )
        checkpoints.complete(
            "validation",
            validation_key,
            [internal_artifact, visual_artifact, verapdf_artifact],
        )

    atomic_write_json(workspace / "provenance.json", {
        "accessibilizer_version": __version__,
        "authoring_contract_version": "2.0",
        "page_prompt_version": page.PAGE_PROMPT_VERSION,
        "page_schema_version": page.PAGE_SCHEMA_VERSION,
        "page_semantics_contract_version": page.PAGE_SEMANTICS_CONTRACT_VERSION,
        "reconstruction_orchestration_version": page.RECONSTRUCTION_ORCHESTRATION_VERSION,
        "region_prompt_version": page.REGION_PROMPT_VERSION,
        "region_schema_version": page.REGION_SCHEMA_VERSION,
        "review_record_schema_version": review.REVIEW_RECORD_SCHEMA_VERSION,
        "source_evidence_contract_version": SOURCE_EVIDENCE_CONTRACT_VERSION,
        "recognition_backend": backend.name,
        "recognition_backend_version": backend.version,
        "recognition_contract_version": recognition.RECOGNITION_CONTRACT_VERSION,
        "recognition_dpi": recognition.RECOGNITION_DPI,
        "recognition_weights_version": backend.weights_version,
        "source_copy_verified": True,
        "source_sha256": source_sha256,
        "source_pages": pages,
        "provider_data_location": provider.data_location,
        "provider_endpoint": provider.base_url,
        "provider_model": provider.model,
        "provider_usage": budget.as_dict(),
    })
    # Publication atomically renames the workspace onto the bundle, so the
    # start event is written to the workspace log (which becomes the bundle log)
    # and the completion event is appended to the published log afterwards.
    publication_start = time.monotonic()
    reporter.emit("bundle-publication", STATE_STARTED)
    with reporter.tracked("bundle-publication"):
        _publish_protected_directory(workspace, bundle, args.replace)
    reporter.retarget(bundle / CONVERSION_EVENTS_FILENAME)
    reporter.emit(
        "bundle-publication", STATE_COMPLETED,
        elapsed_seconds=round(time.monotonic() - publication_start, 3),
    )

    host_bundle = _host_bundle(bundle)
    review_required = bool(review.unresolved_warnings(record))
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


def _render_report_regions(source: Path, record: dict[str, Any], regions: Path) -> None:
    """Derive local review evidence from the immutable Source PDF and point geometry."""
    regions.mkdir(mode=0o700, parents=True, exist_ok=True)
    dpi = 144
    scale = dpi / 72
    for page_number in record["pages"]:
        prefix = regions / f"page-{page_number}"
        rendered = _run([
            "pdftoppm", "-f", str(page_number), "-l", str(page_number), "-singlefile",
            "-r", str(dpi), "-png", str(source), str(prefix),
        ])
        if rendered.returncode:
            raise RuntimeError(f"report page render failed: {rendered.stderr}")
        for region in (item for item in record["source_regions"] if item["page"] == page_number):
            identifier = str(region["id"])
            if identifier.endswith("-r0000"):
                _atomic_copy(prefix.with_suffix(".png"), regions / f"{identifier}.png")
                continue
            x0, y0, x1, y1 = (float(value) for value in region["bbox_points"])
            left = round(x0 * scale)
            top = round(y0 * scale)
            right = round(x1 * scale)
            bottom = round(y1 * scale)
            width = max(1, right - left)
            height = max(1, bottom - top)
            crop = _run([
                "pdftoppm", "-f", str(page_number), "-l", str(page_number), "-singlefile",
                "-r", str(dpi), "-x", str(left), "-y", str(top),
                "-W", str(width), "-H", str(height), "-png", str(source),
                str(regions / identifier),
            ])
            if crop.returncode:
                raise RuntimeError(f"Source Region render failed: {crop.stderr}")


def _generate_report(source: Path, record: dict[str, Any], output: Path) -> None:
    review.validate_review_record(record)
    if file_sha256(source) != record["source_sha256"]:
        raise RuntimeError("the Source PDF does not match the Review Record")
    _render_report_regions(source, record, output / "regions")
    atomic_write_text(output / "review-report.html", review.render_review_report(record))
    atomic_write_text(output / review.REVIEW_REPORT_STYLESHEET, review.review_report_css())
    atomic_write_text(output / review.REVIEW_REPORT_SCRIPT, review.review_report_javascript())


def _report(args: argparse.Namespace) -> int:
    if args.bundle is not None:
        if any(value is not None for value in (args.source, args.record, args.output)):
            raise ValueError("--bundle cannot be combined with --source, --record, or --output")
        record, _ = _load_bundle_record(args.bundle)
        source = args.bundle / "source.pdf"
        if not source.is_file():
            raise RuntimeError("bundle is missing its immutable Source PDF copy")
        staging = Path(tempfile.mkdtemp(prefix=f".{args.bundle.name}.report-", dir=args.bundle.parent))
        try:
            shutil.copytree(args.bundle, staging, dirs_exist_ok=True, copy_function=shutil.copy2)
            shutil.rmtree(staging / "regions", ignore_errors=True)
            _generate_report(staging / "source.pdf", record, staging)
            _publish_protected_directory(staging, args.bundle, True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        result = {"bundle": str(_host_bundle(args.bundle)), "report": str(_host_bundle(args.bundle) / "review-report.html"), "status": "reported"}
    else:
        if args.source is None or args.record is None or args.output is None:
            raise ValueError("standalone report requires --source, --record, and --output")
        if not args.source.is_file() or not args.record.is_file():
            raise RuntimeError("--source and --record must be files")
        if args.output.exists() and not args.replace:
            raise FileExistsError(f"report output already exists: {args.output}")
        record = review.load_yaml(args.record.read_text(encoding="utf-8"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{args.output.name}.report-", dir=args.output.parent))
        try:
            _generate_report(args.source, record, staging)
            _publish_protected_directory(staging, args.output, args.replace)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        result = {"report": str(_host_bundle(args.output) / "review-report.html"), "status": "reported"}
    _emit(args, result, f"Review Report: {result['report']}")
    return 0


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
    _generate_report(bundle / "source.pdf", record, bundle)


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
        record=record,
    )
    _run_output_gates(
        workspace=workspace,
        source_pdf=source_copy,
        output=output,
        record=record,
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
        _generate_report(workspace / "source.pdf", committed, workspace)
        _rebuild_and_validate(workspace, committed)
        atomic_write_json(
            workspace / "provenance.json",
            _finalize_provenance(bundle, committed, reviewer, finalized=True),
        )
        _publish_protected_directory(workspace, bundle, True)
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
        if args.command == "report":
            return _report(args)
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
