# qauvern — IBM Quantum Load Balancer

A Python CLI tool for optimizing quantum allocations across instances to maximize utilization of IBM Quantum resources.

## Overview

qauvern helps administrators manage quantum computing allocations efficiently by:

- Analyzing current instance usage and allocations
- Identifying underutilized instances
- Recommending optimal allocation adjustments
- Automatically applying optimizations to maximize resource utilization

## Key Concepts

- **QAU (Quantum Allocation Unit)**: 1 QAU = 1600 minutes of quantum computing time
- **Rolling Window**: 28-day backward-looking usage period
- **Fairness**: Ratio of consumed time to allocated time (lower fairness = higher priority)
- **Allocation**: Target consumption for an instance during the rolling window
- **Limit**: Hard cap on instance consumption
- **Net Grant**: Temporary time bonus above the base limit, active for a 28-day window. The effective limit decays as pre-grant usage rolls out of the window.

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

# Minimum allocation to maintain for each instance (optional, default: 60 seconds)
minimum_allocation_seconds: 60

# Hold back a percentage of account allocation from rebalancing (optional, default: 0)
# allocation_reserve_percent: 20

balance_period:
  start_date: "2026-01-01T00:00:00+00:00"
  end_date: "2026-12-31T23:59:59+00:00"

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

See [`examples/config-example.yaml`](examples/config-example.yaml) for a complete example. Use [`qauvern configure`](#configure-generate-configuration) to generate your initial file.

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

This command will:
1. Connect to the IBM Quantum API
2. List instances in the specified account that belong to the given plan
3. Generate a base YAML configuration
4. Display a summary of found instances

Options:
- `--account-id, -a`: IBM Cloud account ID (required)
- `--plan, -p`: Plan name — `internal`, `premium`, or `paygo` (required)
- `--api-key, -k`: IBM Cloud API key (or use `IBMCLOUD_API_KEY` env var)
- `--output, -o`: Output file path (default: `config.yaml`)
- `--balance-start`: Balance period start date (ISO format)
- `--balance-end`: Balance period end date (ISO format)

After generating the configuration, optionally set `limit_seconds` and `net_grants` per instance to control hard caps and temporary bonuses.

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

#### Optimize Allocations

Apply optimization recommendations to update instance allocations:

```bash
qauvern optimize --config config.yaml
qauvern optimize --config config.yaml --dry-run   # preview only
qauvern optimize --config config.yaml --yes        # skip confirmation (for automation)
```

This command will:
1. Confirm you want to proceed (bypass with `--yes`)
2. Calculate optimal allocations
3. Display proposed changes
4. Apply allocation and limit updates via API

Use `--dry-run` to compute and display changes without applying them.

> **Scope:** Like `analyze`, `optimize` only modifies instances listed in your config file. Unconfigured instances keep their existing allocation and limit; their allocation is reserved against the account cap when computing the new distribution. To bring an instance under management, add it to the config (or regenerate via `qauvern configure`).

#### Create Instance

Provision a new IBM Quantum service instance:

```bash
qauvern create my-instance \
  --target us-east \
  --resource-group your-resource-group-id \
  --plan premium
```

Options:
- `NAME` (positional, required): Name for the new instance
- `--target, -t`: Deployment region (required, e.g., `us-east`, `eu-de`)
- `--resource-group, -g`: IBM Cloud resource group ID (required)
- `--plan, -p`: Plan name — `internal`, `premium`, or `paygo` (required)
- `--allocation, -a`: Initial allocation (e.g., `96000`, `10h`, `2.5d`, `1qau`)
- `--limit, -l`: Instance limit, set after creation (e.g., `10h`, `1qau`)
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

1. **Classify** instances as active or inactive based on recent consumption
2. **Reclaim** allocation from inactive instances (down to the configured minimum)
3. **Redistribute** freed allocation to active instances weighted by fairness
4. **Enforce limits** using `limit_seconds` and any active `net_grants`

The optimizer targets low fairness values (< 0.5) to ensure instances receive scheduling priority from IBM Quantum's fair-share system. See the `net_grants` field in [Configuration](#configuration) for details on how grant rolloff works.

### Configured vs. Unconfigured Instances

`analyze` and `optimize` only operate on instances listed in your config file. Any other instance that exists on the same account and plan is **unconfigured** and is left exactly as-is — its allocation and limit are never touched.

Unconfigured instances still consume from the account-wide cap, so the optimizer subtracts their allocation before deciding how much to redistribute. Concretely:

```
redistributable = account.target_usage_seconds
                  − sum(unconfigured allocations)   ← reserved, untouched
                  − sum(28-day usage of configured) ← the floor we never go under
```

This means you can safely manage a subset of an account's instances with qauvern: anything you leave out of the config file is opaque to the optimizer except as a fixed reservation. To bring an instance under management, add it to the config (or regenerate with `qauvern configure`).

**Caveat:** the configured instances will absorb all the available account allocation, which leaves no available allocation for the unconfigured instances. For example, if you configure 2 of 10 instances, those 2 will claim every spare second on the account and the remaining 8 are left with no buffer to expand into. We will add a buffer mechanism in the future.

## Examples

### Basic Workflow

```bash
# 1. Generate initial configuration from your account
qauvern configure --account-id your-account-id --plan premium --output config.yaml

# 2. Edit config.yaml to edit instance allocations

# 3. Check current status
qauvern show --config config.yaml

# 4. Analyze and see recommendations
qauvern analyze --config config.yaml

# 5. Apply optimizations
qauvern optimize --config config.yaml
```

### Continuous Optimization

Run the optimizer periodically (e.g., weekly) to maintain optimal allocations:

```bash
# Add to crontab for weekly optimization
0 0 * * 0 qauvern optimize --config /path/to/config.yaml --yes
```
