# macOS Preview and VoiceOver one-page validation

Issue: #3

Result: **failed — do not expand recognition work on this authoring approach**

## Environment

- macOS 26.5.2 (build 25F84)
- Preview 11.0 (build 1113.5.3)
- VoiceOver 10
- Accessibilizer commit `4571974`
- `output.pdf` SHA-256: `dd2a10a54544a57ad8cd7e40dfcadfda155f79b8a28fe724c9f4c9d7a11f4d50`
- Source: page 1 of `testdata/Chapter 20_ Electric Current Resistance and Ohms Law.pdf`

The output was generated through the public host launcher with the canonical
`accessibilizer:test` Docker image and `testdata/one-page-semantic.json`.

## Automated preconditions

- Internal structure validation passed with no reported checks.
- veraPDF reported PDF/UA-1 compliance.
- The rendered source and output were both 1224 by 1607 pixels.
- The different-pixel ratio was `0.0`, below the `0.0001` tolerance.

## Recorded session

The generated PDF was opened in Preview. VoiceOver was launched, its caption
panel was displayed, and focus was moved into the PDF page. Preview's macOS
accessibility tree was inspected after the spoken check as supporting evidence;
the tree inspection did not replace the VoiceOver observation.

| Check | Result | Observation |
| --- | --- | --- |
| Sighted reading | Pass | The full handwritten page opened without visible corruption. |
| Zoom | Pass | Two Preview **Zoom In** actions retained sharp strokes, text, and Formula details with normal scrolling. |
| Print preparation | Pass | **File > Print** produced a one-page color preview of the complete page. The job was canceled without sending it to a printer. |
| Semantic Layer only | Fail | VoiceOver announced `heading level 1 w`; intended heading and paragraph text were reduced to malformed fragments. |
| Logical Reading Order | Fail | Preview reported three contained items rather than the four intended nodes, so the required sequence could not be traversed. |
| Formula | Partial pass | Preview exposed an image whose description was `I equals Q divided by delta t.` |
| Informative Figure | Fail | Preview did not expose the intended Figure Alternative or Detailed Figure Description. |

The first page announcement captured in VoiceOver's caption panel was:

> In Page 1, containing, heading level 1 w, 3 items, heading level 1 w

The intended Logical Reading Order was:

1. Heading: “Electric Current, Resistance, and Ohm's Law”
2. Paragraph: “Electric current is the rate at which charge flows through a surface.”
3. Formula: “I equals Q divided by delta t.”
4. Informative Figure: “A circuit wire carrying electric current,” followed by its Detailed Figure Description

Preview's accessibility tree instead contained a level-one heading followed by
static-text fragments including `w`, `c`, `w`, `e.`, `a`, and `t`. It exposed the
Formula as an image with the correct description, but it did not expose a node
with the intended Figure Alternative.

## Decision point

The Visual Layer preservation premise remains supported: the native page stayed
visually intact in Preview, at increased zoom, and in Preview's print workflow.
The current nonvisual Semantic Layer authoring technique does not work in the
primary assistive-technology environment and must not be extended to recognition
work. ADR 0026 records the rejection of this technique.

The likely mechanism is that Preview derives accessibility text from the glyphs
laid out inside the one-point-wide, zero-opacity paragraphs instead of honoring
their complete `ActualText` values; the empty zero-opacity figure container is
also omitted. This is an inference from the observed accessibility tree and the
current authoring code, not a confirmed Preview implementation detail.
