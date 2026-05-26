# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Command-line interface for qauvern."""

import functools
import sys
from datetime import date
from pathlib import Path

import click
from tabulate import tabulate

from .api_client import IBMQuantumAPIClient
from .commands.configure import build_configure_yaml, build_instance_summary_table
from .config import ConfigParser
from .formatting import format_fairness, format_limit, format_seconds
from .models import Account, Instance, InstanceConfig, OptimizationRecommendation
from .optimizer import AllocationOptimizer
from .plan import Plan, plan_from_name, plan_id_for


def enrich_instances_with_usage_data(
    account: Account,
    instance_configs: list[InstanceConfig],
    client: IBMQuantumAPIClient,
    account_id: str,
    fetch_detailed_usage: bool = False,
) -> None:
    """Enrich account instances with target usage and optionally detailed usage data.

    Args:
        account: Account object with instances to enrich
        instance_configs: List of instance configs with configuration
        client: API client for fetching usage data
        account_id: Account ID for analytics authentication
        fetch_detailed_usage: If True, fetch detailed usage for multiple time periods
    """
    for instance in account.instances:
        # Find the config for this instance
        config = None
        for cfg in instance_configs:
            if cfg.crn == instance.crn:
                config = cfg
                break

        # Set target_usage_seconds from instance config
        if config and config.target_usage_seconds:
            instance.target_usage_seconds = config.target_usage_seconds
        else:
            instance.target_usage_seconds = 0

        # Optionally fetch detailed usage data
        if fetch_detailed_usage:
            try:
                # Usage since balance period start (if config found)
                if config and config.start_date:
                    from datetime import datetime

                    instance.consumed_balance_period = client.get_instance_usage_seconds(
                        instance.crn, config.start_date, datetime.now(), account_id
                    )
                else:
                    instance.consumed_balance_period = 0

                # Get detailed usage for multiple time periods
                detailed_usage = client.get_detailed_usage(instance.crn, account_id)
                instance.consumed_14day = detailed_usage["consumed_14day"]
                instance.consumed_7day = detailed_usage["consumed_7day"]
                instance.consumed_3day = detailed_usage["consumed_3day"]
                instance.consumed_24h = detailed_usage["consumed_24h"]

                # Fetch per-day usage for net grant rolloff calculation (60-day lookback)
                from datetime import date as _date, timedelta as _timedelta

                today_date = _date.today()
                daily_start = today_date - _timedelta(days=60)
                try:
                    instance.daily_usage = client.get_daily_usage(instance.crn, account_id, daily_start, today_date)
                except Exception as daily_e:
                    click.echo(f"Warning: Could not fetch daily usage for {instance.name}: {daily_e}", err=True)
                    instance.daily_usage = {}

            except Exception as e:
                click.echo(f"Warning: Could not fetch usage data for {instance.name}: {e}", err=True)
                instance.consumed_balance_period = 0
                instance.consumed_14day = 0
                instance.consumed_7day = 0
                instance.consumed_3day = 0
                instance.consumed_24h = 0
                instance.daily_usage = {}


def format_limit_display(
    limit_seconds: int | None,
    has_grant: bool = False,
    in_debt: bool = False,
    has_override: bool = False,  # kept for backwards compatibility
) -> str:
    """Format a limit value with optional grant and debt annotations."""
    if limit_seconds is None:
        return "-"
    base = format_limit(limit_seconds)
    annotation = ""
    if has_grant:
        annotation += " (+grant)"
    if in_debt:
        annotation += click.style(" !", fg="red", bold=True)
    return base + annotation


def format_reserve_summary(total: int, reserve_percent: float) -> str:
    """Format account reserve summary line."""
    available_for_rebalancing = int(total * (1 - reserve_percent / 100))
    return f"Reserve: {reserve_percent:.1f}%   Available for rebalancing: {format_seconds(available_for_rebalancing)}"


