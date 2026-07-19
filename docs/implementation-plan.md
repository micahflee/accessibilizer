# Accessibilizer Implementation Plan

Status: proposed

This plan implements the language in `CONTEXT.md` and the accepted decisions in `docs/adr/`. Each milestone must leave the repository testable. Work stops at a milestone boundary when its exit criteria are not met.

## Milestone 0: Repository and contract skeleton

- Add the AGPL-3.0-or-later license, Python and Java project skeletons, pinned toolchains, and canonical multi-architecture Docker build.
- Add a thin `accessibilizer` host launcher for macOS and Linux.
- Define versioned JSON Schema and Pydantic models for the Review Record, Conversion Provenance, semantic document tree, warnings, and resolutions.
- Establish unit, integration, golden-file, render-comparison, and container test lanes.

Exit criteria:

- The launcher runs the container on macOS and Linux-compatible paths.
- Python and Java exchange a minimal valid versioned JSON document.
- CI can build and test both components without an LLM credential.

## Milestone 1: PDF/UA visual-preservation spike

This spike resolves the highest-risk assumption before OCR or LLM integration.

- Import one native page from the sample PDF as artifact content without rasterizing the delivered page.
- Add a minimal ordered Semantic Layer with a heading, paragraph, Formula, Figure Alternative, and Detailed Figure Description.
- Embed required fonts, language, title, PDF/UA metadata, and bookmarks.
- Render source and output and perform a pixel-difference comparison.
- Validate with iText checks and veraPDF's PDF/UA-1 profile.
- Manually inspect the result with macOS Preview and VoiceOver.

Exit criteria:

- The output passes veraPDF PDF/UA-1 validation.
- The Visual Layer remains within the agreed rendering tolerance.
- VoiceOver reads only the intended Semantic Layer in the intended order.
- If native artifact content contaminates assistive output or cannot be tagged conformantly, stop and revise ADR 0002 before proceeding.

## Milestone 2: CLI, configuration, and Conversion Bundles

- Implement `config init` and `config check` with user, project, environment, and flag precedence.
- Implement the OpenAI-compatible capability check for vision input and JSON-Schema output.
- Implement interactive and noninteractive modes, remote-data consent, reviewer identity, structured status output, and exit codes 0, 1, and 2.
- Implement Source PDF preflight and safe rejection of protected or interactive inputs.
- Create protected Conversion Bundles with immutable source copies, SHA-256 verification, atomic checkpoints, and dependency-aware cache keys.
- Add request ceilings, usage accounting, bounded retries, and resume state.

Exit criteria:

- Every command works against a fake OpenAI-compatible server without network access.
- No remote payload is sent without interactive confirmation or `--allow-remote`.
- Interrupted work resumes without repeating valid completed stages.
- Existing bundles and reviewer edits cannot be overwritten accidentally.

## Milestone 3: Specialized recognition

- Pin PaddleOCR code and weights in the Docker image.
- Render source pages at deterministic resolutions for analysis only.
- Extract existing PDF text and geometry as non-authoritative evidence.
- Detect and recognize text or handwriting, Formulas, Informative Figures, Semantic Tables, and Document Structure candidates with stable source-region identifiers.
- Preserve raw candidates and confidence evidence without treating model self-confidence as acceptance.

Exit criteria:

- All 11 sample pages produce schema-valid, source-linked candidates.
- Formula, table, and figure regions can be inspected as stable crops in the Conversion Bundle.
- CPU-only processing completes without registry downloads or an LLM service.

## Milestone 4: Vision semantics and reconciliation

- Implement strict, versioned prompts and schemas for page structure, Logical Reading Order, transcription verification, Spoken Math Alternatives, table semantics, and figure descriptions.
- Use one page-level call plus required high-resolution Formula, table, disputed-region, and complex-figure calls.
- Reconcile existing PDF evidence, PaddleOCR candidates, page-level output, and crop-level output.
- Generate non-bypassable Conversion Warnings for disagreement, ambiguity, unsupported input, suspected source errors, weak figure grounding, and suspected prompt injection.
- Cache only schema-valid responses and record complete Conversion Provenance without credentials or hidden reasoning.

Exit criteria:

- The full sample can run against a deterministic fake provider and the configured `gpt-5.6-sol` baseline.
- Injected disagreements reliably produce warnings instead of silent replacement.
- No source-document instruction can obtain tools or alter pipeline control flow.

## Milestone 5: Review and finalization workflow

- Serialize the human-editable YAML Review Record and validate it against the canonical schema.
- Generate the WCAG 2.2 AA Review Report with source context, candidates, warnings, and resolution history.
- Implement `review`, `finalize`, and `validate`.
- Require explicit `corrected`, `accepted`, or reasoned `not_applicable` resolutions attributed to a Reviewer.
- Ensure finalization performs no OCR or LLM calls and preserves reviewer edits.

Exit criteria:

- Keyboard-only and screen-reader review is possible.
- Unresolved warnings block finalization.
- Corrected records rebuild deterministically without network access.
- Automated accessibility tests and a manual VoiceOver check cover the Review Report.

## Milestone 6: Complete PDF/UA authoring

- Map the semantic document tree to PDF/UA-1 tags for headings, paragraphs, lists, Formulas, figures, detailed descriptions, tables, links, and artifacts.
- Generate document title, language, heading hierarchy, bookmarks, and logical page navigation.
- Add invisible or nonvisual semantic content without changing the Visual Layer.
- Run internal semantic checks followed by veraPDF; treat structural validation failures as operational failures.
- Produce either an Accessible PDF or Review-Required PDF without making a false conformance claim.

Exit criteria:

- Every supported semantic node has a tested PDF representation.
- Exit 0 requires clean semantic checks and clean veraPDF PDF/UA-1 validation.
- Exit 2 is limited to reviewable semantic warnings.
- Visual regression tests cover all 11 pages.

## Milestone 7: Gold acceptance and release readiness

- Draft the complete gold Review Record for the supplied 11-page PDF.
- Obtain user approval of the record and preserve genuine ambiguities as explicit expectations.
- Require zero unflagged semantic errors, all expected warnings, clean rendering comparison, and clean PDF/UA-1 validation.
- Run the macOS Preview and VoiceOver checklist.
- Run and record the Windows Adobe Acrobat Reader and NVDA release checklist.
- Document installation, configuration, privacy behavior, review workflow, limitations, and experimental inputs.

Exit criteria:

- The accepted gold suite passes reproducibly from a clean Docker build.
- Required screen-reader checks pass with recorded application and OS versions.
- No broader accuracy claim is made until a larger representative corpus exists.

## Explicitly deferred

- Batch conversion.
- Native Anthropic support or a bundled Claude proxy.
- PDF/UA-2 and MathML output.
- Windows as a conversion host.
- Required commercial SDKs or hosted services.
- Forms, signed or encrypted PDFs, scripts, embedded media, and other interactive PDFs.
- General document classes, multilingual conversion, music notation, and arbitrary page layouts.
- Interactive browser-based editing beyond the YAML Review Record and read-only HTML Review Report.
- Telemetry and automatic crash uploads.
