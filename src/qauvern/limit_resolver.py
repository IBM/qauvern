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

    Both grant tuples align positionally with the `grants` argument to
    `attribute_usage` — duplicate grants are indistinguishable by value, so
    identity must be tracked by index, not by `NetGrant`.
    """

    grant_credited_seconds: tuple[int, ...]
    grant_unspent_seconds: tuple[int, ...]
    base_seconds_in_window: Mapping[date, int]

    def base_seconds_before(self, day: date) -> int:
        """Sum in-window base usage on days strictly before `day`."""
        return sum(seconds for d, seconds in self.base_seconds_in_window.items() if d < day)


def attribute_usage(grants: Sequence[NetGrant], daily_usage: Mapping[date, int], *, today: date) -> UsageAttribution:
    """Charge each day's usage to the grants that were active on it, then to the base limit.

    A day's usage is consumed greedily against every grant active that day,
    soonest-expiring first, so a grant about to roll off is the one credited with
    what it plausibly funded. Whatever no grant could pay for is the day's *base*
    usage.

    `net_grant_seconds` is a lifetime budget, so it is consumed over the grant's
    whole active period while only the share still inside today's window is
    credited: `grant_unspent_seconds` only ever shrinks, and
    `grant_credited_seconds` can never exceed `net_grant_seconds`. Every active
    day of every grant must therefore appear in `daily_usage`, or spent budget
    looks unspent — see `rolling_window.daily_usage_lookback_days`.

    Days after `today` are ignored, and days outside the window contribute to
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
        grant_credited_seconds=tuple(grant_credited_seconds),
        grant_unspent_seconds=tuple(remaining_grant_seconds),
        base_seconds_in_window=base_seconds_in_window,
    )


@dataclass(frozen=True)
class LimitBreakdown:
    """The effective config-side limit for an instance, broken into its terms."""

    base_seconds: int
    grant_funded_seconds: int
    unspent_grant_seconds: int
    pre_boost_overage_seconds: int
    boost_start_date: date | None

    @property
    def total(self) -> int:
        return (
            self.base_seconds + self.grant_funded_seconds + self.unspent_grant_seconds + self.pre_boost_overage_seconds
        )

    @classmethod
    def _base_limit_only(cls, base_limit: int | None) -> "LimitBreakdown | None":
        if base_limit is None:
            return None
        return cls(
            base_seconds=base_limit,
            grant_funded_seconds=0,
            unspent_grant_seconds=0,
            pre_boost_overage_seconds=0,
            boost_start_date=None,
        )


def resolve_limit(instance_config: InstanceConfig, instance_state: InstanceState, today: date) -> LimitBreakdown | None:
    """Return the effective config-side limit breakdown for the given instance today.

    Returns None when the config sets neither target_limit_seconds nor any grant
    still crediting today, meaning "no config-side override" — callers fall back
    to whatever IQP currently has.

    `breakdown.total`, over the grants that can still credit (`grant_still_credits`):
        base + grant_funded + unspent_grant + max(0, unshielded_pre_boost - base)

        grant_funded          in-window usage attributed to those grants
        unspent_grant         budget still undrawn on the grants active today
        boost_start           earliest start_date among the grants active today
        unshielded_pre_boost  in-window usage before boost_start that no grant paid for

    `net_grant_seconds` is a lifetime budget for the grant's whole period, not a
    per-window allowance: only the base limit refreshes as usage rolls out of the
    window. Crediting a flat `net_grant_seconds` while a grant is active would
    re-grant budget already spent on days that have since rolled out, letting a
    grant longer than the window be drawn several times over.

    The two grant terms diverge at `end_date`: `unspent_grant` drops to zero
    because undrawn budget is forfeit, while `grant_funded` persists and decays as
    the days it paid for leave the window. That decay is what leaves an instance
    no worse off after a grant expires than if the grant had never existed.

    The overage term is gated on an *active* grant on purpose. Anchoring it on the
    earliest still-crediting grant would remove forgiveness whenever an older
    expired grant precedes the active one, so pre-grant debt snaps back at expiry
    instead — which still satisfies the guarantee, the no-grant counterfactual
    being equally negative.

    Attributing only still-crediting grants is what makes `update`'s pruning a
    no-op; see Design.md.
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
    grant_funded_seconds = sum(attribution.grant_credited_seconds)
    unspent_grant_seconds = sum(attribution.grant_unspent_seconds[i] for i in active_grant_indices)

    boost_start_date = min((relevant_grants[i].start_date.date() for i in active_grant_indices), default=None)
    pre_boost_overage_seconds = (
        max(0, attribution.base_seconds_before(boost_start_date) - base_limit) if boost_start_date is not None else 0
    )

    return LimitBreakdown(
        base_seconds=base_limit,
        grant_funded_seconds=grant_funded_seconds,
        unspent_grant_seconds=unspent_grant_seconds,
        pre_boost_overage_seconds=pre_boost_overage_seconds,
        boost_start_date=boost_start_date,
    )
