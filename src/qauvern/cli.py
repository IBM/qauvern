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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from tabulate import tabulate

from .api_client import IBMQuantumAPIClient
from .commands.configure import build_configure_yaml
from .commands.update import (
    UpdateActions,
    compute_update,
    format_update_summary,
    load_config_doc,
    write_config_doc,
)
from .config import ConfigParser, parse_utc_datetime
from .formatting import (
    format_instance_analysis_table,
    format_instance_summary_table,
    format_reserve_summary,
    format_seconds,
    parse_seconds,
)
from .models import (
    Account,
    AllocationChange,
    DiscoveredInstance,
    InstanceConfig,
    InstanceDetailedUsage,
    InstanceState,
    LimitChange,
)
from .optimizer import AllocationOptimizer
from .plan import Plan, plan_from_name
from .region import Region


def enrich_instances_with_usage_data(
    account: Account,
    instance_configs: list[InstanceConfig],
    client: IBMQuantumAPIClient,
) -> None:
    for instance in account.instances:
        config = next((cfg for cfg in instance_configs if cfg.crn == instance.crn), None)

        try:
            # Usage since balance period start (if config found)
            consumed_balance_period = (
                client.get_instance_usage_seconds(
                    instance.crn, config.start_date, datetime.now(tz=timezone.utc), account.account_id
                )
                if config and config.start_date
                else 0
            )

            # Get detailed usage for multiple time periods
            detailed_usage = client.get_detailed_usage(instance.crn, account.account_id)

            # Fetch per-day usage for net grant rolloff calculation (60-day lookback)
            today_date = datetime.now(timezone.utc).date()
            daily_start = today_date - timedelta(days=60)
            try:
                daily = client.get_daily_usage(instance.crn, account.account_id, daily_start, today_date)
            except Exception as daily_e:
                click.echo(f"Warning: Could not fetch daily usage for {instance.name}: {daily_e}", err=True)
                daily = {}

            instance.detailed_usage = InstanceDetailedUsage(
                consumed_balance_period=consumed_balance_period,
                consumed_14day=detailed_usage["consumed_14day"],
                consumed_7day=detailed_usage["consumed_7day"],
                consumed_3day=detailed_usage["consumed_3day"],
                consumed_24h=detailed_usage["consumed_24h"],
                daily_usage=daily,
            )

        except Exception as e:
            click.echo(f"Warning: Could not fetch usage data for {instance.name}: {e}", err=True)
            instance.detailed_usage = InstanceDetailedUsage(
                consumed_balance_period=0,
                consumed_14day=0,
                consumed_7day=0,
                consumed_3day=0,
                consumed_24h=0,
                daily_usage={},
            )


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


def _parse_region(_ctx: click.Context, _param: click.Parameter, value: str | None) -> Region | None:
    return Region(value) if value else None


region_option = click.option(
    "--region",
    default=None,
    type=click.Choice([r.value for r in Region], case_sensitive=False),
    callback=_parse_region,
    help="Limit to instances in a specific region (e.g. us-east, eu-de)",
)


def _parse_balance_date(_ctx: click.Context, param: click.Parameter, value: str) -> str:
    try:
        return parse_utc_datetime(value, provenance=param.opts[0]).isoformat()
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


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


@dataclass(frozen=True)
class CommandSession:
    """Bundle of state shared by every command that loads a config + live API view."""

    config: ConfigParser
    client: IBMQuantumAPIClient
    configured_instances: tuple[DiscoveredInstance, ...]


def _open_session(ctx: click.Context, config: str, api_key: str | None) -> CommandSession:
    config_parser = ConfigParser(config)
    client = _build_client(ctx, api_key)
    discovered = client.discover_instances(config_parser.account_id, config_parser.plan)
    name_drifts = config_parser.validate_instances_against_api(discovered)
    if name_drifts:
        bullets = "\n".join(f"  - {d}" for d in name_drifts)
        click.echo(
            f"Warning: Configured instance names differ from the live API for account "
            f"{config_parser.account_id} on plan {config_parser.plan.value}. "
            f"Update your config:\n{bullets}",
            err=True,
        )
    by_crn = {d.crn: d for d in discovered.active}
    configured = tuple(by_crn[cfg.crn] for cfg in config_parser.instance_configs if cfg.crn in by_crn)
    return CommandSession(config=config_parser, client=client, configured_instances=configured)


