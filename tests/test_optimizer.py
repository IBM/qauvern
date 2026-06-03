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

import pytest

from qauvern.models import (
    Account,
    InstanceState,
    InstanceConfig,
    InstanceDetailedUsage,
    OptimizationRecommendation,
    OptimizationResult,
)
from qauvern.optimizer import AllocationOptimizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instance(crn: str, allocation: int, *, name: str | None = None) -> InstanceState:
    return InstanceState(
        crn=crn,
        name=name or crn,
        allocation_seconds=allocation,
        limit_seconds=None,
        consumed_seconds=0,
        detailed_usage=None,
    )


def _make_account(target: int, *instances: InstanceState) -> Account:
    allocated = sum(i.allocation_seconds for i in instances)
    return Account(
        account_id="test",
        plan_id="test-plan",
        target_usage_seconds=target,
        available_seconds=max(0, target - allocated),
        limit_seconds=None,
        instances=instances,
    )


def _make_config(crn: str, *, target: int | None = None, name: str | None = None) -> InstanceConfig:
    return InstanceConfig(
        name=name or crn,
        crn=crn,
        target_usage_seconds=target,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )


# ---------------------------------------------------------------------------
# analyze / optimize
# ---------------------------------------------------------------------------


@pytest.fixture
def optimizer_account() -> Account:
    instance1 = InstanceState(
        crn="crn:test:1",
        name="Active Instance",
        allocation_seconds=600000,
        consumed_seconds=550000,  # High usage in 28d
        limit_seconds=800000,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=0,
            consumed_14day=400000,
            consumed_7day=300000,
            consumed_3day=150000,
            consumed_24h=50000,  # Active in last 24h
            daily_usage={},
        ),
    )
    instance2 = InstanceState(
        crn="crn:test:2",
        name="Inactive Instance",
        allocation_seconds=400000,
        consumed_seconds=1000,  # Very low usage
        limit_seconds=500000,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=0,
            consumed_14day=0,  # No recent activity
            consumed_7day=0,
            consumed_3day=0,
            consumed_24h=0,
            daily_usage={},
        ),
    )
    instance3 = InstanceState(
        crn="crn:test:3",
        name="Medium Instance",
        allocation_seconds=300000,
        consumed_seconds=150000,  # Medium usage
        limit_seconds=400000,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=0,
            consumed_14day=100000,
            consumed_7day=50000,
            consumed_3day=20000,
            consumed_24h=0,  # Not active in last 24h
            daily_usage={},
        ),
    )
    return Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=2000000,
        available_seconds=700000,  # 2000000 - 1300000 (total allocated)
        limit_seconds=None,
        instances=(instance1, instance2, instance3),
    )


@pytest.fixture
def optimizer_instance_configs() -> list[InstanceConfig]:
    cfg1 = InstanceConfig(
        name="Instance A",
        crn="crn:test:1",
        target_usage_seconds=1000000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
        target_limit_seconds=900000,
    )

    cfg2 = InstanceConfig(
        name="Instance B",
        crn="crn:test:2",
        target_usage_seconds=500000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )

    cfg3 = InstanceConfig(
        name="Instance C",
        crn="crn:test:3",
        target_usage_seconds=400000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )

    return [cfg1, cfg2, cfg3]


def test_optimizer_initialization(optimizer_account: Account, optimizer_instance_configs: list[InstanceConfig]) -> None:
    """Test optimizer initialization."""
    optimizer = AllocationOptimizer(optimizer_account, optimizer_instance_configs)

    assert optimizer.account == optimizer_account
    assert len(optimizer.instance_configs) == 3
    assert len(optimizer._config_by_crn) == 3  # One CRN per cfg