def parse_seconds(value: str) -> int:
    """Parse a human-friendly time string into seconds.

    Accepts plain integers (as seconds), suffixed values (10h, 30m, 2.5d, 96000s),
    or QAU units (1qau = 96000 seconds).
    """
    value = value.strip().lower()

    try:
        return int(value)
    except ValueError:
        pass

    suffixes = {
        "qau": 96000,
        "d": 86400,
        "h": 3600,
        "m": 60,
        "s": 1,
    }

    for suffix, multiplier in suffixes.items():
        if value.endswith(suffix):
            numeric_part = value[: -len(suffix)]
            try:
                return int(float(numeric_part) * multiplier)
            except ValueError:
                pass

    raise click.BadParameter(
        f"Cannot parse '{value}' as a time duration. Use plain seconds, or a suffix: 30m, 10h, 2.5d, 1qau"
    )


def format_instance_table(
    instances: list[Instance],
    instance_configs: list[InstanceConfig] | None = None,
    columns: list[str] | None = None,
    rec_map: dict[str, "OptimizationRecommendation"] | None = None,
) -> tuple[list[list[str]], list[str]]:
    """Format instance data into a table with configurable columns.

    Args:
        instances: List of Instance objects to display
        instance_configs: Optional list of InstanceConfig objects (needed for target columns)
        columns: List of column names to display. Available columns:
            - name: Instance name
            - target: Target usage seconds
            - target_pct: Percentage of target consumed
            - period: Balance period consumption
            - 28d: 28-day consumption
            - 14d: 14-day consumption
            - 7d: 7-day consumption
            - 3d: 3-day consumption
            - 24h: 24-hour consumption
            - allocation: Current allocation
            - consumed: Consumed seconds (28-day)
            - utilization: Utilization percentage
            - limit: Limit seconds
            - fairness: Fairness value
            - recommended: Recommended allocation (from rec_map)
            - change: Change amount (from rec_map)
            - reason: Reason for change (from rec_map)
        rec_map: Optional dict mapping CRN to recommendation data

    Returns:
        Tuple of (table_data, headers) ready for tabulate()
    """
    if columns is None:
        columns = ["name", "allocation", "consumed", "utilization", "limit", "fairness"]

    # Build config map if instance_configs provided
    config_map = {}
    if instance_configs:
        for cfg in instance_configs:
            config_map[cfg.crn] = cfg

    # Column header mapping
    header_map = {
        "name": "Instance",
        "target": "Target",
        "target_pct": "Target%",
        "period": "Period",
        "28d": "28d",
        "14d": "14d",
        "7d": "7d",
        "3d": "3d",
        "24h": "24h",
        "allocation": "Allocation",
        "consumed": "Consumed",
        "utilization": "Utilization",
        "limit": "Cur Limit",
        "new_limit": "New Limit",
        "fairness": "Fairness",
        "recommended": "Recommended",
        "change": "Change",
        "reason": "Reason",
    }

    headers = [header_map.get(col, col) for col in columns]
    table_data = []

    for instance in instances:
        row = []
        config = config_map.get(instance.crn) if config_map else None
        rec = rec_map.get(instance.crn) if rec_map else None

        for col in columns:
            if col == "name":
                row.append(instance.name[:35] if len(instance.name) > 35 else instance.name)
            elif col == "target":
                if config and config.target_usage_seconds is not None:
                    row.append(format_seconds(config.target_usage_seconds))
                else:
                    row.append("-")
            elif col == "target_pct":
                if config and config.target_usage_seconds:
                    pct = (instance.consumed_balance_period / config.target_usage_seconds) * 100
                    row.append(f"{pct:.1f}%")
                else:
                    row.append("-")
            elif col == "period":
                row.append(format_seconds(instance.consumed_balance_period))
            elif col == "28d":
                row.append(format_seconds(instance.consumed_seconds))
            elif col == "14d":
                row.append(format_seconds(instance.consumed_14day))
            elif col == "7d":
                row.append(format_seconds(instance.consumed_7day))
            elif col == "3d":
                row.append(format_seconds(instance.consumed_3day))
            elif col == "24h":
                row.append(format_seconds(instance.consumed_24h))
            elif col == "allocation":
                row.append(format_seconds(instance.allocation_seconds))
            elif col == "consumed":
                row.append(format_seconds(instance.consumed_seconds))
            elif col == "utilization":
                if instance.allocation_seconds > 0:
                    util = (instance.consumed_seconds / instance.allocation_seconds) * 100
                    row.append(f"{util:.1f}%")
                else:
                    row.append("0.0%")
            elif col == "limit":
                _has_grant = False
                _in_debt = getattr(instance, "in_debt", False)
                if config:
                    _today = date.today()
                    for _grant in getattr(config, "net_grants", []):
                        _gs = _grant.start_date.date()
                        if _gs <= _today < _grant.end_date.date():
                            _has_grant = True
                            break
                row.append(format_limit_display(instance.limit_seconds, has_grant=_has_grant, in_debt=_in_debt))
            elif col == "new_limit":
                _has_grant = False
                _in_debt = getattr(instance, "in_debt", False)
                if config:
                    _today = date.today()
                    for _grant in getattr(config, "net_grants", []):
                        _gs = _grant.start_date.date()
                        if _gs <= _today < _grant.end_date.date():
                            _has_grant = True
                            break
                _new_limit = rec.new_limit if rec and rec.new_limit is not None else None
                row.append(format_limit_display(_new_limit, has_grant=_has_grant, in_debt=_in_debt))
            elif col == "fairness":
                row.append(format_fairness(instance.fairness))
            elif col == "recommended":
                if rec:
                    row.append(format_seconds(rec.new_allocation))
                else:
                    row.append("-")
            elif col == "change":
                if rec:
                    change = rec.change
                    if change > 0:
                        change_str = f"+{format_seconds(change)}"
                    else:
                        change_str = f"-{format_seconds(change)}"
                    row.append(change_str)
                else:
                    row.append("-")
            elif col == "reason":
                if rec:
                    row.append(rec.reason[:30] if len(rec.reason) > 30 else rec.reason)
                else:
                    row.append("No change")
            else:
                row.append("-")

        table_data.append(row)

    return table_data, headers


