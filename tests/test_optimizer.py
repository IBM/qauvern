# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for optimization algorithm."""

from datetime import datetime
from typing import Any

import pytest

from qauvern.models import (
    Instance,
    InstanceConfig,
    InstanceUsage,
    ResolvedAccount,
    ResolvedInstance,
)
from qauvern.optimizer import AllocationOptimizer


def _resolved(
    *,
    crn: str,
    name: str,
    allocation_seconds: int,
    consumed_seconds: int = 0,
    limit_seconds: int | None = None,
    target_usage_seconds: int | None = None,
    config_limit_seconds: int | None = None,
    config: InstanceConfig | None = None,
    **usage_kwargs: Any,
) -> ResolvedInstance:
    instance = Instance(
        crn=crn,
        name=name,
        allocation_seconds=allocation_seconds,
        consumed_seconds=consumed_seconds,
        limit_seconds=limit_seconds,
    )
    if config is None:
        config = InstanceConfig(
            name=name,
            crn=crn,
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 12, 31),
            target_usage_seconds=target_usage_seconds,
            limit_seconds=config_limit_seconds,
        )
    return ResolvedInstance(instance=instance, config=config, usage=InstanceUsage(**usage_kwargs))


def _account(
    instances: tuple[ResolvedInstance, ...],
    *,
    target_usage_seconds: int = 2000000,
    available_seconds: int = 0,
) -> ResolvedAccount:
    return ResolvedAccount(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=target_usage_seconds,
        available_seconds=available_seconds,
        limit_seconds=None,
        instances=instances,
    )


@pytest.fixture
def optimizer_account() -> ResolvedAccount:
    r1 = _resolved(
        crn="crn:test:1",
        name="Active Instance",
        allocation_seconds=600000,
        consumed_seconds=550000,
        limit_seconds=800000,
        target_usage_seconds=1000000,
        config_limit_seconds=900000,
        consumed_14day=400000,
        consumed_7day=300000,
        consumed_3day=150000,
        consumed_24h=50000,
    )
    r2 = _resolved(
        crn="crn:test:2",
        name="Inactive Instance",
        allocation_seconds=400000,
        consumed_seconds=1000,
        limit_seconds=500000,
        target_usage_seconds=500000,
    )
    r3 = _resolved(
        crn="crn:test:3",
        name="Medium Instance",
        allocation_seconds=300000,
        consumed_seconds=150000,
        limit_seconds=400000,
        target_usage_seconds=400000,
        consumed_14day=100000,
        consumed_7day=50000,
        consumed_3day=20000,
    )
    return _account((r1, r2, r3), target_usage_seconds=2000000, available_seconds=700000)


def test_optimizer_initialization(optimizer_account: ResolvedAccount) -> None:
    optimizer = AllocationOptimizer(optimizer_account)
    assert optimizer.account is optimizer_account
    assert len(optimizer.instance_configs) == 3


def test_get_active_instances(optimizer_account: ResolvedAccount) -> None:
    optimizer = AllocationOptimizer(optimizer_account)
    active = optimizer._get_active_instances(threshold_seconds=3600)

    assert len(active) == 2
    assert {r.crn for r in active} == {"crn:test:1", "crn:test:3"}


def test_get_inactive_instances(optimizer_account: ResolvedAccount) -> None:
    optimizer = AllocationOptimizer(optimizer_account)
    inactive = optimizer._get_inactive_instances(threshold_seconds=3600)

    assert len(inactive) == 1
    assert inactive[0].crn == "crn:test:2"


def test_remaining_for_resolved(optimizer_account: ResolvedAccount) -> None:
    optimizer = AllocationOptimizer(optimizer_account)
    by_crn = {r.crn: r for r in optimizer_account.instances}

    assert optimizer._remaining_for(by_crn["crn:test:1"]) == 450000  # 1000000 - 550000
    assert optimizer._remaining_for(by_crn["crn:test:2"]) == 499000  # 500000 - 1000
    assert optimizer._remaining_for(by_crn["crn:test:3"]) == 250000  # 400000 - 150000