def test_consumption_for_config(optimizer_account: Account, optimizer_instance_configs: list[InstanceConfig]) -> None:
    """Test calculating cfg consumption."""
    optimizer = AllocationOptimizer(optimizer_account, optimizer_instance_configs)

    cfg1_consumption = optimizer._consumption_for(optimizer_instance_configs[0])
    assert cfg1_consumption == 550000  # instance1 (crn:test:1)

    cfg2_consumption = optimizer._consumption_for(optimizer_instance_configs[1])
    assert cfg2_consumption == 1000  # instance2 (crn:test:2)

    cfg3_consumption = optimizer._consumption_for(optimizer_instance_configs[2])
    assert cfg3_consumption == 150000  # instance3 (crn:test:3)


def test_remaining_for_config(optimizer_account: Account, optimizer_instance_configs: list[InstanceConfig]) -> None:
    """Test calculating cfg remaining allocation."""
    optimizer = AllocationOptimizer(optimizer_account, optimizer_instance_configs)

    cfg1_remaining = optimizer._remaining_for(optimizer_instance_configs[0])
    assert cfg1_remaining == 450000  # 1000000 - 550000

    cfg2_remaining = optimizer._remaining_for(optimizer_instance_configs[1])
    assert cfg2_remaining == 499000  # 500000 - 1000

    cfg3_remaining = optimizer._remaining_for(optimizer_instance_configs[2])
    assert cfg3_remaining == 250000  # 400000 - 150000


def test_optimize_generates_recommendations(
    optimizer_account: Account, optimizer_instance_configs: list[InstanceConfig]
) -> None:
    """Test that optimize generates recommendations with limits."""
    optimizer = AllocationOptimizer(optimizer_account, optimizer_instance_configs)
    result = optimizer.optimize()

    assert len(result.recommendations) > 0

    # Check that limits are included in recommendations
    has_limits = any(rec.new_limit is not None for rec in result.recommendations)
    assert has_limits


# ---------------------------------------------------------------------------
# validate_allocations
# ---------------------------------------------------------------------------


def test_validate_allocations_valid() -> None:
    """Allocation under both account and per-instance targets passes."""
    account = _make_account(10, _make_instance("crn:test:1", 5))
    cfg = _make_config("crn:test:1", target=8)

    is_valid, errors = AllocationOptimizer(account, [cfg]).validate_allocations(OptimizationResult(()))

    assert is_valid
    assert errors == []


def test_validate_allocations_exceeds_instance_target() -> None:
    """An instance allocated above its config target produces a per-config error."""
    account = _make_account(20, _make_instance("crn:test:1", 12))
    cfg = _make_config("crn:test:1", target=10, name="Test Instance")

    is_valid, errors = AllocationOptimizer(account, [cfg]).validate_allocations(OptimizationResult(()))

    assert not is_valid
    assert errors == ["Instance 'Test Instance' total allocation (12s) exceeds target (10s)"]


def test_validate_skips_config_without_target() -> None:
    """A config with target_usage_seconds=None is not gated against the instance allocation."""
    account = _make_account(20, _make_instance("crn:test:1", 15))
    cfg = _make_config("crn:test:1", target=None, name="No Target")

    is_valid, errors = AllocationOptimizer(account, [cfg]).validate_allocations(OptimizationResult(()))

    assert is_valid
    assert errors == []


def test_validate_allocations_uses_result_overrides() -> None:
    """A recommendation that pushes a clean state past the cap is flagged."""
    account = _make_account(5, _make_instance("crn:test:1", 4))
    optimizer = AllocationOptimizer(account, [])

    # Current state is valid (4 <= 5).
    is_valid, _ = optimizer.validate_allocations(OptimizationResult(()))
    assert is_valid

    # Bumping to 6 projects the total over the cap.
    over_cap = OptimizationResult(
        recommendations=(
            OptimizationRecommendation(
                instance_crn="crn:test:1",
                current_allocation=4,
                new_allocation=6,
                reason="test",
            ),
        )
    )
    is_valid, errors = optimizer.validate_allocations(over_cap)
    assert not is_valid
    assert errors == ["Total instance allocations (6s) exceeds account target (5s)"]


