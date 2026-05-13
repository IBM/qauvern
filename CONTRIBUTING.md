# Contributing

## Pre-requisites

- [Just](https://just.systems/man/en/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)`

## Signing Commits

All commits must be signed off under the [Developer Certificate of Origin](https://developercertificate.org/).
Use `git commit -s` (or `--signoff`) to append a `Signed-off-by` trailer to your commit message.
PRs without sign-off will fail the DCO check.

```bash
git commit -s -m "Your commit message"
```

To sign off existing commits, amend or rebase with `--signoff`:

```bash
git commit --amend --signoff
git rebase --signoff main
```

## Pre-commit hooks (detect-secrets)

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
