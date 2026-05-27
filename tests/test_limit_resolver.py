# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for LimitResolver with net grant rolloff."""

from datetime import date, datetime, timedelta

import pytest

from qauvern.limit_resolver import LimitResolver
from qauvern.models import Instance, InstanceConfig, InstanceDetailedUsage, NetGrant


def make_instance_config(
    limit_seconds: int | None = None,
    net_grants: list[NetGrant] | None = None,
    target_usage_seconds: int = 30000,
) -> InstanceConfig:
    return InstanceConfig(
        name="Test",
        crn="crn:test:1",
        target_usage_seconds=target_usage_seconds,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
        limit_seconds=limit_seconds,
        net_grants=tuple(net_grants) if net_grants else (),
    )


def make_instance(
    consumed_balance: int = 0,
    target: int = 30000,
    consumed_seconds: int = 0,
    daily_usage: dict[date, int] | None = None,
) -> Instance:
    return Instance(
        crn="crn:test:1",
        name="Test Instance",
        allocation_seconds=10000,
        target_usage_seconds=target,
        consumed_balance_period=consumed_balance,
        consumed_seconds=consumed_seconds,
        detailed_usage=InstanceDetailedUsage(
            consumed_14day=0,
            consumed_7day=0,
            consumed_3day=0,
            consumed_24h=0,
            daily_usage=daily_usage or {},
        ),
    )


@pytest.fixture
def resolver() -> LimitResolver:
    return LimitResolver()


def test_no_limit_fields_returns_none(resolver: LimitResolver) -> None:
    cfg = make_instance_config()
    instance = make_instance()
    assert resolver.resolve(cfg, instance, date(2026, 4, 27)) is None


def test_base_limit_only_returns_base(resolver: LimitResolver) -> None:
    cfg = make_instance_config(limit_seconds=50000)
    instance = make_instance()
    assert resolver.resolve(cfg, instance, date(2026, 4, 27)) == 50000


def test_exhausted_returns_one(resolver: LimitResolver) -> None:
    cfg = make_instance_config(limit_seconds=50000)
    instance = make_instance(consumed_balance=30000, target=30000)
    assert resolver.resolve(cfg, instance, date(2026, 4, 27)) == 1


def test_exhausted_no_limits_returns_one(resolver: LimitResolver) -> None:
    cfg = make_instance_config()
    instance = make_instance(consumed_balance=30000, target=30000)
    assert resolver.resolve(cfg, instance, date(2026, 4, 27)) == 1


