# Use gpt-5.6-sol for the initial quality baseline

The 11-page gold acceptance run will use OpenAI's `gpt-5.6-sol` through the configured OpenAI-compatible Chat Completions interface, prioritizing semantic accuracy over inference cost. After the pipeline passes, lower-cost OpenAI tiers and Ollama-hosted models may be evaluated against the same gold Review Record; none replaces the baseline based only on subjective output comparisons.
