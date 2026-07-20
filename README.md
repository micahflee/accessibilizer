# Accessibilizer

Accessibilizer turns visually readable Source PDFs into documents whose Visual Layer is preserved and whose Semantic Layer can be consumed by assistive technology.

The current implementation reconstructs the page's Semantic Layer from the Source PDF itself, imports one native source page as artifact content, then gates the result on internal checks, visual comparison, and veraPDF's PDF/UA-1 profile. It also resolves and verifies an exact OpenAI-compatible vision provider before Source PDF work begins.

It additionally produces reproducible, source-linked recognition evidence for the page: a pinned CPU-only PaddleOCR pass detects text or handwriting, Formula, table, figure, and Document Structure candidates, and the existing PDF text layer is retained with its geometry only as non-authoritative evidence. Every candidate receives a stable identifier and a source-region crop.

A single strict, versioned page-level vision call then reconstructs the page's meaning — its title, language, and the ordered heading, paragraph, Formula, and Informative Figure Semantic Layer in Logical Reading Order — and required high-resolution crop calls verify the Formula, table, and Figure regions. The PDF text layer and the PaddleOCR candidates are reconciled against that reconstruction without silently replacing it: disagreement, ambiguity, unsupported static inputs, suspected source errors, and suspected prompt injection each raise a non-bypassable Conversion Warning. Source document content is treated as untrusted data — requests never expose tools, the reply is constrained to a strict JSON Schema, and no field of the source is interpreted as a control instruction.

Two [macOS Preview and VoiceOver validation](docs/validation/2026-07-19-macos-preview-voiceover.md) sessions ([second session](docs/validation/2026-07-19-macos-preview-voiceover-2.md)) found clipped text and missing Figure Alternative and Detailed Figure Description content even though the Visual Layer and automated PDF/UA checks passed, because Preview derived accessibility text from the glyphs laid out inside the one-point-wide, zero-opacity overlay. ADR 0026 rejected that overlay. The Semantic Layer is now authored as full-size text drawn with text rendering mode 3, which produces no marks on screen or in the print path (ADR 0027), so the glyphs Preview reads spell the complete heading, paragraph, and Formula strings in Logical Reading Order; the Figure carries its Alternative in the image alternate text and its Detailed Figure Description on a sibling caption. A [recorded macOS Preview and VoiceOver session](docs/validation/2026-07-20-macos-preview-voiceover-3.md) confirmed VoiceOver reads all four nodes — heading, paragraph, Formula, and Figure with both its Alternative and Detailed Figure Description — in the intended Logical Reading Order, satisfying the acceptance gate for issue #3.

## Build and convert

Build the canonical runtime:

```sh
docker build --tag accessibilizer:0.1.0 .
```

Run the public host launcher:

```sh
./accessibilizer convert \
  "testdata/Chapter 20_ Electric Current Resistance and Ohms Law.pdf" \
  --page 1 \
  --bundle electric-current.accessibilizer \
  --provider-base-url http://localhost:11434/v1 \
  --provider-model exact-model-identifier \
  --provider-data-location local \
  --json
```

The launcher supports macOS and Linux paths and keeps Docker as an implementation detail. It refuses to overwrite an existing Conversion Bundle.
Pass `--replace` to explicitly authorize replacement. Accessibilizer builds the replacement in a protected staging directory and leaves the existing bundle, including Reviewer edits, untouched if conversion fails.

Interrupted or paused work remains in a protected sibling directory named `.BUNDLE.in-progress`.
Repeat the same command with `--resume` to continue it. Completed source, region, recognition, page-semantics, page, and validation stages are reused only when their dependency key and artifact hashes remain valid. Changing a Source PDF, tool version, schema, prompt, model, or relevant rendering setting invalidates the stages that depend on it; changing the provider model or a prompt or schema version re-runs page-semantics reconstruction and everything downstream.

Before conversion, Accessibilizer rejects encrypted or digitally signed Source PDFs and PDFs containing forms, JavaScript, embedded files or media, links, or other interactive actions that this version cannot preserve safely.