def test_analyze_generates_recommendations(optimizer_account: ResolvedAccount) -> None:
    optimizer = AllocationOptimizer(optimizer_account)
    result = optimizer.analyze()

    assert len(result.recommendations) > 0
    assert result.account is optimizer_account
    assert len(result.instance_configs) == 3


def test_optimize_generates_recommendations(optimizer_account: ResolvedAccount) -> None:
    optimizer = AllocationOptimizer(optimizer_account)
    result = optimizer.optimize()

    assert len(result.recommendations) > 0
    assert any(rec.new_limit is not None for rec in result.recommendations)


def test_validate_allocations_valid() -> None:
    r1 = _resolved(
        crn="crn:test:1",
        name="Test1",
        allocation_seconds=500000,
        consumed_seconds=100000,
        target_usage_seconds=1500000,
    )
    r2 = _resolved(
        crn="crn:test:2",
        name="Test2",
        allocation_seconds=500000,
        consumed_seconds=100000,
    )
    account = _account((r1, r2), target_usage_seconds=2000000)

    is_valid, errors = AllocationOptimizer(account).validate_allocations()
    assert is_valid
    assert errors == []


def test_validate_allocations_exceeds_account() -> None:
    r1 = _resolved(
        crn="crn:test:1",
        name="Test1",
        allocation_seconds=400000,
        consumed_seconds=100000,
        target_usage_seconds=1000000,
    )
    r2 = _resolved(
        crn="crn:test:2",
        name="Test2",
        allocation_seconds=300000,
        consumed_seconds=100000,
    )
    account = _account((r1, r2), target_usage_seconds=500000)

    is_valid, errors = AllocationOptimizer(account).validate_allocations()
    assert not is_valid
    assert any("exceeds account target" in e for e in errors)


def test_validate_allocations_exceeds_instance_target() -> None:
    cfg = InstanceConfig(
        name="Test Instance",
        crn="crn:test:1",
        target_usage_seconds=1000000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )
    r1 = _resolved(crn="crn:test:1", name="Test1", allocation_seconds=600000, consumed_seconds=100000, config=cfg)
    r2 = _resolved(crn="crn:test:1", name="Test2", allocation_seconds=600000, consumed_seconds=100000, config=cfg)
    account = _account((r1, r2), target_usage_seconds=2000000)

    is_valid, errors = AllocationOptimizer(account).validate_allocations()
    assert not is_valid
    assert any("exceeds target" in e for e in errors)


def _reserve_account() -> ResolvedAccount:
    """Build account where reserve is the binding allocation constraint.

    Fixture arithmetic:
      freed = 200000 - 100000 = 100000  (active instance reduced to 28d floor)
      total raw = 100000 + 500000 = 600000
      with 20% reserve: int(600000 * 0.8) = 480000  -> new_allocation = 100000 + 480000 = 580000
      with  0% reserve: int(600000 * 1.0) = 600000  -> new_allocation = 100000 + 600000 = 700000
    """
    r = _resolved(
        crn="crn:test:reserve:1",
        name="Active Instance",
        allocation_seconds=200000,
        consumed_seconds=100000,
        target_usage_seconds=2000000,
        consumed_14day=80000,
        consumed_7day=60000,
        consumed_3day=30000,
        consumed_24h=10000,
    )
    return _account((r,), target_usage_seconds=5000000, available_seconds=500000)


def test_reserve_reduces_available_allocation() -> None:
    account = _reserve_account()
    result_with = AllocationOptimizer(account, allocation_reserve_percent=20.0).analyze()
    result_without = AllocationOptimizer(account, allocation_reserve_percent=0.0).analyze()

    alloc_with = next(r.new_allocation for r in result_with.recommendations if r.instance_crn == "crn:test:reserve:1")
    alloc_without = next(
        r.new_allocation for r in result_without.recommendations if r.instance_crn == "crn:test:reserve:1"
    )
    assert alloc_with < alloc_without


