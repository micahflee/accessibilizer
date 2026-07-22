---
status: accepted
---

# Separate source evidence from semantic interpretation

Review Record 3.0 will represent Source Regions as canonical, stably identified visual evidence in displayed-page PDF-point coordinates, separate from Recognition Candidates and Semantic Layer nodes. Every Semantic Layer node will have a stable identity and reference one or more same-page Source Regions; warnings may reference both nodes and regions; crops will be derived from the immutable Source PDF; models may select deterministic regions but may not invent geometry; an imprecise whole-page fallback will raise a Conversion Warning; and all review provenance will remain outside the PDF-authoring contract. This separation prevents positional or duplicated geometry from silently drifting while supporting a portable, accessible Review Report and a future interactive review application.
