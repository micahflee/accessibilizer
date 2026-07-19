# Accessibilizer

Accessibilizer turns visually readable Source PDFs into documents whose Visual Layer is preserved and whose Semantic Layer can be consumed by assistive technology.

The current implementation accepts a deterministic semantic contract, imports one native source page as artifact content, adds representative heading, paragraph, Formula, and Informative Figure semantics, then gates the result on internal checks, visual comparison, and veraPDF's PDF/UA-1 profile. It also resolves and verifies an exact OpenAI-compatible vision provider before Source PDF work begins. Recognition calls are intentionally outside this slice.

The feasibility output is not yet compatible with the primary assistive-technology environment. The [macOS Preview and VoiceOver validation](docs/validation/2026-07-19-macos-preview-voiceover.md) found clipped text and missing Figure Alternative and Detailed Figure Description content even though the Visual Layer and automated PDF/UA checks passed. ADR 0026 blocks recognition work from expanding on the current Semantic Layer overlay.

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
  --semantic-input testdata/one-page-semantic.json \
  --bundle electric-current.accessibilizer \
  --provider-base-url http://localhost:11434/v1 \
  --provider-model exact-model-identifier \
  --provider-data-location local \
  --json
```

The launcher supports macOS and Linux paths and keeps Docker as an implementation detail. It refuses to overwrite an existing Conversion Bundle.
Pass `--replace` to explicitly authorize replacement. Accessibilizer builds the replacement in a protected staging directory and leaves the existing bundle, including Reviewer edits, untouched if conversion fails.

Interrupted or paused work remains in a protected sibling directory named `.BUNDLE.in-progress`.
Repeat the same command with `--resume` to continue it. Completed source, page, region, and validation stages are reused only when their dependency key and artifact hashes remain valid. Changing semantics invalidates authoring and validation without repeating an unaffected source-region render; changing a Source PDF, tool version, schema, prompt, model, or relevant rendering setting invalidates the stages that depend on it.

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

Transient timeouts, connection failures, rate limits, and server errors receive bounded exponential-backoff retries. The current capability-check-only pipeline estimates one provider request when that check is needed and zero when a valid checkpoint can be reused, then enforces `max_requests` before every attempt, including retries. Use `--max-requests`, `--provider-max-retries`, `--provider-retry-base-seconds`, and `--provider-retry-max-seconds` to override the layered settings. Reaching the request ceiling pauses the Conversion Bundle instead of exceeding it; raise the ceiling and pass `--resume` to continue. Conversion Provenance retains estimated and actual request counts and any prompt, completion, and total token usage reported by the provider. Accessibilizer does not estimate dollar cost.

## Conversion Bundle

The generated protected directory contains:

- `source.pdf`: immutable copy of the Source PDF
- `output.pdf`: PDF/UA-1 output
- `review-record.json`: representative Semantic Layer in Logical Reading Order
- `review-report.html`: accessible presentation of the Review Record
- `regions/page-1.png`: stable rendered source context for review
- `authoring.json`: versioned Python-to-Java contract
- `provenance.json`: source hash, authoring versions, and resolved provider identity
- `request-usage.json`: resumable request ceiling, count, estimate, and reported token totals
- `checkpoints/*.json`: atomic dependency keys and hashes for completed stages
- `validation/preflight.json`: Source PDF preflight result
- `validation/internal.json`: semantic invariant results
- `validation/visual.json`: explicit pixel-difference result and tolerance
- `validation/verapdf.xml`: independent PDF/UA-1 validation report

## Verify

```sh
make test
make typecheck
```

The acceptance test invokes the public launcher and therefore requires Docker.

The authoring boundary is documented by `schemas/authoring-1.0.schema.json`.
