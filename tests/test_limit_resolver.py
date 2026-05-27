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
from qauvern.models import (
    Instance,
    InstanceConfig,
    InstanceUsage,
    NetGrant,
    ResolvedInstance,
)


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


def make_resolved(
    cfg: InstanceConfig,
    consumed_balance: int = 0,
    consumed_seconds: int = 0,
    daily_usage: dict[date, int] | None = None,
) -> ResolvedInstance:
    instance = Instance(
        crn="crn:test:1",
        name="Test Instance",
        allocation_seconds=10000,
        consumed_seconds=consumed_seconds,
    )
    usage = InstanceUsage(
        consumed_balance_period=consumed_balance,
        daily_usage=daily_usage or {},
    )
    return ResolvedInstance(instance=instance, config=cfg, usage=usage)


@pytest.fixture
def resolver() -> LimitResolver:
    return LimitResolver()


def test_no_limit_fields_returns_none(resolver: LimitResolver) -> None:
    cfg = make_instance_config()
    assert resolver.resolve(make_resolved(cfg), date(2026, 4, 27)) is None


def test_base_limit_only_returns_base(resolver: LimitResolver) -> None:
    cfg = make_instance_config(limit_seconds=50000)
    assert resolver.resolve(make_resolved(cfg), date(2026, 4, 27)) == 50000


def test_exhausted_returns_one(resolver: LimitResolver) -> None:
    cfg = make_instance_config(limit_seconds=50000, target_usage_seconds=30000)
    assert resolver.resolve(make_resolved(cfg, consumed_balance=30000), date(2026, 4, 27)) == 1


def test_exhausted_no_limits_returns_one(resolver: LimitResolver) -> None:
    cfg = make_instance_config(target_usage_seconds=30000)
    assert resolver.resolve(make_resolved(cfg, consumed_balance=30000), date(2026, 4, 27)) == 1


def test_grant_active_on_start_date(resolver: LimitResolver) -> None:
    grant = NetGrant(start_date=datetime(2026, 5, 1), net_grant_seconds=40000, end_date=datetime(2026, 5, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    assert resolver.resolve(make_resolved(cfg), date(2026, 5, 1)) == 90000


def test_grant_active_on_day_27(resolver: LimitResolver) -> None:
    """Last day of grant window."""
    start = datetime(2026, 5, 1)
    last_day = start.date() + timedelta(days=27)
    grant = NetGrant(start_date=start, net_grant_seconds=40000, end_date=datetime(2026, 5, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    assert resolver.resolve(make_resolved(cfg), last_day) == 90000


def test_grant_expired_on_day_28(resolver: LimitResolver) -> None:
    """Day 28 is exclusive — grant is over."""
    start = datetime(2026, 5, 1)
    day_28 = start.date() + timedelta(days=28)
    grant = NetGrant(start_date=start, net_grant_seconds=40000, end_date=datetime(2026, 5, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    assert resolver.resolve(make_resolved(cfg), day_28) == 50000


def test_future_grant_returns_base_limit(resolver: LimitResolver) -> None:
    grant = NetGrant(start_date=datetime(2026, 5, 1), net_grant_seconds=40000, end_date=datetime(2026, 5, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    assert resolver.resolve(make_resolved(cfg), date(2026, 4, 30)) == 50000


def test_no_rolloff_when_grant_just_started(resolver: LimitResolver) -> None:
    """On grant start day, no pre-grant days have exited yet. Full grant active."""
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=100000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    resolved = make_resolved(cfg, daily_usage={date(2026, 4, 26): 5000})
    assert resolver.resolve(resolved, today) == 150000


def test_one_day_rolled_off_reduces_limit(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 3, 31), net_grant_seconds=100000, end_date=datetime(2026, 4, 28))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    resolved = make_resolved(cfg, daily_usage={date(2026, 3, 29): 5000})
    assert resolver.resolve(resolved, today) == 145000


def test_rolloff_capped_at_grant_amount(resolver: LimitResolver) -> None:
    """Rolloff cannot exceed grant amount; effective limit floor is base_limit."""
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 3, 31), net_grant_seconds=1000, end_date=datetime(2026, 4, 28))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    resolved = make_resolved(cfg, daily_usage={date(2026, 3, 29): 999999})
    assert resolver.resolve(resolved, today) == 50000


def test_days_still_in_window_do_not_roll_off(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 4, 1), net_grant_seconds=100000, end_date=datetime(2026, 4, 29))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    resolved = make_resolved(cfg, daily_usage={date(2026, 4, 2): 5000})
    assert resolver.resolve(resolved, today) == 150000


def test_post_grant_days_never_roll_off(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 3, 31), net_grant_seconds=100000, end_date=datetime(2026, 4, 28))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    resolved = make_resolved(cfg, daily_usage={date(2026, 4, 1): 5000})
    assert resolver.resolve(resolved, today) == 150000


def test_grant_expired_after_28_days(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 3, 30), net_grant_seconds=100000, end_date=datetime(2026, 4, 27))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    resolved = make_resolved(cfg, daily_usage={date(2026, 3, 29): 5000})
    assert resolver.resolve(resolved, today) == 50000


def test_two_active_grants_stack(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    grant1 = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=100000, end_date=datetime(2026, 5, 25))
    grant2 = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=50000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant1, grant2])
    assert resolver.resolve(make_resolved(cfg), today) == 200000