def _fetch_instance_states(
    client: IBMQuantumAPIClient, discovered: Sequence[DiscoveredInstance]
) -> list[InstanceState]:
    """Fetch live usage for each instance, warning and skipping any that fail."""
    states: list[InstanceState] = []
    for d in discovered:
        try:
            states.append(client.get_instance(d))
        except Exception as e:
            click.echo(f"Warning: Could not fetch full data for instance `{d.name}`, so skipping: {e}", err=True)
    return states


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
    """Show current account and instance allocations, including admin info.

    Account totals (target, available, limit) are account-wide. Per-instance
    rows and the "Consumed (configured)" line cover only instances listed in
    the config file; allocation held by unmanaged instances is reported
    separately so the cap math is transparent.
    """
    session = _open_session(ctx, config, api_key)
    config_parser = session.config
    client = session.client

    click.echo(
        f"Fetching account information and {len(config_parser.instance_configs)} configured instances from {config}..."
    )
    instance_states = _fetch_instance_states(client, session.configured_instances)
    account = client.get_account(config_parser.account_id, config_parser.plan, instance_states)

    click.echo("\n" + "=" * 80)
    click.echo("ACCOUNT SUMMARY")
    click.echo("=" * 80)
    click.echo(f"Account ID: {account.account_id}")
    click.echo(f"Plan: {config_parser.plan.value}")
    click.echo(f"Allocation budget: {format_seconds(account.allocation_budget_seconds)}")
    click.echo(f"Unallocated: {format_seconds(account.unallocated_seconds)}")
    click.echo(f"Consumed (configured instances): {format_seconds(account.consumed_seconds)}")
    if account.unmanaged_allocation_seconds > 0:
        click.echo(
            f"Held by unconfigured instances: {format_seconds(account.unmanaged_allocation_seconds)} "
            "(not shown below; counted against cap)"
        )
    if config_parser.allocation_reserve_percent > 0:
        pool, _ = AllocationOptimizer(
            account,
            config_parser.instance_configs,
            config_parser.minimum_allocation_seconds,
            allocation_reserve_percent=config_parser.allocation_reserve_percent,
        ).redistribution_pool()
        click.echo(format_reserve_summary(pool, config_parser.allocation_reserve_percent))
    limit_display = format_seconds(account.limit_seconds) if account.limit_seconds else "Unlimited"
    click.echo(f"Limit: {limit_display}")

    # Display instance details using utility function
    click.echo("\n" + "=" * 80)
    click.echo(f"INSTANCE USAGE SUMMARY ({len(account.instances)} configured)")
    click.echo("=" * 80)

    table_data, headers = format_instance_summary_table(account.instances)

    click.echo(tabulate(table_data, headers=headers, tablefmt="grid"))


@main.command()
@config_option
@api_key_option
@click.pass_context
@handle_errors
def instances(ctx, config: str, api_key: str | None):
    """Show instance usage summary, without requiring admin privileges.

    Only instances listed in the config file are queried. Account-wide
    totals (and unmanaged-instance allocation) are not available here
    because that data requires admin access — use `show` for that view.
    """
    session = _open_session(ctx, config, api_key)
    config_parser = session.config
    client = session.client

    click.echo(
        f"Fetching usage information for {len(config_parser.instance_configs)} configured instances from {config}"
    )

    instances_data = _fetch_instance_states(client, session.configured_instances)

    if not instances_data:
        click.echo("No instances found or accessible.", err=True)
        sys.exit(1)

    # Display instance summary using utility function
    click.echo("\n" + "=" * 80)
    click.echo("INSTANCE USAGE SUMMARY")
    click.echo("=" * 80)

    # Sort by fairness
    sorted_instances = sorted(instances_data, key=lambda x: x.fairness, reverse=True)

    table_data, headers = format_instance_summary_table(sorted_instances)

    click.echo(tabulate(table_data, headers=headers, tablefmt="grid"))

    # Calculate totals
    total_allocation = sum(inst.allocation_seconds for inst in instances_data)
    total_consumed = sum(inst.consumed_seconds for inst in instances_data)

    # Display totals (configured instances only — account-wide cap is not visible without admin)
    click.echo("\n" + "=" * 80)
    click.echo("TOTALS (configured instances)")
    click.echo("=" * 80)
    click.echo(f"Configured Instances: {len(instances_data)}")
    click.echo(f"Total Allocation: {format_seconds(total_allocation)}")
    click.echo(f"Total Consumed: {format_seconds(total_consumed)}")
    if total_allocation > 0:
        utilization = (total_consumed / total_allocation) * 100
        click.echo(f"Utilization (consumed / allocation): {utilization:.1f}%")

    click.echo("\nNote: This command does not require admin privileges.")
    click.echo("Use 'show' for account-wide totals (requires admin access).")