With `--json`, every outcome is machine-readable. Exit `0` reports `accessible`, exit `2` reports `review_required` when unresolved Conversion Warnings remain, and exit `1` reports `operational_failure`.

## Provider configuration and consent

Provider settings resolve in this order, from lowest to highest precedence:

1. User configuration at `${XDG_CONFIG_HOME:-~/.config}/accessibilizer/config.toml`
2. Project configuration at `./accessibilizer.toml`
3. CLI flags

Both TOML files use the same shape:

```toml
[provider]
base_url = "https://api.openai.com/v1"
model = "gpt-5.6-sol"
api_key_env = "OPENAI_API_KEY"
data_location = "remote"

[conversion]
max_requests = 100
provider_max_retries = 3
provider_retry_base_seconds = 0.5
provider_retry_max_seconds = 8.0
```

`base_url` and `model` are required after the layers are merged. The corresponding flags are `--provider-base-url`, `--provider-model`, `--provider-api-key-env`, and `--provider-data-location`. The model must be an explicit, exact identifier; explicit `latest` aliases are rejected. URL credentials, queries, and fragments are rejected so the recorded endpoint cannot capture a query-string secret.

For the initial hosted OpenAI quality baseline, use `gpt-5.6-sol`, the official identifier for the flagship GPT-5.6 variant. Do not configure the shorter `gpt-5.6` routing alias when exact model selection and reproducible Conversion Provenance are required. See OpenAI's [GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model).

`api_key_env` names an environment variable; it does not contain the secret. The host launcher forwards only that named variable into the canonical container. Credentials are never written to TOML or Conversion Provenance. Providers that do not require a key may omit it.

`data_location` is `local` or `remote`. When omitted, loopback URLs default to local and every other or uncertain endpoint defaults to remote. For the canonical Docker runtime, loopback provider requests are routed to the host through Docker's `host.docker.internal` gateway while provenance retains the configured loopback URL. A remote run prompts for per-run confirmation in an interactive terminal. Automation must pass `--allow-remote`; configuration and credentials alone never authorize transmission. There is no provider fallback.

After authorization, Accessibilizer sends a small base64 capability image to `POST /chat/completions` beneath the configured base URL and requires a strict JSON-Schema answer derived from its visible content. A provider that cannot use the image and satisfy the response schema fails before the Source PDF is copied, rendered, inspected, or converted. Conversion Provenance records the resolved base URL, exact model, and data location, but not credentials, environment-variable names, request dumps, or hidden reasoning.

Transient timeouts, connection failures, rate limits, and server errors receive bounded exponential-backoff retries. The pipeline estimates the provider requests it will make — the capability check when needed, plus one page-level reconstruction call and one call per verified Formula, table, and Figure crop — counting zero for any stage whose checkpoint can be reused, then enforces `max_requests` before every attempt, including retries. Use `--max-requests`, `--provider-max-retries`, `--provider-retry-base-seconds`, and `--provider-retry-max-seconds` to override the layered settings. Reaching the request ceiling pauses the Conversion Bundle instead of exceeding it; raise the ceiling and pass `--resume` to continue. Conversion Provenance retains estimated and actual request counts and any prompt, completion, and total token usage reported by the provider. Accessibilizer does not estimate dollar cost.

## Conversion Bundle

The generated protected directory contains:

- `source.pdf`: immutable copy of the Source PDF
- `output.pdf`: PDF/UA-1 output
- `review-record.yaml`: the human-editable Review Record — the reconstructed Semantic Layer, the retained recognition candidates, the Conversion Warnings with their resolution history, and reconstruction provenance. It validates against `schemas/review-record-1.0.schema.json`.
- `review-baseline.json`: the last tool-committed snapshot of the Review Record, used to detect changed resolutions and preserve history; not meant for editing
- `review-report.html`: WCAG 2.2 AA presentation of the Review Record, pairing source-region context with the generated interpretations, warnings, and resolutions
- `page-semantics.json`: the reconstructed Semantic Layer and warnings the Review Record is built from
- `recognition/page-1.json`: non-authoritative recognition candidates and existing PDF text evidence
- `regions/page-1.png`: stable rendered source context for review
- `regions/page-1-recognition.png`: full-page render used for recognition
- `regions/page-1-rNNNN.png`: stable per-candidate source-region crops
- `authoring.json`: versioned Python-to-Java contract
- `provenance.json`: source hash, authoring versions, and resolved provider identity
- `request-usage.json`: resumable request ceiling, count, estimate, and reported token totals
- `checkpoints/*.json`: atomic dependency keys and hashes for completed stages
- `validation/preflight.json`: Source PDF preflight result
- `validation/internal.json`: semantic invariant results
- `validation/visual.json`: explicit pixel-difference result and tolerance
- `validation/verapdf.xml`: independent PDF/UA-1 validation report

