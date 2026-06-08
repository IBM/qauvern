# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Resolves the effective limit for an instance given net grants and rolloff."""

from datetime import date, timedelta

from .models import InstanceState, InstanceConfig


def resolve_limit(instance_config: InstanceConfig, instance_state: InstanceState, today: date) -> int | None:
    """Return the effective config-side limit in seconds for the given instance today.

    Returns None when the config sets neither target_limit_seconds nor any active
    grants — callers should treat that as "no config-side override" and fall back
    to whatever IQP currently has.

    Formula when there is at least one active grant:
        base + grant_total + max(0, rolloff - base)

    where:
        grant_total = sum of net_grant_seconds across grants active today
        boost_start = earliest start_date among active grants
        rolloff     = sum of daily_usage on days strictly before boost_start that
                      are still inside the current 28-day rolling window
                      [today - 28, today]

    The max(0, rolloff - base) term lets pre-grant usage that exceeded the base
    limit decay out of the effective limit as those days exit the rolling window.
    Pre-grant days that stayed at or below the base limit contribute nothing.
    """
    base_limit = instance_config.target_limit_seconds

    if not instance_config.net_grants:
        return base_limit

    active_grants = [g for g in instance_config.net_grants if g.start_date.date() <= today < g.end_date.date()]
    if not active_grants:
        return base_limit

    if base_limit is None:
        raise AssertionError("InstanceConfig invariant violated: net_grants without target_limit_seconds")

    grant_total = sum(g.net_grant_seconds for g in active_grants)
    boost_start = min(g.start_date.date() for g in active_grants)

    window_floor = today - timedelta(days=28)
    rolloff_end = boost_start - timedelta(days=1)

    if rolloff_end < window_floor:
        rolloff = 0
    else:
        rolloff = sum(
            seconds for day, seconds in instance_state.usage.daily_usage.items() if window_floor <= day <= rolloff_end
        )

    return base_limit + grant_total + max(0, rolloff - base_limit)
