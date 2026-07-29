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
                provider_concurrency=None,
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
            self.assertEqual(limits.provider_concurrency, 4)
            self.assertEqual(limits.provider_max_retries, 5)
            self.assertEqual(limits.provider_retry_base_seconds, 0.25)
            self.assertEqual(limits.provider_retry_max_seconds, 8.0)

    def test_provider_concurrency_uses_layered_precedence_and_requires_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            user = temporary / "user.toml"
            project = temporary / "project.toml"
            user.write_text("[conversion]\nprovider_concurrency = 2\n")
            project.write_text("[conversion]\nprovider_concurrency = 3\n")
            environment = {
                "ACCESSIBILIZER_USER_CONFIG": str(user),
                "ACCESSIBILIZER_PROJECT_CONFIG": str(project),
            }
            with patch.dict("os.environ", environment):
                resolved = resolve_conversion_limits(
                    argparse.Namespace(provider_concurrency=5)
                )
                self.assertEqual(resolved.provider_concurrency, 5)
                for invalid in (0, -1, 1.5, True):
                    with self.subTest(invalid=invalid), self.assertRaisesRegex(
                        ValueError, "--provider-concurrency must be an integer greater than or equal to 1"
                    ):
                        resolve_conversion_limits(
                            argparse.Namespace(provider_concurrency=invalid)
                        )

    def test_invalid_provider_concurrency_is_rejected_from_each_toml_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            user = temporary / "user.toml"
            project = temporary / "project.toml"
            environment = {
                "ACCESSIBILIZER_USER_CONFIG": str(user),
                "ACCESSIBILIZER_PROJECT_CONFIG": str(project),
            }
            for invalid_path, other_path in ((user, project), (project, user)):
                with self.subTest(path=invalid_path):
                    invalid_path.write_text("[conversion]\nprovider_concurrency = 0\n")
                    other_path.write_text("")
                    with patch.dict("os.environ", environment), self.assertRaisesRegex(
                        ValueError,
                        "--provider-concurrency must be an integer greater than or equal to 1",
                    ):
                        resolve_conversion_limits(
                            argparse.Namespace(provider_concurrency=None)
                        )


if __name__ == "__main__":
    unittest.main()