def test_one_active_one_expired_grant(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    expired = NetGrant(start_date=datetime(2026, 3, 30), net_grant_seconds=100000, end_date=datetime(2026, 4, 27))
    active = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=50000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[expired, active])
    assert resolver.resolve(make_resolved(cfg), today) == 100000


def test_two_grants_independent_rolloff(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    grant1 = NetGrant(start_date=datetime(2026, 3, 31), net_grant_seconds=100000, end_date=datetime(2026, 4, 28))
    grant2 = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=50000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant1, grant2])
    resolved = make_resolved(cfg, daily_usage={date(2026, 3, 29): 5000})
    assert resolver.resolve(resolved, today) == 50000 + 95000 + 50000  # 195000


def test_active_grant_no_base_limit_returns_grant_only(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=40000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=None, net_grants=[grant])
    assert resolver.resolve(make_resolved(cfg), today) == 40000


def test_exhausted_with_active_grant_returns_one(resolver: LimitResolver) -> None:
    today = date(2026, 4, 27)
    grant = NetGrant(start_date=datetime(2026, 4, 27), net_grant_seconds=40000, end_date=datetime(2026, 5, 25))
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant], target_usage_seconds=30000)
    assert resolver.resolve(make_resolved(cfg, consumed_balance=30000), today) == 1


def test_grant_with_custom_end_date_active(resolver: LimitResolver) -> None:
    grant = NetGrant(
        start_date=datetime(2026, 5, 1),
        net_grant_seconds=40000,
        end_date=datetime(2026, 6, 15),
    )
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    assert resolver.resolve(make_resolved(cfg), date(2026, 5, 31)) == 90000


def test_grant_with_short_end_date_expired(resolver: LimitResolver) -> None:
    grant = NetGrant(
        start_date=datetime(2026, 5, 1),
        net_grant_seconds=40000,
        end_date=datetime(2026, 5, 15),
    )
    cfg = make_instance_config(limit_seconds=50000, net_grants=[grant])
    assert resolver.resolve(make_resolved(cfg), date(2026, 5, 15)) == 50000


@pytest.fixture
def rolloff_scenario() -> tuple[LimitResolver, ResolvedInstance]:
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
    resolved = make_resolved(
        cfg,
        daily_usage={
            date(2026, 1, 1): 1800,
            date(2026, 1, 2): 1800,
            date(2026, 1, 3): 1800,
            date(2026, 1, 4): 1800,
        },
    )
    return resolver, resolved


def test_jan5_grant_start_full_grant(rolloff_scenario: tuple[LimitResolver, ResolvedInstance]) -> None:
    resolver, resolved = rolloff_scenario
    assert resolver.resolve(resolved, date(2026, 1, 5)) == 18000


def test_jan29_no_rolloff_yet(rolloff_scenario: tuple[LimitResolver, ResolvedInstance]) -> None:
    resolver, resolved = rolloff_scenario
    assert resolver.resolve(resolved, date(2026, 1, 29)) == 18000


def test_jan30_first_day_rolls_off(rolloff_scenario: tuple[LimitResolver, ResolvedInstance]) -> None:
    resolver, resolved = rolloff_scenario
    assert resolver.resolve(resolved, date(2026, 1, 30)) == 16200


def test_jan31_two_days_rolled_off(rolloff_scenario: tuple[LimitResolver, ResolvedInstance]) -> None:
    resolver, resolved = rolloff_scenario
    assert resolver.resolve(resolved, date(2026, 1, 31)) == 14400


def test_feb1_three_days_rolled_off(rolloff_scenario: tuple[LimitResolver, ResolvedInstance]) -> None:
    resolver, resolved = rolloff_scenario
    assert resolver.resolve(resolved, date(2026, 2, 1)) == 12600


def test_feb2_grant_expired(rolloff_scenario: tuple[LimitResolver, ResolvedInstance]) -> None:
    resolver, resolved = rolloff_scenario
    assert resolver.resolve(resolved, date(2026, 2, 2)) == 12000
