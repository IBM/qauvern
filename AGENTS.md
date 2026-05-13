# AGENTS.md

This is a Python CLI tool for optimizing IBM Quantum resource allocations across cloud accounts,
using fairness-based scheduling to rebalance QAU allocations among service instances. See
`Design.md` for full architecture, algorithm details, and API endpoint documentation.

## Development Setup

Requires [Just](https://just.systems/man/en/) and [uv](https://docs.astral.sh/uv/).

## Running Tests and Linting

Always run both before committing:

```bash
just lint
just test
```

To auto-format:

```bash
just fmt
```

## Testing Rules

- **Never make live IBM Cloud API calls to test code.** Use `tests/mock_api.py`, which provides
  a complete configurable mock of `IBMQuantumAPIClient`.
- Tests do not require real credentials or network access.
- The mock supports setting account allocation, per-instance allocations/limits, and usage across
  five time windows (28d, 14d, 7d, 3d, 24h).

## Committing

All commits must be DCO signed-off. Use `git commit -s`.

## Key Prohibitions

- Do not run `qauvern optimize` against a real account to verify behavior — use `analyze`
  with `--dry-run` or write a test with `mock_api.py`.
- Do not hardcode API keys or CRNs in source files.

## Release Process

Releases are automated via GitHub Actions. To release a new version:

1. Update the `version` in `pyproject.toml` (semver format: `X.Y.Z`)
2. Add a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md` with release notes
3. Add the version link at the bottom of `CHANGELOG.md`
4. Merge to `main`

The release workflow triggers on any push to `main` that changes `pyproject.toml`. It will:
- Validate the version format and check no tag already exists
- Run the full test suite across all supported Python versions
- Build the distribution
- Publish to PyPI via trusted publishing
- Create a git tag and GitHub Release with changelog notes

PyPI publishing uses trusted publishing (OIDC), configured via the `pypi` GitHub environment.

## Maintenance Rule

After any design change (algorithm, API endpoints, data model, config format), update `Design.md`
so the project can be reconstructed from that document alone.
