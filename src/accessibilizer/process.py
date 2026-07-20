from __future__ import annotations

import subprocess


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def tool_version(command: list[str]) -> str:
    result = run(command)
    version = (result.stdout + result.stderr).strip()
    if result.returncode or not version:
        raise RuntimeError(f"could not determine tool version: {command[0]}")
    return version