config_option = click.option(
    "--config",
    "-c",
    type=click.Path(exists=True),
    required=True,
    help="Path to configuration YAML file",
)

api_key_option = click.option(
    "--api-key",
    "-k",
    envvar="IBMCLOUD_API_KEY",
    help="IBM Cloud API key (or set IBMCLOUD_API_KEY env var)",
)


def _parse_plan(_ctx: click.Context, _param: click.Parameter, value: str | None) -> Plan | None:
    return plan_from_name(value) if value else None


plan_option = click.option(
    "--plan",
    "-p",
    type=click.Choice([p.value for p in Plan], case_sensitive=False),
    required=True,
    callback=_parse_plan,
    help="Plan name: " + ", ".join(p.value for p in Plan),
)


def _build_client(ctx: click.Context, api_key: str | None) -> IBMQuantumAPIClient:
    staging = ctx.obj.get("staging", False)
    return IBMQuantumAPIClient(api_key=api_key, staging=staging)


def _load_config_and_client(
    ctx: click.Context, config: str, api_key: str | None
) -> tuple[ConfigParser, IBMQuantumAPIClient]:
    config_parser = ConfigParser(config)
    client = _build_client(ctx, api_key)
    return config_parser, client


def handle_errors(func):
    """Print any exception as ``Error: ...`` on stderr and exit with status 1."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    return wrapper


@click.group()
@click.version_option(package_name="qauvern")
@click.option(
    "--staging",
    is_flag=True,
    envvar="IBMCLOUD_STAGING",
    help="Use staging environment (test.cloud.ibm.com instead of cloud.ibm.com)",
)
@click.pass_context
def main(ctx, staging):
    """qauvern — IBM Quantum Load Balancer.

    Optimize quantum instance allocations to maximize utilization of your
    quantum allocation.
    """
    # Store staging flag in context for subcommands
    ctx.ensure_object(dict)
    ctx.obj["staging"] = staging


@main.command()
@config_option
@api_key_option
@click.pass_context
@handle_errors
def show(ctx, config: str, api_key: str | None):
    """Show current account and instance allocations for a specific plan."""
    config_parser, client = _load_config_and_client(ctx, config, api_key)
    account_id = config_parser.account_id
    plan = config_parser.plan

    click.echo(f"Fetching account information for plan {plan.value}...")
    account = client.get_account_with_instances(account_id, plan)

    click.echo("\n" + "=" * 80)
    click.echo("ACCOUNT SUMMARY")
    click.echo("=" * 80)
    click.echo(f"Account ID: {account.account_id}")
    click.echo(f"Plan: {plan.value}")
    click.echo(f"Target Usage: {format_seconds(account.target_usage_seconds)}")
    click.echo(f"Consumed: {format_seconds(account.consumed_seconds)}")
    click.echo(f"Available: {format_seconds(account.available_seconds)}")
    if account.allocation_reserve_percent > 0:
        click.echo(format_reserve_summary(account.available_seconds, account.allocation_reserve_percent))
    limit_display = format_seconds(account.limit_seconds) if account.limit_seconds else "Unlimited"
    click.echo(f"Limit: {limit_display}")
    click.echo(f"Utilization: {account.utilization:.1f}%")

    # Display instance details using utility function
    click.echo("\n" + "=" * 80)
    click.echo("INSTANCE USAGE SUMMARY")
    click.echo("=" * 80)

    columns = ["name", "allocation", "consumed", "utilization", "limit", "fairness"]
    table_data, headers = format_instance_table(account.instances, columns=columns)

    click.echo(tabulate(table_data, headers=headers, tablefmt="grid"))


@main.command()
@config_option
@api_key_option
@click.pass_context
@handle_errors
def instances(ctx, config: str, api_key: str | None):
    """Show instance usage summary for a specific plan.

    This command displays usage information for all instances defined in the
    configuration file that match the specified plan, without requiring admin privileges.
    """
    config_parser, client = _load_config_and_client(ctx, config, api_key)
    plan = config_parser.plan
    plan_uuid = plan_id_for(plan)
    instance_configs = config_parser.instance_configs

    all_crns = [cfg.crn for cfg in instance_configs]

    click.echo(f"Fetching usage information for {len(all_crns)} instances (plan: {plan.value})...")

    instances_data = []

    for crn in all_crns:
        try:
            instance = client.get_instance(crn)

            if instance.plan != plan_uuid:
                continue

            # Fetch the friendly name from Resource Controller
            instance.name = client.get_instance_name_from_crn(crn)

            # Fetch 28-day usage data using /v1/instances/usage endpoint
            # This endpoint does not require admin privileges
            try:
                instance.consumed_seconds = client.get_instance_usage_28d(crn)
            except ValueError as usage_error:
                # Show detailed error for JSON parsing issues
                click.echo(f"Warning: Could not fetch usage for {instance.name}:", err=True)
                click.echo(f"  {usage_error}", err=True)
                instance.consumed_seconds = 0
            except Exception as usage_error:
                click.echo(
                    f"Warning: Could not fetch usage for {instance.name}: {usage_error}",
                    err=True,
                )
                instance.consumed_seconds = 0

            instances_data.append(instance)
        except Exception as e:
            click.echo(f"Warning: Could not fetch instance {crn}: {e}", err=True)

    if not instances_data:
        click.echo("No instances found or accessible.", err=True)
        sys.exit(1)

    # Display instance summary using utility function
    click.echo("\n" + "=" * 80)
    click.echo("INSTANCE USAGE SUMMARY")
    click.echo("=" * 80)

    # Sort by fairness
    sorted_instances = sorted(instances_data, key=lambda x: x.fairness, reverse=True)

    columns = ["name", "allocation", "consumed", "utilization", "limit", "fairness"]
    table_data, headers = format_instance_table(sorted_instances, columns=columns)

    click.echo(tabulate(table_data, headers=headers, tablefmt="grid"))

    # Calculate totals
    total_allocation = sum(inst.allocation_seconds for inst in instances_data)
    total_consumed = sum(inst.consumed_seconds for inst in instances_data)

    # Display totals
    click.echo("\n" + "=" * 80)
    click.echo("TOTALS")
    click.echo("=" * 80)
    click.echo(f"Total Instances: {len(instances_data)}")
    click.echo(f"Total Allocation: {format_seconds(total_allocation)}")
    click.echo(f"Total Consumed: {format_seconds(total_consumed)}")
    if total_allocation > 0:
        utilization = (total_consumed / total_allocation) * 100
        click.echo(f"Overall Utilization: {utilization:.1f}%")

    click.echo("\nNote: This command does not require admin privileges.")
    click.echo("Use 'show' command for full account-level information (requires admin access).")


@main.command()
@config_option
@api_key_option
@click.pass_context
@handle_errors
def analyze(ctx, config: str, api_key: str | None):
    """Analyze allocations and show optimization recommendations."""
    config_parser, client = _load_config_and_client(ctx, config, api_key)
    account_id = config_parser.account_id
    plan = config_parser.plan
    instance_configs = config_parser.instance_configs

    click.echo(f"Fetching account information for plan {plan.value}...")
    account = client.get_account_with_instances(account_id, plan)

    # Enrich instances with target usage and detailed usage data
    click.echo("Fetching usage data for different time periods...")
    enrich_instances_with_usage_data(account, instance_configs, client, account_id, fetch_detailed_usage=True)

    # Get minimum allocation from config
    minimum_allocation_seconds = config_parser.minimum_allocation_seconds

    # Run optimization analysis
    account.allocation_reserve_percent = config_parser.allocation_reserve_percent
    click.echo("Analyzing allocations...")
    optimizer = AllocationOptimizer(account, instance_configs, minimum_allocation_seconds)
    result = optimizer.optimize()

    # Validate current allocations
    is_valid, errors = optimizer.validate_allocations()

    if not is_valid:
        click.echo("\n" + "=" * 80)
        click.echo("VALIDATION ERRORS")
        click.echo("=" * 80)
        for error in errors:
            click.echo(f"❌ {error}")

    # Display account summary with target usage and percentage
    click.echo("\n" + "=" * 80)
    click.echo("ACCOUNT PLAN ALLOCATION SUMMARY")
    click.echo("=" * 80)
    click.echo(f"Plan: {plan.value}")
    click.echo(f"Target Usage: {format_seconds(account.target_usage_seconds)}")

    # Calculate target usage percentage for balance period
    target_percentage = 0.0
    if account.target_usage_seconds > 0:
        # Sum up consumed_balance_period for all instances
        total_balance_consumed = sum(inst.consumed_balance_period for inst in account.instances)
        target_percentage = (total_balance_consumed / account.target_usage_seconds) * 100

    click.echo(
        f"Consumed (Balance Period): {format_seconds(sum(inst.consumed_balance_period for inst in account.instances))} ({target_percentage:.1f}% of target)"
    )
    click.echo(f"Consumed (28-day): {format_seconds(account.consumed_seconds)}")
    click.echo(f"Available: {format_seconds(account.available_seconds)}")
    if account.allocation_reserve_percent > 0:
        click.echo(format_reserve_summary(account.available_seconds, account.allocation_reserve_percent))
    limit_str = format_seconds(account.limit_seconds) if account.limit_seconds else "Unlimited"
    click.echo(f"Limit: {limit_str}")

    # Display instance analysis table (show ALL instances) using utility function
    click.echo("\n" + "=" * 80)
    click.echo("INSTANCE ANALYSIS")
    click.echo("=" * 80)

    # Build a map of recommendations by CRN
    rec_map = {}
    for rec in result.recommendations:
        rec_map[rec.instance_crn] = rec

    # Use utility function with all analysis columns
    columns = [
        "name",
        "target",
        "target_pct",
        "period",
        "28d",
        "14d",
        "7d",
        "3d",
        "24h",
        "allocation",
        "limit",
        "new_limit",
        "recommended",
        "change",
        "reason",
    ]
    table_data, headers = format_instance_table(
        account.instances, instance_configs=instance_configs, columns=columns, rec_map=rec_map
    )

    click.echo(tabulate(table_data, headers=headers, tablefmt="grid"))

    if result.recommendations:
        click.echo(f"\nTotal recommendations: {len(result.recommendations)}")
        click.echo("\nTo apply these recommendations, run: qauvern optimize")
    else:
        click.echo("\n✓ No optimization recommendations. Allocations are optimal.")


@main.command()
@config_option
@api_key_option
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be changed without making actual changes",
)
@click.confirmation_option(prompt="Are you sure you want to optimize allocations?")
@click.pass_context
@handle_errors
def optimize(ctx, config: str, api_key: str | None, dry_run: bool):
    """Optimize instance allocations and apply changes for a specific plan."""
    config_parser, client = _load_config_and_client(ctx, config, api_key)
    account_id = config_parser.account_id
    plan = config_parser.plan
    instance_configs = config_parser.instance_configs

    click.echo(f"Fetching account information for plan {plan.value}...")
    account = client.get_account_with_instances(account_id, plan)

    # Enrich instances with target usage (no detailed usage needed for optimize)
    enrich_instances_with_usage_data(account, instance_configs, client, account_id, fetch_detailed_usage=True)

    # Get minimum allocation from config
    minimum_allocation_seconds = config_parser.minimum_allocation_seconds

    # Run optimization
    account.allocation_reserve_percent = config_parser.allocation_reserve_percent
    click.echo("Computing optimal allocations...")
    optimizer = AllocationOptimizer(account, instance_configs, minimum_allocation_seconds)
    result = optimizer.optimize()

    if not result.recommendations:
        click.echo("✓ No optimization needed. Allocations are already optimal.")
        return

    # Display what will be changed
    click.echo("\n" + "=" * 80)
    click.echo("CHANGES TO BE APPLIED")
    click.echo("=" * 80)

    # Build a map of CRN to instance name
    instance_map = {inst.crn: inst.name for inst in account.instances}

    rec_data = []
    for rec in result.recommendations:
        change = rec.change
        if change > 0:
            change_str = f"+{format_seconds(change)}"
        else:
            change_str = f"{format_seconds(change)}"
        new_limit_str = format_seconds(rec.new_limit) if rec.new_limit is not None else "None"

        # Get instance name, truncate if too long
        instance_name = instance_map.get(rec.instance_crn, rec.instance_crn[:40] + "...")
        if len(instance_name) > 40:
            instance_name = instance_name[:37] + "..."

        rec_data.append(
            [
                instance_name,
                format_seconds(rec.current_allocation),
                format_seconds(rec.new_allocation),
                change_str,
                new_limit_str,
            ]
        )

    click.echo(
        tabulate(
            rec_data,
            headers=["Instance Name", "Current", "New", "Change", "New Limit"],
            tablefmt="grid",
        )
    )

    if dry_run:
        click.echo("\n[DRY RUN] No changes were made.")
        return

    # Apply changes - process reductions first, then additions
    click.echo("\nApplying changes...")
    success_count = 0
    error_count = 0

    # Process reductions first to free up allocation
    all_changes = list(result.reductions) + list(result.additions)

    for rec in all_changes:
        # Get instance name
        instance_name = instance_map.get(rec.instance_crn, rec.instance_crn[:40] + "...")
        if len(instance_name) > 40:
            instance_name = instance_name[:37] + "..."

        # Format the change
        change = rec.change
        if change > 0:
            change_str = f"+{format_seconds(change)}"
        else:
            change_str = f"{format_seconds(change)}"

        try:
            # Show what we're doing
            click.echo(
                f"  Updating {instance_name}: {format_seconds(rec.current_allocation)} → {format_seconds(rec.new_allocation)} ({change_str})"
            )

            # Update allocation
            client.update_instance_allocation(rec.instance_crn, rec.new_allocation)

            # Update limit if specified
            if rec.new_limit is not None:
                click.echo(f"    Setting limit: {format_seconds(rec.new_limit)}")
                client.update_instance_limit(rec.instance_crn, rec.new_limit)

            success_count += 1
            click.echo("    ✓ Success")
        except Exception as e:
            click.echo(f"    ❌ Failed: {e}", err=True)
            error_count += 1

    click.echo(f"\n✓ Successfully updated {success_count} instances")
    if error_count > 0:
        click.echo(f"❌ Failed to update {error_count} instances")


@main.command()
@click.option(
    "--account-id",
    "-a",
    required=True,
    help="IBM Cloud account ID to configure",
)
@plan_option
@api_key_option
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default="config.yaml",
    help="Output YAML configuration file path (default: config.yaml)",
)
@click.option(
    "--balance-start",
    default="2026-01-01T00:00:00",
    help="Balance period start date (ISO format, default: 2026-01-01T00:00:00)",
)
@click.option(
    "--balance-end",
    default="2026-12-31T23:59:59",
    help="Balance period end date (ISO format, default: 2026-12-31T23:59:59)",
)
@click.pass_context
@handle_errors
def configure(
    ctx,
    account_id: str,
    plan: Plan,
    api_key: str | None,
    output: str,
    balance_start: str,
    balance_end: str,
):
    """Generate a configuration file from an existing account.

    Queries the IBM Quantum API to list instances in the specified account
    that belong to the given plan, then generates a YAML configuration file.
    """
    click.echo(f"Connecting to IBM Quantum API for account {account_id} (plan: {plan.value})...")
    client = _build_client(ctx, api_key)

    click.echo("Fetching instances...")
    instances = client.get_account_with_instances(account_id, plan).instances

    if not instances:
        click.echo("⚠ No instances found in this account.", err=True)
        sys.exit(1)

    click.echo(f"Found {len(instances)} instances")
    click.echo("\nGenerating configuration file...")

    output_path = Path(output)
    output_path.write_text(build_configure_yaml(account_id, plan, instances, balance_start, balance_end))

    click.echo(f"\n✓ Configuration file created: {output_path}")
    click.echo("\nNext steps:")
    click.echo(f"1. Edit {output_path} to customize instance allocations")
    click.echo("2. Run 'qauvern analyze' to see optimization recommendations")
    click.echo("3. Run 'qauvern optimize' to apply optimizations")

    click.echo("\n" + "=" * 80)
    click.echo("INSTANCE SUMMARY")
    click.echo("=" * 80)

    rows, headers = build_instance_summary_table(instances)
    click.echo(tabulate(rows, headers=headers, tablefmt="grid"))


@main.command()
@click.argument("name")
@click.option(
    "--target",
    "-t",
    required=True,
    help="Deployment region (e.g., us-east, eu-de)",
)
@click.option(
    "--resource-group",
    "-g",
    required=True,
    help="IBM Cloud resource group ID",
)
@plan_option
@api_key_option
@click.option(
    "--allocation",
    "-a",
    default=None,
    help="Initial allocation (e.g., 96000, 10h, 2.5d, 1qau)",
)
@click.option(
    "--limit",
    "-l",
    default=None,
    help="Instance limit (e.g., 96000, 10h, 2.5d, 1qau). Set after creation.",
)
@click.option(
    "--tag",
    multiple=True,
    help="Tags to apply (can be specified multiple times)",
)
@click.pass_context
@handle_errors
def create(
    ctx,
    name: str,
    target: str,
    resource_group: str,
    plan: Plan,
    api_key: str | None,
    allocation: str | None,
    limit: str | None,
    tag: tuple,
):
    """Create a new IBM Quantum service instance.

    NAME is the name for the new instance.
    """
    allocation_seconds = None
    if allocation is not None:
        allocation_seconds = parse_seconds(allocation)
        if allocation_seconds < 0:
            raise click.BadParameter("Allocation must be non-negative")

    limit_seconds = None
    if limit is not None:
        limit_seconds = parse_seconds(limit)
        if limit_seconds <= 0:
            raise click.BadParameter("Limit must be positive")

    client = _build_client(ctx, api_key)

    click.echo(f"Creating instance '{name}' in {target} with plan {plan.value}...")
    if allocation_seconds is not None:
        click.echo(f"  Initial allocation: {format_seconds(allocation_seconds)}")

    tags = list(tag) if tag else None
    result = client.create_instance(
        name=name,
        target=target,
        resource_group=resource_group,
        plan=plan,
        allocation_seconds=allocation_seconds,
        tags=tags,
    )

    instance_crn = result.get("id", "")
    instance_name = result.get("name", name)
    instance_state = result.get("state", "unknown")

    click.echo("\nInstance created successfully.")
    click.echo(f"  Name:   {instance_name}")
    click.echo(f"  CRN:    {instance_crn}")
    click.echo(f"  State:  {instance_state}")
    click.echo(f"  Region: {target}")
    click.echo(f"  Plan:   {plan.value}")
    if allocation_seconds is not None:
        click.echo(f"  Allocation: {format_seconds(allocation_seconds)}")

    if limit_seconds is not None and instance_crn:
        click.echo(f"\nSetting instance limit to {format_seconds(limit_seconds)}...")
        try:
            client.update_instance_limit(instance_crn, limit_seconds)
            click.echo(f"  Limit set successfully: {format_seconds(limit_seconds)}")
        except Exception as limit_error:
            click.echo(
                f"  Warning: Instance created but limit could not be set: {limit_error}",
                err=True,
            )
            click.echo(
                "  You can set the limit later using the Quantum API.",
                err=True,
            )

    click.echo(f"\nInstance '{instance_name}' is ready.")


if __name__ == "__main__":
    main()