## Review and finalize

When a conversion exits `2`, its Conversion Bundle is Review-Required: one or more
Conversion Warnings remain unresolved. A Reviewer resolves them by hand-editing the
YAML Review Record and finalizing without repeating OCR or provider calls. None of
these commands touch the network; the launcher runs them with container networking
disabled.

Open `review-record.yaml` and, for each warning, set its `resolution` to exactly one
of `corrected`, `accepted`, or `not_applicable`. A `not_applicable` resolution
requires a `reason`. You may also correct the Semantic Layer text (heading,
paragraph, Spoken Math Alternative, Figure Alternative, and Detailed Figure
Description) directly; those edits are preserved.

```sh
# Check the record against the canonical schema and report finalizability.
./accessibilizer validate --bundle electric-current.accessibilizer --json

# Stamp the edited resolutions with your identifier, move any superseded
# resolution into history, and regenerate the Review Report.
./accessibilizer review --bundle electric-current.accessibilizer --reviewer jdoe

# Rebuild the Accessible PDF deterministically from the corrected record.
./accessibilizer finalize --bundle electric-current.accessibilizer --reviewer jdoe --json
```

Each resolution carries the Reviewer's non-secret identifier, taken from `--reviewer`
or from a `[review]` table (`reviewer = "jdoe"`) in the user or project configuration.
`finalize` verifies the immutable Source PDF hash, then refuses (`exit 2`) while any
warning is unresolved. Once every warning is resolved it re-authors the PDF, re-runs
the internal semantic checks, the visual comparison, and veraPDF's PDF/UA-1 profile,
and exits `0` with an Accessible PDF. Superseded resolutions are retained in each
warning's `history`, and the original recognition candidates are never discarded.

## Recognition evidence

For each converted page, Accessibilizer renders the source at a deterministic
recognition resolution and runs a pinned, CPU-only PaddleOCR pass. It records
text or handwriting, Formula, table, figure, and Document Structure candidates,
each with a stable identifier (`page-N-rNNNN`), a bounding box, a confidence
value used only as evidence, and a source-region crop under `regions/`. The
existing Source PDF text layer is extracted with its geometry and stored as
`pdf_text_evidence` marked `"authoritative": false`, so it can inform later
reconciliation without ever contaminating the Semantic Layer. Recognition is a
checkpointed stage: it is reused when its source, tool versions, resolution,
backend, and weights are unchanged. The candidate contract is documented by
`schemas/recognition-1.0.schema.json`.

PaddleOCR code and weights are pinned in the canonical image, so recognition
runs offline with no runtime model downloads. Set
`ACCESSIBILIZER_RECOGNITION_BACKEND=fake` to select a deterministic backend that
fabricates one candidate per type without running OCR; this is intended for
fast, credential-free tests, never for a real conversion.

## Verify

```sh
make test
make typecheck
```

The acceptance test invokes the public launcher and therefore requires Docker.
The fast suite selects the deterministic `fake` recognition backend. Set
`ACCESSIBILIZER_RUN_REAL_OCR=1` to additionally run the opt-in check that pinned
PaddleOCR produces schema-valid candidates for all 11 sample pages offline.

The authoring boundary is documented by `schemas/authoring-1.0.schema.json`, and
the recognition-evidence contract by `schemas/recognition-1.0.schema.json`.
