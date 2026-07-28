# Frozen vision-only baseline experiment

Issue #77 freezes revision `vision-only-baseline-1`, identity
`4bfd496ef022298e659e6a36c9eeabef3b2b99a0646a695273da6df329100584`.
The identity binds the exact `gpt-5.6-sol` model identifier, prompt, strict schema,
normalizer, semantic and warning evaluator, geometry evaluator, resource evaluator,
and the dated 2026-07-27 USD pricing basis. It also binds the run procedure. The
normalizer and evaluators are hashes of their complete implementation modules,
not version labels. The component hashes are recorded in every run manifest;
changing frozen code, prompt, schema, or the component set without creating a new
revision causes startup to fail.

The experiment remains a Python-only prototype. It does not modify the production
`convert` command, Review Record contract, checkpoint graph, Dockerfile, or
canonical Docker runtime. Requests expose neither tools nor functions. The system
and page instructions identify the Source PDF image and native PDF context as
untrusted, non-authoritative data.

## Provider configuration and authorization

Set `OPENAI_API_KEY` in the process environment; never place its value in source,
configuration, commands saved to the repository, or run artifacts. Construct a
`ProviderConfig` with:

- `base_url="https://api.openai.com/v1"`
- `model="gpt-5.6-sol"`
- `api_key_env="OPENAI_API_KEY"`
- `data_location="remote"`

Call `run_frozen_baseline_experiment(...)` with a new run identity and
`allow_remote=True`. That argument is the explicit authorization to transmit the
Source PDF to the named remote provider; an API key alone is not consent. Omit it
for a local provider. The function rejects a different model, an existing run
identity, and remote use without explicit authorization.

Acceptance runs A, B, and C must each use a fresh run directory and make all 11
page requests. Do not copy, cache, or replay shakedown or acceptance responses into
a fresh run. Each page input and recorded response is bound to its run identity,
experiment revision, page number, and response hash. Replay rejects a mismatched
binding or experiment identity.

## Shakedown and artifacts

`tests.test_baseline_experiment` performs the complete 11-page shakedown against a
credential-free loopback provider. It exercises semantic, warning, geometry,
schema/repair, and resource-limit categories and produces the live layout:

```text
<run-id>/
├── evaluation.json
├── manifest.json
├── pages/page-N/{input.json,normalized.json,page.png,response.json}
└── prompt/{page.txt,schema.json,system.txt}
```

`evaluation.json` keeps the five failure categories separate. A failed geometry
check additionally creates focused files under `geometry-review/`. The manifest
records prompt/schema artifacts, normalized results, repairs, provider calls,
tokens, latency, pricing, and calculated cost. It excludes credentials,
authorization headers, hidden reasoning, and full HTTP traces.
