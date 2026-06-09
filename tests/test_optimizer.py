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

import dataclasses
from datetime import date, datetime

import pytest

from qauvern.models import (
    Account,
    AllocationChange,
    InstanceState,
    InstanceConfig,
    InstanceDetailedUsage,
    LimitChange,
    NetGrant,
    OptimizationResult,
)
from qauvern.optimizer import AllocationOptimizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _usage(
    *,
    consumed_24h: int = 0,
    consumed_3day: int = 0,
    consumed_7day: int = 0,
    consumed_14day: int = 0,
    daily_usage: dict[date, int] | None = None,
) -> InstanceDetailedUsage:
    return InstanceDetailedUsage(
        consumed_balance_period=0,
        consumed_14day=consumed_14day,
        consumed_7day=consumed_7day,
        consumed_3day=consumed_3day,
        consumed_24h=consumed_24h,
        daily_usage=daily_usage or {},
    )


def _make_instance(
    crn: str,
    allocation: int,
    *,
    name: str | None = None,
    consumed: int = 0,
    limit: int | None = None,
    detailed_usage: InstanceDetailedUsage | None = None,
) -> InstanceState:
    return InstanceState(
        crn=crn,
        name=name or crn,
        allocation_seconds=allocation,
        limit_seconds=limit,
        consumed_seconds=consumed,
        detailed_usage=detailed_usage,
    )


def _active_instance(
    crn: str,
    allocation: int,
    *,
    consumed: int = 0,
    limit: int | None = None,
    consumed_24h: int = 1,
) -> InstanceState:
    return _make_instance(
        crn,
        allocation,
        consumed=consumed,
        limit=limit,
        detailed_usage=_usage(consumed_24h=consumed_24h),
    )


def _inactive_instance(
    crn: str,
    allocation: int,
    *,
    consumed: int = 0,
    limit: int | None = None,
) -> InstanceState:
    return _make_instance(
        crn,
        allocation,
        consumed=consumed,
        limit=limit,
        detailed_usage=_usage(),
    )


def _make_account(target: int, *instances: InstanceState) -> Account:
    allocated = sum(i.allocation_seconds for i in instances)
    return Account(
        account_id="test",
        plan_id="test-plan",
        allocation_budget_seconds=target,
        unallocated_seconds=max(0, target - allocated),
        limit_seconds=None,
        instances=instances,
    )


def _make_config(crn: str, *, name: str | None = None, target_limit_seconds: int | None = None) -> InstanceConfig:
    return InstanceConfig(
        name=name or crn,
        crn=crn,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
        target_limit_seconds=target_limit_seconds,
    )


def _projected(result: OptimizationResult, account: Account) -> dict[str, int]:
    """Return projected allocation per crn, applying any AllocationChange."""
    chg = {c.instance_crn: c.new for c in result.allocation_changes}
    return {inst.crn: chg.get(inst.crn, inst.allocation_seconds) for inst in account.instances}


# ---------------------------------------------------------------------------
# validate_allocations
# ---------------------------------------------------------------------------


def test_validate_allocations_valid() -> None:
    """An already-valid state with no recommendations passes all invariants."""
    # allocation=100 satisfies invariant 4 (>= default minimum of 60)
    account = _make_account(1000, _make_instance("crn:test:1", 100))
    cfg = _make_config("crn:test:1")

    is_valid, errors = AllocationOptimizer(account, [cfg]).validate_allocations(OptimizationResult((), ()))

    assert is_valid
    assert errors == []


def test_validate_allocations_catches_preexisting_violation() -> None:
    """Current-state violations are flagged even when the result has no recommendations."""
    # allocation < consumed: no recommendation produced, validator still fires invariant 3.
    inst = _make_instance("crn:test:1", 50, consumed=100)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:test:1")])

    is_valid, errors = optimizer.validate_allocations(OptimizationResult((), ()))

    assert not is_valid
    assert any("28-day usage" in e for e in errors)


