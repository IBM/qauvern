# Contributing

## Pre-requisites

- [Just](https://just.systems/man/en/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

### Pre-commit Hooks

This project uses `detect-secrets` to prevent accidental commits of sensitive information.

```bash
uvx pre-commit install

# First time only: create the secrets baseline
uvx detect-secrets scan > .secrets.baseline

# Verify hooks work
uvx pre-commit run --all-files
```

If the hook detects a potential secret, either remove it from your code or add it to the baseline if it's a false positive:

```bash
uvx detect-secrets scan --baseline .secrets.baseline
```

## Running Tests

```bash
# Run all tests
just test

# Run with coverage
just test --cov=qauvern --cov-report=term-missing

# Run a specific file
just test tests/test_models.py

# Verbose output
just test -v
```

## Code Quality

```bash
just fmt
```

```bash
just lint
```