@main.command()
@config_option
@api_key_option
@click.pass_context
@handle_errors
def analyze(ctx, config: str, api_key: str | None):
    """Analyze allocations and show optimization recommendations.

    Only instances listed in the config file are analyzed and modified.
    Allocation held by unmanaged instances is preserved as-is and
    counted toward the account cap.
    """
    session = _open_session(ctx, config, api_key)
    config_parser = session.config
    client = session.client
    account_id = config_parser.account_id
    plan = config_parser.plan
    instance_configs = config_parser.instance_configs

    click.echo(f"Fetching account information for {len(instance_configs)} configured instances on plan {plan.value}...")
    instance_states = _fetch_instance_states(client, session.configured_instances)
    account = client.get_account(account_id, plan, instance_states)

    # Enrich instances with target usage and detailed usage data
    click.echo("Fetching usage data for different time periods...")
    enrich_instances_with_usage_data(account, instance_configs, client)

    # Get minimum allocation from config
    minimum_allocation_seconds = config_parser.minimum_allocation_seconds

    # Run optimization analysis
    click.echo("Analyzing allocations...")
    optimizer = AllocationOptimizer(
        account,
        instance_configs,
        minimum_allocation_seconds,
        allocation_reserve_percent=config_parser.allocation_reserve_percent,
    )
    result = optimizer.optimize()

    is_valid, errors = optimizer.validate_allocations(result)

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
    click.echo(f"Allocation budget: {format_seconds(account.allocation_budget_seconds)}")
    click.echo(f"Unallocated: {format_seconds(account.unallocated_seconds)}")

    total_balance_consumed = sum(inst.usage.consumed_balance_period for inst in account.instances)
    click.echo(f"Consumed (Balance Period, configured): {format_seconds(total_balance_consumed)}")
    click.echo(f"Consumed (28-day, configured): {format_seconds(account.consumed_seconds)}")
    if account.unmanaged_allocation_seconds > 0:
        click.echo(
            f"Held by unconfigured instances: {format_seconds(account.unmanaged_allocation_seconds)} "
            "(not modified; counted against cap)"
        )
    if config_parser.allocation_reserve_percent > 0:
        pool, _ = optimizer.redistribution_pool()
        click.echo(format_reserve_summary(pool, config_parser.allocation_reserve_percent))
    limit_str = format_seconds(account.limit_seconds) if account.limit_seconds else "Unlimited"
    click.echo(f"Limit: {limit_str}")
    click.echo(f"Configured instances analyzed: {len(instance_configs)}")

    # Display instance analysis table (show ALL instances) using utility function
    click.echo("\n" + "=" * 80)
    click.echo("INSTANCE ANALYSIS")
    click.echo("=" * 80)

    table_data, headers = format_instance_analysis_table(
        account.instances,
        alloc_map=result.allocation_changes,
        limit_map=result.limit_changes,
    )

    click.echo(tabulate(table_data, headers=headers, tablefmt="grid"))

    total_changes = len(result.allocation_changes) + len(result.limit_changes)
    if total_changes:
        click.echo(
            f"\nTotal changes: {total_changes} ({len(result.allocation_changes)} allocation, {len(result.limit_changes)} limit)"
        )
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
@click.pass_context
@handle_errors
def optimize(ctx, config: str, api_key: str | None, dry_run: bool):
    """Optimize instance allocations and apply changes for a specific plan.

    Only instances listed in the config file are modified. Allocation held
    by unmanaged instances is preserved and counted toward the account
    cap so we never overcommit.
    """
    if not dry_run:
        click.confirm("Are you sure you want to optimize allocations?", abort=True)

    session = _open_session(ctx, config, api_key)
    config_parser = session.config
    client = session.client
    account_id = config_parser.account_id
    plan = config_parser.plan
    instance_configs = config_parser.instance_configs

    click.echo(f"Fetching account information for {len(instance_configs)} configured instances on plan {plan.value}...")
    instance_states = _fetch_instance_states(client, session.configured_instances)
    account = client.get_account(account_id, plan, instance_states)

    # Enrich instances with target usage (no detailed usage needed for optimize)
    enrich_instances_with_usage_data(account, instance_configs, client)

    # Get minimum allocation from config
    minimum_allocation_seconds = config_parser.minimum_allocation_seconds

    # Run optimization
    click.echo("Computing optimal allocations...")
    optimizer = AllocationOptimizer(
        account,
        instance_configs,
        minimum_allocation_seconds,
        allocation_reserve_percent=config_parser.allocation_reserve_percent,
    )
    result = optimizer.optimize()

    is_valid, errors = optimizer.validate_allocations(result)
    if not is_valid:
        click.echo("\nVALIDATION ERRORS", err=True)
        for err in errors:
            click.echo(f"❌ {err}", err=True)
        raise click.ClickException("Validation failed; refusing to apply changes.")

    if not result.allocation_changes and not result.limit_changes:
        click.echo("✓ No optimization needed. Allocations are already optimal.")
        return

    # Display what will be changed
    click.echo("\n" + "=" * 80)
    click.echo("CHANGES TO BE APPLIED")
    click.echo("=" * 80)
    click.echo(f"Scope: {len(instance_configs)} configured instances (others left untouched)")
    if account.unmanaged_allocation_seconds > 0:
        click.echo(
            f"Held by unconfigured instances: {format_seconds(account.unmanaged_allocation_seconds)} "
            "(reserved against the account cap)"
        )

    instance_map = {inst.crn: inst.name for inst in account.instances}

    changed_crns = set(result.allocation_changes) | set(result.limit_changes)
    changed_instances = sorted(
        (inst for inst in account.instances if inst.crn in changed_crns),
        key=lambda inst: inst.name,
    )
    rec_data, rec_headers = format_instance_analysis_table(
        changed_instances,
        alloc_map=result.allocation_changes,
        limit_map=result.limit_changes,
        include_usage=False,
    )
    click.echo(tabulate(rec_data, headers=rec_headers, tablefmt="grid"))

    if dry_run:
        click.echo("\n[DRY RUN] No changes were made.")
        return

    # Apply in safe order: decreases first (free headroom), then limits, then increases
    click.echo("\nApplying changes...")
    success_count = 0
    error_count = 0

    def _apply_allocation(crn: str, chg: AllocationChange) -> None:
        nonlocal success_count, error_count
        instance_name = instance_map.get(crn, crn[:40] + "...")
        if len(instance_name) > 40:
            instance_name = instance_name[:37] + "..."
        delta = chg.delta
        change_str = f"+{format_seconds(delta)}" if delta > 0 else f"-{format_seconds(delta)}"
        try:
            click.echo(
                f"  Updating {instance_name}: {format_seconds(chg.current)} → {format_seconds(chg.new)} ({change_str})"
            )
            client.update_instance_allocation(crn, chg.new)
            success_count += 1
            click.echo("    ✓ Success")
        except Exception as e:
            click.echo(f"    ❌ Failed: {e}", err=True)
            error_count += 1

    def _apply_limit(crn: str, chg: LimitChange) -> None:
        nonlocal success_count, error_count
        instance_name = instance_map.get(crn, crn[:40] + "...")
        if len(instance_name) > 40:
            instance_name = instance_name[:37] + "..."
        if chg.current is None:
            transition = f"Setting limit for {instance_name}: {format_seconds(chg.new)}"
        else:
            transition = (
                f"Updating limit for {instance_name}: {format_seconds(chg.current)} → {format_seconds(chg.new)}"
            )
        try:
            click.echo(f"  {transition}")
            client.update_instance_limit(crn, chg.new)
            success_count += 1
            click.echo("    ✓ Success")
        except Exception as e:
            click.echo(f"    ❌ Failed: {e}", err=True)
            error_count += 1

    for crn, chg in result.decreases.items():
        _apply_allocation(crn, chg)
    for crn, chg in result.limit_changes.items():
        _apply_limit(crn, chg)
    for crn, chg in result.increases.items():
        _apply_allocation(crn, chg)

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
@region_option
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default="config.yaml",
    help="Output YAML configuration file path (default: config.yaml)",
)
@click.option(
    "--balance-start",
    default="2026-01-01T00:00:00+00:00",
    callback=_parse_balance_date,
    help="Balance period start date (ISO format with UTC offset, default: 2026-01-01T00:00:00+00:00)",
)
@click.option(
    "--balance-end",
    default="2026-12-31T23:59:59+00:00",
    callback=_parse_balance_date,
    help="Balance period end date (ISO format with UTC offset, default: 2026-12-31T23:59:59+00:00)",
)
@click.pass_context
@handle_errors
def configure(
    ctx,
    account_id: str,
    plan: Plan,
    api_key: str | None,
    region: Region | None,
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
    discovered = client.discover_instances(account_id, plan).filter_by_region(region)

    if discovered.archived:
        click.echo(f"Skipping {len(discovered.archived)} archived instance(s)", err=True)

    if not discovered.active:
        click.echo("⚠ No active instances found in this account.", err=True)
        sys.exit(1)

    region_suffix = f" in region {region.value}" if region is not None else ""
    click.echo(f"Found {len(discovered.active)} instance(s){region_suffix}")
    click.echo("\nGenerating configuration file...")

    output_path = Path(output)
    output_path.write_text(build_configure_yaml(account_id, plan, discovered.active, balance_start, balance_end))

    click.echo(f"\n✓ Configuration file created: {output_path}")
    click.echo("\nNext steps:")
    click.echo(f"1. Edit {output_path} to customize instance allocations")
    click.echo("2. Run `qauvern show` or `qauvern instances` for usage information (`show` requires admin permissions)")
    click.echo("3. Run 'qauvern analyze' to see optimization recommendations")
    click.echo("4. Run 'qauvern optimize' to apply optimizations")


@main.command()
@config_option
@api_key_option
@region_option
@click.option("--dry-run", is_flag=True, help="Show what would change without writing the file")
@click.option("--no-net-grants", is_flag=True, help="Skip dropping expired net_grants")
@click.option("--no-add", is_flag=True, help="Skip adding newly discovered instances")
@click.option("--no-names", is_flag=True, help="Skip fixing instance name drift")
@click.option("--no-remove", is_flag=True, help="Skip removing archived/missing instances")
@click.pass_context
@handle_errors
def update(
    ctx,
    config: str,
    api_key: str | None,
    region: Region | None,
    dry_run: bool,
    no_net_grants: bool,
    no_add: bool,
    no_names: bool,
    no_remove: bool,
):
    """Reconcile a configuration file with the live IBM Quantum API.

    Drops expired `net_grants`, adds newly discovered instances, fixes
    instance name drift, and removes archived or missing instances. Each
    action runs by default; use the `--no-*` flags to opt out.

    Comments and customizations in the YAML (custom dates, `limit_seconds`,
    `allocation_reserve_percent`, etc.) are preserved.
    """
    config_path = Path(config)
    config_parser = ConfigParser(config)

    click.echo(
        f"Connecting to IBM Quantum API for account {config_parser.account_id} (plan: {config_parser.plan.value})..."
    )
    client = _build_client(ctx, api_key)

    click.echo("Fetching instances...")
    discovered = client.discover_instances(config_parser.account_id, config_parser.plan).filter_by_region(region)

    actions = UpdateActions(
        expire_net_grants=not no_net_grants,
        add_instances=not no_add,
        fix_names=not no_names,
        remove_instances=not no_remove,
    )

    doc = load_config_doc(config_path)
    summary = compute_update(doc, discovered, now=datetime.now(tz=timezone.utc), actions=actions)

    click.echo("\n" + format_update_summary(summary))

    if summary.is_empty:
        click.echo("\n✓ Config is already up to date.")
        return

    if dry_run:
        click.echo("\nDry run — no changes written.")
        return

    click.confirm(f"\nApply these changes to {config_path}?", abort=True)
    write_config_doc(config_path, doc)
    click.echo(f"\n✓ Updated {config_path}")


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
    help="Initial allocation (e.g., 96000, 10h, 2.5d)",
)
@click.option(
    "--limit",
    "-l",
    default=None,
    help="Instance limit (e.g., 96000, 10h, 2.5d). Set after creation.",
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
