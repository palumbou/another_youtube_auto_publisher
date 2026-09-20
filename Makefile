.PHONY: setup test lint e2e serve
VENV ?= .venv
PY := $(VENV)/bin/python

setup:
	uv venv $(VENV) && uv pip install --python $(PY) -e ".[dev]"

lint:
	$(VENV)/bin/ruff check .

test: lint
	$(VENV)/bin/pytest -q

e2e:
	$(PY) -m autopublisher.local e2e --workdir $${E2E_DIR:-/tmp/autopublisher-e2e}

# One command for API/UI/workers with the filesystem bucket, mock providers and the fake YouTube adapter.
serve:
	LOCAL_MODE=1 ENVIRONMENT=local $(PY) -m autopublisher.local serve --bucket-root $${BUCKET_ROOT:-./local/bucket} --state-root $${STATE_ROOT:-./local/state} --port $${PORT:-8080}
