# Require a vision- and schema-capable OpenAI-compatible provider

The model provider must implement `POST /v1/chat/completions` with base64 image inputs and JSON-Schema `response_format`, configured through a base URL, model name, and API-key environment-variable reference. Accessibilizer will run a capability check before conversion and reject providers that cannot satisfy the contract rather than loosely parse prose. Ollama and OpenAI are supported examples; native Anthropic support and a bundled Claude proxy are outside version-one scope.
