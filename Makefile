.PHONY: image test test-cli typecheck

image:
	docker build --tag accessibilizer:0.1.0 --tag accessibilizer:test .

test-cli:
	uv run python -m unittest discover -s tests -v

test: image test-cli

typecheck:
	uv run mypy --strict src tests
