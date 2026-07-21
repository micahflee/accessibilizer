from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "accessibilizer", *arguments],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def unwrapped(text: str) -> str:
    """Collapse argparse's line wrapping so assertions can match phrases as one line."""
    return " ".join(text.split())


class TopLevelHelpTest(unittest.TestCase):
    def test_top_level_help_describes_the_tool_and_exits_zero(self) -> None:
        result = run_cli("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        stdout = unwrapped(result.stdout)
        self.assertIn("Visual Layer", stdout)
        self.assertIn("assistive technology", stdout)

    def test_provider_key_env_command_stays_hidden(self) -> None:
        result = run_cli("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("provider-key-env", result.stdout)


class ConvertHelpTest(unittest.TestCase):
    def test_convert_help_documents_every_argument(self) -> None:
        result = run_cli("convert", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in (
            "source",
            "--page",
            "--bundle",
            "--provider-base-url",
            "--provider-model",
            "--provider-api-key-env",
            "--provider-data-location",
            "--allow-remote",
            "--max-requests",
            "--provider-max-retries",
            "--provider-retry-base-seconds",
            "--provider-retry-max-seconds",
            "--replace",
            "--resume",
            "--json",
        ):
            self.assertIn(flag, result.stdout)

    def test_convert_help_explains_page_is_an_optional_subset(self) -> None:
        result = run_cli("convert", "--help")

        stdout = unwrapped(result.stdout)
        self.assertIn("1-indexed", stdout)
        self.assertIn("subset", stdout)
        self.assertIn("whole document", stdout)

    def test_convert_help_includes_a_worked_example(self) -> None:
        result = run_cli("convert", "--help")

        self.assertIn("examples:", result.stdout)
        self.assertIn("accessibilizer convert source.pdf", result.stdout)


if __name__ == "__main__":
    unittest.main()
