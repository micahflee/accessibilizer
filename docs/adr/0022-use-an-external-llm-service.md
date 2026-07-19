# Use an external LLM service

Accessibilizer will call an already-running OpenAI-compatible endpoint for vision inference and will not install, launch, or manage an Ollama process or other LLM server. The local CPU-only requirement applies to rendering, specialized PaddleOCR recognition, PDF authoring, and validation; most semantic inference may occur on OpenAI or an existing Ollama server configured by the user.
