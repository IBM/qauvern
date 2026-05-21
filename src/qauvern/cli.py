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

import sys
from datetime import date
from pathlib import Path

import click
from tabulate import tabulate

from .api_client import IBMQuantumAPIClient, get_plan_id, get_plan_name
from .config import ConfigParser, load_config
from .models import Account, Instance, OptimizationRecommendation, Project
from .optimizer import AllocationOptimizer


def enrich_instances_with_usage_data(
    account: Account,
    projects: list[Project],
    client: IBMQuantumAPIClient,
    account_id: str,
    fetch_detailed_usage: bool = False,
) -> None:
    """Enrich account instances with target usage and optionally detailed usage data.

    Args:
        account: Account object with instances to enrich
        projects: List of projects with configuration
        client: API client for fetching usage data
        account_id: Account ID for analytics authentication
        fetch_detailed_usage: If True, fetch detailed usage for multiple time periods
    """
    for instance in account.instances:
        # Find the project for this instance
        project = None
        for proj in projects:
            if proj.crn == instance.crn:
                project = proj
                break

        # Set target_usage_seconds from project configuration
        if project and project.target_usage_seconds:
            instance.target_usage_seconds = project.target_usage_seconds
        else:
            instance.target_usage_seconds = 0

        # Optionally fetch detailed usage data
        if fetch_detailed_usage:
            try:
                # Usage since balance period start (if project found)
                if project and project.start_date:
                    from datetime import datetime

                    instance.consumed_balance_period = client.get_instance_usage_seconds(
                        instance.crn, project.start_date, datetime.now(), account_id
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


def format_seconds(seconds: int) -> str:
    """Format seconds into a human-readable string."""
    seconds = abs(seconds)
    hours = seconds / 3600
    if hours < 1:
        return f"{seconds}s"
    elif hours < 24:
        return f"{hours:.1f}h"
    else:
        days = hours / 24
        return f"{days:.1f}d"


def format_fairness(fairness: float) -> str:
    """Format fairness value with color indicators."""
    if fairness < 0.5:
        return click.style(f"{fairness:.2f} ✓", fg="green")
    elif fairness < 1.0:
        return click.style(f"{fairness:.2f} ⚠", fg="yellow")
    else:
        return click.style(f"{fairness:.2f} ✗", fg="red")


def format_limit_display(
    limit_seconds: int | None,
    has_grant: bool = False,
    in_debt: bool = False,
    has_override: bool = False,  # kept for backwards compatibility
) -> str:
    """Format a limit value with optional grant and debt annotations."""
    if limit_seconds is None:
        return "-"
    base = format_seconds(limit_seconds)
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
    projects: list[Project] | None = None,
    columns: list[str] | None = None,
    rec_map: dict[str, "OptimizationRecommendation"] | None = None,
) -> tuple[list[list[str]], list[str]]:
    """Format instance data into a table with configurable columns.

    Args:
        instances: List of Instance objects to display
        projects: Optional list of Project objects (needed for target columns)
        columns: List of column names to display. Available columns:
            - name: Instance name
            - plan: Plan name
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
        columns = ["name", "plan", "allocation", "consumed", "utilization", "limit", "fairness"]

    # Build project map if projects provided
    project_map = {}
    if projects:
        for proj in projects:
            project_map[proj.crn] = proj

    # Column header mapping
    header_map = {
        "name": "Instance",
        "plan": "Plan",
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
        project = project_map.get(instance.crn) if project_map else None
        rec = rec_map.get(instance.crn) if rec_map else None

        for col in columns:
            if col == "name":
                row.append(instance.name[:35] if len(instance.name) > 35 else instance.name)
            elif col == "plan":
                row.append(get_plan_name(instance.plan))
            elif col == "target":
                if project and project.target_usage_seconds is not None:
                    row.append(format_seconds(project.target_usage_seconds))
                else:
                    row.append("-")
            elif col == "target_pct":
                if project and project.target_usage_seconds:
                    pct = (instance.consumed_balance_period / project.target_usage_seconds) * 100
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
                if project:
                    _today = date.today()
                    for _grant in getattr(project, "net_grants", []):
                        _gs = _grant.start_date.date()
                        if _gs <= _today < _grant.end_date.date():
                            _has_grant = True
                            break
                row.append(format_limit_display(instance.limit_seconds, has_grant=_has_grant, in_debt=_in_debt))
            elif col == "new_limit":
                _has_grant = False
                _in_debt = getattr(instance, "in_debt", False)
                if project:
                    _today = date.today()
                    for _grant in getattr(project, "net_grants", []):
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


def _build_client(ctx: click.Context, api_key: str | None) -> IBMQuantumAPIClient:
    staging = ctx.obj.get("staging", False)
    return IBMQuantumAPIClient(api_key=api_key, staging=staging)


def _load_config_and_client(
    ctx: click.Context, config: str, api_key: str | None
) -> tuple[ConfigParser, IBMQuantumAPIClient]:
    config_parser = load_config(config)
    client = _build_client(ctx, api_key)
    return config_parser, client


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

    Optimize quantum instance allocations across projects to maximize
    utilization of your quantum allocation.
    """
    # Store staging flag in context for subcommands
    ctx.ensure_object(dict)
    ctx.obj["staging"] = staging


