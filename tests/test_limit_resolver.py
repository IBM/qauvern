# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for resolve_limit and attribute_usage.

Formula under test, over the grants that can still credit today:
    result = base + active_grant_budget + expired_carryover + max(0, unshielded_pre_boost - base)

where per-day usage is first attributed to the grants active on that day
(soonest-expiring first), `expired_carryover` sums the in-window share charged
to grants that have since expired, and `unshielded_pre_boost` sums the in-window
leftover on days strictly before the earliest active grant's start.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from qauvern.limit_resolver import attribute_usage, resolve_limit
from qauvern.models import InstanceConfig, InstanceDetailedUsage, InstanceState, NetGrant


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def make_config(
    limit_seconds: int | None = None,
    net_grants: list[NetGrant] | None = None,
) -> InstanceConfig:
    return InstanceConfig(
        name="Test",
        crn="crn:test:1",
        target_limit_seconds=limit_seconds,
        net_grants=tuple(net_grants) if net_grants else (),
    )


def make_instance(daily_usage: dict[date, int] | None = None) -> InstanceState:
    return InstanceState(
        crn="crn:test:1",
        name="Test Instance",
        allocation_seconds=10000,
        consumed_seconds=0,
        limit_seconds=None,
        detailed_usage=InstanceDetailedUsage(
            consumed_14day=0,
            consumed_7day=0,
            consumed_3day=0,
            consumed_24h=0,
            daily_usage=daily_usage or {},
        ),
    )


def _total(cfg: InstanceConfig, instance: InstanceState, today: date) -> int | None:
    limit_breakdown = resolve_limit(cfg, instance, today)
    return limit_breakdown.total if limit_breakdown is not None else None


# -------------------------------------------------------------------
# Trivial paths
# -------------------------------------------------------------------


def test_no_limit_and_no_grants_returns_none() -> None:
    assert resolve_limit(make_config(), make_instance(), date(2026, 4, 27)) is None


def test_base_limit_only_no_grants_returns_base() -> None:
    assert _total(make_config(limit_seconds=500), make_instance(), date(2026, 4, 27)) == 500


def test_all_grants_expired_returns_base() -> None:
    grant = NetGrant(
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        net_grant_seconds=400,
        end_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    assert _total(cfg, make_instance(), date(2026, 4, 27)) == 500


def test_future_grant_returns_base() -> None:
    today = date(2026, 4, 30)
    grant = NetGrant(
        start_date=_dt(today + timedelta(days=1)), net_grant_seconds=400, end_date=_dt(today + timedelta(days=29))
    )
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    assert _total(cfg, make_instance(), today) == 500


# -------------------------------------------------------------------
# LimitBreakdown field-level checks (not just .total)
# -------------------------------------------------------------------


def test_base_only_breakdown_has_zeroed_grant_fields() -> None:
    limit_breakdown = resolve_limit(make_config(limit_seconds=500), make_instance(), date(2026, 4, 27))
    assert limit_breakdown is not None
    assert limit_breakdown.base_seconds == 500
    assert limit_breakdown.active_grant_seconds == 0
    assert limit_breakdown.expired_carryover_seconds == 0
    assert limit_breakdown.pre_boost_overage_seconds == 0
    assert limit_breakdown.boost_start_date is None
    assert limit_breakdown.total == 500


def test_active_grant_breakdown_fields() -> None:
    """Same scenario as test_pregrant_excess_compares_total_to_base, field-by-field."""
    today = date(2026, 4, 27)
    grant = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=6,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=12, net_grants=[grant])
    instance = make_instance(daily_usage={date(2026, 4, 1): 8, date(2026, 4, 5): 8})
    limit_breakdown = resolve_limit(cfg, instance, today)
    assert limit_breakdown is not None
    assert limit_breakdown.base_seconds == 12
    assert limit_breakdown.active_grant_seconds == 6
    assert limit_breakdown.expired_carryover_seconds == 0
    assert limit_breakdown.pre_boost_overage_seconds == 4
    assert limit_breakdown.boost_start_date == date(2026, 4, 20)
    assert limit_breakdown.total == 22


