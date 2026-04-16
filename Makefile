.PHONY: install repl test clean

install:
	python -m venv .venv && .venv/bin/pip install -e '.[dev,channels]'

repl:
	.venv/bin/python -m spawn

test:
	.venv/bin/pytest -q tests/

clean:
	rm -rf data/*.db data/*.db-* logs/*.log
	find . -name __pycache__ -exec rm -rf {} +