@main.command()
@config_option
@api_key_option
@click.pass_context
def show(ctx, config: str, api_key: str | None):
    """Show current account and instance allocations for a specific plan."""
    try:
        config_parser, client = _load_config_and_client(ctx, config, api_key)
        account_id = config_parser.account_id
        plan_id = config_parser.plan_id

        # Get account with instances filtered by plan
        click.echo(f"Fetching account information for plan {get_plan_name(plan_id)}...")
        account = client.get_account_with_instances(account_id, plan_id)

        # Display account summary
        click.echo("\n" + "=" * 80)
        click.echo("ACCOUNT SUMMARY")
        click.echo("=" * 80)
        click.echo(f"Account ID: {account.account_id}")
        click.echo(f"Plan: {get_plan_name(account.plan_id)}")
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

        columns = ["name", "plan", "allocation", "consumed", "utilization", "limit", "fairness"]
        table_data, headers = format_instance_table(account.instances, columns=columns)

        click.echo(tabulate(table_data, headers=headers, tablefmt="grid"))
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command()
@config_option
@api_key_option
@click.pass_context
def instances(ctx, config: str, api_key: str | None):
    """Show instance usage summary for a specific plan.

    This command displays usage information for all instances defined in the
    configuration file that match the specified plan, without requiring admin privileges.
    """
    try:
        config_parser, client = _load_config_and_client(ctx, config, api_key)
        plan_id = config_parser.plan_id
        projects = config_parser.projects

        # Collect all CRNs from projects (one CRN per project)
        all_crns = [project.crn for project in projects]

        click.echo(f"Fetching usage information for {len(all_crns)} instances (plan: {get_plan_name(plan_id)})...")

        # Fetch instance information
        instances_data = []
        project_map = {}

        # Build project map (one CRN per project)
        for project in projects:
            project_map[project.crn] = project.name

        # Fetch each instance with 28-day usage data and filter by plan
        for crn in all_crns:
            try:
                instance = client.get_instance(crn)

                # Filter by plan_id - only include instances matching the configured plan
                if instance.plan != plan_id:
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

        columns = ["name", "plan", "allocation", "consumed", "utilization", "limit", "fairness"]
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

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command()
@config_option
@api_key_option
@click.pass_context
def analyze(ctx, config: str, api_key: str | None):
    """Analyze allocations and show optimization recommendations."""
    try:
        config_parser, client = _load_config_and_client(ctx, config, api_key)
        account_id = config_parser.account_id
        plan_id = config_parser.plan_id
        projects = config_parser.projects

        # Get account with instances filtered by plan
        click.echo(f"Fetching account information for plan {get_plan_name(plan_id)}...")
        account = client.get_account_with_instances(account_id, plan_id)

        # Enrich instances with target usage and detailed usage data
        click.echo("Fetching usage data for different time periods...")
        enrich_instances_with_usage_data(account, projects, client, account_id, fetch_detailed_usage=True)

        # Get minimum allocation from config
        minimum_allocation_seconds = config_parser.minimum_allocation_seconds

        # Run optimization analysis
        account.allocation_reserve_percent = config_parser.allocation_reserve_percent
        click.echo("Analyzing allocations...")
        optimizer = AllocationOptimizer(account, projects, minimum_allocation_seconds)
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
        click.echo(f"Plan: {get_plan_name(plan_id)}")
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
            account.instances, projects=projects, columns=columns, rec_map=rec_map
        )

        click.echo(tabulate(table_data, headers=headers, tablefmt="grid"))

        if result.recommendations:
            click.echo(f"\nTotal recommendations: {len(result.recommendations)}")
            click.echo("\nTo apply these recommendations, run: qauvern optimize")
        else:
            click.echo("\n✓ No optimization recommendations. Allocations are optimal.")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


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
def optimize(ctx, config: str, api_key: str | None, dry_run: bool):
    """Optimize instance allocations and apply changes for a specific plan."""
    try:
        config_parser, client = _load_config_and_client(ctx, config, api_key)
        account_id = config_parser.account_id
        plan_id = config_parser.plan_id
        projects = config_parser.projects

        # Get account with instances filtered by plan
        click.echo(f"Fetching account information for plan {get_plan_name(plan_id)}...")
        account = client.get_account_with_instances(account_id, plan_id)

        # Enrich instances with target usage (no detailed usage needed for optimize)
        enrich_instances_with_usage_data(account, projects, client, account_id, fetch_detailed_usage=True)

        # Get minimum allocation from config
        minimum_allocation_seconds = config_parser.minimum_allocation_seconds

        # Run optimization
        account.allocation_reserve_percent = config_parser.allocation_reserve_percent
        click.echo("Computing optimal allocations...")
        optimizer = AllocationOptimizer(account, projects, minimum_allocation_seconds)
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

    except Exception as e:
        click.echo(f"Error: {e}", err=True)