def test_validate_allocations_includes_unconfigured() -> None:
    """Allocation held by instances not loaded must still count toward the cap."""
    # target=10, available=1, loaded holds 4 → 5 sits on instances we did not load.
    account = Account(
        account_id="test",
        plan_id="test-plan",
        target_usage_seconds=10,
        available_seconds=1,
        limit_seconds=None,
        instances=(_make_instance("crn:test:1", 4),),
    )
    assert account.unconfigured_allocation_seconds == 5
    optimizer = AllocationOptimizer(account, [])

    # Bumping the loaded instance to 5 fits the cap exactly (5 + 5 unconfigured = 10).
    fits = OptimizationResult(
        recommendations=(
            OptimizationRecommendation(
                instance_crn="crn:test:1",
                current_allocation=4,
                new_allocation=5,
                reason="test",
            ),
        )
    )
    is_valid, _ = optimizer.validate_allocations(fits)
    assert is_valid

    # Bumping to 6 overflows: 6 + 5 unconfigured > 10.
    over = OptimizationResult(
        recommendations=(
            OptimizationRecommendation(
                instance_crn="crn:test:1",
                current_allocation=4,
                new_allocation=6,
                reason="test",
            ),
        )
    )
    is_valid, errors = optimizer.validate_allocations(over)
    assert not is_valid
    assert errors == ["Total instance allocations (11s) exceeds account target (10s)"]


# ---------------------------------------------------------------------------
# Reserve, limits, and no-target handling
# ---------------------------------------------------------------------------


def _make_account_and_config() -> tuple[Account, InstanceConfig]:
    """Build account+cfg where reserve is the binding allocation constraint.

    Fixture arithmetic:
      freed = 200000 - 100000 = 100000  (active instance reduced to 28d floor)
      total raw = 100000 + 500000 = 600000
      with 20% reserve: int(600000 * 0.8) = 480000  -> new_allocation = 100000 + 480000 = 580000
      with  0% reserve: int(600000 * 1.0) = 600000  -> new_allocation = 100000 + 600000 = 700000
      remaining = 2000000 - 100000 = 1900000, far above both, so reserve is binding.
    """
    instance = InstanceState(
        crn="crn:test:reserve:1",
        name="Active Instance",
        allocation_seconds=200000,
        consumed_seconds=100000,
        limit_seconds=None,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=0,
            consumed_14day=80000,
            consumed_7day=60000,
            consumed_3day=30000,
            consumed_24h=10000,
            daily_usage={},
        ),
    )
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=5000000,
        available_seconds=500000,
        limit_seconds=None,
        instances=(instance,),
    )
    cfg = InstanceConfig(
        name="Instance A",
        crn="crn:test:reserve:1",
        target_usage_seconds=2000000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )
    return account, cfg


def test_reserve_reduces_available_allocation() -> None:
    """With 20% reserve, new allocation is measurably lower than with 0% reserve."""
    account, cfg = _make_account_and_config()
    result_with = AllocationOptimizer(account, [cfg], allocation_reserve_percent=20.0).optimize()
    result_without = AllocationOptimizer(account, [cfg], allocation_reserve_percent=0.0).optimize()

    alloc_with = next(r.new_allocation for r in result_with.recommendations if r.instance_crn == "crn:test:reserve:1")
    alloc_without = next(
        r.new_allocation for r in result_without.recommendations if r.instance_crn == "crn:test:reserve:1"
    )
    assert alloc_with < alloc_without


def test_zero_reserve_is_deterministic() -> None:
    """Two runs with 0% reserve on identical fixtures produce identical recommendations."""
    account, cfg = _make_account_and_config()

    result_a = AllocationOptimizer(account, [cfg], allocation_reserve_percent=0.0).optimize()
    result_b = AllocationOptimizer(account, [cfg], allocation_reserve_percent=0.0).optimize()

    recs_a = {r.instance_crn: r.new_allocation for r in result_a.recommendations}
    recs_b = {r.instance_crn: r.new_allocation for r in result_b.recommendations}
    assert recs_a == recs_b