# -------------------------------------------------------------------
# Single-grant boundary behavior
# -------------------------------------------------------------------


def test_grant_active_on_start_date() -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=_dt(today), net_grant_seconds=400, end_date=_dt(today + timedelta(days=28)))
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    assert _total(cfg, make_instance(), today) == 900


def test_grant_active_day_before_end() -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(
        start_date=_dt(today - timedelta(days=26)), net_grant_seconds=400, end_date=_dt(today + timedelta(days=1))
    )
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    # rolloff window: window_floor=today-28, rolloff_end=grant_start-1; no usage → rolloff=0
    assert _total(cfg, make_instance(), today) == 900


def test_grant_inactive_on_end_date() -> None:
    today = date(2026, 4, 28)
    grant = NetGrant(start_date=_dt(today - timedelta(days=27)), net_grant_seconds=400, end_date=_dt(today))
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    assert _total(cfg, make_instance(), today) == 500


# -------------------------------------------------------------------
# Pre-grant usage at-or-below base contributes nothing
# -------------------------------------------------------------------


def test_pregrant_usage_below_base_does_not_extend_limit() -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=400,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    # rolloff window: [Mar 30, Apr 19]; Apr 1 usage 50 is well under base 500
    instance = make_instance(daily_usage={date(2026, 4, 1): 50})
    # 500 + 400 + max(0, 50 - 500) = 900
    assert _total(cfg, instance, today) == 900


def test_pregrant_total_below_base_does_not_extend_limit() -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=400,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    # multiple days, but their sum (120) is still under base
    instance = make_instance(daily_usage={date(2026, 4, 1): 40, date(2026, 4, 5): 40, date(2026, 4, 10): 40})
    assert _total(cfg, instance, today) == 900


# -------------------------------------------------------------------
# Pre-grant usage above base extends the limit
# -------------------------------------------------------------------


def test_pregrant_usage_above_base_adds_excess_headroom() -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=6,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=12, net_grants=[grant])
    # rolloff window: [Mar 30, Apr 19]; one day at 20 (above base 12)
    instance = make_instance(daily_usage={date(2026, 4, 1): 20})
    # 12 + 6 + max(0, 20 - 12) = 26
    assert _total(cfg, instance, today) == 26


def test_pregrant_excess_compares_total_to_base() -> None:
    """Rolloff is summed across pre-grant days before the max(0, rolloff - base) cut."""
    today = date(2026, 4, 27)
    grant = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=6,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=12, net_grants=[grant])
    # two days at 8 each → rolloff=16, excess over base=4
    instance = make_instance(daily_usage={date(2026, 4, 1): 8, date(2026, 4, 5): 8})
    # 12 + 6 + 4 = 22
    assert _total(cfg, instance, today) == 22


def test_pregrant_days_outside_window_do_not_contribute() -> None:
    today = date(2026, 5, 10)
    # grant_start Apr 1 → rolloff_end Mar 31; window_floor = Apr 12 → rolloff_end < window_floor
    grant = NetGrant(
        start_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
        net_grant_seconds=6,
        end_date=datetime(2026, 5, 30, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=12, net_grants=[grant])
    instance = make_instance(daily_usage={date(2026, 3, 20): 999})
    # entire pre-grant window is outside the current 28-day window → rolloff=0
    assert _total(cfg, instance, today) == 18


def test_day_equal_to_boost_start_does_not_count_in_rolloff() -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=6,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=12, net_grants=[grant])
    # Apr 20 is the grant start; rolloff_end=Apr 19, so this day is excluded
    instance = make_instance(daily_usage={date(2026, 4, 20): 999})
    assert _total(cfg, instance, today) == 18


# -------------------------------------------------------------------
# Multiple grants
# -------------------------------------------------------------------


def test_two_grants_same_start_contributions_sum() -> None:
    today = date(2026, 4, 27)
    g1 = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=400,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    g2 = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=200,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=500, net_grants=[g1, g2])
    assert _total(cfg, make_instance(), today) == 1100


