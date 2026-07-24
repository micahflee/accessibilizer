"""Tests for the vision-only prototype (issue #72).

These tests verify:
1. Box validation and normalization into Source Region identities
2. Model response schema validation
3. A replay test that simulates a fake provider returning valid data

The tests do NOT invoke real vision providers.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
import unittest
from typing import Any
from unittest.mock import patch

from accessibilizer.provider import ProviderConfig
from accessibilizer.vision_only import (
    PAGE_SEMANTICS_12_SCHEMA_VERSION,
    build_page_request,
    normalize_model_node,
    normalize_semantic_layer,
    reconstruct_page_vision_only,
    validate_and_normalize_boxes,
    validate_page_response_12,
)


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQ"
    "DJ/pLvAAAAAElFTkSuQmCC"
)


def create_temp_image() -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(PNG_BYTES)
    tmp.close()
    return Path(tmp.name)


class BoxNormalizationTest(unittest.TestCase):
    def test_valid_boxes_are_normalized_to_source_regions(self) -> None:
        boxes = [
            [0.1, 0.1, 0.5, 0.3],
            [0.5, 0.1, 0.9, 0.4],  
            [0.2, 0.5, 0.8, 0.9],  
        ]
        
        source_regions, validated = validate_and_normalize_boxes(
            boxes,
            page_width_points=612.0,
            page_height_points=803.25,
            source_region_base_id="page-1",
        )
        
        self.assertEqual(len(source_regions), 3)
        self.assertEqual(len(validated), 3)
        self.assertEqual(source_regions[0]["id"], "page-1-r0001")
        self.assertEqual(source_regions[1]["id"], "page-1-r0002")
        self.assertEqual(source_regions[2]["id"], "page-1-r0003")
        
    def test_shared_geometry_is_deduplicated(self) -> None:
        same_box = [0.2, 0.2, 0.6, 0.6]
        
        source_regions, validated = validate_and_normalize_boxes(
            [same_box, same_box],
            page_width_points=612.0,
            page_height_points=803.25,
            source_region_base_id="page-1",
        )
        
        self.assertEqual(len(source_regions), 1)
        self.assertEqual(len(validated), 2)
    
    def test_inverted_box_raises_error(self) -> None:
        with self.assertRaises(ValueError):
            validate_and_normalize_boxes(
                [[0.5, 0.5, 0.2, 0.2]],
                page_width_points=612.0,
                page_height_points=803.25,
                source_region_base_id="page-1",
            )
    
    def test_negative_coordinates_raises_error(self) -> None:
        with self.assertRaises(ValueError):
            validate_and_normalize_boxes(
                [[-0.1, 0.0, 0.5, 0.5]],
                page_width_points=612.0,
                page_height_points=803.25,
                source_region_base_id="page-1",
            )
    
    def test_out_of_page_bounds_raises_error(self) -> None:
        with self.assertRaises(ValueError):
            validate_and_normalize_boxes(
                [[0.0, 0.0, 1.1, 0.5]],
                page_width_points=612.0,
                page_height_points=803.25,
                source_region_base_id="page-1",
            )
    
    def test_non_finite_values_raises_error(self) -> None:
        with self.assertRaises(ValueError):
            validate_and_normalize_boxes(
                [[float('nan'), 0.0, 0.5, 0.5]],
                page_width_points=612.0,
                page_height_points=803.25,
                source_region_base_id="page-1",
            )
    
    def test_whole_page_fallback_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_and_normalize_boxes(
                [[0.0, 0.0, 0.98, 0.99]],
                page_width_points=612.0,
                page_height_points=803.25,
                source_region_base_id="page-1",
            )


class ModelNodeNormalizationTest(unittest.TestCase):
    def test_node_boxes_normalized_with_existing_regions(self) -> None:
        # Use exact converted values from normalize_model_node for matching
        source_regions_by_id: dict[str, dict[str, Any]] = {
            "page-1-r0001": {"id": "page-1-r0001", "page": 1,
                            "bbox_points": [61.2, 80.33, 306.0, 240.97]},
        }
        
        node = {
            "type": "paragraph",
            "text": "Test paragraph",
            "boxes": [[0.1, 0.1, 0.5, 0.3]],
        }
        
        normalized_node, region_ids = normalize_model_node(
            node,
            page_number=1,
            source_regions_by_id=source_regions_by_id,
            page_width_points=612.0,
            page_height_points=803.25,
        )
        
        self.assertEqual(normalized_node["type"], "paragraph")
        self.assertNotIn("boxes", normalized_node)
        self.assertEqual(region_ids, ["page-1-r0001"])
        
    def test_shared_geometry_creates_one_region(self) -> None:
        source_regions_by_id: dict[str, dict[str, Any]] = {}
        
        node1 = {
            "type": "heading",
            "level": 1,
            "text": "Heading",
            "boxes": [[0.2, 0.2, 0.6, 0.6]],
        }
        
        _, region_ids_1 = normalize_model_node(
            node1,
            page_number=1,
            source_regions_by_id=source_regions_by_id,
            page_width_points=612.0,
            page_height_points=803.25,
        )
        
        self.assertEqual(len(region_ids_1), 1)
        first_region_id = region_ids_1[0]
        
        node2 = {
            "type": "paragraph",
            "text": "Shared region",
            "boxes": [[0.2, 0.2, 0.6, 0.6]],
        }
        
        _, region_ids_2 = normalize_model_node(
            node2,
            page_number=1,
            source_regions_by_id=source_regions_by_id,
            page_width_points=612.0,
            page_height_points=803.25,
        )
        
        self.assertEqual(region_ids_2[0], first_region_id)


class SemanticLayerNormalizationTest(unittest.TestCase):
    def test_multiple_nodes_with_deduplication(self) -> None:
        # Use exact values that will match when converted
        source_regions_by_id: dict[str, dict[str, Any]] = {
            "page-1-r0001": {"id": "page-1-r0001", "page": 1,
                            "bbox_points": [61.2, 80.33, 306.0, 240.97]},
            "page-1-r0002": {"id": "page-1-r0002", "page": 1,
                            "bbox_points": [306.0, 80.33, 550.8, 321.3]},
        }
        
        nodes: list[dict[str, Any]] = [
            {
                "type": "heading",
                "level": 1,
                "text": "Electric Current",
                "boxes": [[0.1, 0.1, 0.5, 0.3]],
            },
            {
                "type": "paragraph", 
                "text": "Current is the flow of charge.",
                "boxes": [[0.5, 0.1, 0.9, 0.4]],
            },
        ]
        
        semantic_layer, final_source_regions = normalize_semantic_layer(
            nodes,
            source_regions_by_id,
            page_width_points=612.0,
            page_height_points=803.25,
        )
        
        self.assertEqual(len(semantic_layer), 2)
        self.assertIn("source_regions", semantic_layer[0])
        self.assertEqual(semantic_layer[0]["source_regions"], ["page-1-r0001"])
        self.assertEqual(semantic_layer[1]["source_regions"], ["page-1-r0002"])


class SchemaValidationTest(unittest.TestCase):
    def test_valid_model_response_passes_validation(self) -> None:
        valid_response = {
            "title": "Test Page",
            "language": "en-US",
            "primary_language_is_english": True,
            "document_class": "stem_instructional",
            "reading_order_is_unambiguous": True,
            "nodes": [
                {
                    "type": "heading",
                    "level": 1,
                    "text": "Heading Text",
                    "boxes": [[0.1, 0.1, 0.9, 0.2]],
                },
                {
                    "type": "formula",
                    "normalized_math": "E = mc^2",
                    "spoken_math_alternative": "E equals m c squared",
                    "boxes": [[0.3, 0.3, 0.7, 0.45]],
                },
            ],
            "suspected_source_errors": [],
        }
        
        validate_page_response_12(valid_response)
    
    def test_invalid_response_fails_validation(self) -> None:
        invalid_responses = [
            {
                "title": "",
                "language": "en",
                "primary_language_is_english": True,
                "document_class": "stem_instructional",
                "reading_order_is_unambiguous": True,
                "nodes": [],
                "suspected_source_errors": [],
            },
            {
                "title": "Test",
                "language": "en", 
                "primary_language_is_english": True,
                "document_class": "invalid_class",
                "reading_order_is_unambiguous": True,
                "nodes": [],
                "suspected_source_errors": [],
            },
        ]
        
        for invalid in invalid_responses:
            with self.assertRaises(ValueError):
                validate_page_response_12(invalid)


class ReconstructPageVisionOnlyTest(unittest.TestCase):
    def test_mock_page_reconstruction_succeeds(self) -> None:
        image_path = create_temp_image()
        
        try:
            mock_response = {
                "title": "Test Page from Model",
                "language": "en-US",
                "primary_language_is_english": True,
                "document_class": "stem_instructional",
                "reading_order_is_unambiguous": True,
                "nodes": [
                    {
                        "type": "heading",
                        "level": 1,
                        "text": "Electric Current",
                        "boxes": [[0.1, 0.1, 0.5, 0.3]],
                    },
                    {
                        "type": "paragraph",
                        "text": "Current is the flow of charge.",
                        "boxes": [[0.5, 0.1, 0.9, 0.4]],
                    },
                ],
                "suspected_source_errors": [],
            }
            
            config = ProviderConfig(
                base_url="http://localhost:11434/v1",
                model="test-model",
                api_key_env=None,
                data_location="local",
            )
            
            pdf_words = [
                {"text": "Electric", "bbox_points": [61.2, 80.325, 122.4, 96]},
                {"text": "Current", "bbox_points": [122.4, 80.325, 183.6, 96]},
            ]
            
            with (
                patch("accessibilizer.vision_only.request_chat_completion") as mock_request,
                patch("accessibilizer.provider.parse_schema_content") as mock_parse,
            ):
                mock_request.return_value = {
                    "choices": [{"message": {"content": json.dumps(mock_response)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 200},
                }
                mock_parse.return_value = mock_response
                
                result = reconstruct_page_vision_only(
                    config,
                    page=1,
                    source_sha256="a" * 64,
                    page_image=image_path,
                    pdf_words=pdf_words,
                    page_width_points=612.0,
                    page_height_points=803.25,
                )
            
            self.assertEqual(result["schema_version"], PAGE_SEMANTICS_12_SCHEMA_VERSION)
            self.assertEqual(result["page"], 1)
            self.assertEqual(result["title"], "Test Page from Model")
            self.assertIn("semantic_layer", result)
            self.assertIn("source_regions", result)
            
            self.assertEqual(len(result["semantic_layer"]), 2)
            for node in result["semantic_layer"]:
                self.assertNotIn("boxes", node)
                self.assertIn("source_regions", node)
            
            self.assertGreaterEqual(len(result["source_regions"]), 1)
            for region in result["source_regions"]:
                self.assertIsNotNone(region.get("bbox_points"))
                bbox = region["bbox_points"]
                self.assertEqual(len(bbox), 4)
                self.assertTrue(all(isinstance(v, (int, float)) for v in bbox))
        
        finally:
            image_path.unlink()


if __name__ == "__main__":
    unittest.main()
