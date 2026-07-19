from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time
try:
    import tomllib
except ImportError:  # pragma: no cover - the canonical Python 3.10 image uses tomli
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]
from typing import Any, Callable, Literal, Mapping, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from accessibilizer.configuration import config_path


CAPABILITY_IMAGE = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAIAAAAt/+nTAAAAQ0lEQVR42u3WsQ0A"
    "IAwDsJzI/8/QD4AJUeSoayp5S2bzBAAAAAAAAOA5QMbqNh/vdgEAAAAAAD4E2EIA"
    "AAAAAAAARymQ/vFUcvTOZQAAAABJRU5ErkJggg=="
)
DataLocation = Literal["local", "remote"]


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key_env: str | None
    data_location: DataLocation


class RequestCeilingExceeded(RuntimeError):
    pass


class RequestBudget:
    def __init__(
        self,
        *,
        estimated_requests: int,
        ceiling: int,
        actual_requests: int = 0,
        reported_token_usage: Mapping[str, int] | None = None,
        on_change: Callable[[RequestBudget], None] | None = None,
    ) -> None:
        if estimated_requests < 0 or ceiling < 0 or actual_requests < 0:
            raise ValueError("request counts and request ceiling must not be negative")
        if actual_requests > ceiling:
            raise RequestCeilingExceeded(
                f"conversion already used {actual_requests} requests, above the configured "
                f"request ceiling {ceiling}; resume with a higher --max-requests value"
            )
        self.estimated_requests = estimated_requests
        self.ceiling = ceiling
        self.actual_requests = actual_requests
        self.reported_token_usage: dict[str, int] = {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = (reported_token_usage or {}).get(name)
            if value is not None:
                if isinstance(value, bool) or value < 0:
                    raise ValueError("reported token usage must not be negative")
                self.reported_token_usage[name] = int(value)
        self._on_change = on_change

    def reserve(self) -> None:
        if self.actual_requests >= self.ceiling:
            raise RequestCeilingExceeded(
                f"conversion paused before exceeding request ceiling {self.ceiling}; "
                "resume with a higher --max-requests value"
            )
        self.actual_requests += 1
        self._changed()

    def record_reported_usage(self, response: object) -> None:
        if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
            return
        usage: dict[object, object] = response["usage"]
        changed = False
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.reported_token_usage[name] = (
                    self.reported_token_usage.get(name, 0) + value
                )
                changed = True
        if changed:
            self._changed()

    def as_dict(self) -> dict[str, object]:
        return {
            "actual_requests": self.actual_requests,
            "estimated_requests": self.estimated_requests,
            "reported_token_usage": dict(self.reported_token_usage),
            "request_ceiling": self.ceiling,
        }

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change(self)


def _load_provider_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        document: Any = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"invalid provider configuration in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"invalid provider configuration in {path}")
    provider = document.get("provider", {})
    if not isinstance(provider, dict):
        raise ValueError(f"[provider] must be a table in {path}")
    supported = {"base_url", "model", "api_key_env", "data_location"}
    unknown = provider.keys() - supported
    if unknown:
        raise ValueError(f"unknown provider setting in {path}: {', '.join(sorted(unknown))}")
    result: dict[str, str] = {}
    for key, value in provider.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"provider.{key} must be a non-empty string in {path}")
        result[key] = value.strip()
    return result


