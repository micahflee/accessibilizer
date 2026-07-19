.PHONY: image test test-cli typecheck

image:
	docker build --tag accessibilizer:0.1.0 --tag accessibilizer:test .

test-cli:
	python3 -m unittest discover -s tests -v

test: image test-cli

typecheck:
	uvx mypy --strict src tests
