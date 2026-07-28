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
from tests.test_vision_prototype import FakeVisionProvider, POPPLER, PRICING, SOURCE


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
    regions = {region["id"]: region for region in gold["source_regions"]}
    dimensions = {
        item["page"]: (item["width_points"], item["height_points"])
        for item in gold["page_dimensions"]
    }
    for page in gold["pages"]:
        nodes: list[dict[str, Any]] = []
        for gold_node in (
            node for node in gold["semantic_layer"] if node["page"] == page
        ):
            node = {
                key: deepcopy(value)
                for key, value in gold_node.items()
                if key not in {"id", "page", "source_regions"}
            }
            gold_boxes = [
                regions[reference]["bbox_points"]
                for reference in gold_node["source_regions"]
            ]
            width, height = dimensions[page]
            node["boxes"] = [[
                min(box[0] for box in gold_boxes) / width,
                min(box[1] for box in gold_boxes) / height,
                max(box[2] for box in gold_boxes) / width,
                max(box[3] for box in gold_boxes) / height,
            ]]
            if node["type"] == "figure":
                node.setdefault("detailed_figure_description", None)
            if node["type"] == "table":
                node["boundaries_are_uncertain"] = False
                node["headers_are_uncertain"] = False
            nodes.append(node)
        if page == 1:
            first, second = nodes[0]["boxes"][0], nodes[1]["boxes"][0]
            shared_coarse_box = [
                min(first[0], second[0]),
                min(first[1], second[1]),
                max(first[2], second[2]),
                max(first[3], second[3]),
            ]
            nodes[0]["boxes"] = [shared_coarse_box]
            nodes[1]["boxes"] = [shared_coarse_box]
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


