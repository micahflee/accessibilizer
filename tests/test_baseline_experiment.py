from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from accessibilizer.baseline_experiment import (
    FROZEN_EXPERIMENT,
    replay_frozen_baseline_experiment,
    run_frozen_baseline_experiment,
)
from accessibilizer.review import load_yaml
from tests.test_prototype_evaluation import gold_provider_responses
from tests.test_vision_prototype import FakeVisionProvider, POPPLER, SOURCE


ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "testdata" / "gold-review-record.yaml"


@unittest.skipUnless(POPPLER, "poppler is required for the frozen shakedown")
class FrozenBaselineExperimentTests(unittest.TestCase):
    def test_offline_complete_document_shakedown_uses_frozen_revision(self) -> None:
        gold = load_yaml(GOLD.read_text(encoding="utf-8"))
        with (
            FakeVisionProvider(
                gold_provider_responses(gold), expect_native_context=False
            ) as provider,
            tempfile.TemporaryDirectory() as directory,
        ):
            provider_config = provider.config
            object.__setattr__(provider_config, "model", "gpt-5.6-sol")
            result = run_frozen_baseline_experiment(
                provider_config,
                source_pdf=SOURCE,
                gold_review_record=GOLD,
                artifacts_root=Path(directory),
                run_id="shakedown-77",
                include_native_pdf_context=False,
                max_retries=0,
            )

            run_dir = Path(directory) / "shakedown-77"
            manifest = json.loads((run_dir / "manifest.json").read_text())
            evaluation = json.loads((run_dir / "evaluation.json").read_text())
            artifact_names = sorted(path.name for path in run_dir.iterdir())

        self.assertEqual(result, evaluation)
        self.assertTrue(result["passed"])
        self.assertEqual(
            FROZEN_EXPERIMENT.identity,
            "4bfd496ef022298e659e6a36c9eeabef3b2b99a0646a695273da6df329100584",
        )
        self.assertEqual(manifest["experiment"], FROZEN_EXPERIMENT.as_dict())
        self.assertEqual(manifest["model"], "gpt-5.6-sol")
        self.assertEqual(manifest["provider_data_location"], "local")
        self.assertTrue(manifest["provider_endpoint"].startswith("http://127.0.0.1:"))
        self.assertEqual(result["failures"], {
            "semantic": [],
            "warning": {"recall": [], "precision": []},
            "geometry": [],
            "schema_or_repair": [],
            "resource_limit": [],
        })
        self.assertEqual(len(provider.requests), 11)
        self.assertTrue(all("tools" not in request for request in provider.requests))
        self.assertTrue(all("functions" not in request for request in provider.requests))
        self.assertEqual(
            artifact_names,
            ["evaluation.json", "manifest.json", "pages", "prompt"],
        )

    def test_replay_rejects_page_response_from_another_run_identity(self) -> None:
        gold = load_yaml(GOLD.read_text(encoding="utf-8"))
        first_responses = gold_provider_responses(gold)
        second_responses = gold_provider_responses(gold)
        second_responses[0]["nodes"][0]["text"] += " second run"
        with (
            FakeVisionProvider(
                first_responses + second_responses, expect_native_context=False
            ) as provider,
            tempfile.TemporaryDirectory() as directory,
        ):
            provider_config = provider.config
            object.__setattr__(provider_config, "model", "gpt-5.6-sol")
            root = Path(directory)
            for run_id in ("first", "second"):
                run_frozen_baseline_experiment(
                    provider_config,
                    source_pdf=SOURCE,
                    gold_review_record=GOLD,
                    artifacts_root=root,
                    run_id=run_id,
                    include_native_pdf_context=False,
                    max_retries=0,
                )
            shutil.copyfile(
                root / "first/pages/page-1/response.json",
                root / "second/pages/page-1/response.json",
            )
            with self.assertRaisesRegex(ValueError, "recorded response identity"):
                replay_frozen_baseline_experiment(root / "second", GOLD)
