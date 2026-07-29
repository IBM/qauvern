# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for resolve_limit.

Formula under test (when at least one grant is active today):
    result = base + grant_total + max(0, rolloff - base)

where rolloff sums daily_usage on days in [today - 28, boost_start - 1].
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from qauvern.limit_resolver import resolve_limit
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


# -------------------------------------------------------------------
# Trivial paths
# -------------------------------------------------------------------


def test_no_limit_and_no_grants_returns_none() -> None:
    assert resolve_limit(make_config(), make_instance(), date(2026, 4, 27)) is None


def test_base_limit_only_no_grants_returns_base() -> None:
    assert resolve_limit(make_config(limit_seconds=500), make_instance(), date(2026, 4, 27)) == 500


def test_all_grants_expired_returns_base() -> None:
    grant = NetGrant(
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        net_grant_seconds=400,
        end_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    assert resolve_limit(cfg, make_instance(), date(2026, 4, 27)) == 500


def test_future_grant_returns_base() -> None:
    today = date(2026, 4, 30)
    grant = NetGrant(
        start_date=_dt(today + timedelta(days=1)), net_grant_seconds=400, end_date=_dt(today + timedelta(days=29))
    )
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    assert resolve_limit(cfg, make_instance(), today) == 500


# -------------------------------------------------------------------
# Single-grant boundary behavior
# -------------------------------------------------------------------


def test_grant_active_on_start_date() -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=_dt(today), net_grant_seconds=400, end_date=_dt(today + timedelta(days=28)))
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    assert resolve_limit(cfg, make_instance(), today) == 900


def test_grant_active_day_before_end() -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(
        start_date=_dt(today - timedelta(days=26)), net_grant_seconds=400, end_date=_dt(today + timedelta(days=1))
    )
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    # rolloff window: window_floor=today-28, rolloff_end=grant_start-1; no usage → rolloff=0
    assert resolve_limit(cfg, make_instance(), today) == 900


def test_grant_inactive_on_end_date() -> None:
    today = date(2026, 4, 28)
    grant = NetGrant(start_date=_dt(today - timedelta(days=27)), net_grant_seconds=400, end_date=_dt(today))
    cfg = make_config(limit_seconds=500, net_grants=[grant])
    assert resolve_limit(cfg, make_instance(), today) == 500


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
    assert resolve_limit(cfg, instance, today) == 900


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
    assert resolve_limit(cfg, instance, today) == 900


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
    assert resolve_limit(cfg, instance, today) == 26


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
    assert resolve_limit(cfg, instance, today) == 22


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
    assert resolve_limit(cfg, instance, today) == 18


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
    assert resolve_limit(cfg, instance, today) == 18


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
    assert resolve_limit(cfg, make_instance(), today) == 1100


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
    assert resolve_limit(cfg, instance, today) == 210


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
    assert resolve_limit(cfg, instance, today) == 200


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
    assert resolve_limit(cfg, instance, today) == expected
