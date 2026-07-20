"""Download and cache the pinned PaddleOCR weights at image-build time.

Instantiating PP-Structure fetches the detection, recognition, classification,
layout, and table model archives into ``$HOME/.paddleocr``. Baking them into the
canonical image guarantees CPU-only recognition never downloads artifacts at
runtime (issue #7 acceptance criterion 1).
"""

from paddleocr import PPStructure


def main() -> None:
    # Constructing the pipeline triggers the one-time weight downloads into the
    # build-time HOME. No inference is required to populate the cache.
    PPStructure(show_log=False)


if __name__ == "__main__":
    main()
