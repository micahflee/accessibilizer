# macOS Preview and VoiceOver one-page validation (second session)

Issue: #3

Result: **failed — fragment reading reconfirmed; ADR 0026's rejection stands**

## Environment

- macOS 26.5.2 (build 25F84)
- Preview 11.0
- VoiceOver 10
- Accessibilizer commit `2b06871`
- `output.pdf` SHA-256: `2ae86e931acf23366e26d8b316c765bdb402293d7a8326796959529c4165c52a`
- Source: page 1 of `testdata/Chapter 20_ Electric Current Resistance and Ohms Law.pdf`
- Provider: `gemma4:26b` via a local Ollama server (capability check only; this
  slice makes no recognition calls, so the model cannot affect the PDF)

The output was regenerated at current `main` through the public host launcher
with the canonical `accessibilizer:test` Docker image and
`testdata/one-page-semantic.json`. The Semantic Layer authoring technique is
unchanged since the first session at commit `4571974`; only unrelated
preflight input-rejection code was added in between.

## Automated preconditions

- Internal structure validation passed; the extracted structure tree contained
  all four intended nodes in the intended Logical Reading Order.
- veraPDF reported PDF/UA-1 compliance.
- The rendered source and output were both 1224 by 1607 pixels with a
  different-pixel ratio of `0.0`, below the `0.0001` tolerance.
- Preflight reported no unsupported features.

## Recorded session

The generated PDF was opened in Preview and read with VoiceOver.

| Check | Result | Observation |
| --- | --- | --- |
| Semantic Layer only | Fail | VoiceOver announced single-letter fragments such as `w` and `c` instead of the intended heading and paragraph text. |

The remaining checks from the first session (sighted reading, zoom, print
preparation, Logical Reading Order traversal, Formula, Informative Figure)
were not re-recorded in this session. The first session's observations in
`2026-07-19-macos-preview-voiceover.md` remain the record for those checks.

## Decision point

The failure reproduces at current `main` with the same signature as the first
session: Preview derives accessibility text from the glyphs laid out inside
the one-point-wide, zero-opacity paragraphs instead of honoring their complete
`ActualText` values. ADR 0026's rejection of this authoring technique is
reconfirmed by direct observation. Recognition work (#7 and its chain) remains
blocked until a replacement Semantic Layer authoring technique passes a
recorded Preview and VoiceOver session for issue #3.

Evidence from the first session that should guide the replacement:

- Preview reads laid-out glyphs, so a technique that lays out the real text
  invisibly at full size (text rendering mode 3, the standard OCR text-layer
  approach) is the leading candidate.
- Preview honored the `Alt` description on the Formula image, so image-carried
  alternatives are a viable channel for Figure Alternatives.
- Preview omitted the empty zero-opacity figure container entirely, so figure
  semantics must be attached to real content rather than empty elements.
