# Recognition's role and handwritten Source Region geometry

Research for [issue #58](https://github.com/micahflee/accessibilizer/issues/58), 2026-07-22.

## Decision

Choose **(c) a hybrid**, but not the current hybrid.

1. Generate **type-neutral Source Regions deterministically** from the rendered page, native PDF word geometry, and low-level recognition geometry. The model may select the resulting Source Region IDs but may not emit coordinates.
2. Retain PaddleOCR Recognition Candidates as non-authoritative, independent evidence. Separate layout confidence from OCR transcription confidence and gate whether a Candidate is eligible to trigger a disagreement.
3. Give the page model an original page image plus an ID overlay (or equivalent region thumbnails), so the model can visually bind a Semantic Layer node to deterministic Source Regions.
4. Keep implausible Candidates in the Review Record and Conversion Provenance, with their ineligibility reasons, but do not allow them to create recognition-disagreement warnings.

This preserves the independent-specialized-recognition decision in [ADR 0010](../adr/0010-use-independent-specialized-and-vision-recognition-passes.md), the default-backend seam in [ADR 0019](../adr/0019-use-paddleocr-as-the-default-specialized-backend.md), and the prohibition on model-invented geometry in [ADR 0028](../adr/0028-separate-source-evidence-from-semantic-interpretation.md). It addresses the problem that the current Paddle layout output is neither sufficiently precise nor sufficiently complete to be the sole source of geometry on the handwritten sample.

In the implementation design below, lowercase **region proposal** names only a transient geometry-algorithm intermediate, not a new Review Record or glossary entity. After deterministic normalization, plausibility checks, and deduplication, each accepted box is materialized as a canonical Source Region and receives its stable Source Region ID. The model-visible overlay and schema contain those Source Region IDs; there is no separate proposal identity or lifecycle in the domain model.

## Answers to the spike questions

### Is handwriting a v1 target?

Yes. `Supported Source PDF` explicitly includes printed or handwritten prose, equations, diagrams, and tables in [CONTEXT.md](../../CONTEXT.md), and [ADR 0004](../adr/0004-limit-version-one-to-english-stem-instructional-material.md) names the handwritten sample as the v1 exemplar. Treating handwriting as experimental would reverse accepted scope rather than resolve this issue.

### What is recognition for?

Recognition has two distinct jobs that the current implementation conflates:

- **Geometry proposal:** locate visual evidence from which canonical Source Regions can be constructed.
- **Independent verification:** provide a non-authoritative interpretation that can corroborate or contradict the model's Semantic Layer reconstruction.

A Recognition Candidate must not be treated as a Source Region. That distinction is already canonical in [CONTEXT.md](../../CONTEXT.md) and accepted by [ADR 0028](../adr/0028-separate-source-evidence-from-semantic-interpretation.md). The current migration path still creates one Source Region per Paddle candidate box and maps one Candidate back to it in [`_review_page_document`](../../src/accessibilizer/cli.py#L605). That temporary one-to-one mapping lets a bad layout classification corrupt both geometry and verification.

Recognition should remain the independent-verification leg. Its geometry may contribute proposals, but recognition type and text must not define canonical Source Region boundaries or make a Candidate warning-eligible by themselves.

### Can the vision model supply geometry?

It can emit syntactically valid coordinate objects, but that is not enough to make the coordinates trustworthy. OpenAI's Structured Outputs documentation says schema conformance does not prevent mistakes in field values, even though Structured Outputs works with vision inputs ([OpenAI, "Introducing Structured Outputs"](https://openai.com/index/introducing-structured-outputs-in-the-api/)). This repository also supports multiple OpenAI-compatible providers and exact model identifiers ([ADR 0011](../adr/0011-require-a-vision-and-schema-capable-openai-compatible-provider.md), [ADR 0015](../adr/0015-require-an-explicit-and-recorded-model.md)); coordinate quality would therefore be a per-model capability requiring a separate gold evaluation, not a portable provider guarantee.

More importantly, model-authored coordinates directly conflict with accepted [ADR 0028](../adr/0028-separate-source-evidence-from-semantic-interpretation.md): models may select deterministic regions but may not invent geometry. Reversing that decision is unnecessary. A model can perform the semantic binding while deterministic code owns the coordinates.

No remote coordinate-generation call was made during this spike. [ADR 0012](../adr/0012-allow-remote-models-with-explicit-run-time-consent.md) says provider configuration is not consent, and the requested run-time consent was not supplied. The empirical work instead tests the current deterministic geometry against the gold fixture. A provider-specific coordinate benchmark would still be required before any future proposal to reverse ADR 0028.

The current request does not even make ID selection spatially identifiable. [`_evidence_json`](../../src/accessibilizer/page.py#L325) sends only each Candidate's ID, type, and text plus a flat list of Source Region IDs. It omits bounding boxes, confidence, crops, and an ID overlay. The page image contains no region IDs. Thus the model can understand the page and still have no reliable way to associate an opaque `page-N-rNNNN` identifier with a visible item. The page-1 correct heading transcription attached to the page-sized formula region is consistent with this contract defect.

### How should recognition disagreements be gated?

Gate on **verification eligibility**, not a single confidence threshold. Eligibility should be a deterministic, auditable decision made before reconciliation:

- the Candidate references a nonfallback Source Region;
- the Source Region passed geometry plausibility checks and is visually bound to the Semantic Layer node being checked;
- the Candidate's recognition type is compatible with that node;
- the backend supplied the relevant confidence signal, with layout-class confidence and OCR text confidence retained separately;
- the recognized content is sufficiently specific for the comparison being made (for example, prose-heavy OCR spanning a quarter-page is not specialized Formula evidence);
- the comparison has enough signal under the existing token rules.

Only an eligible independent Candidate may trigger `recognition-disagreement` or `formula-recognition-disagreement`. An ineligible Candidate remains review evidence, including why it was gated out. Missing required independent evidence is not a disagreement, but it also must not silently pass as agreement: record verification coverage, and raise a distinct insufficient-verification warning when the required coverage policy for prose, Formula, Semantic Table, or complex Informative Figure content is unmet. The follow-up must produce enough eligible evidence on the gold sample to avoid that warning rather than merely suppressing noisy Candidates.

## Empirical sample evaluation

### Method

The pinned, offline `accessibilizer:0.1.0` image (`sha256:13631063c53665ae72545b0ac801877c7086f341ab2bafdb25580e42beb3fe27`) ran the repository's real `PaddleBackend` over all 11 pages at 300 DPI. The Source PDF SHA-256 was `fe203e79ddc803a7eaa4e401222a186b861c6b04ed632c865a2eaad858f2c077`. Candidate boxes were compared in PDF-point coordinates with the 119 hand-authored Source Regions in [`testdata/gold-review-record.yaml`](../../testdata/gold-review-record.yaml). For every gold region and every Candidate, the analysis calculated intersection-over-union (IoU), gold-region containment, and Candidate-to-gold area ratio.

The gold Review Record is still a draft pending maintainer approval in [issue #15](https://github.com/micahflee/accessibilizer/issues/15). It is nevertheless the repository's current acceptance oracle under [ADR 0020](../adr/0020-use-the-sample-as-the-first-end-to-end-acceptance-gate.md), so these measurements are useful but their expected-warning labels remain provisional.

### Results

| Observation | Result |
|---|---:|
| Paddle Candidates | 53 |
| Candidate types | 31 Formula, 11 synthesized Document Structure, 8 text, 2 figure, 1 table, **0 handwriting** |
| Candidates without confidence | 15 |
| Candidate area at least 25% / 50% / 75% of page | 25 / 15 / 8 |
| Gold Source Regions whose best Candidate IoU is at least 0.3 / 0.5 | 25 / 7 of 119 |
| Gold Source Regions at least 80% contained without Candidate area exceeding 2x gold | 5 of 119 |
| Candidates whose best gold-region IoU is at least 0.5 | 8 of 53 |
| Candidates combining area at least 25% with OCR confidence at least 0.7 | 14 |

Page 1 reproduces the issue directly. `page-1-r0001` is classified as Formula, covers 87.53% of the page (`[32.64, 6.24, 586.32, 783.36]` points), has reported confidence `0.8203`, and has best gold-region IoU `0.170`. The adapter's `confidence` is the mean confidence of recognized text lines in [`_paddle_region_text`](../../src/accessibilizer/recognition.py#L204), not the layout detector's class confidence. The synthesized Document Structure Candidate then duplicates the union of detected boxes in [`_page_structure_candidate`](../../src/accessibilizer/recognition.py#L178), so it cannot provide independent tighter geometry.

These data establish three points:

1. **Confidence-only gating is invalid.** Fourteen broad Candidates have apparently good OCR confidence; page 1 is the clearest example. OCR text confidence does not answer whether the layout class or extent is plausible.
2. **Area gating is necessary but insufficient.** Rejecting boxes at or above the gold fixture's existing 80% page-area limit would catch the page-1 whole-page box, but current Candidate geometry has IoU at least 0.5 for only 7 of 119 gold regions. Thresholding cannot manufacture the missing Source Regions.
3. **The evidence indicates that Paddle's layout labels are out of domain on this sample.** The backend emits no handwriting Candidates and labels most output Formula. Paddle's own PP-Structure v2 model list says its English layout model is trained on PubLayNet and recognizes only Text, Title, Table, Picture, and List regions ([PaddleOCR v2 model list](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version2.x/ppstructure/models_list.en.md)). Paddle now labels PP-StructureV2 as maintenance-bound for discontinuation and recommends PP-StructureV3 ([PaddleOCR PP-Structure upgrade notice](https://github.com/PaddlePaddle/PaddleOCR/blob/main/ppstructure/README.md)). An eventual v3 evaluation is warranted, but upgrading does not remove the need to separate Source Region geometry from Recognition Candidates.

### Warning target

Issue #58 reports 102 warnings, but its enumerated recognition/grounding counts total 96: 49 `formula-recognition-disagreement`, 34 `recognition-disagreement`, and 13 `imprecise-source-grounding`. The issue does not classify the remaining six. Separately, the draft gold Review Record contains six expected concerns and no recognition-derived concern:

- page 1: `ambiguous-reading-order`
- page 3: `ambiguous-reading-order`, `table-merged-cells`
- page 6: `ambiguous-reading-order`, `suspected-source-error`
- page 7: `ambiguous-reading-order`

The measurable target for the follow-up is therefore:

- **0 false-positive recognition-derived warnings** on this sample;
- **all 6 provisional expected concerns retained**, yielding exactly 6 warnings with the current draft oracle;
- once issue #15 approves or changes the oracle, exact warning-multiset equality with that approved revision supersedes the provisional number.

This target does not permit warning suppression to hide semantic errors. [ADR 0020](../adr/0020-use-the-sample-as-the-first-end-to-end-acceptance-gate.md) still requires zero unflagged semantic errors.

## Options considered

### (a) Keep PaddleOCR and add plausibility gating

Keep part of this option, but reject it as the whole solution.

It is appropriate to gate disagreement eligibility using area, type compatibility, the correct confidence signal, and content shape. That would remove obviously invalid comparisons. It cannot solve grounding geometry: 112 of 119 gold Source Regions lack a Paddle Candidate with IoU at least 0.5. A threshold-only patch would trade false warnings for whole-page fallbacks or incorrectly attached surviving boxes.

### (b) Let the model emit Source Region coordinates

Reject for v1.

It violates ADR 0028, ties correctness to a provider-specific and model-specific localization capability, and confuses schema-valid numbers with accurate geometry. It would also make the semantic reconstruction model the source of both meaning and its purported visual evidence, weakening audit independence.

### (c) Hybrid: deterministic geometry, model selection, gated recognition

Recommend.

The key is that "hybrid" must mean independent seams rather than a Paddle box used simultaneously as geometry, type, text, and warning authority.

## Implementation-ready design

### 1. Add a deterministic Source Region proposal stage

Build a proposal set without assigning Semantic Layer meaning:

1. Start with native PDF word boxes when present. Poppler represents word bounding boxes in PDF points ([Poppler `TextBox`](https://poppler.freedesktop.org/api/qt5/classPoppler_1_1TextBox.html)); the repository already extracts these through `pdftotext -bbox` in [`recognize_page`](../../src/accessibilizer/recognition.py#L450).
2. Preserve Paddle layout boxes, OCR line polygons, table-cell geometry, and Formula geometry as separate proposal sources. Do not merge their recognition labels into Source Region identity.
3. Add raster-derived proposals from the rendered page: threshold foreground ink, use morphology to join nearby strokes into lines/blocks, and extract component bounds at more than one scale. OpenCV documents morphology as shape-based binary-image operations and notes that dilation can join broken parts ([OpenCV morphology documentation](https://docs.opencv.org/master/d9/d61/tutorial_py_morphological_ops.html)).
4. Normalize to displayed-page points, clip, deduplicate near-identical boxes, and retain parent/child proposals where both are useful. Reject any nonfallback proposal at or above 80% of page area, matching the existing gold geometry invariant in [`test_source_regions_have_tight_in_page_geometry`](../../tests/test_gold_review_record.py#L150).
5. Always retain `page-N-r0000` only as the explicit whole-page fallback. It is never verification-eligible.

The proposal algorithm should be deterministic for a fixed source hash, DPI, algorithm version, and parameters, all recorded in Conversion Provenance.

### 2. Make Source Region IDs visually selectable

Send the model:

- the unmodified rendered page;
- a deterministic overlay labeling each Source Region ID (short labels can map to full IDs in JSON);
- Source Region metadata containing ID and bounds, but no authoritative semantic type;
- recognition text/type as explicitly non-authoritative Candidate evidence.

Keep the response schema ID-only. The model selects one or more existing same-page Source Regions for every Semantic Layer node; it cannot return coordinates or unknown IDs. This directly implements ADR 0028 and fixes the current opaque-ID request.

If the proposal count exceeds a provider's practical image or schema budget, use deterministic page partitions and region thumbnails in a second binding request. Do not silently fall back based only on token pressure.

### 3. Separate geometry provenance from recognition evidence

Each Source Region records only canonical geometry. The geometry-generation algorithm and version belong in Conversion Provenance. Each Recognition Candidate has its own Candidate ID, references exactly one Source Region, and retains:

- backend/component name and version;
- raw layout class and layout-class confidence, if supplied;
- recognized text and OCR text confidence, if supplied;
- verification-eligibility boolean and stable reason codes.

Do not synthesize Document Structure as a Recognition Candidate spanning the union of page regions. Document Structure belongs in the Semantic Layer; reading-order geometry is the ordered set of selected Source Regions.

### 4. Gate reconciliation

Refactor reconciliation so:

- region-crop verification first verifies that the model's node-to-region binding describes the same visible content;
- specialized disagreement runs only for eligible Candidates attached to those regions;
- Formula comparison requires a Formula-eligible Candidate rather than any region classified Formula;
- page-level prose grounding uses only eligible text/handwriting Candidates and native PDF text evidence;
- ineligible Candidates never generate disagreement warnings but remain visible to a Reviewer;
- the Review Record reports aggregate eligible-verification coverage by type for evaluation;
- missing evidence required by the coverage policy produces a distinct insufficient-verification warning instead of being mislabeled as disagreement or silently accepted.

Thresholds and reason codes must be calibrated against the gold sample, committed as named constants, and tested at their boundaries. Do not treat a mean OCR confidence as layout plausibility.

### 5. Evaluate a newer specialized backend separately

Run the same proposal-recall and warning-precision harness against PP-StructureV3 before changing the pinned default. Paddle's first-party documentation recommends v3 over v2, but ADR 0019 requires pinned offline weights and a stable adapter; a version change therefore needs its own reproducibility, image-size, license, performance, and acceptance evidence. The hybrid contract means such a backend evaluation no longer blocks fixing geometry.

## Acceptance criteria for the follow-up

1. On the 11-page sample, the final warning `(page, code)` multiset exactly matches the issue-#15-approved gold oracle; while #15 is open, the provisional target is the six concerns listed above.
2. There are zero `recognition-disagreement`, `formula-recognition-disagreement`, `imprecise-source-grounding`, or insufficient-verification warnings unless the approved oracle explicitly expects one; this must be achieved by meeting verification coverage, not suppressing the warning.
3. Every one of the 115 gold Semantic Layer nodes selects at least one nonfallback same-page Source Region.
4. For every gold Source Region, the generated Source Region set contains a region with either IoU at least 0.5, or at least 80% gold containment with generated-region area no more than 2x gold area. This turns the empirical comparison into a regression test rather than accepting the current 5-of-119 useful-containment result.
5. Selected Source Regions satisfy the existing finite, contained, same-page, and less-than-80%-page-area invariants.
6. Broad high-OCR-confidence false Candidates, including the page-1 87.53% Formula box at confidence 0.8203, are retained but ineligible and create no disagreement warning.
7. A plausible localized Candidate that truly contradicts a reconstruction still produces the relevant Conversion Warning, proving the independent-verification leg was not disabled.
8. Every content class covered by the verification policy has eligible independent evidence on the sample; a negative fixture without such evidence produces the distinct insufficient-verification warning.
9. Verification-eligibility reason codes, proposal algorithm version, backend versions, and both confidence kinds round-trip through checkpoints and Conversion Provenance without exposing hidden model reasoning.
10. The full sample still meets ADR 0020's zero-unflagged-semantic-error requirement and existing PDF/UA, visual, and Review Record validation gates.

## Follow-up implementation issue

Created as [issue #60](https://github.com/micahflee/accessibilizer/issues/60).

**Title:** Decouple deterministic Source Region proposals from Paddle recognition and gate disagreement warnings

**Scope:**

- introduce the deterministic, type-neutral proposal stage and versioned checkpoint;
- add visually identifiable Source Region IDs to the page-model request while keeping the response ID-only;
- preserve distinct Source Region and Recognition Candidate identities;
- retain layout confidence separately from OCR text confidence;
- add verification-eligibility reason codes and gate reconciliation;
- remove synthesized union-box Document Structure Candidates;
- add the sample proposal-recall, node-binding, warning-multiset, and true-disagreement regression tests described above.

**Out of scope:** switching the default backend to PP-StructureV3, changing the approved v1 document domain, allowing model-authored coordinates, or weakening the zero-unflagged-semantic-error acceptance gate.

## Residual risks

- Raster proposal parameters can over-segment handwriting or merge adjacent columns. Multi-scale proposals plus multi-region node references reduce this risk, but the gold geometry regression must choose parameters.
- An ID overlay can obscure source ink. Send the original and overlay separately, and test binding against the gold node-region relationships.
- The same vision model performs reconstruction and region-binding checks, so that binding step is not an independent content recognizer. Independence comes from specialized Recognition Candidates and native PDF evidence; the model's binding task only selects deterministic visual evidence.
- The sample is the v1 acceptance gate, not a representative accuracy corpus. Passing it supports the stated release milestone but not broader handwriting claims.
