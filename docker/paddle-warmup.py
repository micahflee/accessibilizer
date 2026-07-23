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
    # PaddlePaddle 2.6.x's self-attention IR fusion pass can execute an
    # unsupported CPU instruction on otherwise supported x86-64 hosts. Model
    # initialization and inference do not require that optional optimization.
    PPStructure(show_log=False, ir_optim=False)


if __name__ == "__main__":
    main()
