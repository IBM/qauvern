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

from dataclasses import dataclass
from datetime import date, timedelta

from .models import InstanceConfig, InstanceState
from .rolling_window import window_start


@dataclass(frozen=True)
class LimitBreakdown:
    """The effective config-side limit for an instance, broken into its terms.

    `expired_carryover_seconds` is always 0 until the rolling net-grant expiry
    feature lands; it exists now so callers have a stable field set to code
    against.
    """

    base_seconds: int
    active_grant_seconds: int
    expired_carryover_seconds: int
    pre_boost_overage_seconds: int
    boost_start_date: date | None

    @property
    def total(self) -> int:
        return (
            self.base_seconds
            + self.active_grant_seconds
            + self.expired_carryover_seconds
            + self.pre_boost_overage_seconds
        )

    @classmethod
    def _base_limit_only(cls, base_limit: int | None) -> "LimitBreakdown | None":
        if base_limit is None:
            return None
        return cls(
            base_seconds=base_limit,
            active_grant_seconds=0,
            expired_carryover_seconds=0,
            pre_boost_overage_seconds=0,
            boost_start_date=None,
        )


def resolve_limit(instance_config: InstanceConfig, instance_state: InstanceState, today: date) -> LimitBreakdown | None:
    """Return the effective config-side limit breakdown for the given instance today.

    Returns None when the config sets neither target_limit_seconds nor any active
    grants — callers should treat that as "no config-side override" and fall back
    to whatever IQP currently has.

    `breakdown.total` formula when there is at least one active grant:
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
        return LimitBreakdown._base_limit_only(base_limit)

    active_grants = [g for g in instance_config.net_grants if g.start_date.date() <= today < g.end_date.date()]
    if not active_grants:
        return LimitBreakdown._base_limit_only(base_limit)

    if base_limit is None:
        raise AssertionError("InstanceConfig invariant violated: net_grants without target_limit_seconds")

    grant_total = sum(g.net_grant_seconds for g in active_grants)
    boost_start_date = min(g.start_date.date() for g in active_grants)

    earliest_in_window_date = window_start(today)
    rolloff_end_date = boost_start_date - timedelta(days=1)

    if rolloff_end_date < earliest_in_window_date:
        rolloff = 0
    else:
        rolloff = sum(
            seconds
            for day, seconds in instance_state.usage.daily_usage.items()
            if earliest_in_window_date <= day <= rolloff_end_date
        )

    return LimitBreakdown(
        base_seconds=base_limit,
        active_grant_seconds=grant_total,
        expired_carryover_seconds=0,
        pre_boost_overage_seconds=max(0, rolloff - base_limit),
        boost_start_date=boost_start_date,
    )
