# Changelog

All notable changes to qauvern will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-05-26

### Changed

- In the configuration file, `projects` was renamed to `instances` and `project_limit_seconds` to `limit_seconds`. To fix, either manually update your config file keys or run `qauvern configure` to generate a new config file. (This change was to make the program simpler for users; projects and instances were the same thing.)
- `qauvern configure` now sets the name of `instances` to your actual instance's name, rather than values like `Project 1`. You do not need to update your config file, but it may be clearer to regenerate it with `qauvern configure`.
- Plans were reworked.
   - In config files, `plan_id` was replaced with `plan`, which should be set to `premium`, `internal`, or `paygo` rather than a UUID.
   - `qauvern configure` now requires the `--plan` argument.

### Fixed

- `qauvern configure` now sets the `target_usage_seconds` and `limit_seconds` for each instance based on its current settings.

## [0.2.3] - 2026-05-15

### Fixed

- `list_instances` now fetches all pages from the IBM Resource Controller API instead of silently returning only the first 100 results. Accounts with more than 100 instances were getting truncated lists.

## [0.2.2] - 2026-05-14

### Fixed

- Fix Pex platform handling

## [0.2.1] - 2026-05-14

### Added

- Upload Pex file to GitHub Release

## [0.2.0] - 2026-05-13

### Added

- Release workflow for automated PyPI publishing via GitHub Actions
- CI test matrix across Python 3.10, 3.11, 3.12, 3.13, 3.14
- Python 3.12, 3.13, 3.14 support

## [0.1.0] - 2026-05-06

### Added

- CLI with `analyze`, `optimize`, `configure`, `instances`, `show`, and `create` commands
- Activity-score-based allocation algorithm with exponential time weighting
- Rolling-window-aware `net_grants` for additive budget boosts
- IBM Cloud IAM authentication (API key via `IBMCLOUD_API_KEY`)
- Regional endpoint support (auto-extracted from CRN)
- Staging environment support (`--staging` flag)
- YAML configuration with `configure` command for auto-discovery
- Configurable minimum allocation floor

[0.2.3]: https://github.com/ibm/qauvern/releases/tag/v0.2.3
[0.2.0]: https://github.com/ibm/qauvern/releases/tag/v0.2.0
[0.1.0]: https://github.com/ibm/qauvern/releases/tag/v0.1.0
