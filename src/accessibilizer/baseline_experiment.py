"""Frozen, isolated vision-only baseline experiment for issue #77."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Mapping

from accessibilizer.prototype_evaluation import evaluate_prototype_fidelity
from accessibilizer.provider import ProviderConfig
from accessibilizer.review import load_yaml
from accessibilizer.vision_prototype import (
    PAGE_INSTRUCTIONS,
    SYSTEM_INSTRUCTIONS,
    PrototypePricing,
    prototype_page_response_schema,
    reconstruct_prototype_document,
    replay_prototype_document,
)


def _sha256(value: object) -> str:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    else:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class FrozenExperiment:
    revision: str
    identity: str
    model: str
    components: Mapping[str, str]
    pricing: PrototypePricing

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "identity": self.identity,
            "model": self.model,
            "components": dict(self.components),
            "pricing": self.pricing.as_dict(),
        }


_MODEL = "gpt-5.6-sol"
_PRICING = PrototypePricing(
    as_of="2026-07-27",
    input_per_million_tokens=1.0,
    output_per_million_tokens=10.0,
)
_REVISION = "vision-only-baseline-1"


def _load_gold(path: Path) -> dict[str, Any]:
    value = load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("gold Review Record must be an object")
    return value


def _evaluate(
    replayed: Mapping[str, Any], gold_path: Path, run_dir: Path
) -> dict[str, Any]:
    fidelity = evaluate_prototype_fidelity(
        replayed,
        _load_gold(gold_path),
        visual_review_dir=run_dir / "geometry-review",
    )
    page_artifacts = replayed.get("page_artifacts", [])
    schema_or_repair = [
        {"page": item.get("page"), "repair_reason": item.get("repair_reason")}
        for item in page_artifacts
        if isinstance(item, dict) and item.get("repaired")
    ]
    checks = replayed.get("run_report", {}).get("checks", {})
    resource_limit = [name for name, passed in checks.items() if not passed]
    failures = {
        "semantic": fidelity["semantic_fidelity_failures"],
        "warning": fidelity["warning_failures"],
        "geometry": fidelity["geometry_failures"],
        "schema_or_repair": schema_or_repair,
        "resource_limit": resource_limit,
    }
    result = {
        "run_id": replayed["run_id"],
        "experiment": FROZEN_EXPERIMENT.as_dict(),
        "passed": fidelity["passed"] and not schema_or_repair and not resource_limit,
        "failures": failures,
        "adjudication_queue": fidelity["adjudication_queue"],
        "visual_review_artifacts": fidelity["visual_review_artifacts"],
    }
    (run_dir / "evaluation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def run_frozen_baseline_experiment(
    config: ProviderConfig,
    *,
    source_pdf: Path,
    gold_review_record: Path,
    artifacts_root: Path,
    run_id: str,
    allow_remote: bool = False,
    include_native_pdf_context: bool = True,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Run the exact frozen revision and write its categorized evaluation."""
    if config.model != FROZEN_EXPERIMENT.model:
        raise ValueError(f"frozen baseline model must be {FROZEN_EXPERIMENT.model}")
    if config.data_location == "remote" and not allow_remote:
        raise PermissionError("remote Source PDF transmission requires allow_remote=True")
    reconstruct_prototype_document(
        config,
        source_pdf=source_pdf,
        artifacts_root=artifacts_root,
        pricing=FROZEN_EXPERIMENT.pricing,
        run_id=run_id,
        include_native_pdf_context=include_native_pdf_context,
        max_retries=max_retries,
        experiment=FROZEN_EXPERIMENT.as_dict(),
    )
    return replay_frozen_baseline_experiment(
        artifacts_root / run_id, gold_review_record
    )


def replay_frozen_baseline_experiment(
    run_dir: Path, gold_review_record: Path
) -> dict[str, Any]:
    """Replay and evaluate only artifacts bound to this frozen revision."""
    replayed = replay_prototype_document(run_dir)
    if replayed.get("experiment") != FROZEN_EXPERIMENT.as_dict():
        raise ValueError("run uses a different frozen experiment revision")
    return _evaluate(replayed, gold_review_record, run_dir)


def _implementation_hash(module_name: str) -> str:
    return hashlib.sha256(Path(__file__).with_name(module_name).read_bytes()).hexdigest()


_VISION_IMPLEMENTATION = _implementation_hash("vision_prototype.py")
_EVALUATION_IMPLEMENTATION = _implementation_hash("prototype_evaluation.py")
_COMPONENTS = {
    "model_identifier": _sha256(_MODEL),
    "prompt": _sha256({"system": SYSTEM_INSTRUCTIONS, "page": PAGE_INSTRUCTIONS}),
    "strict_schema": _sha256(prototype_page_response_schema()),
    "normalizer": _VISION_IMPLEMENTATION,
    "semantic_warning_evaluator": _EVALUATION_IMPLEMENTATION,
    "geometry_evaluator": _EVALUATION_IMPLEMENTATION,
    "resource_evaluator": _VISION_IMPLEMENTATION,
    "pricing_basis": _sha256(_PRICING.as_dict()),
    "run_procedure": _sha256(
        inspect.getsource(run_frozen_baseline_experiment)
        + inspect.getsource(replay_frozen_baseline_experiment)
        + inspect.getsource(_evaluate)
        + inspect.getsource(_load_gold)
    ),
}
_FROZEN_COMPONENTS = {
    "model_identifier": "419255f2bb4c6801939e868fe2cdbbdc34d51742400939d1842d262a1fec749c",
    "prompt": "e22fbd9504d895beddc7cecf7caa41775ffc4a1e9d51143c9e71d376751254fd",
    "strict_schema": "04ace2803722202c777c67d2ecec05b2a016e3ed306870b21d40c2c308f0c006",
    "normalizer": "139c871884d68847378db4ffbf4481ad9ab5b5484238633819a875119b23fa51",
    "semantic_warning_evaluator": "30acf7803eb0a706cf1ad31b5d463c7906d371e9989f72d9c6af1adabefea7ab",
    "geometry_evaluator": "30acf7803eb0a706cf1ad31b5d463c7906d371e9989f72d9c6af1adabefea7ab",
    "resource_evaluator": "139c871884d68847378db4ffbf4481ad9ab5b5484238633819a875119b23fa51",
    "pricing_basis": "58e4449c93f11c4adc13554c448e7c459e67700776c0eee00ede10384dfb5550",
    "run_procedure": "828fa4de31ee16dd1b1ba110275798999fb543ce816f30726c3ade247f3a2ed4",
}
if _COMPONENTS != _FROZEN_COMPONENTS:
    raise RuntimeError(
        "frozen baseline implementation changed; create a new experiment revision"
    )
_CALCULATED_IDENTITY = _sha256(
    {"revision": _REVISION, "components": _COMPONENTS}
)
_FROZEN_IDENTITY = "4bfd496ef022298e659e6a36c9eeabef3b2b99a0646a695273da6df329100584"
if _CALCULATED_IDENTITY != _FROZEN_IDENTITY:
    raise RuntimeError("frozen baseline identity does not match its component set")
FROZEN_EXPERIMENT = FrozenExperiment(
    revision=_REVISION,
    identity=_FROZEN_IDENTITY,
    model=_MODEL,
    components=_COMPONENTS,
    pricing=_PRICING,
)