def test_two_grants_different_starts_anchor_at_earliest() -> None:
    """boost_start is the min of active starts. Days between the two starts must NOT count as rolloff."""
    today = date(2026, 4, 27)
    g_early = NetGrant(
        start_date=datetime(2026, 4, 10, tzinfo=timezone.utc),
        net_grant_seconds=100,
        end_date=datetime(2026, 5, 8, tzinfo=timezone.utc),
    )
    g_late = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=100,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=10, net_grants=[g_early, g_late])
    # boost_start = Apr 10 → rolloff_end = Apr 9. Apr 15 is AFTER boost_start, excluded.
    # If we wrongly anchored at max (Apr 20), Apr 15 would inflate rolloff by 100.
    instance = make_instance(daily_usage={date(2026, 4, 15): 100})
    # 10 + 200 + max(0, 0 - 10) = 210
    assert _total(cfg, instance, today) == 210


def test_active_plus_expired_grant_anchors_at_active() -> None:
    today = date(2026, 4, 27)
    expired = NetGrant(
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        net_grant_seconds=999,
        end_date=datetime(2026, 1, 31, tzinfo=timezone.utc),
    )
    active = NetGrant(
        start_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
        net_grant_seconds=100,
        end_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=10, net_grants=[expired, active])
    # boost_start = Apr 20 (only the active grant counts) → rolloff_end = Apr 19
    instance = make_instance(daily_usage={date(2026, 4, 10): 100})
    # rolloff = 100, excess over base = 90 → 10 + 100 + 90 = 200
    assert _total(cfg, instance, today) == 200


# -------------------------------------------------------------------
# End-to-end timeline showing decay as pre-grant days roll out
# -------------------------------------------------------------------


@pytest.fixture
def decay_scenario() -> tuple[InstanceConfig, InstanceState]:
    """Pre-grant usage exceeds base; effective limit decays as those days roll out.

    Setup:
      - base = 5s, grant = 5s, grant window [Jan 5, Feb 2)
      - Jan 1-4 usage = 4s each (sum = 16 > base)
      - boost_start = Jan 5; rolloff_end = Jan 4
    """
    cfg = make_config(
        limit_seconds=5,
        net_grants=[
            NetGrant(
                start_date=datetime(2026, 1, 5, tzinfo=timezone.utc),
                net_grant_seconds=5,
                end_date=datetime(2026, 2, 2, tzinfo=timezone.utc),
            )
        ],
    )
    instance = make_instance(
        daily_usage={
            date(2026, 1, 1): 4,
            date(2026, 1, 2): 4,
            date(2026, 1, 3): 4,
            date(2026, 1, 4): 4,
        }
    )
    return cfg, instance


@pytest.mark.parametrize(
    "today,expected",
    [
        # Jan 5: window_floor=Dec 8, rolloff covers Jan 1-4 (16), excess=11
        (date(2026, 1, 5), 5 + 5 + 11),
        # Jan 29: window_floor=Jan 1, rolloff covers Jan 1-4 (16), excess=11
        (date(2026, 1, 29), 21),
        # Jan 30: window_floor=Jan 2 → Jan 1 exited; rolloff=Jan 2-4 (12), excess=7
        (date(2026, 1, 30), 5 + 5 + 7),
        # Jan 31: window_floor=Jan 3; rolloff=Jan 3-4 (8), excess=3
        (date(2026, 1, 31), 5 + 5 + 3),
        # Feb 1: window_floor=Jan 4; rolloff=Jan 4 only (4), excess=max(0, 4-5)=0
        (date(2026, 2, 1), 5 + 5),
        # Feb 2: grant expired → base only
        (date(2026, 2, 2), 5),
    ],
)
def test_decay_timeline(decay_scenario: tuple[InstanceConfig, InstanceState], today: date, expected: int) -> None:
    cfg, instance = decay_scenario
    assert _total(cfg, instance, today) == expected


# -------------------------------------------------------------------
# Expired-grant carryover: no minute debt the day a grant ends
# -------------------------------------------------------------------

GRANT_1000 = NetGrant(
    start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
    net_grant_seconds=1000,
    end_date=datetime(2026, 3, 29, tzinfo=timezone.utc),
)


