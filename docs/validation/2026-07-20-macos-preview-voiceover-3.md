# macOS Preview and VoiceOver one-page validation (third session)

Issue: #3

Result: **passed — the replacement authoring technique (ADR 0027) is consumed completely and in the intended Logical Reading Order by macOS Preview and VoiceOver**

## Environment

- macOS 26.5.2 (build 25F84)
- Preview 11.0 (build 1113.5.3)
- VoiceOver 10
- Accessibilizer commit `94a93cd` (branch `23-replace-nonvisual-semantic-overlay`)
- `output.pdf` SHA-256: `408caf1f6d05e9472334bd4c08edab8e82ac3e209949a93235e7603a7cbb3738`
- Source: page 1 of `testdata/Chapter 20_ Electric Current Resistance and Ohms Law.pdf`

The output was generated with the replacement authoring technique from ADR 0027,
which draws the Semantic Layer as real strings at full size with text rendering
mode 3 and carries the Detailed Figure Description on a sibling Caption.

## Automated preconditions

- Internal structure validation passed; the extracted structure tree contained
  all four intended nodes in the intended Logical Reading Order.
- veraPDF reported PDF/UA-1 compliance.
- The rendered source and output were both 1224 by 1607 pixels with a
  different-pixel ratio of `0.0`, below the `0.0001` tolerance.

## Recorded session

The generated PDF was opened in Preview and read with VoiceOver.

| Check | Result | Observation |
| --- | --- | --- |
| Sighted reading | Pass | The full handwritten page opened without visible corruption. |
| Zoom | Pass | Zooming in retained sharp strokes, text, and Formula details; no hidden text became visible. |
| Print preparation | Pass | The print preview reproduced the complete page with no invisible text; the job was canceled. |
| Semantic Layer | Pass | VoiceOver announced the complete heading and paragraph strings, not the single-letter fragments from the earlier sessions. |
| Logical Reading Order | Pass | All four intended nodes were traversed in order: heading, paragraph, Formula, Figure. |
| Formula | Pass | Preview exposed the Formula with the description "I equals Q divided by delta t." |
| Informative Figure | Pass | VoiceOver announced the Figure Alternative "A circuit wire carrying electric current." and then reached the Detailed Figure Description "A wire passes through a surface. Positive charge moves along the wire in the direction of conventional current." |

## Iteration during validation

An earlier build of the replacement technique drew the Detailed Figure
Description on the Figure element itself (as laid-out glyphs and `ActualText`).
That build reached the Figure Alternative but not the Detailed Figure
Description, because Preview treats a Figure as an image: it reads the Figure's
`/Alt` and ignores that element's own glyphs and `ActualText`. Moving the
Detailed Figure Description onto a sibling `Caption` — a text element whose
glyphs Preview reads like the heading and paragraph — resolved the gap, and this
session confirms VoiceOver reaches both Figure strings.

## Decision point

The replacement authoring technique recorded in ADR 0027 resolves every failure
that ADR 0026 rejected: the heading and paragraph are announced as complete
strings rather than single-letter fragments, all four nodes are traversed in the
intended Logical Reading Order, the Formula is exposed, and the Figure is no
longer dropped — both its Alternative and its Detailed Figure Description are
announced. The Visual Layer stays visually identical under zoom and in the print
path. This passes the recorded Preview and VoiceOver session that ADR 0026
required before recognition work could expand, so it satisfies the final
acceptance gate for issue #3 and unblocks #7.
