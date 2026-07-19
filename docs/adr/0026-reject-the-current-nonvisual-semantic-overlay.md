---
status: accepted
---

# Reject the current nonvisual Semantic Layer overlay

The one-point-wide, zero-opacity text and empty figure overlay used by the feasibility slice is rejected because macOS Preview 11.0 with VoiceOver 10 exposes clipped text fragments, omits the intended Figure Alternative and Detailed Figure Description, and cannot present the required Logical Reading Order. ADR 0002's native Visual Layer preservation decision remains in force, but recognition work must not expand until a replacement Semantic Layer authoring technique passes the recorded Preview and VoiceOver session in `docs/validation/2026-07-19-macos-preview-voiceover.md`.
