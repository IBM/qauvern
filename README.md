# qauvern — IBM Quantum Load Balancer

A Python CLI tool for optimizing quantum allocations across instances to maximize utilization of IBM Quantum resources.

> The name comes from **QAU** (Quantum Allocation Unit) + **govern** — managing and governing those allocations.

## Overview

qauvern helps administrators manage quantum computing allocations efficiently by:

- Analyzing current instance usage and allocations
- Identifying underutilized instances
- Recommending optimal allocation adjustments
- Automatically applying optimizations to maximize resource utilization

## Key Concepts

- **Rolling Window**: 28-day backward-looking usage period
- **Fairness**: Ratio of consumed time to allocated time (lower fairness = higher priority)
- **Allocation**: Target consumption for an instance during the rolling window
- **Limit**: Hard cap on instance consumption
- **Net Grant**: Temporary time bonus above the base limit. Multiple grants stack, and any pre-grant usage that exceeded the base limit decays out of the effective limit as those days exit the 28-day rolling window.

## Installation

There are two ways to install qauvern:

- **Pex** — a single self-contained file you can download and run directly. No virtual environment or dependency management needed; just Python 3.10+ on your system. Best if you want to get running quickly or avoid modifying your Python environment. ([What is Pex?](https://docs.pex-tool.org/))
- **pip** — a standard install from source into a virtual environment. Best if you want to pin a version in a requirements file or integrate with an existing Python workflow.

### Option 1: Pex (single-file executable)

Download `qauvern.pex` from the [GitHub Releases](https://github.com/ibm/qauvern/releases) page. The Pex file only requires Python 3.10+ on your macOS or Linux system — no pip or virtual environment needed.

```bash
chmod +x qauvern.pex
./qauvern.pex --help
```

You can also build the Pex yourself if you have the repo cloned and [Just](https://github.com/casey/just) installed (see [CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
just pex
./dist/qauvern.pex --help
```

### Option 2: pip install

Install ["qauvern"](https://pypi.org/project/qauvern/) into a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install qauvern
```

The `qauvern` CLI is available while the virtual environment is active:

```bash
qauvern --help
```

## Configuration

The program operates on YAML configuration files where you define your account, plan, and instances, such as:

```yaml
# config.yaml
account_id: "your-ibm-cloud-account-id"
plan: "premium"  # one of: internal, premium, paygo

# Minimum allocation to maintain for each instance.
# `qauvern configure` defaults to 60 seconds.
minimum_allocation_seconds: 60

# Hold back a percentage of account allocation from rebalancing (optional, default: 0)
# allocation_reserve_percent: 20

instances:
  - name: "Quantum Chemistry Research"
    crn: "crn:v1:bluemix:public:quantum-computing:us-east:a/abc123:instance-1::"

    # Optional: Hard limit applied on every optimize run
    # limit_seconds: 216000

    # Optional: Temporary time bonus above limit_seconds.
    # end_date is optional; defaults to start_date + 28 days if omitted.
    # net_grants:
    #   - start_date: "2026-05-01T00:00:00+00:00"
    #     net_grant_seconds: 360000  # 100 extra hours for a May sprint
    #   - start_date: "2026-06-15T00:00:00+00:00"
    #     end_date: "2026-07-31T00:00:00+00:00"
    #     net_grant_seconds: 180000

  - name: "Quantum Machine Learning"
    crn: "crn:v1:bluemix:public:quantum-computing:us-east:a/abc123:instance-2::"
    limit_seconds: 72000
```

See [`examples/config-example.yaml`](examples/config-example.yaml) for a complete example. Use [`qauvern configure`](#configure-generate-configuration) to generate your initial file. Use [`qauvern update`](#update-reconcile-configuration) to automatically update the config file, such as adding new instances.

You should check in this file to version control.

## Usage

### Authentication

The tool uses IBM Cloud IAM for authentication. Set your IBM Cloud API key as an environment variable:

```bash
export IBMCLOUD_API_KEY="your-ibm-cloud-api-key"
```

Or pass it directly with the `--api-key` flag to any command.

#### Obtaining an IBM Cloud API Key

1. Log in to [IBM Cloud](https://cloud.ibm.com/)
2. Go to **Manage** > **Access (IAM)** > **API keys**
3. Click **Create an IBM Cloud API key**
4. Give it a name and description
5. Copy the API key (you won't be able to see it again)

### Commands

#### Configure (Generate Configuration)

Generate a base configuration file from an existing IBM Cloud account:

```bash
qauvern configure --account-id your-account-id --plan premium --output config.yaml
```

This queries the IBM Quantum API to discover active instances for the given account and plan, then writes a YAML config file you can then edit.

Options:
- `--account-id, -a`: IBM Cloud account ID (required)
- `--plan, -p`: Plan name — `internal`, `premium`, or `paygo` (required)
- `--api-key, -k`: IBM Cloud API key (or use `IBMCLOUD_API_KEY` env var)
- `--region`: Limit to instances in a specific region (e.g., `us-east`, `eu-de`). Because all other commands only operate on instances in your config file, you can use this to restrict `qauvern` to a single region.
- `--output, -o`: Output file path (default: `config.yaml`)

After generating the configuration, optionally make these edits:

- Set `limit_seconds` and `net_grants` per instance to control hard caps and temporary bonuses.
- Change `minimum_allocation_seconds` from its default of 60 seconds.
- Set `allocation_reserve_percent` from `[0, 100)` to reserve a buffer of allocation.

Use [`qauvern update`](#update-reconcile-configuration) to keep the file in sync as instances are added, removed, or renamed.

#### Update (Reconcile Configuration)

Reconcile an existing configuration file with the live IBM Quantum API:

```bash
qauvern update --config config.yaml
qauvern update --config config.yaml --dry-run   # preview changes only
```

The `update` command asks for confirmation before making edits.

Whereas `configure` generates a fresh file from scratch, `update` is for ongoing maintenance of an existing config. It performs four reconciliation steps by default:

- **Expire net_grants**: drops `net_grants` entries whose `end_date` has passed
- **Remove instances**: removes entries for archived or missing instances
- **Fix names**: updates instance names that have drifted from the live API
- **Add instances**: appends newly discovered instances

Comments and customizations (`limit_seconds`, `allocation_reserve_percent`, custom dates, etc.) are preserved because the file is rewritten in round-trip YAML mode.

Options:
- `--config, -c`: Path to the config file (required)
- `--api-key, -k`: IBM Cloud API key (or use `IBMCLOUD_API_KEY` env var)
- `--region`: Limit discovery to a specific region, like `us-east` or `eu-de`.
- `--dry-run`: Print planned changes without writing the file
- `--yes, -y`: Skip the confirmation prompt (for automation)
- `--no-net-grants`: Skip expiring `net_grants`
- `--no-add`: Skip adding newly discovered instances
- `--no-names`: Skip fixing instance name drift
- `--no-remove`: Skip removing archived/missing instances

#### Show Current Allocations

Display a summary of your account and instance allocations:

```bash
qauvern show --config config.yaml
```

Output includes:
- Account summary (total allocation, consumption, utilization)
- Instance details with fairness values

#### Instances (Non-Admin View)

Display instance usage summary, without requiring admin privileges:

```bash
qauvern instances --config config.yaml
```

#### Analyze Allocations

Analyze current allocations and show optimization recommendations, without making changes:

```bash
qauvern analyze --config config.yaml
```

This command identifies underutilized instances, calculates optimal reallocations, and shows what changes would be made. The output includes a **Cur Limit** column (the live API value) and a **New Limit** column (what the optimizer would set). A `(+grant)` annotation indicates an active net grant is boosting the limit; `!` indicates the instance is in debt.

> **Scope:** `analyze` only considers instances listed in your config file. Allocation held by unconfigured instances on the same account+plan is left untouched, but it is counted against the account cap so the recommendations never overcommit. The summary block reports it on the `Held by unconfigured instances` line.

##### Output formats

Use `--format` to choose how results are rendered:

- `table` (default) — human-readable summary block plus an instance table.
- `csv` — one row per configured instance, suitable for spreadsheets or quick pipelines. Account-level info is omitted because CSV is a flat row-based format.
- `json` — a structured payload for scripts. Includes account-level info, the reserve, validation errors, and per-instance rows with pre-computed allocation and limit deltas.

Both `csv` and `json` write data to stdout and logs to stderr, allowing you to pipe the stdout:

```bash
qauvern analyze --config config.yaml --format csv  > analysis.csv
qauvern analyze --config config.yaml --format json > analysis.json
```

`json` writes validatoin errors to a `validation_errors` array, whereas `csv` logs the errors to stderr.

Common notes for both machine formats:

- All durations are raw integer seconds.
- The "new" allocation/limit value is always emitted, even if it is the same as the current value. Use the delta fields to quickly determine if there was a change, such as `limit_delta_seconds` with `json`. 

Inspect the JSON schema with `jq keys` and `jq '.instances[0] | keys'` against a real run.

#### Optimize Allocations

Apply optimization recommendations to update instance allocations:

```bash
qauvern optimize --config config.yaml
qauvern optimize --config config.yaml --dry-run   # preview only
```

This command will:
1. Calculate optimal allocations.
2. Display proposed changes.
3. Prompt for confirmation.
4. Apply allocation and limit updates via API.

Use `--dry-run` to compute and display changes without applying them. Use `--yes` / `-y` to skip the confirmation prompt in automated pipelines.

> **Scope:** Like `analyze`, `optimize` only modifies instances listed in your config file. Unconfigured instances keep their existing allocation and limit; their allocation is reserved against the account cap when computing the new distribution. To bring an instance under management, add it to the config (or regenerate via `qauvern configure`).

#### Create Instance

Provision a new IBM Quantum service instance:

```bash
qauvern create my-instance \
  --target us-east \
  --resource-group your-resource-group-id \
  --plan premium \
  --allocation 10h
```

Options:
- `NAME` (positional, required): Name for the new instance
- `--target, -t`: Deployment region (required, e.g., `us-east`, `eu-de`)
- `--resource-group, -g`: IBM Cloud resource group ID (required)
- `--plan, -p`: Plan name — `internal`, `premium`, or `paygo` (required)
- `--allocation, -a`: Initial allocation (required, e.g., `96000`, `10h`, `2.5d`)
- `--limit, -l`: Instance limit (e.g., `9600`, `10h`)
- `--tag`: Tags to apply (repeatable)

### Staging Environment

To target the IBM Quantum staging environment (`test.cloud.ibm.com`) instead of production, use the `--staging` flag or set the `IBMCLOUD_STAGING` environment variable:

```bash
qauvern --staging analyze --config config-staging.yaml
# or
export IBMCLOUD_STAGING=True
```

The `--staging` flag is a global option and applies to all commands.

## How It Works

### Optimization Algorithm

For each managed instance, qauvern:

1. **Resolves the effective limit** from `limit_seconds` and any active `net_grants` in the config file. If the config file does not set `limit_seconds` or `net_grants`, use the live limit in IBM Quantum Platform, if any. This is the upper bound on the instance's allocation.
2. **Computes an activity score** by exponentially weighting recent usage (24h carries 16× the weight of 28d). Instances with no usage across all buckets get score 0 and are classified inactive.

Then, account-wide:

3. **Pins every managed instance to its floor** — `max(minimum_allocation_seconds, 28-day consumed)`. Inactive instances stay at the floor.
4. **Builds a redistribution pool** from unallocated headroom plus everything managed instances hold above their floor. If `allocation_reserve_percent` is set, scales the pool down by that fraction.
5. **Water-fills the pool across active instances** proportional to activity score. When an instance hits its effective limit, it drops out and its surplus flows to the rest. If every active instance is capped, leftover capacity stays unallocated rather than being forced onto any instance.

See [Design.md](Design.md) for full algorithm details and the invariants the optimizer enforces.

### Configured vs. Unconfigured Instances

`analyze`, `optimize`, `show`, and `instances` only operate on instances listed in your config file. Any other instance that exists on the same account and plan is **unconfigured** and is left exactly as-is — its allocation and limit are never touched.

Unconfigured instances still consume from the account-wide cap, so the optimizer subtracts their allocation before deciding how much to redistribute. Concretely:

```
raw_pool = account.allocation_budget
           − sum(unconfigured allocations)   ← reserved, untouched
           − sum(floors of configured)       ← max(minimum_allocation_seconds, 28-day usage)

redistributable = raw_pool × (1 − allocation_reserve_percent / 100)
```

This means you can safely manage a subset of an account's instances with qauvern: anything you leave out of the config file is opaque to the optimizer except as a fixed reservation. To bring an instance under management, add it to the config (or regenerate with `qauvern configure`).

**Caveat:** the configured instances will absorb all the available account allocation, which leaves no available allocation for the unconfigured instances. For example, if you configure 2 of 10 instances, those 2 will claim every spare second on the account and the remaining 8 are left with no buffer to expand into. Use `allocation_reserve_percent` to hold back a fraction of the pool if you need headroom for unconfigured instances.

## Examples

### Basic Workflow

```bash
# 1. Generate initial configuration from your account
qauvern configure --account-id your-account-id --plan premium --output config.yaml

# 2. Edit config.yaml to customize limits, net_grants, and other config like `allocation_reserve_percent`.

# 3. Check current status
qauvern show --config config.yaml

# 4. Analyze and see recommendations
qauvern analyze --config config.yaml

# 5. Apply optimizations
qauvern optimize --config config.yaml
```

After the initial setup, run `qauvern update --config config.yaml` periodically to keep the config in sync with the live API (new instances, renames, archived instances, expired net_grants).

### Automations

`qauvern optimize --yes` skips the confirmation prompt, and `qauvern analyze --format json` emits a structured report — together they make the tool easy to drive from cron, CI, or a monitoring script.

Run the optimizer on a schedule to keep allocations balanced:

```bash
# Add to crontab for weekly optimization
0 0 * * 0 qauvern optimize --config /path/to/config.yaml --yes
```

Capture a periodic analysis snapshot for dashboards or alerting:

```bash
qauvern analyze --config /path/to/config.yaml --format json > /var/log/qauvern/$(date +%Y-%m-%d).json
```

For example, surface instances the optimizer wants to shrink:

```bash
qauvern analyze --config config.yaml --format json \
  | jq '.instances[] | select(.allocation_delta_seconds < 0) | {name, allocation_delta_seconds, allocation_change_reason}'
```
