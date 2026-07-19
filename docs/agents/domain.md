# Domain Docs

How engineering skills should consume this repository’s domain documentation.

## Before exploring, read these

- `CONTEXT.md` at the repository root.
- Relevant decisions under `docs/adr/`.

If either is absent, proceed silently. Domain-modeling workflows create these files only when terms or decisions are resolved.

## File structure

This is a single-context repository:

/
├── CONTEXT.md
├── docs/
│   └── adr/
└── src/

## Use the glossary’s vocabulary

When output names a domain concept—in an issue title, proposal, hypothesis, or test—use the canonical term from `CONTEXT.md`. Avoid synonyms that the glossary explicitly rejects.

If a needed concept is absent, reconsider whether it is domain-specific or note the gap for `/domain-modeling`.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, surface the conflict explicitly instead of silently overriding the decision.