@pytest.fixture
def lr_account_and_config() -> tuple[Account, InstanceConfig]:
    instance = InstanceState(
        crn="crn:test:lr:1",
        name="Test Instance",
        allocation_seconds=250000,
        consumed_seconds=100000,
        limit_seconds=None,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=0,
            consumed_14day=80000,
            consumed_7day=60000,
            consumed_3day=30000,
            consumed_24h=10000,
            daily_usage={},
        ),
    )
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        available_seconds=200000,
        limit_seconds=None,
        instances=(instance,),
    )
    cfg = InstanceConfig(
        name="Instance A",
        crn="crn:test:lr:1",
        target_usage_seconds=300000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
        target_limit_seconds=350000,
    )
    return account, cfg


def test_optimize_uses_limit_seconds(lr_account_and_config: tuple[Account, InstanceConfig]) -> None:
    """Optimizer sets new_limit from limit_seconds via LimitResolver."""
    account, cfg = lr_account_and_config
    optimizer = AllocationOptimizer(account, [cfg])
    result = optimizer.optimize()
    limit_recs = [r for r in result.recommendations if r.new_limit is not None]
    assert len(limit_recs) > 0
    assert limit_recs[0].new_limit == 350000


def test_optimize_uses_active_grant(lr_account_and_config: tuple[Account, InstanceConfig]) -> None:
    """Optimizer sets new_limit from active net grant via LimitResolver."""
    import dataclasses
    from datetime import date
    from qauvern.models import NetGrant

    account, cfg = lr_account_and_config
    grant = NetGrant(
        start_date=datetime(2026, 4, 15),
        net_grant_seconds=300000,
        end_date=datetime(2026, 5, 13),
    )
    cfg = dataclasses.replace(cfg, target_limit_seconds=200000, net_grants=(grant,))
    optimizer = AllocationOptimizer(account, [cfg], today=date(2026, 4, 15))
    result = optimizer.optimize()
    limit_recs = [r for r in result.recommendations if r.new_limit is not None]
    assert len(limit_recs) > 0
    assert limit_recs[0].new_limit == 500000  # 200000 base + 300000 grant


def test_no_target_usage_caps_at_limit() -> None:
    """Optimizer caps allocation at limit_seconds when cfg has no target."""
    instance = InstanceState(
        crn="crn:test:1",
        name="Active Instance",
        allocation_seconds=100000,
        consumed_seconds=50000,
        limit_seconds=200000,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=0,
            consumed_14day=40000,
            consumed_7day=30000,
            consumed_3day=15000,
            consumed_24h=5000,
            daily_usage={},
        ),
    )
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=2000000,
        available_seconds=500000,
        limit_seconds=None,
        instances=(instance,),
    )

    cfg = InstanceConfig(
        name="No Target Instance",
        crn="crn:test:1",
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
        target_limit_seconds=200000,
    )

    optimizer = AllocationOptimizer(account, [cfg])
    result = optimizer.optimize()

    rec = next((r for r in result.recommendations if r.instance_crn == "crn:test:1"), None)
    assert rec is not None
    assert instance.limit_seconds is not None
    assert rec.new_allocation <= instance.limit_seconds


def test_no_target_instance_never_exhausted() -> None:
    """Instance with no cfg target is never exhausted."""
    instance = InstanceState(
        crn="crn:test:1",
        name="Heavy Instance",
        allocation_seconds=500000,
        consumed_seconds=500000,
        limit_seconds=600000,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=999999,
            consumed_14day=0,
            consumed_7day=0,
            consumed_3day=0,
            consumed_24h=0,
            daily_usage={},
        ),
    )
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=2000000,
        available_seconds=0,
        limit_seconds=None,
        instances=(instance,),
    )

    cfg = InstanceConfig(
        name="No Target",
        crn="crn:test:1",
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )

    optimizer = AllocationOptimizer(account, [cfg])
    result = optimizer.optimize()

    # Should NOT be treated as exhausted (no allocation=0 recommendation)
    rec = next((r for r in result.recommendations if r.instance_crn == "crn:test:1"), None)
    if rec is not None:
        assert rec.new_allocation > 0