def test_validate_allocations_uses_result_overrides() -> None:
    """A recommendation that pushes a clean state past the cap is flagged."""
    account = _make_account(5, _make_instance("crn:test:1", 4))
    optimizer = AllocationOptimizer(account, [])

    # Current state is valid (4 <= 5).
    is_valid, _ = optimizer.validate_allocations(OptimizationResult((), ()))
    assert is_valid

    # Bumping to 6 projects the total over the cap.
    over_cap = OptimizationResult(
        allocation_changes=(AllocationChange(instance_crn="crn:test:1", current=4, new=6, reason="test"),),
        limit_changes=(),
    )
    is_valid, errors = optimizer.validate_allocations(over_cap)
    assert not is_valid
    assert errors == ["Total instance allocations (6s) exceeds account budget (5s)"]


def test_validate_allocations_includes_unmanaged() -> None:
    """Allocation held by instances not loaded must still count toward the cap."""
    # target=10, available=1, loaded holds 4 → 5 sits on instances we did not load.
    account = Account(
        account_id="test",
        plan_id="test-plan",
        allocation_budget_seconds=10,
        unallocated_seconds=1,
        limit_seconds=None,
        instances=(_make_instance("crn:test:1", 4),),
    )
    assert account.unmanaged_allocation_seconds == 5
    optimizer = AllocationOptimizer(account, [])

    # Bumping the loaded instance to 5 fits the cap exactly (5 + 5 unmanaged = 10).
    fits = OptimizationResult(
        allocation_changes=(AllocationChange(instance_crn="crn:test:1", current=4, new=5, reason="test"),),
        limit_changes=(),
    )
    is_valid, errors = optimizer.validate_allocations(fits)
    assert is_valid, errors

    # Bumping to 6 overflows: 6 + 5 unmanaged > 10.
    over = OptimizationResult(
        allocation_changes=(AllocationChange(instance_crn="crn:test:1", current=4, new=6, reason="test"),),
        limit_changes=(),
    )
    is_valid, errors = optimizer.validate_allocations(over)
    assert not is_valid
    assert errors == ["Total instance allocations (11s) exceeds account budget (10s)"]


# ---------------------------------------------------------------------------
# Invariant 2: reserve buffer
# ---------------------------------------------------------------------------


