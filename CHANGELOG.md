# Changelog

All notable changes to qauvern will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] - 2026-06-xx

## Added

- The `configure` command has an optional `--region` argument to restrict which instances are added. Because all the other commands only run on the instances in your config file, you can use this new argument to restrict `qauvern` to only run on `us-east` or `eu-de`.

## Changed

- Reworked the allocation algorithm so it composes properly with the new limit-resolution logic from 0.6.0. From a user-facing perspective:
  - The effective limit (including any active net grant) now caps allocations on the same run that updates the limit. Previously the optimizer used the live IQP limit, so a fresh net grant or a config-driven limit bump didn't take effect on allocation until the next run.
  - When an active instance hits its effective limit, the surplus from its proportional share is now redistributed to other active instances in proportion to activity score. Previously that surplus was silently dropped, leaving allocation on the table.
  - If every active instance is capped by limits, leftover capacity stays unallocated rather than being forced onto any instance — no more phantom overshoots.
- Improved the error message for when `analyze` and `optimize` propose a plan that would exceed your account's allocation budget.
- The `analyze` command no longer shows `-` in the "New Limit" column for instances whose limit is unchanged. It now shows the current limit, so the column reflects the actual post-run state rather than ambiguously suggesting the limit will be removed.

## [0.6.0] - 2026-06-08

## Added

- `qauvern` now enforces all six invariants from [here](https://github.com/IBM/qauvern/issues/102) when validating the proposal from `analyze` and `optimize`.

## Changed

- Reworked how the effective limit is computed when `net_grants` are configured (see [#102](https://github.com/IBM/qauvern/issues/102)). The new formula is `base + grant_total + max(0, rolloff - base)`, where:
  - `base` is the instance's configured `limit_seconds`.
  - `grant_total` is the sum of `net_grant_seconds` across grants active today.
  - `rolloff` is the sum of `daily_usage` on days strictly before the earliest active grant's start that are still inside the current 28-day rolling window.
  
  Previously, rolloff was subtracted from each grant's contribution per-grant. Under the new formula, pre-grant days that were already at or below the base limit contribute nothing — only excess usage above base extends the effective limit, and that excess decays naturally as those days exit the rolling window. When multiple grants are active, rolloff is anchored at the earliest active grant's start, not computed per-grant.

- Setting `net_grants` on an instance config now requires also setting `limit_seconds`. Configs that violate this will fail to load.
 
 ### Fixed

- Fixed timezone handling for net grants.

## [0.5.0] - 2026-06-03

### Changed

- `qauvern` now eagerly errors if the config file has any archived instances.
- Removed `target_usage_seconds` from the config file, as it was duplicative of IBM Quantum Platform's limit mechanism. See [here](https://github.com/IBM/qauvern/issues/102) for more motivation.

### Fixed

- `qauvern configure` no longer includes archived instances.

## [0.4.0] - 2026-05-28

### Changed

- `qauvern` only runs on the instances in your config file, whereas it would previously run on all instances in your account and plan. `optimize` and `analyze` will still take into consideration any unconfigured instances, but it will not touch their allocations or limits.
  - Caveat: with `optimize` and `analyze`, the configured instances will absorb all the available account allocation, which leaves no available allocation for the unconfigured instances. For example, if you configure 2 of 10 instances, those 2 will claim every spare second on the account and the remaining 8 are left with no buffer to expand into. We will add a buffer mechanism in the future.
- Output now uses the instance name you set in your config file with the `instances.name` key. Previously, it would use the live API name. The program will warn you if the names ever drift between your config file and the live API.
- `qauvern configure` no longer auto-populates `target_usage_seconds` for each instance. Auto-populating the optional field resulted in a bug where the optimizer could not increase allocations beyond the initial allocation. You can still manually configure `target_usage_seconds`.
- Dates in the config file must now set the UTC timezone offset in the ISO 8601 format, e.g. `2026-06-15T00:00:00+00:00`.

### Added

- `qauvern` now eagerly validates that all instances in the config file exist and still belong to your account and plan. This is to ensure that `qauvern` never runs on instances you do not expect.
- The `configure` command sorts instances by name.

### Fixed

- The `optimize` and `analyze` commands now validate that the proposed changes do not not exceed the account cap or any per-instance config target.
- Fix naive timezone handling, which could skew usage windows.
- Fix calculation of the 28-day consumption to consistently use IBM's official window, rather than using qauvern's own window.

## [0.3.0] - 2026-05-26

The focus of this release is improving `qauvern configure` and the configuration file.

### Changed

- In the configuration file, `projects` was renamed to `instances` and `project_limit_seconds` to `limit_seconds`. To fix, either manually update your config file keys or run `qauvern configure` to generate a new config file. (This change was to make the program simpler for users; projects and instances were the same thing.)
- `qauvern configure` now sets the name of `instances` to your actual instance's name, rather than values like `Project 1`. You do not need to update your config file, but it may be clearer to regenerate it with `qauvern configure`.
- Plans were reworked.
   - In config files, `plan_id` was replaced with `plan`, which should be set to `premium`, `internal`, or `paygo` rather than a UUID.
   - `qauvern configure` now requires the `--plan` argument.

### Fixed

- `qauvern configure` now sets the `target_usage_seconds` and `limit_seconds` for each instance based on its current settings.
- `qauvern optimize --dry-run` does not ask for confirmation.

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
