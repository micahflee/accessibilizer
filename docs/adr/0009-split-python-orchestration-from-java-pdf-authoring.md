# Split Python orchestration from Java PDF authoring

Python will own the CLI, recognition pipeline, model-provider interface, Review Record, warning logic, and review report, while a narrow Java helper built on iText will construct PDF/UA-1 output. The components will communicate through a versioned JSON representation, and veraPDF will independently validate the result; this keeps model-heavy development in Python without abandoning the strongest available open-source PDF/UA authoring API.
