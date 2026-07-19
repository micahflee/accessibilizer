# Treat source documents as untrusted model input

Source PDF content will be treated as untrusted data, never as instructions to the vision model. Model calls receive no tools, filesystem access, or network capabilities; prompts delimit document content, outputs must satisfy strict schemas, and instruction-like text is transcribed rather than followed. Suspected prompt injection produces a Conversion Warning so malicious or accidental embedded instructions cannot silently alter the conversion workflow.
