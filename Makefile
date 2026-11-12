PYTHON ?= .venv/bin/python

.PHONY: check fast slow all lint type clean

check: fast lint type

fast:
	$(PYTHON) -m pytest -m fast --durations=10

slow:
	$(PYTHON) -m pytest -m slow

all:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests

type:
	$(PYTHON) -m mypy src

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
