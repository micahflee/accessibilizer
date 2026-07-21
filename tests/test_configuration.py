from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

from accessibilizer.configuration import user_config_default


class UserConfigDefaultTest(unittest.TestCase):
    def test_honors_xdg_config_home_when_set(self) -> None:
        with mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": "/custom/config"}):
            self.assertEqual(
                user_config_default(),
                Path("/custom/config/accessibilizer/config.toml"),
            )

    def test_falls_back_to_home_config_when_xdg_config_home_unset(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False) as environment:
            environment.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(
                user_config_default(),
                Path.home() / ".config" / "accessibilizer" / "config.toml",
            )

    def test_treats_empty_xdg_config_home_as_unset(self) -> None:
        with mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(
                user_config_default(),
                Path.home() / ".config" / "accessibilizer" / "config.toml",
            )


if __name__ == "__main__":
    unittest.main()