def test_validate_allocations_reserve_passes() -> None:
    """Projected total within the reserve-adjusted budget passes."""
    # unallocated = 800, above-floor = 200 - max(60, 100) = 100 → available = 900
    # reserve_amount = int(900 * 0.20) = 180; effective_budget = 820
    # projected total = 820 ≤ 820 → valid
    inst = _make_instance("crn:a", 200, consumed=100)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], allocation_reserve_percent=20.0)
    chg = AllocationChange(instance_crn="crn:a", current=200, new=820, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert is_valid, errors


def test_validate_allocations_reserve_violation() -> None:
    """Projected total that consumes the reserve buffer is rejected."""
    # same fixture; new=821 > effective_budget=820, but 821 ≤ 1000 so only reserve fires
    inst = _make_instance("crn:a", 200, consumed=100)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], allocation_reserve_percent=20.0)
    chg = AllocationChange(instance_crn="crn:a", current=200, new=821, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert not is_valid
    assert any("reserve" in e for e in errors)


# ---------------------------------------------------------------------------
# Invariant 3: new_allocation >= 28-day consumed usage
# ---------------------------------------------------------------------------


def test_validate_allocations_usage_floor_passes() -> None:
    """new_allocation equal to consumed_seconds passes invariant 3."""
    inst = _make_instance("crn:a", 200, consumed=100)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(instance_crn="crn:a", current=200, new=100, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert is_valid, errors


def test_validate_allocations_usage_floor_violation() -> None:
    """new allocation below consumed_seconds is rejected."""
    inst = _make_instance("crn:a", 200, consumed=150)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(instance_crn="crn:a", current=200, new=100, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert not is_valid
    assert any("28-day usage" in e for e in errors)


# ---------------------------------------------------------------------------
# Invariant 4: new_allocation >= minimum_allocation_seconds
# ---------------------------------------------------------------------------


def test_validate_allocations_minimum_floor_passes() -> None:
    """new_allocation equal to minimum_allocation_seconds passes invariant 4."""
    inst = _make_instance("crn:a", 200)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], minimum_allocation_seconds=60)
    chg = AllocationChange(instance_crn="crn:a", current=200, new=60, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert is_valid, errors


def test_validate_allocations_minimum_floor_violation() -> None:
    """new allocation below minimum_allocation_seconds is rejected."""
    inst = _make_instance("crn:a", 200)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], minimum_allocation_seconds=60)
    chg = AllocationChange(instance_crn="crn:a", current=200, new=30, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert not is_valid
    assert any("minimum" in e for e in errors)


# ---------------------------------------------------------------------------
# Invariant 5: new_allocation <= effective limit
# ---------------------------------------------------------------------------


def test_validate_allocations_limit_ceiling_passes() -> None:
    """new_allocation at inst.limit_seconds passes invariant 5."""
    inst = _make_instance("crn:a", 200, limit=500)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(instance_crn="crn:a", current=200, new=500, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert is_valid, errors


def test_validate_allocations_limit_ceiling_violation() -> None:
    """new allocation above inst.limit_seconds is rejected."""
    inst = _make_instance("crn:a", 200, limit=500)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(instance_crn="crn:a", current=200, new=600, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert not is_valid
    assert any("effective limit" in e for e in errors)


def test_validate_allocations_new_limit_takes_precedence() -> None:
    """A LimitChange overrides inst.limit_seconds for the ceiling check."""
    inst = _make_instance("crn:a", 200, limit=500)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    # limit_chg tightens the ceiling to 400; alloc_chg of 450 exceeds it
    alloc_chg = AllocationChange(instance_crn="crn:a", current=200, new=450, reason="t")
    limit_chg = LimitChange(instance_crn="crn:a", current=500, new=400, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((alloc_chg,), (limit_chg,)))
    assert not is_valid
    assert any("effective limit" in e for e in errors)


def test_validate_allocations_consumed_floor_beats_limit_ceiling() -> None:
    """When 28d consumed exceeds the limit, holding new_allocation at consumed is valid.

    The limit breach is unavoidable (invariant 3 forces new_allocation >= consumed),
    so invariant 5 must yield to avoid a non-actionable error.
    """
    inst = _make_instance("crn:a", 600, consumed=600, limit=500)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(instance_crn="crn:a", current=600, new=600, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert is_valid, errors


def test_validate_allocations_gratuitous_breach_above_floor_still_fires() -> None:
    """new allocation above max(consumed, minimum) AND above limit is gratuitous and still errors."""
    inst = _make_instance("crn:a", 600, consumed=600, limit=500)
    optimizer = AllocationOptimizer(_make_account(2000, inst), [_make_config("crn:a")])
    # consumed=600 forces a floor of 600; 700 exceeds both that floor and the limit.
    chg = AllocationChange(instance_crn="crn:a", current=600, new=700, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert not is_valid
    assert any("effective limit" in e for e in errors)


# ---------------------------------------------------------------------------
# Invariant 6: no archiving
# ---------------------------------------------------------------------------


def test_validate_allocations_no_archive_violation() -> None:
    """new_allocation == 0 is rejected regardless of minimum_allocation_seconds."""
    inst = _make_instance("crn:a", 200)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], minimum_allocation_seconds=0)
    chg = AllocationChange(instance_crn="crn:a", current=200, new=0, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult((chg,), ()))
    assert not is_valid
    assert any("archiving" in e for e in errors)


# ---------------------------------------------------------------------------
# Inactive handling (activity_score == 0)
# ---------------------------------------------------------------------------


def test_inactive_with_excess_allocation_drops_to_minimum_floor() -> None:
    """Score=0 with allocation > floor: pinned at minimum_allocation_seconds (no 28d usage)."""
    inst = _inactive_instance("crn:a", allocation=10_000, consumed=0)
    optimizer = AllocationOptimizer(_make_account(20_000, inst), [_make_config("crn:a")])

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 60  # default minimum_allocation_seconds


def test_inactive_below_minimum_floor_is_bumped_up() -> None:
    """Score=0 with allocation < minimum: bumped up to minimum_allocation_seconds."""
    inst = _inactive_instance("crn:a", allocation=10, consumed=0)
    optimizer = AllocationOptimizer(
        _make_account(20_000, inst), [_make_config("crn:a")], minimum_allocation_seconds=100
    )

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 100


def test_in_debt_above_live_limit_pins_at_consumed() -> None:
    """consumed > limit: floor (consumed) wins, breach is unavoidable, validator accepts it.

    With consumed_seconds=600 the activity_score is positive (28d bucket > 0), so this
    flows through the active path. Effective limit (500) is below floor (600), so
    water-fill awards 0 and the instance stays at floor.
    """
    inst = _make_instance("crn:a", allocation=600, consumed=600, limit=500, detailed_usage=_usage())
    optimizer = AllocationOptimizer(_make_account(2_000, inst), [_make_config("crn:a")])

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 600
    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors


# ---------------------------------------------------------------------------
# Active redistribution — proportional shares
# ---------------------------------------------------------------------------


def test_single_active_instance_takes_full_pool() -> None:
    """One active instance, no limit, takes all available headroom above its floor."""
    inst = _active_instance("crn:a", allocation=100, consumed=50, consumed_24h=10)
    # account budget=1000, allocated=100 → unallocated=900. Floor=max(60,50)=60.
    # pool = 900 (unallocated) + (100 - 60) = 940. Final alloc = 60 + 940 = 1000.
    optimizer = AllocationOptimizer(_make_account(1_000, inst), [_make_config("crn:a")])

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 1_000
    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors


def test_two_active_instances_split_proportional_to_score() -> None:
    """Score ratio 3:1 → 75/25 of the pool (above their floors)."""
    # Identical floors so the split is purely score-driven.
    a = _active_instance("crn:a", allocation=100, consumed_24h=30)
    b = _active_instance("crn:a:b", allocation=100, consumed_24h=10)
    a = dataclasses.replace(a, crn="crn:a")
    b = dataclasses.replace(b, crn="crn:b")
    # account=1300; allocated=200 → unallocated=1100. Floors=60 each. pool=1100+40+40=1180.
    # a:b ratio 3:1 → a gets 885 above floor, b gets 295. Final: a=945, b=355.
    optimizer = AllocationOptimizer(
        _make_account(1_300, a, b),
        [_make_config("crn:a"), _make_config("crn:b")],
    )

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    pool_total = (projected["crn:a"] - 60) + (projected["crn:b"] - 60)
    a_share = projected["crn:a"] - 60
    # 75% of pool (within rounding tolerance)
    assert abs(a_share / pool_total - 0.75) < 0.01
    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors


def test_three_active_uncapped_is_proportional() -> None:
    a = dataclasses.replace(_active_instance("x", 100, consumed_24h=4), crn="crn:a")
    b = dataclasses.replace(_active_instance("x", 100, consumed_24h=2), crn="crn:b")
    c = dataclasses.replace(_active_instance("x", 100, consumed_24h=1), crn="crn:c")
    # account=10_000; allocated=300 → unallocated=9700. floors=60. pool=9700 + 40*3 = 9820.
    optimizer = AllocationOptimizer(
        _make_account(10_000, a, b, c),
        [_make_config("crn:a"), _make_config("crn:b"), _make_config("crn:c")],
    )

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)
    above = {k: v - 60 for k, v in projected.items()}
    total = sum(above.values())

    # Score weights: 4:2:1 → ~57/29/14 of the pool. Allow rounding slack.
    assert abs(above["crn:a"] / total - 4 / 7) < 0.005
    assert abs(above["crn:b"] / total - 2 / 7) < 0.005
    assert abs(above["crn:c"] / total - 1 / 7) < 0.005
    # Rounding leaves at most N-1 seconds unspent across N instances.
    assert total <= 9_820 and total >= 9_820 - 3


# ---------------------------------------------------------------------------
# Active redistribution — water-fill (surplus from capped flows to uncapped)
# ---------------------------------------------------------------------------


def test_water_fill_redistributes_surplus_to_uncapped() -> None:
    """When one active instance hits its limit, surplus goes to the uncapped peer."""
    # a: high score (would get majority share) but capped at 200
    # b: low score, no cap
    a = dataclasses.replace(_active_instance("x", 100, consumed_24h=10), crn="crn:a")
    b = dataclasses.replace(_active_instance("x", 100, consumed_24h=1), crn="crn:b")
    cfg_a = _make_config("crn:a", target_limit_seconds=200)
    cfg_b = _make_config("crn:b")

    # account=1000, allocated=200 → unallocated=800. floors=60. pool = 800 + 40 + 40 = 880.
    # Naive: a gets (10/11)*880=800, b gets (1/11)*880=80. But a's room=200-60=140, so a takes
    # 140 and is capped. Round 2: only b remains; gets remaining 880-(140+80)=660. Final: b=140+660=800.
    optimizer = AllocationOptimizer(_make_account(1_000, a, b), [cfg_a, cfg_b])

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 200
    # b absorbs all of a's surplus; a + b should saturate the budget.
    assert projected["crn:b"] == 800
    assert projected["crn:a"] + projected["crn:b"] == 1_000
    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors


def test_all_capped_leaves_leftover_unallocated() -> None:
    """If pool exceeds total room, surplus stays unallocated rather than over-cap any instance."""
    a = dataclasses.replace(_active_instance("x", 100, consumed_24h=5), crn="crn:a")
    b = dataclasses.replace(_active_instance("x", 100, consumed_24h=5), crn="crn:b")
    cfg_a = _make_config("crn:a", target_limit_seconds=150)
    cfg_b = _make_config("crn:b", target_limit_seconds=150)

    # pool huge, but each instance can only grow to 150 (90 above floor).
    optimizer = AllocationOptimizer(_make_account(10_000, a, b), [cfg_a, cfg_b])

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 150
    assert projected["crn:b"] == 150
    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors


def test_cap_uses_resolved_limit_not_stale_iqp_limit() -> None:
    """A config-driven limit bump (active grant) raises the ceiling on the same run."""
    # Live IQP limit is 200; config raises it via an active grant to 200 + 300 = 500.
    inst = InstanceState(
        crn="crn:a",
        name="a",
        allocation_seconds=100,
        limit_seconds=200,  # stale IQP limit
        consumed_seconds=0,
        detailed_usage=_usage(consumed_24h=10),
    )
    grant = NetGrant(
        start_date=datetime(2026, 4, 15),
        net_grant_seconds=300,
        end_date=datetime(2026, 5, 13),
    )
    cfg = InstanceConfig(
        name="a",
        crn="crn:a",
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
        target_limit_seconds=200,
        net_grants=(grant,),
    )

    optimizer = AllocationOptimizer(_make_account(10_000, inst), [cfg], today=date(2026, 4, 15))
    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    # If the optimizer used the stale IQP limit (200), allocation would be capped at 200.
    # With the resolved limit (500), allocation grows to 500.
    assert projected["crn:a"] == 500
    # The LimitChange should also reflect the resolved limit.
    limit_chg = next(c for c in result.limit_changes if c.instance_crn == "crn:a")
    assert limit_chg.new == 500


# ---------------------------------------------------------------------------
# Limit interaction
# ---------------------------------------------------------------------------


def test_target_limit_emitted_as_limit_change_and_used_as_ceiling() -> None:
    inst = InstanceState(
        crn="crn:a",
        name="a",
        allocation_seconds=100,
        limit_seconds=None,
        consumed_seconds=0,
        detailed_usage=_usage(consumed_24h=10),
    )
    cfg = _make_config("crn:a", target_limit_seconds=300)
    optimizer = AllocationOptimizer(_make_account(10_000, inst), [cfg])

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 300
    assert any(c.new == 300 for c in result.limit_changes)


def test_no_config_limit_means_no_ceiling_and_no_limit_change() -> None:
    inst = _active_instance("crn:a", allocation=100, consumed_24h=10)
    cfg = _make_config("crn:a")  # no target_limit_seconds, no grants

    optimizer = AllocationOptimizer(_make_account(5_000, inst), [cfg])
    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 5_000  # the entire pool flows to it
    assert result.limit_changes == ()


# ---------------------------------------------------------------------------
# Floor edge cases
# ---------------------------------------------------------------------------


def test_in_debt_instance_pinned_at_consumed_above_limit() -> None:
    """consumed > effective limit: floor wins, allocation pinned at consumed."""
    inst = _active_instance("crn:a", allocation=600, consumed=600, consumed_24h=10)
    cfg = _make_config("crn:a", target_limit_seconds=400)
    optimizer = AllocationOptimizer(_make_account(2_000, inst), [cfg])

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    # floor=600 already at/above limit=400; water-fill awards 0 because room=400-600<0.
    assert projected["crn:a"] == 600
    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors


def test_zero_consumed_active_grows_to_share() -> None:
    inst = _active_instance("crn:a", allocation=100, consumed=0, consumed_24h=5)
    cfg = _make_config("crn:a")
    optimizer = AllocationOptimizer(_make_account(10_000, inst), [cfg])

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 10_000


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------


def test_reserve_reduces_allocation_and_validator_agrees() -> None:
    """Reserve pulls headroom out of the pool; result still passes validation."""
    inst = _active_instance("crn:a", allocation=200, consumed=100, consumed_24h=10)
    cfg = _make_config("crn:a")
    account = _make_account(5_000_000, inst)

    with_reserve = AllocationOptimizer(account, [cfg], allocation_reserve_percent=20.0).optimize()
    without_reserve = AllocationOptimizer(account, [cfg], allocation_reserve_percent=0.0).optimize()

    a_with = next(c.new for c in with_reserve.allocation_changes if c.instance_crn == "crn:a")
    a_without = next(c.new for c in without_reserve.allocation_changes if c.instance_crn == "crn:a")
    assert a_with < a_without

    optimizer = AllocationOptimizer(account, [cfg], allocation_reserve_percent=20.0)
    is_valid, errors = optimizer.validate_allocations(with_reserve)
    assert is_valid, errors


# ---------------------------------------------------------------------------
# Unmanaged instances
# ---------------------------------------------------------------------------


def test_unmanaged_instances_are_ignored_by_optimizer() -> None:
    """Instances without a config in instance_configs are not modified."""
    managed = _active_instance("crn:m", allocation=100, consumed_24h=10)
    unmanaged = _active_instance("crn:u", allocation=300, consumed_24h=10)
    optimizer = AllocationOptimizer(
        _make_account(2_000, managed, unmanaged),
        [_make_config("crn:m")],  # only the managed one has a config
    )

    result = optimizer.optimize()
    crns_changed = {c.instance_crn for c in result.allocation_changes}

    assert "crn:u" not in crns_changed
    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors


# ---------------------------------------------------------------------------
# End-to-end regression: realistic mix passes validation
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_account() -> tuple[Account, list[InstanceConfig]]:
    """Active+inactive+capped+uncapped, with realistic sizes."""
    active_high = InstanceState(
        crn="crn:active_high",
        name="High",
        allocation_seconds=600_000,
        consumed_seconds=550_000,
        limit_seconds=800_000,
        detailed_usage=_usage(
            consumed_24h=50_000, consumed_3day=150_000, consumed_7day=300_000, consumed_14day=400_000
        ),
    )
    inactive = InstanceState(
        crn="crn:inactive",
        name="Inactive",
        allocation_seconds=400_000,
        consumed_seconds=0,
        limit_seconds=500_000,
        detailed_usage=_usage(),
    )
    active_med_capped = InstanceState(
        crn="crn:active_med_capped",
        name="MedCapped",
        allocation_seconds=300_000,
        consumed_seconds=150_000,
        limit_seconds=400_000,
        detailed_usage=_usage(consumed_3day=20_000, consumed_7day=50_000, consumed_14day=100_000),
    )
    account = Account(
        account_id="test",
        plan_id="test-plan",
        allocation_budget_seconds=2_000_000,
        unallocated_seconds=700_000,
        limit_seconds=None,
        instances=(active_high, inactive, active_med_capped),
    )
    configs = [
        _make_config("crn:active_high", target_limit_seconds=900_000),
        _make_config("crn:inactive"),
        _make_config("crn:active_med_capped", target_limit_seconds=400_000),
    ]
    return account, configs


def test_end_to_end_mixed_passes_validation(mixed_account: tuple[Account, list[InstanceConfig]]) -> None:
    account, configs = mixed_account
    optimizer = AllocationOptimizer(account, configs)
    result = optimizer.optimize()

    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors

    projected = _projected(result, account)
    # Inactive (score=0) drops to floor = max(60, 0) = 60.
    assert projected["crn:inactive"] == 60
    # Capped instance can't exceed its limit.
    assert projected["crn:active_med_capped"] <= 400_000
    # High-activity instance soaks up most of the headroom.
    assert projected["crn:active_high"] > account.instances[0].allocation_seconds