def test_grant_active_on_start_date(resolver: LimitResolver) -> None:
    grant = NetGrant(start_date=datetime(2026, 5, 1), net_grant_seconds=40000, end_date=datetime(2026, 5, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    result = resolver.resolve(cfg, make_instance(), date(2026, 5, 1))
    assert result == 90000


def test_grant_active_on_day_27(resolver: LimitResolver) -> None:
    """Last day of grant window."""
    start = datetime(2026, 5, 1)
    last_day = start.date() + timedelta(days=27)
    grant = NetGrant(start_date=start, net_grant_seconds=40000, end_date=datetime(2026, 5, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    result = resolver.resolve(cfg, make_instance(), last_day)
    assert result == 90000


def test_grant_expired_on_day_28(resolver: LimitResolver) -> None:
    """Day 28 is exclusive — grant is over."""
    start = datetime(2026, 5, 1)
    day_28 = start.date() + timedelta(days=28)
    grant = NetGrant(start_date=start, net_grant_seconds=40000, end_date=datetime(2026, 5, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    result = resolver.resolve(cfg, make_instance(), day_28)
    assert result == 50000


def test_future_grant_returns_base_limit(resolver: LimitResolver) -> None:
    grant = NetGrant(start_date=datetime(2026, 5, 1), net_grant_seconds=40000, end_date=datetime(2026, 5, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    result = resolver.resolve(cfg, make_instance(), date(2026, 4, 30))
    assert result == 50000


def test_no_rolloff_when_grant_just_started(resolver: LimitResolver) -> None:
    """On grant start day, no pre-grant days have exited yet. Full grant active."""
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=100000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    # yesterday is still inside the 28-day window (today - 28 = 2026-03-30)
    instance = make_instance(daily_usage={date(2026, 4, 26): 5000})
    result = resolver.resolve(cfg, instance, today)
    assert result == 150000  # full grant, no rolloff


def test_one_day_rolled_off_reduces_limit(resolver: LimitResolver) -> None:
    """A pre-grant day exits the 28-day window, reducing the effective limit."""
    # Grant started 2026-03-31 (27 days before today 2026-04-27) — still active (day 27 of 28)
    # today - 28 = 2026-03-30
    # Pre-grant exit: d < 2026-03-31 (pre-grant) AND d >= 2026-03-31 - 27 = 2026-03-04
    #                 AND d < 2026-03-30 (exited window)
    # 2026-03-29 qualifies: pre-grant (< 2026-03-31), was in window at grant start (>= 2026-03-04),
    #                       and now exited (< 2026-03-30)
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 3, 31), net_grant_seconds=100000, end_date=datetime(2026, 4, 28))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    instance = make_instance(daily_usage={date(2026, 3, 29): 5000})
    result = resolver.resolve(cfg, instance, today)
    # rolloff = 5000, grant contribution = 100000 - 5000 = 95000
    assert result == 145000


def test_rolloff_capped_at_grant_amount(resolver: LimitResolver) -> None:
    """Rolloff cannot exceed grant amount; effective limit floor is base_limit."""
    today = date(2026, 4, 27)
    # Grant started 2026-03-31 (27 days ago) — still active
    grant = NetGrant(start_date=datetime(2026, 3, 31), net_grant_seconds=1000, end_date=datetime(2026, 4, 28))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    # 2026-03-29: pre-grant, was in window at grant start (>= 2026-03-04), now exited (< 2026-03-30)
    instance = make_instance(daily_usage={date(2026, 3, 29): 999999})
    result = resolver.resolve(cfg, instance, today)
    assert result == 50000  # grant contribution = max(0, 1000 - 999999) = 0


def test_days_still_in_window_do_not_roll_off(resolver: LimitResolver) -> None:
    """Days inside the current 28-day window have not rolled off — no rolloff."""
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 4, 1), net_grant_seconds=100000, end_date=datetime(2026, 4, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    # 2026-04-02 is 25 days ago — still inside 28-day window (today - 28 = 2026-03-30)
    instance = make_instance(daily_usage={date(2026, 4, 2): 5000})
    result = resolver.resolve(cfg, instance, today)
    assert result == 150000  # no rolloff


def test_post_grant_days_never_roll_off(resolver: LimitResolver) -> None:
    """Usage after grant start does not count as rolloff (not pre-grant)."""
    today = date(2026, 4, 27)
    # Grant started 2026-03-31 (27 days ago) — still active
    grant = NetGrant(start_date=datetime(2026, 3, 31), net_grant_seconds=100000, end_date=datetime(2026, 4, 28))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    # 2026-04-01 is after grant start — not a pre-grant day, cannot roll off
    instance = make_instance(daily_usage={date(2026, 4, 1): 5000})
    result = resolver.resolve(cfg, instance, today)
    assert result == 150000  # no rolloff


def test_grant_expired_after_28_days(resolver: LimitResolver) -> None:
    """After 28 days, the grant is inactive regardless of rolloff."""
    today = date(2026, 4, 27)
    # Grant started exactly 28 days ago — window closed today
    grant = NetGrant(start_date=datetime(2026, 3, 30), net_grant_seconds=100000, end_date=datetime(2026, 4, 27))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    instance = make_instance(daily_usage={date(2026, 3, 29): 5000})
    result = resolver.resolve(cfg, instance, today)
    assert result == 50000  # grant inactive, base only


def test_two_active_grants_stack(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    grant1 = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=100000, end_date=datetime(2026, 5, 25))
    grant2 = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=50000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant1, grant2])
    result = resolver.resolve(cfg, make_instance(), today)
    assert result == 200000  # 50000 + 100000 + 50000


def test_one_active_one_expired_grant(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    expired = NetGrant(
        start_date=datetime(2026, 3, 30), net_grant_seconds=100000, end_date=datetime(2026, 4, 27)
    )  # expired
    active = NetGrant(
        start_date=datetime(2026, 4, 27), net_grant_seconds=50000, end_date=datetime(2026, 5, 25)
    )  # active today
    cfg = make_instance_config(limit_seconds=50000, net_grants=[expired, active])
    result = resolver.resolve(cfg, make_instance(), today)
    assert result == 100000  # 50000 base + 50000 active grant only


def test_two_grants_independent_rolloff(resolver: LimitResolver) -> None:
    """Each grant's rolloff is computed independently from its own pre-grant days."""
    today = date(2026, 4, 27)
    # Grant 1: started 2026-03-31 (27 days ago, still active on day 27 of 28)
    # today - 28 = 2026-03-30; pre-grant days < 2026-03-31 and < 2026-03-30 and >= 2026-03-04
    # 2026-03-29 qualifies for grant1 rolloff
    grant1 = NetGrant(start_date=datetime(2026, 3, 31), net_grant_seconds=100000, end_date=datetime(2026, 4, 28))
    # Grant 2: started today; no rolloff
    grant2 = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=50000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant1, grant2])
    instance = make_instance(daily_usage={date(2026, 3, 29): 5000})
    result = resolver.resolve(cfg, instance, today)
    # grant1 contribution: 100000 - 5000 = 95000
    # grant2 contribution: 50000 (no rolloff — 2026-03-29 is before grant2 start window)
    assert result == 50000 + 95000 + 50000  # 195000


def test_active_grant_no_base_limit_returns_grant_only(resolver: LimitResolver) -> None:
    """Grant without limit_seconds: base treated as 0."""
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=40000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=None, net_grants=[grant])
    result = resolver.resolve(cfg, make_instance(), today)
    assert result == 40000


def test_exhausted_with_active_grant_returns_one(resolver: LimitResolver) -> None:
    """Exhausted always wins."""
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=40000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    instance = make_instance(consumed_balance=30000, target=30000)
    result = resolver.resolve(cfg, instance, today)
    assert result == 1


def test_grant_with_custom_end_date_active(resolver: LimitResolver) -> None:
    """Grant with explicit end_date 45 days out is still active on day 30."""
    start = datetime(2026, 5, 1)
    grant = NetGrant(
        start_date=start,
        net_grant_seconds=40000,
        end_date=datetime(2026, 6, 15),
    )
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    result = resolver.resolve(cfg, make_instance(), date(2026, 5, 31))
    assert result == 90000


def test_grant_with_short_end_date_expired(resolver: LimitResolver) -> None:
    """Grant with explicit end_date 14 days out is inactive on day 14."""
    start = datetime(2026, 5, 1)
    grant = NetGrant(
        start_date=start,
        net_grant_seconds=40000,
        end_date=datetime(2026, 5, 15),
    )
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    result = resolver.resolve(cfg, make_instance(), date(2026, 5, 15))
    assert result == 50000  # end_date is exclusive, grant expired


@pytest.fixture
def rolloff_scenario() -> tuple[LimitResolver, InstanceConfig, Instance]:
    """End-to-end rolloff scenario fixture.

    Setup (all values in seconds; 30m=1800s, 100m=6000s, 200m=12000s):
      - Base limit: 12000s
      - Jan 1-4: 1800s usage/day (pre-grant)
      - Jan 5: grant of 6000s starts; grant window = [Jan 5, Feb 2) (28 days)
      - Jan 5 onward: no usage
    """
    resolver = LimitResolver()
    grant_start = datetime(2026, 1, 5)
    cfg = make_instance_config(
        limit_seconds=12000,
        net_grants=[NetGrant(start_date=grant_start, net_grant_seconds=6000, end_date=datetime(2026, 2, 2))],
    )
    instance = make_instance(
        daily_usage={
            date(2026, 1, 1): 1800,
            date(2026, 1, 2): 1800,
            date(2026, 1, 3): 1800,
            date(2026, 1, 4): 1800,
        }
    )
    return resolver, cfg, instance


def test_jan5_grant_start_full_grant(rolloff_scenario: tuple[LimitResolver, InstanceConfig, Instance]) -> None:
    """Jan 5 (grant start): no rolloff yet, full grant active."""
    resolver, cfg, instance = rolloff_scenario
    assert resolver.resolve(cfg, instance, date(2026, 1, 5)) == 18000


def test_jan29_no_rolloff_yet(rolloff_scenario: tuple[LimitResolver, InstanceConfig, Instance]) -> None:
    """Jan 29: today-28=Jan 1, but rolloff requires d < Jan 1 strict, so no rolloff."""
    # today - 28 = Jan 1; condition is d < Jan 1 (strict), so Jan 1 itself has not exited
    resolver, cfg, instance = rolloff_scenario
    assert resolver.resolve(cfg, instance, date(2026, 1, 29)) == 18000


def test_jan30_first_day_rolls_off(rolloff_scenario: tuple[LimitResolver, InstanceConfig, Instance]) -> None:
    """Jan 30: today-28=Jan 2, Jan 1 exits (d < Jan 2), rolloff=1800."""
    resolver, cfg, instance = rolloff_scenario
    assert resolver.resolve(cfg, instance, date(2026, 1, 30)) == 16200  # 12000+4200


def test_jan31_two_days_rolled_off(rolloff_scenario: tuple[LimitResolver, InstanceConfig, Instance]) -> None:
    """Jan 31: today-28=Jan 3, Jan 1+2 exit, rolloff=3600."""
    resolver, cfg, instance = rolloff_scenario
    assert resolver.resolve(cfg, instance, date(2026, 1, 31)) == 14400  # 12000+2400


def test_feb1_three_days_rolled_off(rolloff_scenario: tuple[LimitResolver, InstanceConfig, Instance]) -> None:
    """Feb 1: today-28=Jan 4, Jan 1+2+3 exit, rolloff=5400, last active day of grant."""
    resolver, cfg, instance = rolloff_scenario
    assert resolver.resolve(cfg, instance, date(2026, 2, 1)) == 12600  # 12000+600


def test_feb2_grant_expired(rolloff_scenario: tuple[LimitResolver, InstanceConfig, Instance]) -> None:
    """Feb 2: grant_end=Jan 5+28=Feb 2, today >= grant_end so grant inactive, base only."""
    resolver, cfg, instance = rolloff_scenario
    assert resolver.resolve(cfg, instance, date(2026, 2, 2)) == 12000
