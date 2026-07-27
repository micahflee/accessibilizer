from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
import tempfile
import unittest

from accessibilizer.prototype_evaluation import evaluate_prototype_fidelity
from accessibilizer.review import load_yaml
from accessibilizer.vision_prototype import (
    reconstruct_prototype_document,
    replay_prototype_document,
)
from tests.test_vision_prototype import FakeVisionProvider, POPPLER, SOURCE


ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "testdata" / "gold-review-record.yaml"


def replayed_gold_run(gold: dict[str, Any]) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for page in gold["pages"]:
        nodes = [
            deepcopy(node) for node in gold["semantic_layer"] if node["page"] == page
        ]
        for node in nodes:
            node.pop("source_regions")
        warnings = [
            {
                "id": f"prototype-{warning['id']}",
                "page": page,
                "code": warning["code"],
                "message": "Provider wording and geometry may differ.",
                "semantic_nodes": [],
                "source_regions": [],
            }
            for warning in gold["warnings"]
            if warning["page"] == page
        ]
        pages.append(
            {"page": page, "semantic_layer": nodes, "warnings": warnings}
        )
    return {"run_id": "replayed-run-75", "pages": pages}


def gold_provider_responses(gold: dict[str, Any]) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    for page in gold["pages"]:
        nodes: list[dict[str, Any]] = []
        for index, gold_node in enumerate(
            (node for node in gold["semantic_layer"] if node["page"] == page),
            start=1,
        ):
            node = {
                key: deepcopy(value)
                for key, value in gold_node.items()
                if key not in {"id", "page", "source_regions"}
            }
            node["boxes"] = [[0.05, index / 100, 0.95, (index + 0.5) / 100]]
            if node["type"] == "figure":
                node.setdefault("detailed_figure_description", None)
            if node["type"] == "table":
                node["boundaries_are_uncertain"] = False
                node["headers_are_uncertain"] = False
            nodes.append(node)
        warnings = [
            {
                "code": warning["code"],
                "message": "Provider wording may differ.",
                "node_indices": [],
                "boxes": [[0.01, 0.9 + index / 1000, 0.99, 0.905 + index / 1000]],
            }
            for index, warning in enumerate(
                (warning for warning in gold["warnings"] if warning["page"] == page)
            )
        ]
        responses.append({"nodes": nodes, "warnings": warnings})
    return responses


class PrototypeFidelityEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gold: dict[str, Any] = load_yaml(GOLD.read_text(encoding="utf-8"))

    def replay_gold(self) -> dict[str, Any]:
        with (
            FakeVisionProvider(
                gold_provider_responses(self.gold), expect_native_context=False
            ) as provider,
            tempfile.TemporaryDirectory() as directory,
        ):
            reconstruct_prototype_document(
                provider.config,
                source_pdf=SOURCE,
                artifacts_root=Path(directory),
                run_id="replayed-run-75",
                include_native_pdf_context=False,
                max_retries=0,
            )
            replayed = replay_prototype_document(Path(directory) / "replayed-run-75")
        return replayed

    @unittest.skipUnless(POPPLER, "poppler is required for the 11-page replay")
    def test_replayed_eleven_page_gold_run_passes_deterministically(self) -> None:
        replayed = self.replay_gold()

        result = evaluate_prototype_fidelity(replayed, self.gold)

        self.assertTrue(result["passed"])
        self.assertEqual(result["run_id"], "replayed-run-75")
        self.assertEqual(result["semantic_fidelity_failures"], [])
        self.assertEqual(result["warning_failures"], {"recall": [], "precision": []})
        self.assertEqual(result["adjudication_queue"], [])

    @unittest.skipUnless(POPPLER, "poppler is required for the 11-page replay")
    def test_replayed_representative_mismatches_are_clearly_categorized(self) -> None:
        run = self.replay_gold()
        page_one = run["pages"][0]
        page_one["semantic_layer"][0]["level"] = 2
        formula = next(
            node for node in page_one["semantic_layer"] if node["type"] == "formula"
        )
        formula["spoken_math_alternative"] = "I is charge over elapsed time."
        page_three = run["pages"][2]
        table = next(
            node for node in page_three["semantic_layer"] if node["type"] == "table"
        )
        table["rows"][0]["cells"][0]["scope"] = "row"
        page_one["warnings"] = []
        run["pages"][1]["warnings"].append(
            {
                "id": "extra-warning",
                "page": 2,
                "code": "unsupported-input",
                "message": "Extra warning",
                "semantic_nodes": [],
                "source_regions": [],
            }
        )

        result = evaluate_prototype_fidelity(run, self.gold)

        self.assertFalse(result["passed"])
        self.assertEqual(
            {(item["page"], item["node"], item["field"]) for item in result["semantic_fidelity_failures"]},
            {(1, "page-1-s0001", "level"), (3, "page-3-s0009", "rows")},
        )
        self.assertEqual(
            result["warning_failures"],
            {
                "recall": [{"page": 1, "code": "ambiguous-reading-order"}],
                "precision": [{"page": 2, "code": "unsupported-input"}],
            },
        )
        self.assertEqual(
            result["adjudication_queue"],
            [
                {
                    "id": "replayed-run-75:page-1-s0004:spoken_math_alternative",
                    "run_id": "replayed-run-75",
                    "page": 1,
                    "node": "page-1-s0004",
                    "field": "spoken_math_alternative",
                    "gold_wording": "I equals Q divided by delta t, which equals Q divided by t.",
                    "produced_wording": "I is charge over elapsed time.",
                    "reviewer_decision": None,
                }
            ],
        )

        item_id = result["adjudication_queue"][0]["id"]
        accepted = evaluate_prototype_fidelity(
            run,
            self.gold,
            reviewer_decisions={item_id: "meaning-equivalent"},
        )
        self.assertFalse(accepted["passed"], "other representative failures remain")
        self.assertEqual(
            accepted["adjudication_queue"][0]["reviewer_decision"],
            "meaning-equivalent",
        )

    def test_reviewer_decision_resolves_or_rejects_wording_adjudication(self) -> None:
        run = replayed_gold_run(self.gold)
        formula = next(
            node
            for node in run["pages"][0]["semantic_layer"]
            if node["type"] == "formula"
        )
        formula["spoken_math_alternative"] = "I is charge over elapsed time."
        item_id = "replayed-run-75:page-1-s0004:spoken_math_alternative"

        accepted = evaluate_prototype_fidelity(
            run, self.gold, reviewer_decisions={item_id: "meaning-equivalent"}
        )
        rejected = evaluate_prototype_fidelity(
            run, self.gold, reviewer_decisions={item_id: "not-meaning-equivalent"}
        )

        self.assertTrue(accepted["passed"])
        self.assertFalse(rejected["passed"])
        self.assertEqual(
            rejected["semantic_fidelity_failures"][0]["field"],
            "spoken_math_alternative",
        )

    def test_logical_reading_order_is_compared_explicitly(self) -> None:
        run = replayed_gold_run(self.gold)
        nodes = run["pages"][0]["semantic_layer"]
        paragraph_indices = [
            index for index, node in enumerate(nodes) if node["type"] == "paragraph"
        ]
        first, second = paragraph_indices[:2]
        nodes[first], nodes[second] = nodes[second], nodes[first]

        result = evaluate_prototype_fidelity(run, self.gold)

        self.assertEqual(
            result["semantic_fidelity_failures"][0]["field"],
            "logical_reading_order",
        )


if __name__ == "__main__":
    unittest.main()