def resolve_provider(args: argparse.Namespace) -> ProviderConfig:
    user_default = Path.home() / ".config" / "accessibilizer" / "config.toml"
    user_path = config_path("ACCESSIBILIZER_USER_CONFIG", user_default)
    project_path = config_path(
        "ACCESSIBILIZER_PROJECT_CONFIG", Path.cwd() / "accessibilizer.toml"
    )
    resolved = _load_provider_config(user_path)
    resolved.update(_load_provider_config(project_path))
    cli_values = {
        "base_url": getattr(args, "provider_base_url", None),
        "model": getattr(args, "provider_model", None),
        "api_key_env": getattr(args, "provider_api_key_env", None),
        "data_location": getattr(args, "provider_data_location", None),
    }
    resolved.update({key: value for key, value in cli_values.items() if value is not None})

    missing = [name for name in ("base_url", "model") if name not in resolved]
    if missing:
        flags = ", ".join(f"--provider-{name.replace('_', '-')}" for name in missing)
        raise ValueError(f"provider configuration requires {flags}")
    base_url = resolved["base_url"].strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("provider base_url must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("provider base_url must not contain a query or fragment")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("provider base_url contains an invalid port") from error
    model = resolved["model"].strip()
    if model.lower() == "latest" or model.lower().endswith(":latest"):
        raise ValueError("provider model must be an exact identifier, not a latest alias")
    data_location_value = resolved.get("data_location")
    if data_location_value is None:
        data_location_value = (
            "local" if parsed.hostname in {"localhost", "127.0.0.1", "::1"} else "remote"
        )
    if data_location_value not in {"local", "remote"}:
        raise ValueError("provider data_location must be local or remote")
    data_location = cast(DataLocation, data_location_value)
    api_key_env_value = resolved.get("api_key_env")
    api_key_env = api_key_env_value.strip() if api_key_env_value is not None else None
    if api_key_env is not None and (
        not api_key_env.replace("_", "a").isalnum() or api_key_env[0].isdigit()
    ):
        raise ValueError("provider api_key_env must be an environment-variable name")
    return ProviderConfig(base_url, model, api_key_env, data_location)


def authorize_remote(config: ProviderConfig, *, allow_remote: bool) -> None:
    if config.data_location == "local" or allow_remote:
        return
    if not sys.stdin.isatty():
        raise PermissionError(
            "remote or uncertain provider transmission requires --allow-remote in noninteractive use"
        )
    answer = input(
        f"Transmit rendered Source PDF content to {config.base_url} using {config.model}? [y/N] "
    )
    if answer.strip().lower() not in {"y", "yes"}:
        raise PermissionError("remote provider transmission was not authorized")


def _api_key(config: ProviderConfig) -> str | None:
    if config.api_key_env is None:
        return None
    value = os.environ.get(config.api_key_env)
    if not value:
        raise ValueError(f"provider API key environment variable is not set: {config.api_key_env}")
    return value


def _capability_base_url(config: ProviderConfig) -> str:
    parsed = urlparse(config.base_url)
    if (
        os.environ.get("ACCESSIBILIZER_CONTAINERIZED") == "1"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        port = f":{parsed.port}" if parsed.port is not None else ""
        return parsed._replace(netloc=f"host.docker.internal{port}").geturl()
    return config.base_url


def check_capabilities(
    config: ProviderConfig,
    *,
    budget: RequestBudget | None = None,
    max_retries: int = 3,
    retry_base_seconds: float = 0.5,
    retry_max_seconds: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if max_retries < 0:
        raise ValueError("provider max retries must not be negative")
    if retry_base_seconds < 0 or retry_max_seconds < 0:
        raise ValueError("provider retry delays must not be negative")
    schema = {
        "type": "object",
        "properties": {
            "blue_square_count": {"type": "integer", "minimum": 0, "maximum": 10}
        },
        "required": ["blue_square_count"],
        "additionalProperties": False,
    }
    payload = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Count the blue squares in the image and return the required JSON object.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{CAPABILITY_IMAGE}"},
                    },
                ],
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "accessibilizer_capability_check", "strict": True, "schema": schema},
        },
        "max_completion_tokens": 256,
    }
    headers = {"Content-Type": "application/json"}
    api_key = _api_key(config)
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        f"{_capability_base_url(config)}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    result: Any = None
    for attempt in range(max_retries + 1):
        if budget is not None:
            budget.reserve()
        try:
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read())
            if budget is not None:
                budget.record_reported_usage(result)
            break
        except HTTPError as error:
            transient = error.code in {408, 409, 425, 429} or 500 <= error.code <= 599
            error.close()
            if not transient or attempt == max_retries:
                raise RuntimeError(
                    "provider capability check failed; base64 vision input and "
                    "JSON-Schema responses are required"
                ) from error
        except (URLError, TimeoutError) as error:
            if attempt == max_retries:
                raise RuntimeError(
                    "provider capability check failed; base64 vision input and "
                    "JSON-Schema responses are required"
                ) from error
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "provider capability check failed; provider returned invalid JSON"
            ) from error
        delay = min(retry_base_seconds * (2**attempt), retry_max_seconds)
        if delay:
            sleep(delay)
    try:
        content = result["choices"][0]["message"]["content"]
        checked: Any = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "provider capability check failed; provider returned an invalid schema response"
        ) from error
    if checked != {"blue_square_count": 3}:
        raise RuntimeError(
            "provider capability check failed; provider did not satisfy the required schema"
        )
