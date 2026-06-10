# AGENTS.md

This is a Python CLI tool for optimizing IBM Quantum resource allocations across cloud accounts,
using fairness-based scheduling to rebalance allocations among service instances. See
`Design.md` for full architecture and algorithm details.

## Development Setup

Requires [Just](https://just.systems/man/en/) and [uv](https://docs.astral.sh/uv/).

## Running Tests and Linting

Use `just test` to run tests and `just lint` to run linters/typecheckers. Run both to verify changes.

Use `just fmt` to autoformat.

## Test the CLI

Use `just run` to start the CLI, such as `just run --help`.

## Testing Rules

- **Never make live IBM Cloud API calls to test code.** Use `tests/mock_api.py`, which provides
  a complete configurable mock of `IBMQuantumAPIClient`.
- Tests do not require real credentials or network access.
- The mock supports setting account allocation, per-instance allocations/limits, and usage across
  five time windows (28d, 14d, 7d, 3d, 24h).

## Committing

All commits must be DCO signed-off. Use `git commit -s`.

## CLI Output Rules

Progress/log messages must use `click.echo(..., err=True)` so stdout stays clean for data output
(e.g. JSON from `analyze --export`). Only final structured output goes to stdout.

## Key Prohibitions

- Do not run `qauvern optimize` against a real account to verify behavior — use
  `analyze`, `optimize --dry-run`, or write a test with `mock_api.py`.
- Do not hardcode API keys or CRNs in source files.

## Release Process

Releases are automated via GitHub Actions. To release a new version:

1. Update the `version` in `pyproject.toml` (semver format: `X.Y.Z`)
2. Add a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md` with release notes
3. Add the version link at the bottom of `CHANGELOG.md`
4. Merge to `main`

The release workflow triggers on any push to `main` that changes `pyproject.toml`.

## Maintenance Rule

After any relevant design change (algorithm, config format, etc), update `Design.md`
so the document accurately describes the project. We do not document every detail there
because often it's sufficient to read the source code.
