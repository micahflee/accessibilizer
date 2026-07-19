from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from accessibilizer.runtime import resolve_conversion_limits


class ConversionLimitsTest(unittest.TestCase):
    def test_user_project_and_cli_limits_use_documented_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            user = temporary / "user.toml"
            project = temporary / "project.toml"
            user.write_text(
                "[conversion]\nmax_requests = 20\nprovider_max_retries = 5\n"
            )
            project.write_text(
                "[conversion]\nmax_requests = 10\nprovider_retry_base_seconds = 0.25\n"
            )
            args = argparse.Namespace(
                max_requests=7,
                provider_max_retries=None,
                provider_retry_base_seconds=None,
                provider_retry_max_seconds=None,
            )

            with patch.dict(
                "os.environ",
                {
                    "ACCESSIBILIZER_USER_CONFIG": str(user),
                    "ACCESSIBILIZER_PROJECT_CONFIG": str(project),
                },
            ):
                limits = resolve_conversion_limits(args)

            self.assertEqual(limits.max_requests, 7)
            self.assertEqual(limits.provider_max_retries, 5)
            self.assertEqual(limits.provider_retry_base_seconds, 0.25)
            self.assertEqual(limits.provider_retry_max_seconds, 8.0)


if __name__ == "__main__":
    unittest.main()
