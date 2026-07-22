---
status: accepted
---

# Separate source evidence from semantic interpretation

Review Record 3.0 will represent Source Regions as canonical, stably identified visual evidence in displayed-page PDF-point coordinates, separate from Recognition Candidates and Semantic Layer nodes. The record stores each displayed page's width and height in PDF points so Source Region bounds can be validated as finite, nonnegative, ordered, and contained by that page. Every Semantic Layer node will have a stable identity and reference one or more same-page Source Regions; every Recognition Candidate will have a distinct identity and reference exactly one Source Region; warnings may reference both nodes and regions, while page- and document-wide warnings may use empty reference arrays. Crops will be derived as `regions/<source-region-id>.png` from the immutable Source PDF and canonical geometry rather than stored as Review Record paths. Models may select deterministic regions but may not invent geometry; an imprecise whole-page fallback will raise a Conversion Warning; and node identities, Source Region references, and all other review provenance will be projected away before the PDF-authoring contract. This separation prevents positional or duplicated geometry from silently drifting while supporting a portable, accessible Review Report and a future interactive review application.
