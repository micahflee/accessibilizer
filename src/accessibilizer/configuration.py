from __future__ import annotations

import os
from pathlib import Path


def config_path(environment_name: str, default: Path) -> Path:
    configured = os.environ.get(environment_name)
    return Path(configured) if configured else default
