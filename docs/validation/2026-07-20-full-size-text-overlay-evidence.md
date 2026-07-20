# Full-size text-overlay authoring — programmatic evidence

Issue: #23 (prepares the artifact for the recorded human session tracked by #3)

Result: **supporting evidence — the fragment signature from ADR 0026 is resolved; the recorded human Preview and VoiceOver session remains the final gate**

## Environment

- macOS 26.5.2 (build 25F84)
- Accessibilizer replacement authoring technique per ADR 0027
- `output.pdf` SHA-256: `b7c024b7940a0da5d4094bf313c8786779dec8bdbabdd47c88ccd82d596b103e`
- Source: page 1 of `testdata/Chapter 20_ Electric Current Resistance and Ohms Law.pdf`
- Semantic input: `testdata/one-page-semantic.json`
- Built with the canonical `accessibilizer:test` Docker image.

## What changed

ADR 0026 rejected the one-point-wide, zero-opacity `ActualText` overlay because
macOS Preview derives its accessibility text from the glyphs physically laid out
on the page. The narrow boxes forced one glyph per line, so Preview announced
single-letter fragments such as `w` and `c`, and it dropped the empty
zero-opacity figure container entirely.

The replacement (ADR 0027) draws the real strings at a readable size across the
page with text rendering mode 3, which produces no marks on screen or in the
print path. Each node occupies its own vertical band, top to bottom in Logical
Reading Order. Because Preview reads glyphs rather than the `ActualText`/`Alt`
attributes, every string a reader must reach is drawn as a glyph run: the
Formula draws its normalized math, and the Figure draws both its alternative and
its detailed description while still carrying them in `Alt` and `ActualText`.

## Automated gates (acceptance criterion 1)

- Internal structure extraction returned the four intended nodes in Logical
  Reading Order (`validation/internal.json`: `passed: true`, `checks: []`).
- veraPDF reported PDF/UA-1 compliance (`isCompliant="true"`).
- The rendered source and output were both 1224 by 1607 pixels with a
  different-pixel ratio of `0.0`, below the `0.0001` tolerance.

## Laid-out glyph evidence (acceptance criterion 2, ahead of the human session)

Preview was shown to read the glyphs physically laid out on the page. Extracting
that laid-out text with `pdftotext -layout` is a programmatic proxy for the same
channel. Under the rejected overlay this channel produced single-letter
fragments; under the replacement it produces the complete strings, each in its
own band and in Logical Reading Order:

```
Electric Current, Resistance, and Ohm's Law
Electric current is the rate at which charge flows through a surface.
I = Q / delta t
A circuit wire carrying electric current. A wire passes through a surface. Positive charge moves along the wire in the direction of conventional current.
```

(The extraction is interleaved with the preserved Visual Layer's own artifact
text, which is marked as an artifact and excluded from the accessibility tree.)

The Formula's Spoken Math Alternative and the Figure's alternative are also
carried in `Alt`; the recorded first session confirmed Preview honours `Alt` on
a Formula. The added text is drawn with text rendering mode 3 — every text run
in the content stream carries the `3 Tr` operator — so it produces no marks,
consistent with the `0.0` pixel-difference result on screen and in the print
path.

## Not yet verified here

A live inspection of Preview's own accessibility tree, and the VoiceOver
announcement of the Figure Alternative and Detailed Figure Description, are part
of the recorded human Preview and VoiceOver session for issue #3. `pdftotext`
extracts the laid-out glyphs — the channel Preview was shown to read — but it is
not Preview's accessibility tree itself. This spike prepares the artifact for
that session and cannot close #3 itself.
