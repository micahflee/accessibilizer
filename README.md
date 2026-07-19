# Accessibilizer

Accessibilizer turns visually readable Source PDFs into documents whose Visual Layer is preserved and whose Semantic Layer can be consumed by assistive technology.

The current implementation is the one-page PDF/UA feasibility slice from issue #2. It accepts a deterministic semantic contract, imports one native source page as artifact content, adds representative heading, paragraph, Formula, and Informative Figure semantics, then gates the result on internal checks, visual comparison, and veraPDF's PDF/UA-1 profile. Recognition and model-provider integration are intentionally outside this slice.

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
  --json
```

The launcher supports macOS and Linux paths and keeps Docker as an implementation detail. It refuses to overwrite an existing Conversion Bundle.
Pass `--replace` to explicitly authorize replacement. Accessibilizer builds the replacement in a protected staging directory and leaves the existing bundle, including Reviewer edits, untouched if conversion fails.

Before conversion, Accessibilizer rejects encrypted or digitally signed Source PDFs and PDFs containing forms, JavaScript, embedded files or media, links, or other interactive actions that this version cannot preserve safely.

With `--json`, every outcome is machine-readable. Exit `0` reports `accessible`, exit `2` reports `review_required` when unresolved Conversion Warnings remain, and exit `1` reports `operational_failure`.

## Conversion Bundle

The generated protected directory contains:

- `source.pdf`: immutable copy of the Source PDF
- `output.pdf`: PDF/UA-1 output
- `review-record.json`: representative Semantic Layer in Logical Reading Order
- `review-report.html`: accessible presentation of the Review Record
- `regions/page-1.png`: stable rendered source context for review
- `authoring.json`: versioned Python-to-Java contract
- `provenance.json`: source hash and authoring versions
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
