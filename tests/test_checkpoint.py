from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from accessibilizer.checkpoint import CheckpointStore, dependency_key


class DependencyKeyTest(unittest.TestCase):
    def test_key_changes_only_when_a_declared_dependency_changes(self) -> None:
        page_dependencies = {
            "source_sha256": "source-a",
            "page": 1,
            "rendering": {"dpi": 144, "format": "png"},
        }

        original = dependency_key(page_dependencies)

        self.assertEqual(original, dependency_key(dict(reversed(page_dependencies.items()))))
        self.assertNotEqual(
            original,
            dependency_key({**page_dependencies, "page": 2}),
        )


class CheckpointStoreTest(unittest.TestCase):
    def test_completed_stage_is_reused_only_while_its_artifacts_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "regions" / "page-1.png"
            artifact.parent.mkdir()
            artifact.write_bytes(b"valid crop")
            store = CheckpointStore(root)
            key = dependency_key({"source_sha256": "source-a", "page": 1})

            store.complete("region-page-1", key, [artifact])

            self.assertTrue(store.is_reusable("region-page-1", key))
            manifest = json.loads(
                (root / "checkpoints" / "region-page-1.json").read_text()
            )
            self.assertEqual(manifest["dependency_key"], key)
            self.assertEqual(manifest["artifacts"], ["regions/page-1.png"])

            artifact.write_bytes(b"corrupt crop")

            self.assertFalse(store.is_reusable("region-page-1", key))


if __name__ == "__main__":
    unittest.main()