def test_zero_reserve_is_deterministic() -> None:
    account = _reserve_account()
    result_a = AllocationOptimizer(account, allocation_reserve_percent=0.0).analyze()
    result_b = AllocationOptimizer(account, allocation_reserve_percent=0.0).analyze()

    recs_a = {r.instance_crn: r.new_allocation for r in result_a.recommendations}
    recs_b = {r.instance_crn: r.new_allocation for r in result_b.recommendations}
    assert recs_a == recs_b


@pytest.fixture
def lr_account() -> ResolvedAccount:
    r = _resolved(
        crn="crn:test:lr:1",
        name="Test Instance",
        allocation_seconds=250000,
        consumed_seconds=100000,
        target_usage_seconds=300000,
        config_limit_seconds=350000,
        consumed_14day=80000,
        consumed_7day=60000,
        consumed_3day=30000,
        consumed_24h=10000,
    )
    return _account((r,), target_usage_seconds=1000000, available_seconds=200000)


def test_optimize_uses_limit_seconds(lr_account: ResolvedAccount) -> None:
    optimizer = AllocationOptimizer(lr_account)
    result = optimizer.optimize()
    limit_recs = [r for r in result.recommendations if r.new_limit is not None]
    assert len(limit_recs) > 0
    assert limit_recs[0].new_limit == 350000


def test_optimize_uses_active_grant(lr_account: ResolvedAccount) -> None:
    """Optimizer sets new_limit from active net grant via LimitResolver."""
    import dataclasses
    from datetime import date

    from qauvern.models import NetGrant

    grant = NetGrant(
        start_date=datetime(2026, 4, 15),
        net_grant_seconds=300000,
        end_date=datetime(2026, 5, 13),
    )
    original = lr_account.instances[0]
    new_config = dataclasses.replace(original.config, limit_seconds=200000, net_grants=(grant,))
    new_resolved = dataclasses.replace(original, config=new_config)
    account = dataclasses.replace(lr_account, instances=(new_resolved,))

    optimizer = AllocationOptimizer(account, today=date(2026, 4, 15))
    result = optimizer.optimize()
    limit_recs = [r for r in result.recommendations if r.new_limit is not None]
    assert len(limit_recs) > 0
    assert limit_recs[0].new_limit == 500000  # 200000 base + 300000 grant


def test_no_target_usage_caps_at_limit() -> None:
    r = _resolved(
        crn="crn:test:1",
        name="Active Instance",
        allocation_seconds=100000,
        consumed_seconds=50000,
        limit_seconds=200000,
        config_limit_seconds=200000,
        consumed_14day=40000,
        consumed_7day=30000,
        consumed_3day=15000,
        consumed_24h=5000,
    )
    account = _account((r,), target_usage_seconds=2000000, available_seconds=500000)
    result = AllocationOptimizer(account).analyze()

    rec = next((x for x in result.recommendations if x.instance_crn == "crn:test:1"), None)
    assert rec is not None
    assert r.limit_seconds is not None
    assert rec.new_allocation <= r.limit_seconds


def test_no_target_instance_never_exhausted() -> None:
    r = _resolved(
        crn="crn:test:1",
        name="Heavy Instance",
        allocation_seconds=500000,
        consumed_seconds=500000,
        limit_seconds=600000,
        consumed_balance_period=999999,
    )
    account = _account((r,), target_usage_seconds=2000000)
    result = AllocationOptimizer(account).analyze()

    rec = next((x for x in result.recommendations if x.instance_crn == "crn:test:1"), None)
    if rec is not None:
        assert rec.new_allocation > 0


def test_validate_skips_config_without_target() -> None:
    r = _resolved(
        crn="crn:test:1",
        name="Instance",
        allocation_seconds=999999,
        limit_seconds=1000000,
    )
    account = _account((r,), target_usage_seconds=2000000, available_seconds=2000000)

    is_valid, errors = AllocationOptimizer(account).validate_allocations()
    assert not any("No Target" in e for e in errors)
