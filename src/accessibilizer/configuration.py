from __future__ import annotations

import os
from pathlib import Path


def config_path(environment_name: str, default: Path) -> Path:
    configured = os.environ.get(environment_name)
    return Path(configured) if configured else default


def user_config_default() -> Path:
    """Return the module's fallback user-config path, honoring ``XDG_CONFIG_HOME``.

    Mirrors the resolution the ``accessibilizer`` launcher performs before it
    sets ``ACCESSIBILIZER_USER_CONFIG``, so the two agree when the module is
    run directly instead of through the launcher.
    """
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return base / "accessibilizer" / "config.toml"
