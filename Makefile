.PHONY: install repl slack whatsapp telegram test clean

install:
	python -m venv .venv && .venv/bin/pip install -e '.[slack,dev]'

repl:
	.venv/bin/python -m aisha

slack:
	.venv/bin/python -m aisha --slack

whatsapp:
	.venv/bin/python -m aisha --whatsapp

telegram:
	.venv/bin/python -m aisha --telegram

test:
	.venv/bin/pytest -q tests/

clean:
	rm -rf data/aisha.db data/aisha.db-* data/chroma logs/*.log
	find . -name __pycache__ -exec rm -rf {} +
