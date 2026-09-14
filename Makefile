# Developer convenience targets. Project metadata requires Python 3.12+
# (CI runs 3.12/3.13). Override the interpreter with e.g.
# `make setup PYTHON=python3.12` if your default `python3` is older.
PYTHON ?= python3
VENV ?= .venv
VPY := $(VENV)/bin/python

.PHONY: setup test lint typecheck e2e

setup:
	@$(PYTHON) -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
		|| { echo "Python >= 3.12 required (set PYTHON=python3.12). Found: $$($(PYTHON) --version 2>&1)"; exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(VPY) -m pip install --upgrade pip
	$(VPY) -m pip install -r requirements.txt -c constraints.txt
	$(VPY) -m pip install -r requirements-dev.txt
	npm ci

test:
	$(VPY) -m pytest

lint:
	$(VPY) -m ruff check .
	npm run lint

typecheck:
	$(VPY) -m mypy backend scripts cloudflare tests

e2e:
	PYTHON=$(abspath $(VPY)) npm run test:e2e
