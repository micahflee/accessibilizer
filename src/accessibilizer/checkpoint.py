from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterable


def dependency_key(dependencies: object) -> str:
    """Return a stable key for exactly the inputs a stage declares."""
    encoded = json.dumps(
        dependencies,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, write: Callable[[Any], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: object) -> None:
    def write(stream: Any) -> None:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")

    _atomic_write(path, write)


def atomic_write_text(path: Path, text: str) -> None:
    _atomic_write(path, lambda stream: stream.write(text))


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.directory = root / "checkpoints"

    def _path(self, stage: str) -> Path:
        valid_characters = "abcdefghijklmnopqrstuvwxyz0123456789-_"
        if not stage or any(character not in valid_characters for character in stage):
            raise ValueError(f"invalid checkpoint stage: {stage}")
        return self.directory / f"{stage}.json"

    def is_reusable(self, stage: str, key: str) -> bool:
        path = self._path(stage)
        try:
            manifest: Any = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                return False
            if manifest.get("dependency_key") != key:
                return False
            artifacts = manifest["artifacts"]
            hashes = manifest["artifact_sha256"]
            if not isinstance(artifacts, list) or not isinstance(hashes, dict):
                return False
            for relative_name in artifacts:
                if not isinstance(relative_name, str):
                    return False
                relative = Path(relative_name)
                if relative.is_absolute() or ".." in relative.parts:
                    return False
                artifact = self.root / relative
                if (
                    not artifact.is_file()
                    or hashes.get(relative_name) != file_sha256(artifact)
                ):
                    return False
            return True
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def complete(self, stage: str, key: str, artifacts: Iterable[Path]) -> None:
        relative_names: list[str] = []
        hashes: dict[str, str] = {}
        for artifact in artifacts:
            relative_name = str(artifact.relative_to(self.root))
            relative_names.append(relative_name)
            hashes[relative_name] = file_sha256(artifact)
        atomic_write_json(
            self._path(stage),
            {
                "artifact_sha256": hashes,
                "artifacts": relative_names,
                "dependency_key": key,
                "stage": stage,
            },
        )
