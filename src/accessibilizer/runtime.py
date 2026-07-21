from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
try:
    import tomllib
except ImportError:  # pragma: no cover - the canonical Python 3.10 image uses tomli
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]
from typing import Any

from accessibilizer.configuration import config_path, user_config_default


@dataclass(frozen=True)
class ConversionLimits:
    max_requests: int
    provider_max_retries: int
    provider_retry_base_seconds: float
    provider_retry_max_seconds: float


DEFAULTS: dict[str, int | float] = {
    "max_requests": 100,
    "provider_max_retries": 3,
    "provider_retry_base_seconds": 0.5,
    "provider_retry_max_seconds": 8.0,
}


def _load_conversion_config(path: Path) -> dict[str, int | float]:
    if not path.is_file():
        return {}
    try:
        document: Any = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"invalid conversion configuration in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"invalid conversion configuration in {path}")
    conversion = document.get("conversion", {})
    if not isinstance(conversion, dict):
        raise ValueError(f"[conversion] must be a table in {path}")
    unknown = conversion.keys() - DEFAULTS.keys()
    if unknown:
        raise ValueError(f"unknown conversion setting in {path}: {', '.join(sorted(unknown))}")
    values: dict[str, int | float] = {}
    for key, value in conversion.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"conversion.{key} must be a number in {path}")
        values[key] = value
    return values


def resolve_conversion_limits(args: argparse.Namespace) -> ConversionLimits:
    user_path = config_path("ACCESSIBILIZER_USER_CONFIG", user_config_default())
    project_path = config_path(
        "ACCESSIBILIZER_PROJECT_CONFIG", Path.cwd() / "accessibilizer.toml"
    )
    resolved = dict(DEFAULTS)
    resolved.update(_load_conversion_config(user_path))
    resolved.update(_load_conversion_config(project_path))
    cli_values = {key: getattr(args, key, None) for key in DEFAULTS}
    resolved.update({key: value for key, value in cli_values.items() if value is not None})

    integer_names = ("max_requests", "provider_max_retries")
    for name in integer_names:
        value = resolved[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"--{name.replace('_', '-')} must be a non-negative integer")
    delay_names = ("provider_retry_base_seconds", "provider_retry_max_seconds")
    for name in delay_names:
        value = resolved[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError(f"--{name.replace('_', '-')} must be non-negative")
    return ConversionLimits(
        max_requests=int(resolved["max_requests"]),
        provider_max_retries=int(resolved["provider_max_retries"]),
        provider_retry_base_seconds=float(resolved["provider_retry_base_seconds"]),
        provider_retry_max_seconds=float(resolved["provider_retry_max_seconds"]),
    )


def _load_reviewer_config(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        document: Any = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"invalid review configuration in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"invalid review configuration in {path}")
    review = document.get("review", {})
    if not isinstance(review, dict):
        raise ValueError(f"[review] must be a table in {path}")
    unknown = review.keys() - {"reviewer"}
    if unknown:
        raise ValueError(f"unknown review setting in {path}: {', '.join(sorted(unknown))}")
    reviewer = review.get("reviewer")
    if reviewer is None:
        return None
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError(f"review.reviewer must be a non-empty string in {path}")
    return reviewer.strip()


def resolve_reviewer(args: argparse.Namespace) -> str | None:
    """Resolve the Reviewer's non-secret identifier from flags then configuration.

    A ``--reviewer`` flag wins over project configuration, which wins over the
    user default. Warning resolutions carry this identifier so semantic approvals
    are attributable.
    """
    cli_value = getattr(args, "reviewer", None)
    if cli_value is not None:
        text = str(cli_value).strip()
        if not text:
            raise ValueError("--reviewer must not be empty")
        return text
    user_path = config_path("ACCESSIBILIZER_USER_CONFIG", user_config_default())
    project_path = config_path(
        "ACCESSIBILIZER_PROJECT_CONFIG", Path.cwd() / "accessibilizer.toml"
    )
    resolved = _load_reviewer_config(user_path)
    project_reviewer = _load_reviewer_config(project_path)
    if project_reviewer is not None:
        resolved = project_reviewer
    return resolved