@pytest.mark.parametrize(
    "used,expected_available",
    [
        # Under-spent grant: only the 400 drawn is credited (limit 500, not 1100), yet
        # attribution is grant-first, so none of it lands on base and all 100 is available.
        (400, 100),
        # Spent within the grant: base is untouched, so all of it is still available.
        (1000, 100),
        # Spent 50 past the grant: only that 50 eats into base.
        (1050, 50),
        # Spent the whole grant plus the whole base: nothing left, but no debt either.
        (1100, 0),
    ],
)
def test_never_worse_off_the_day_a_grant_expires(used: int, expected_available: int) -> None:
    """base 100 + grant 1000, all spent on the grant's last day, checked the day it ends."""
    today = date(2026, 3, 29)  # == end_date, so the grant is expired but still crediting
    cfg = make_config(limit_seconds=100, net_grants=[GRANT_1000])
    instance = make_instance(daily_usage={date(2026, 3, 28): used})
    total = _total(cfg, instance, today)
    assert total is not None
    # 100 + 0 active + min(used, 1000) carryover + 0 overage
    assert total == 100 + min(used, 1000)
    assert total - used == expected_available


def test_carryover_caps_at_the_grant_budget() -> None:
    """A grant can never credit back more than net_grant_seconds, however much was burned."""
    cfg = make_config(limit_seconds=100, net_grants=[GRANT_1000])
    instance = make_instance(daily_usage={date(2026, 3, 28): 5000})
    limit_breakdown = resolve_limit(cfg, instance, date(2026, 3, 29))
    assert limit_breakdown is not None
    assert limit_breakdown.expired_carryover_seconds == 1000
    assert limit_breakdown.total == 100 + 1000


@pytest.mark.parametrize(
    "today,expected",
    [
        # end_date Mar 29 + 28 = Apr 26 is the first day the grant cannot credit.
        (date(2026, 4, 25), 100 + 1000),
        (date(2026, 4, 26), 100),
    ],
)
def test_relevance_boundary_matches_attribution_boundary(today: date, expected: int) -> None:
    """The last day a grant is `relevant` is the last day its usage is still in-window."""
    cfg = make_config(limit_seconds=100, net_grants=[GRANT_1000])
    instance = make_instance(daily_usage={date(2026, 3, 28): 1000})
    assert _total(cfg, instance, today) == expected


def test_budget_consumed_over_full_period_credited_only_in_window() -> None:
    """Out-of-window usage still spends the grant, so it can't be credited twice."""
    today = date(2026, 3, 30)  # window floor Mar 2, so the Mar 1 usage has rolled out
    cfg = make_config(limit_seconds=100, net_grants=[GRANT_1000])
    instance = make_instance(daily_usage={date(2026, 3, 1): 900, date(2026, 3, 20): 200})

    attribution = attribute_usage([GRANT_1000], instance.usage.daily_usage, today=today)
    # Mar 1 spends 900 of the budget but is out of window, so only the remaining 100 is credited.
    assert attribution.grant_credited_seconds == (100,)
    assert attribution.base_seconds_in_window == {date(2026, 3, 20): 100}

    # 100 + 0 active + 100 carryover + 0 overage. In-window-only attribution would say 300.
    assert _total(cfg, instance, today) == 200


def test_carryover_and_overage_do_not_double_count_the_same_usage() -> None:
    """Usage shielded by an expired grant is not also charged as pre-boost overage."""
    today = date(2026, 3, 20)
    g1 = NetGrant(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        net_grant_seconds=1000,
        end_date=datetime(2026, 3, 11, tzinfo=timezone.utc),
    )
    g2 = NetGrant(
        start_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
        net_grant_seconds=100,
        end_date=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=100, net_grants=[g1, g2])
    instance = make_instance(daily_usage={date(2026, 3, 5): 900})
    limit_breakdown = resolve_limit(cfg, instance, today)
    assert limit_breakdown is not None
    # g1 paid for all 900, so nothing is left over to charge before g2's start.
    assert limit_breakdown.pre_boost_overage_seconds == 0
    assert limit_breakdown.expired_carryover_seconds == 900
    # 100 + 100 + 900 + 0. Adding an independent carryover on top of the old rolloff gives 1900.
    assert limit_breakdown.total == 1100
    assert limit_breakdown.total - 900 == 100 + 100  # available == base + the active grant


