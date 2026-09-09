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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from .models import InstanceConfig, InstanceState, NetGrant
from .rolling_window import grant_still_credits, window_start


@dataclass(frozen=True)
class UsageAttribution:
    """Per-day usage split between the grants that funded it and the base limit.

    `grant_credited_seconds` aligns positionally with the `grants` argument to
    `attribute_usage` — duplicate grants are indistinguishable by value, so
    identity must be tracked by index, not by `NetGrant`.
    """

    grant_credited_seconds: tuple[int, ...]
    base_seconds_in_window: Mapping[date, int]

    def base_seconds_before(self, day: date) -> int:
        """Sum in-window base usage on days strictly before `day`."""
        return sum(seconds for d, seconds in self.base_seconds_in_window.items() if d < day)


def attribute_usage(grants: Sequence[NetGrant], daily_usage: Mapping[date, int], *, today: date) -> UsageAttribution:
    """Charge each day's usage to the grants that were active on it, then to the base limit.

    A day's usage is consumed greedily against the budget of every grant active
    on that day, soonest-expiring first, so that a grant about to roll off is
    the one that gets credit for what it plausibly funded. Whatever no grant
    could pay for is the day's *base* usage.

    Two properties matter for the caller:

    - Budgets are consumed over each grant's **full active period**, but only
      the share landing inside today's rolling window is *credited*. A grant can
      therefore never credit back more than `net_grant_seconds`, and usage that
      has already rolled out of the window still counts as spent budget.
    - Days after `today` are ignored, and days outside the window contribute to
      neither `grant_credited_seconds` nor `base_seconds_in_window`.
    """
    earliest_date_in_window = window_start(today)
    remaining_grant_seconds = [grant.net_grant_seconds for grant in grants]
    grant_credited_seconds = [0] * len(grants)
    base_seconds_in_window: dict[date, int] = {}

    for day in sorted(daily_usage):
        if day > today:
            continue
        in_window = earliest_date_in_window <= day <= today
        unpaid_seconds = daily_usage[day]
        covering_grant_indices = sorted(
            (i for i, grant in enumerate(grants) if grant.start_date.date() <= day < grant.end_date.date()),
            key=lambda i: (grants[i].end_date, grants[i].start_date, i),
        )
        for i in covering_grant_indices:
            if unpaid_seconds <= 0:
                break
            paid_seconds = min(unpaid_seconds, remaining_grant_seconds[i])
            remaining_grant_seconds[i] -= paid_seconds
            unpaid_seconds -= paid_seconds
            if in_window:
                grant_credited_seconds[i] += paid_seconds
        if in_window:
            base_seconds_in_window[day] = unpaid_seconds

    return UsageAttribution(
        grant_credited_seconds=tuple(grant_credited_seconds), base_seconds_in_window=base_seconds_in_window
    )


@dataclass(frozen=True)
class LimitBreakdown:
    """The effective config-side limit for an instance, broken into its terms."""

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

    Returns None when the config sets neither target_limit_seconds nor any grant
    still crediting today — callers should treat that as "no config-side
    override" and fall back to whatever IQP currently has.

    `breakdown.total` formula:
        base + active_grant_budget + expired_grant_carryover + max(0, unshielded_pre_boost_usage - base)

    where, over the grants that can still credit today (`grant_still_credits`):
        active_grant_budget = sum of net_grant_seconds across grants active today
        expired_grant_carryover = in-window usage attributed to grants that have
                                  since expired, so the usage they funded does
                                  not become debt the moment they end
        boost_start = earliest start_date among active grants
        unshielded_pre_boost_usage = in-window usage on days strictly before
                                     boost_start that no grant paid for

    The carryover term is what makes an instance never worse off after a grant
    expires than if the grant had never existed: the usage the grant funded
    stays in IQP's 28-day window, so its credit stays in the limit until that
    usage rolls out too.

    The max(0, ... - base) overage term lets pre-grant usage that exceeded the
    base limit decay out of the effective limit as those days exit the window.
    It is deliberately gated on having an *active* grant: anchoring it on the
    earliest still-crediting grant instead would *remove* forgiveness whenever
    an older expired grant precedes the active one. Pre-grant debt therefore
    snaps back at expiry, which still satisfies "never worse off than if the
    grant never existed" — the counterfactual is equally negative.

    Only grants that can still credit are attributed, never every configured
    grant. That is what makes `update`'s pruning provably a no-op: a grant with
    `end_date <= window_start(today)` is excluded from attribution, from the
    active budget, and from the carryover, so deleting it from the config cannot
    change the resolved limit.
    """
    base_limit = instance_config.target_limit_seconds

    if not instance_config.net_grants:
        return LimitBreakdown._base_limit_only(base_limit)

    if base_limit is None:
        raise AssertionError("InstanceConfig invariant violated: net_grants without target_limit_seconds")

    relevant_grants = [grant for grant in instance_config.net_grants if grant_still_credits(grant, today)]
    if not relevant_grants:
        return LimitBreakdown._base_limit_only(base_limit)

    attribution = attribute_usage(relevant_grants, instance_state.usage.daily_usage, today=today)

    active_grant_indices = {
        i for i, grant in enumerate(relevant_grants) if grant.start_date.date() <= today < grant.end_date.date()
    }
    active_grant_seconds = sum(relevant_grants[i].net_grant_seconds for i in active_grant_indices)
    expired_carryover_seconds = sum(
        seconds for i, seconds in enumerate(attribution.grant_credited_seconds) if i not in active_grant_indices
    )

    boost_start_date = min((relevant_grants[i].start_date.date() for i in active_grant_indices), default=None)
    pre_boost_overage_seconds = (
        max(0, attribution.base_seconds_before(boost_start_date) - base_limit) if boost_start_date is not None else 0
    )

    return LimitBreakdown(
        base_seconds=base_limit,
        active_grant_seconds=active_grant_seconds,
        expired_carryover_seconds=expired_carryover_seconds,
        pre_boost_overage_seconds=pre_boost_overage_seconds,
        boost_start_date=boost_start_date,
    )
