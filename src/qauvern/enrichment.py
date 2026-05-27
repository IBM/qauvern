# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Pair :class:`Account` instances with their configs and analytics data.

The result is a :class:`ResolvedAccount` whose every instance carries the data
the optimizer and limit resolver need — no in-place mutation, no sequencing
trap where a downstream consumer reads zero-defaults because enrichment never
ran.
"""

from datetime import date, datetime, timedelta

import click

from .api_client import IBMQuantumAPIClient
from .models import (
    Account,
    InstanceConfig,
    InstanceUsage,
    ResolvedAccount,
    ResolvedInstance,
)


def enrich_instances_with_usage_data(
    account: Account,
    instance_configs: list[InstanceConfig],
    client: IBMQuantumAPIClient,
) -> ResolvedAccount:
    """Pair each Instance with its InstanceConfig and fetch detailed usage.

    Instances without a matching config in ``instance_configs`` are dropped
    (matching the optimizer's existing behavior of skipping such instances).
    """
    config_by_crn = {config.crn: config for config in instance_configs}
    today_date = date.today()
    daily_start = today_date - timedelta(days=60)

    resolved: list[ResolvedInstance] = []
    for instance in account.instances:
        config = config_by_crn.get(instance.crn)
        if config is None:
            continue
        usage = _fetch_usage(instance.crn, instance.name, config, account.account_id, daily_start, today_date, client)
        resolved.append(ResolvedInstance(instance=instance, config=config, usage=usage))

    return ResolvedAccount(
        account_id=account.account_id,
        plan_id=account.plan_id,
        target_usage_seconds=account.target_usage_seconds,
        available_seconds=account.available_seconds,
        limit_seconds=account.limit_seconds,
        instances=tuple(resolved),
    )


def _fetch_usage(
    crn: str,
    name: str,
    config: InstanceConfig,
    account_id: str,
    daily_start: date,
    today_date: date,
    client: IBMQuantumAPIClient,
) -> InstanceUsage:
    """Fetch detailed usage for one instance.

    Errors degrade to zero-valued usage with a warning so a single instance's
    analytics outage doesn't break the whole run — preserving the existing
    behavior of ``enrich_instances_with_usage_data``.
    """
    try:
        consumed_balance_period = (
            client.get_instance_usage_seconds(crn, config.start_date, datetime.now(), account_id)
            if config.start_date
            else 0
        )
        detailed = client.get_detailed_usage(crn, account_id)
    except Exception as e:
        click.echo(f"Warning: Could not fetch usage data for {name}: {e}", err=True)
        return InstanceUsage()

    try:
        daily_usage = client.get_daily_usage(crn, account_id, daily_start, today_date)
    except Exception as daily_e:
        click.echo(f"Warning: Could not fetch daily usage for {name}: {daily_e}", err=True)
        daily_usage = {}

    return InstanceUsage(
        consumed_balance_period=consumed_balance_period,
        consumed_14day=detailed["consumed_14day"],
        consumed_7day=detailed["consumed_7day"],
        consumed_3day=detailed["consumed_3day"],
        consumed_24h=detailed["consumed_24h"],
        daily_usage=daily_usage,
    )
