.PHONY: image test test-cli test-real-ocr typecheck browser-install browser-tests ci

image:
	docker build --tag accessibilizer:0.1.0 --tag accessibilizer:test .

test-cli:
	uv run python -m unittest discover -s tests -v

# Same discovery as test-cli, but opts the pinned real-PaddleOCR acceptance test
# in so it runs rather than skips. Requires the accessibilizer:test image.
test-real-ocr:
	ACCESSIBILIZER_RUN_REAL_OCR=1 uv run python -m unittest discover -s tests -v

test: image test-cli

typecheck:
	uv run mypy --strict src tests

# Deterministic browser-test setup: install from the committed lockfile and
# fetch Chromium plus its Linux system dependencies.
browser-install:
	cd tests/browser && npm ci && npm run install-browser

browser-tests:
	cd tests/browser && npm test

# One local command with the same verification surfaces as CI: strict typing,
# deterministic browser setup and tests, the canonical Docker build, and every
# Python test with the real PaddleOCR acceptance check enabled.
ci: typecheck browser-install browser-tests image test-real-ocr