@main.command()
@click.option(
    "--account-id",
    "-a",
    required=True,
    help="IBM Cloud account ID to configure",
)
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
def configure(
    ctx,
    account_id: str,
    api_key: str | None,
    output: str,
    balance_start: str,
    balance_end: str,
):
    """Generate a configuration file from an existing account.

    This command queries the IBM Quantum API to list all instances in the
    specified account and generates a base YAML configuration file that can
    be customized with project information.
    """
    try:
        import yaml

        click.echo(f"Connecting to IBM Quantum API for account {account_id}...")
        client = _build_client(ctx, api_key)

        # List instances (does not require admin privileges)
        click.echo("Fetching instances...")
        instances = client.list_instances(account_id)

        if not instances:
            click.echo("⚠ No instances found in this account.", err=True)
            sys.exit(1)

        click.echo(f"Found {len(instances)} instances")

        # Build configuration structure
        config = {
            "account_id": account_id,
            "balance_period": {
                "start_date": balance_start,
                "end_date": balance_end,
            },
            "projects": [],
        }

        # Create one project per instance (since projects and instances are 1:1)
        # Users can customize names and allocations
        for i, inst in enumerate(instances, 1):
            project = {
                "name": f"Project {i}",
                "description": f"Auto-generated from instance {inst.name or inst.crn[:50]}",
                "crn": inst.crn,
                "target_usage_seconds": inst.allocation_seconds or 96000,  # Default 1 QAU if not set
            }
            config["projects"].append(project)

        # Add comments about customization
        click.echo("\nGenerating configuration file...")

        # Write YAML file
        output_path = Path(output)
        with open(output_path, "w") as f:
            f.write("# qauvern configuration\n")
            f.write("# Auto-generated configuration file\n")
            f.write("#\n")
            f.write("# IMPORTANT: This is a base configuration with all instances\n")
            f.write("# grouped into a single project. You should customize this by:\n")
            f.write("#\n")
            f.write("# 1. Creating separate projects for different teams/purposes\n")
            f.write("# 2. Assigning instance CRNs to appropriate projects\n")
            f.write("# 3. Setting appropriate target_usage_seconds for each project\n")
            f.write("# 4. Adjusting balance period dates as needed\n")
            f.write("#\n")
            f.write(f"# Account: {account_id}\n")
            f.write(f"# Instances Found: {len(instances)}\n")
            f.write("#\n")
            f.write("# Note: Each project corresponds to exactly one service instance.\n")
            f.write("#\n\n")

            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

            f.write("\n# Instance Details:\n")
            f.write("# The following instances were found in your account:\n")
            f.write("#\n")
            for inst in instances:
                f.write(f"# - {inst.name or 'Unnamed'}\n")
                f.write(f"#   CRN: {inst.crn}\n")
                f.write(f"#   Allocation: {format_seconds(inst.allocation_seconds)}\n")
                f.write(f"#   Consumed: {format_seconds(inst.consumed_seconds)}\n")
                if inst.limit_seconds:
                    f.write(f"#   Limit: {format_seconds(inst.limit_seconds)}\n")
                f.write(f"#   Fairness: {inst.fairness:.2f}\n")
                f.write("#\n")

        click.echo(f"\n✓ Configuration file created: {output_path}")
        click.echo("\nNext steps:")
        click.echo(f"1. Edit {output_path} to customize project names and allocations")
        click.echo("2. Run 'qauvern analyze' to see optimization recommendations")
        click.echo("3. Run 'qauvern optimize' to apply optimizations")

        # Display summary table
        click.echo("\n" + "=" * 80)
        click.echo("INSTANCE SUMMARY")
        click.echo("=" * 80)

        instance_data = []
        for inst in instances:
            instance_data.append(
                [
                    inst.name[:40],
                    format_seconds(inst.allocation_seconds),
                    format_seconds(inst.consumed_seconds),
                    format_fairness(inst.fairness),
                ]
            )

        click.echo(
            tabulate(
                instance_data,
                headers=["Instance Name", "Allocation", "Consumed", "Fairness"],
                tablefmt="grid",
            )
        )

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

        sys.exit(1)


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
@click.option(
    "--plan",
    "-p",
    required=True,
    help="Plan name (internal, premium, paygo) or plan UUID",
)
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
def create(
    ctx,
    name: str,
    target: str,
    resource_group: str,
    plan: str,
    api_key: str | None,
    allocation: str | None,
    limit: str | None,
    tag: tuple,
):
    """Create a new IBM Quantum service instance.

    NAME is the name for the new instance.
    """
    try:
        plan_id = get_plan_id(plan)

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

        click.echo(f"Creating instance '{name}' in {target} with plan {get_plan_name(plan_id)}...")
        if allocation_seconds is not None:
            click.echo(f"  Initial allocation: {format_seconds(allocation_seconds)}")

        tags = list(tag) if tag else None
        result = client.create_instance(
            name=name,
            target=target,
            resource_group=resource_group,
            resource_plan_id=plan_id,
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
        click.echo(f"  Plan:   {get_plan_name(plan_id)}")
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

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