def geometry_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    gold = {
        "pages": [1],
        "page_dimensions": [
            {"page": 1, "width_points": 100.0, "height_points": 100.0}
        ],
        "source_regions": [
            {"id": "gold-a", "page": 1, "bbox_points": [10, 10, 20, 20]},
            {"id": "gold-b", "page": 1, "bbox_points": [30, 30, 40, 40]},
        ],
        "semantic_layer": [
            {
                "id": "page-1-s0001", "page": 1, "type": "paragraph",
                "text": "First", "source_regions": ["gold-a"],
            },
            {
                "id": "page-1-s0002", "page": 1, "type": "paragraph",
                "text": "Second", "source_regions": ["gold-b"],
            },
        ],
        "warnings": [],
    }
    run = {
        "run_id": "geometry-run",
        "pages": [{
            "page": 1,
            "page_dimensions": {"width_points": 100.0, "height_points": 100.0},
            "source_regions": [{
                "id": "page-1-r0001", "page": 1,
                "bbox_points": [5, 5, 45, 45],
            }],
            "semantic_layer": [
                {
                    "id": "page-1-s0001", "page": 1, "type": "paragraph",
                    "text": "First", "source_regions": ["page-1-r0001"],
                },
                {
                    "id": "page-1-s0002", "page": 1, "type": "paragraph",
                    "text": "Second", "source_regions": ["page-1-r0001"],
                },
            ],
            "warnings": [],
        }],
    }
    return run, gold


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
                pricing=PRICING,
            )
            replayed = replay_prototype_document(Path(directory) / "replayed-run-75")
        return replayed

    def test_valid_coarse_and_shared_geometry_passes(self) -> None:
        run, gold = geometry_documents()

        result = evaluate_prototype_fidelity(run, gold)

        self.assertTrue(result["passed"])
        self.assertEqual(result["geometry_failures"], [])
        self.assertEqual(result["visual_review_artifacts"], [])

    def test_center_miss_is_a_geometry_failure(self) -> None:
        run, gold = geometry_documents()
        run["pages"][0]["source_regions"][0]["bbox_points"] = [50, 50, 60, 60]

        result = evaluate_prototype_fidelity(run, gold)

        self.assertFalse(result["passed"])
        self.assertEqual(
            {(failure["node"], failure["rule"]) for failure in result["geometry_failures"]},
            {
                ("page-1-s0001", "gold-content-center-contained"),
                ("page-1-s0002", "gold-content-center-contained"),
            },
        )

    def test_malformed_and_duplicate_boxes_are_geometry_failures(self) -> None:
        run, gold = geometry_documents()
        run["pages"][0]["source_regions"].extend([
            {"id": "page-1-r0002", "page": 1, "bbox_points": [5, 5, 45, 45]},
            {"id": "page-1-r0003", "page": 1, "bbox_points": [20, 10, 10, 30]},
        ])

        result = evaluate_prototype_fidelity(run, gold)

        self.assertEqual(
            {(failure["source_region"], failure["rule"]) for failure in result["geometry_failures"]},
            {
                ("page-1-r0002", "deterministic-identity"),
                ("page-1-r0003", "finite-nonempty-contained"),
            },
        )

    def test_unjustified_near_whole_page_region_fails_without_iou_matching(self) -> None:
        run, gold = geometry_documents()
        run["pages"][0]["source_regions"][0]["bbox_points"] = [0, 0, 100, 90]

        result = evaluate_prototype_fidelity(run, gold)

        self.assertEqual(
            [failure["rule"] for failure in result["geometry_failures"]],
            ["unjustified-near-whole-page", "unjustified-near-whole-page"],
        )

    def test_distant_gold_regions_do_not_justify_near_whole_page_geometry(self) -> None:
        run, gold = geometry_documents()
        gold["source_regions"][0]["bbox_points"] = [0, 0, 10, 10]
        gold["source_regions"][1]["bbox_points"] = [90, 90, 100, 100]
        gold["semantic_layer"][0]["source_regions"] = ["gold-a", "gold-b"]
        run["pages"][0]["source_regions"][0]["bbox_points"] = [0, 0, 100, 100]

        result = evaluate_prototype_fidelity(run, gold)

        self.assertIn(
            "unjustified-near-whole-page",
            [failure["rule"] for failure in result["geometry_failures"]],
        )

    @unittest.skipUnless(POPPLER, "poppler is required for the 11-page replay")
    def test_replayed_run_categorizes_representative_geometry_failures(self) -> None:
        replayed = self.replay_gold()
        page = replayed["pages"][0]
        width = page["page_dimensions"]["width_points"]
        height = page["page_dimensions"]["height_points"]

        cases = {
            "center miss": ([width - 10, height - 10, width, height], "gold-content-center-contained"),
            "malformed": ([20, 20, 10, 30], "finite-nonempty-contained"),
            "near whole page": ([0, 0, width, height], "unjustified-near-whole-page"),
        }
        for name, (bounds, expected_rule) in cases.items():
            with self.subTest(name=name):
                run = deepcopy(replayed)
                run["pages"][0]["source_regions"][0]["bbox_points"] = bounds
                result = evaluate_prototype_fidelity(run, self.gold)
                self.assertIn(
                    expected_rule,
                    [failure["rule"] for failure in result["geometry_failures"]],
                )

    def test_only_semantically_matched_nodes_receive_geometry_checks(self) -> None:
        run, gold = geometry_documents()
        run["pages"][0]["semantic_layer"].insert(
            0,
            {
                "id": "extra-node", "page": 1, "type": "paragraph",
                "text": "Not in gold", "source_regions": ["page-1-r0001"],
            },
        )

        result = evaluate_prototype_fidelity(run, gold)

        self.assertEqual(result["geometry_failures"], [])
        self.assertFalse(result["passed"], "the extra node remains a semantic failure")

    def test_only_failed_geometry_produces_focused_visual_review_artifacts(self) -> None:
        run, gold = geometry_documents()
        with tempfile.TemporaryDirectory() as directory:
            passed = evaluate_prototype_fidelity(
                run, gold, visual_review_dir=Path(directory)
            )
            self.assertEqual(list(Path(directory).iterdir()), [])

            run["pages"][0]["source_regions"][0]["bbox_points"] = [50, 50, 60, 60]
            failed = evaluate_prototype_fidelity(
                run, gold, visual_review_dir=Path(directory)
            )

            self.assertEqual(len(failed["visual_review_artifacts"]), 2)
            artifact = Path(failed["visual_review_artifacts"][0])
            self.assertTrue(artifact.is_file())
            svg = artifact.read_text(encoding="utf-8")
            self.assertIn("Produced Source Region", svg)
            self.assertIn("Gold content", svg)
            self.assertIn("viewBox=\"5 5 60 60\"", svg)

    @unittest.skipUnless(POPPLER, "poppler is required for the 11-page replay")
    def test_replayed_eleven_page_gold_run_passes_deterministically(self) -> None:
        replayed = self.replay_gold()

        result = evaluate_prototype_fidelity(replayed, self.gold)

        self.assertEqual(
            {(warning["page"], warning["code"]) for warning in self.gold["warnings"]},
            {
                (1, "ambiguous-reading-order"),
                (3, "ambiguous-reading-order"),
                (3, "table-merged-cells"),
                (6, "ambiguous-reading-order"),
                (6, "suspected-source-error"),
                (7, "ambiguous-reading-order"),
            },
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["run_id"], "replayed-run-75")
        self.assertEqual(result["semantic_fidelity_failures"], [])
        self.assertEqual(result["warning_failures"], {"recall": [], "precision": []})
        self.assertEqual(result["adjudication_queue"], [])
        replayed_references = [
            reference
            for page in replayed["pages"]
            for node in page["semantic_layer"]
            for reference in node["source_regions"]
        ]
        self.assertLess(
            len(set(replayed_references)),
            len(replayed_references),
            "the replay covers valid shared geometry",
        )
        first_page = replayed["pages"][0]
        first_region_id = first_page["semantic_layer"][0]["source_regions"][0]
        first_region = next(
            region
            for region in first_page["source_regions"]
            if region["id"] == first_region_id
        )
        produced_box = first_region["bbox_points"]
        gold_region_id = self.gold["semantic_layer"][0]["source_regions"][0]
        gold_box = next(
            region["bbox_points"]
            for region in self.gold["source_regions"]
            if region["id"] == gold_region_id
        )
        self.assertGreater(
            (produced_box[2] - produced_box[0]) * (produced_box[3] - produced_box[1]),
            (gold_box[2] - gold_box[0]) * (gold_box[3] - gold_box[1]),
            "the replay covers valid coarse geometry",
        )

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

    def test_wording_change_does_not_mask_logical_reading_order_failure(self) -> None:
        run = replayed_gold_run(self.gold)
        formulas = [
            node
            for node in run["pages"][0]["semantic_layer"]
            if node["type"] == "formula"
        ]
        first_index = run["pages"][0]["semantic_layer"].index(formulas[0])
        second_index = run["pages"][0]["semantic_layer"].index(formulas[1])
        formulas[0]["spoken_math_alternative"] = "Changed wording."
        nodes = run["pages"][0]["semantic_layer"]
        nodes[first_index], nodes[second_index] = nodes[second_index], nodes[first_index]

        result = evaluate_prototype_fidelity(run, self.gold)

        self.assertIn(
            "logical_reading_order",
            {failure["field"] for failure in result["semantic_fidelity_failures"]},
        )

    def test_missing_alternative_is_a_semantic_failure_not_an_adjudication(self) -> None:
        run = replayed_gold_run(self.gold)
        formula = next(
            node
            for node in run["pages"][0]["semantic_layer"]
            if node["type"] == "formula"
        )
        formula["spoken_math_alternative"] = None

        result = evaluate_prototype_fidelity(run, self.gold)

        self.assertEqual(result["adjudication_queue"], [])
        self.assertEqual(
            result["semantic_fidelity_failures"][0]["field"],
            "spoken_math_alternative",
        )


if __name__ == "__main__":
    unittest.main()
