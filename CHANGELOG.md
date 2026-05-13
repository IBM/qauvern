# Changelog

All notable changes to qauvern will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/ibm/qauvern/releases/tag/v0.1.0
