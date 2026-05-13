all: lint test

fmt:
    uv run ruff format

lint:
    uv run ruff format --check
    uv run ruff check
    uv run ty check --error-on-warning

test *args:
    uv run pytest {{args}}

pex:
    uvx pex . -c qauvern -o dist/qauvern.pex --interpreter-constraint 'CPython>=3.10' --python-shebang '/usr/bin/env python3' --venv prepend