def test_uncovered_pre_boost_days_still_produce_overage() -> None:
    """The old rolloff behavior survives: days no grant covered fall through to base."""
    today = date(2026, 3, 20)
    expired = NetGrant(
        start_date=datetime(2026, 3, 10, tzinfo=timezone.utc),
        net_grant_seconds=50,
        end_date=datetime(2026, 3, 12, tzinfo=timezone.utc),
    )
    active = NetGrant(
        start_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
        net_grant_seconds=100,
        end_date=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=10, net_grants=[expired, active])
    # Mar 5 predates both grants, so nothing shields it; Mar 11 is covered by the expired grant.
    instance = make_instance(daily_usage={date(2026, 3, 5): 40, date(2026, 3, 11): 50})
    limit_breakdown = resolve_limit(cfg, instance, today)
    assert limit_breakdown is not None
    assert limit_breakdown.pre_boost_overage_seconds == 30  # max(0, 40 - 10)
    assert limit_breakdown.expired_carryover_seconds == 50
    assert limit_breakdown.total == 10 + 100 + 50 + 30


# -------------------------------------------------------------------
# Attribution order across overlapping grants
# -------------------------------------------------------------------


def _overlapping_grants() -> tuple[NetGrant, NetGrant]:
    """Two grants covering Mar 5, one expiring soon and one still active on Mar 20."""
    soon = NetGrant(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        net_grant_seconds=100,
        end_date=datetime(2026, 3, 10, tzinfo=timezone.utc),
    )
    late = NetGrant(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        net_grant_seconds=100,
        end_date=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    return soon, late


def test_overlapping_grants_charge_soonest_expiring_first() -> None:
    soon, late = _overlapping_grants()
    cfg = make_config(limit_seconds=10, net_grants=[soon, late])
    instance = make_instance(daily_usage={date(2026, 3, 5): 150})
    limit_breakdown = resolve_limit(cfg, instance, date(2026, 3, 20))
    assert limit_breakdown is not None
    # `soon` absorbs its full 100 first; `late` covers the remaining 50 out of its own budget.
    assert limit_breakdown.expired_carryover_seconds == 100
    assert limit_breakdown.active_grant_seconds == 100
    # 10 + 100 + 100 + 0. Charging the latest-expiring grant first credits `soon` only 50 → 160.
    assert limit_breakdown.total == 210


def test_attribution_order_follows_end_date_not_list_position() -> None:
    """Reversing the argument order mirrors the credits positionally, it does not change them."""
    soon, late = _overlapping_grants()
    usage = {date(2026, 3, 5): 150}
    forward = attribute_usage([soon, late], usage, today=date(2026, 3, 20))
    reversed_ = attribute_usage([late, soon], usage, today=date(2026, 3, 20))
    assert forward.grant_credited_seconds == (100, 50)
    assert reversed_.grant_credited_seconds == (50, 100)


def test_two_identical_grants_get_independent_budgets() -> None:
    """Credits are keyed positionally: identical (hence equal-hashing) grants must not merge."""
    grant = NetGrant(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        net_grant_seconds=500,
        end_date=datetime(2026, 3, 29, tzinfo=timezone.utc),
    )
    attribution = attribute_usage([grant, grant], {date(2026, 3, 5): 800}, today=date(2026, 3, 20))
    assert attribution.grant_credited_seconds == (500, 300)
    assert attribution.base_seconds_in_window == {date(2026, 3, 5): 0}


def test_active_grant_credits_full_budget_not_attributed_usage() -> None:
    """An active grant contributes its whole budget; carryover is only for expired ones."""
    today = date(2026, 3, 10)
    grant = NetGrant(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        net_grant_seconds=1000,
        end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=100, net_grants=[grant])
    instance = make_instance(daily_usage={date(2026, 3, 5): 400})
    limit_breakdown = resolve_limit(cfg, instance, today)
    assert limit_breakdown is not None
    assert limit_breakdown.active_grant_seconds == 1000
    assert limit_breakdown.expired_carryover_seconds == 0
    assert limit_breakdown.total == 1100


# -------------------------------------------------------------------
# Post-expiry decay: availability never drops as days roll out
# -------------------------------------------------------------------

CARRYOVER_DECAY_TODAYS = [
    date(2026, 3, 11),  # the day the grant ends
    date(2026, 4, 2),  # both usage days still in window
    date(2026, 4, 3),  # Mar 5 rolls out
    date(2026, 4, 6),  # Mar 8 rolls out too
    date(2026, 4, 8),  # the grant itself stops being relevant
]


@pytest.mark.parametrize("spend", [(500, 300), (700, 500)])
def test_carryover_decay_never_reduces_availability(spend: tuple[int, int]) -> None:
    """Availability is monotonically non-decreasing as the funded days roll out of the window.

    A property, not a table of magic numbers: whatever the limit does, an operator
    must never see less headroom tomorrow than today purely from time passing.
    """
    grant = NetGrant(
        start_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        net_grant_seconds=1000,
        end_date=datetime(2026, 3, 11, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=100, net_grants=[grant])
    daily_usage = {date(2026, 3, 5): spend[0], date(2026, 3, 8): spend[1]}
    instance = make_instance(daily_usage=daily_usage)

    available = []
    for today in CARRYOVER_DECAY_TODAYS:
        total = _total(cfg, instance, today)
        assert total is not None
        in_window = sum(s for d, s in daily_usage.items() if today - timedelta(days=28) <= d <= today)
        available.append(total - in_window)

    assert available == sorted(available)
    assert available[-1] == 100  # fully decayed back to base, with no debt carried in


def test_pre_grant_debt_snaps_back_at_expiry_matching_the_no_grant_case() -> None:
    """The active-gate on overage is deliberate: forgiveness ends with the grant.

    That still satisfies "never worse off than if the grant never existed" — the
    no-grant counterfactual is equally negative.
    """
    grant = NetGrant(
        start_date=datetime(2026, 3, 10, tzinfo=timezone.utc),
        net_grant_seconds=1000,
        end_date=datetime(2026, 3, 20, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=100, net_grants=[grant])
    no_grant_cfg = make_config(limit_seconds=100)
    instance = make_instance(daily_usage={date(2026, 3, 1): 500})

    # While active, the 400 of pre-grant debt above base is forgiven.
    assert _total(cfg, instance, date(2026, 3, 15)) == 100 + 1000 + 400
    # At expiry it snaps back — and lands exactly where it would with no grant at all.
    assert _total(cfg, instance, date(2026, 3, 20)) == _total(no_grant_cfg, instance, date(2026, 3, 20)) == 100


# -------------------------------------------------------------------
# Degenerate inputs
# -------------------------------------------------------------------


def test_no_daily_usage_with_relevant_expired_grant_returns_base() -> None:
    cfg = make_config(limit_seconds=100, net_grants=[GRANT_1000])
    assert _total(cfg, make_instance(), date(2026, 3, 29)) == 100


def test_usage_after_today_is_ignored() -> None:
    cfg = make_config(limit_seconds=100, net_grants=[GRANT_1000])
    instance = make_instance(daily_usage={date(2026, 3, 28): 400, date(2026, 3, 30): 999})
    assert _total(cfg, instance, date(2026, 3, 29)) == 100 + 400


def test_zero_length_grant_credits_nothing() -> None:
    """`start.date() == end.date()` covers no day, so it can never be charged usage."""
    grant = NetGrant(
        start_date=datetime(2026, 3, 5, tzinfo=timezone.utc),
        net_grant_seconds=1000,
        end_date=datetime(2026, 3, 5, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=100, net_grants=[grant])
    instance = make_instance(daily_usage={date(2026, 3, 5): 400})
    assert _total(cfg, instance, date(2026, 3, 10)) == 100


def test_grants_without_base_limit_raise_even_when_none_is_active() -> None:
    """The config invariant is checked whenever grants exist, not only on the active path."""
    cfg = make_config(net_grants=[GRANT_1000])
    with pytest.raises(AssertionError, match="net_grants without target_limit_seconds"):
        resolve_limit(cfg, make_instance(), date(2026, 12, 1))
