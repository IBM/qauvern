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
from datetime import date, datetime, timezone

import pytest

from qauvern.models import (
    Account,
    AllocationChange,
    InstanceConfig,
    InstanceDetailedUsage,
    InstanceState,
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
        target_limit_seconds=target_limit_seconds,
    )


def _projected(result: OptimizationResult, account: Account) -> dict[str, int]:
    """Return projected allocation per crn, applying any AllocationChange."""
    return {
        inst.crn: result.allocation_changes[inst.crn].new
        if inst.crn in result.allocation_changes
        else inst.allocation_seconds
        for inst in account.instances
    }


# ---------------------------------------------------------------------------
# validate_allocations
# ---------------------------------------------------------------------------


def test_validate_allocations_valid() -> None:
    """An already-valid state with no recommendations passes all invariants."""
    # allocation=100 satisfies invariant 4 (>= default minimum of 60)
    account = _make_account(1000, _make_instance("crn:test:1", 100))
    cfg = _make_config("crn:test:1")

    is_valid, errors = AllocationOptimizer(account, [cfg]).validate_allocations(OptimizationResult({}, {}))

    assert is_valid
    assert errors == []


def test_validate_allocations_catches_preexisting_violation() -> None:
    """Current-state violations are flagged even when the result has no recommendations."""
    # allocation < consumed: no recommendation produced, validator still fires invariant 3.
    inst = _make_instance("crn:test:1", 50, consumed=100)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:test:1")])

    is_valid, errors = optimizer.validate_allocations(OptimizationResult({}, {}))

    assert not is_valid
    assert any("28-day usage" in e for e in errors)


def test_validate_allocations_uses_result_overrides() -> None:
    """A recommendation that pushes a clean state past the cap is flagged."""
    account = _make_account(5, _make_instance("crn:test:1", 4))
    optimizer = AllocationOptimizer(account, [])

    # Current state is valid (4 <= 5).
    is_valid, _ = optimizer.validate_allocations(OptimizationResult({}, {}))
    assert is_valid

    # Bumping to 6 projects the total over the cap.
    over_cap = OptimizationResult(
        allocation_changes={"crn:test:1": AllocationChange(current=4, new=6, reason="test")},
        limit_changes={},
    )
    is_valid, errors = optimizer.validate_allocations(over_cap)
    assert not is_valid
    assert errors == ["Total instance allocations (6s) exceed the 5s account budget by 1s."]


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
        allocation_changes={"crn:test:1": AllocationChange(current=4, new=5, reason="test")},
        limit_changes={},
    )
    is_valid, errors = optimizer.validate_allocations(fits)
    assert is_valid, errors

    # Bumping to 6 overflows: 6 + 5 unmanaged > 10.
    over = OptimizationResult(
        allocation_changes={"crn:test:1": AllocationChange(current=4, new=6, reason="test")},
        limit_changes={},
    )
    is_valid, errors = optimizer.validate_allocations(over)
    assert not is_valid
    assert errors == [
        "Total instance allocations (11s) exceed the 10s account budget by 1s. Driven by: unmanaged instances hold 5s."
    ]


# ---------------------------------------------------------------------------
# Invariant 1: total allocation cap (account budget − reserve)
#
# A single check: total projected allocation must fit under the effective budget
# (account budget minus any reserve). The reserve is 0 when unset, so these cases
# exercise the plain-budget cap; the reserve-buffer cases follow below.
#
# Note that we expect the optimizer _can_ produce plans that violate this
# invariant.
# ---------------------------------------------------------------------------


def test_optimize_in_debt_account_overruns_budget_with_consumed_floor_diagnostic() -> None:
    """Floors driven by consumed_seconds: diagnostic blames 28-day usage and tells
    the operator to raise the budget (config can't lower the floor)."""
    # budget=100, unallocated=10. consumed (80, 70) ≥ min_alloc=60 so floors come
    # from consumed_seconds and sum to 150 > 100. raw_pool is negative → pool=0.
    a = _inactive_instance("crn:a", allocation=50, consumed=80)
    b = _inactive_instance("crn:b", allocation=40, consumed=70)
    optimizer = AllocationOptimizer(_make_account(100, a, b), [_make_config("crn:a"), _make_config("crn:b")])

    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)
    assert projected == {"crn:a": 80, "crn:b": 70}

    is_valid, errors = optimizer.validate_allocations(result)
    assert not is_valid
    msg = next(e for e in errors if "account budget" in e)
    assert "exceed the 100s account budget by 50s" in msg
    assert "Driven by: 28-day usage requires 150s" in msg
    assert "minimum_allocation_seconds" not in msg
    # Consumed usage is the sole driver and no knob is in play: no fix is offered
    # (we never advise contacting support or re-running optimize).
    assert "support" not in msg
    assert "To fix:" not in msg


def test_optimize_min_alloc_squeeze_overruns_budget_with_config_fix_diagnostic() -> None:
    """Floors driven by minimum_allocation_seconds: diagnostic suggests lowering it."""
    # budget=100, unallocated=0, three instances each holding ~33 with consumed=0.
    # min_alloc=50 forces each floor to 50; sum=150 > 100. consumed_bucket=0.
    a = _inactive_instance("crn:a", allocation=34)
    b = _inactive_instance("crn:b", allocation=33)
    c = _inactive_instance("crn:c", allocation=33)
    optimizer = AllocationOptimizer(
        _make_account(100, a, b, c),
        [_make_config("crn:a"), _make_config("crn:b"), _make_config("crn:c")],
        minimum_allocation_seconds=50,
    )

    result = optimizer.optimize()
    is_valid, errors = optimizer.validate_allocations(result)
    assert not is_valid
    msg = next(e for e in errors if "account budget" in e)
    assert "exceed the 100s account budget by 50s" in msg
    assert "Driven by: minimum_allocation_seconds requires 150s" in msg
    assert "28-day usage" not in msg
    assert "To fix: lower minimum_allocation_seconds." in msg
    assert "IBM Quantum support" not in msg


def test_optimize_floor_tie_attributes_to_consumed_seconds() -> None:
    """When consumed == minimum_allocation_seconds, the floor source ties to consumed_seconds.

    Diagnostic should blame 28-day usage (the unfixable source) rather than the config knob.
    """
    # budget=100, two instances each with consumed=60 == min_alloc=60. floors sum to 120 > 100.
    a = _inactive_instance("crn:a", allocation=50, consumed=60)
    b = _inactive_instance("crn:b", allocation=50, consumed=60)
    optimizer = AllocationOptimizer(_make_account(100, a, b), [_make_config("crn:a"), _make_config("crn:b")])

    result = optimizer.optimize()
    is_valid, errors = optimizer.validate_allocations(result)
    assert not is_valid
    msg = next(e for e in errors if "account budget" in e)
    assert "exceed the 100s account budget by 20s" in msg
    assert "Driven by: 28-day usage requires 120s" in msg
    assert "minimum_allocation_seconds" not in msg
    assert "To fix:" not in msg


def test_optimize_unmanaged_drag_overruns_budget_with_diagnostic() -> None:
    """Unmanaged allocation plus a min-alloc floor pushes the projection over budget."""
    # budget=100, unallocated=5, loaded A holds 50 → unmanaged=45.
    # consumed=10 < min_alloc=60 → floor=60 from minimum_allocation_seconds.
    # floor_required = 60 + 45 = 105 > 100.
    inst = _inactive_instance("crn:a", allocation=50, consumed=10)
    optimizer = AllocationOptimizer(
        Account(
            account_id="test",
            plan_id="test-plan",
            allocation_budget_seconds=100,
            unallocated_seconds=5,
            limit_seconds=None,
            instances=(inst,),
        ),
        [_make_config("crn:a")],
    )

    result = optimizer.optimize()
    is_valid, errors = optimizer.validate_allocations(result)
    assert not is_valid
    msg = next(e for e in errors if "account budget" in e)
    assert "exceed the 100s account budget by 5s" in msg
    assert "minimum_allocation_seconds requires 60s" in msg
    assert "unmanaged instances hold 45s" in msg
    assert "To fix: lower minimum_allocation_seconds." in msg


# ---------------------------------------------------------------------------
# Invariant 1 (reserve buffer): the effective-budget cap with a reserve in play
# ---------------------------------------------------------------------------


def test_validate_allocations_reserve_passes() -> None:
    """Projected total within the budget-based reserve cap passes."""
    # budget = 1000, reserve = 20% → reserve_amount = int(1000 * 0.20) = 200
    # effective_budget = 1000 - 200 = 800; projected total = 800 ≤ 800 → valid
    inst = _make_instance("crn:a", 200, consumed=100)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], allocation_reserve_percent=20.0)
    chg = AllocationChange(current=200, new=800, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert is_valid, errors


def test_validate_allocations_reserve_violation() -> None:
    """Projected total that crosses the budget-based reserve cap is rejected."""
    # same fixture; new=801 > effective_budget=800, but 801 ≤ 1000 so only reserve fires
    inst = _make_instance("crn:a", 200, consumed=100)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], allocation_reserve_percent=20.0)
    chg = AllocationChange(current=200, new=801, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert not is_valid
    msg = next(e for e in errors if "cap" in e)
    # Overshoot by 1s: the message quantifies the miss, names the reserve in the cap
    # breakdown, and offers the one knob in play (the reserve). It never advises
    # re-running optimize — this message only ever follows an optimize run.
    assert "exceed the 800s cap (1000s account budget − 200s reserve at 20.0%) by 1s" in msg
    assert "run `qauvern optimize`" not in msg
    assert "To fix: lower allocation_reserve_percent." in msg


def test_validate_allocations_reserve_fails_when_floors_exceed_cap() -> None:
    """When unavoidable floors exceed the budget-based reserve cap, invariant 2 fails.

    The reserve is a fixed fraction of the account budget, so it does not go silent
    just because the movable pool is empty. budget=100, reserve=50% → cap=50, but the
    instance is parked at its consumed floor of 100 > 50, so the reserve cannot be
    honored and invariant 2 fires.
    """
    # Only managed instance is parked at its consumed floor; unallocated=0 (account is full).
    inst = _make_instance("crn:a", 100, consumed=100)
    account = Account(
        account_id="test",
        plan_id="test-plan",
        allocation_budget_seconds=100,
        unallocated_seconds=0,
        limit_seconds=None,
        instances=(inst,),
    )
    optimizer = AllocationOptimizer(account, [_make_config("crn:a")], allocation_reserve_percent=50.0)
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({}, {}))
    assert not is_valid
    msg = next(e for e in errors if "cap" in e)
    # The reserve is unachievable, not just overshot: floors alone overflow the cap.
    # The message says it's unavoidable, names the reserve in the cap, and the driver.
    assert "exceed the 50s cap (100s account budget − 50s reserve at 50.0%) by 50s" in msg
    assert "Driven by: 28-day usage requires 100s" in msg
    assert "To fix: lower allocation_reserve_percent." in msg


def test_reserve_too_high_names_minimum_allocation_driver() -> None:
    """When the config minimum (not 28-day usage) is what overflows the cap, the
    message names minimum_allocation_seconds and offers lowering it as a fix."""
    # No usage, so the floor is driven entirely by minimum_allocation_seconds=80.
    inst = _make_instance("crn:a", 80, consumed=0)
    account = Account(
        account_id="test",
        plan_id="test-plan",
        allocation_budget_seconds=100,
        unallocated_seconds=20,
        limit_seconds=None,
        instances=(inst,),
    )
    optimizer = AllocationOptimizer(
        account, [_make_config("crn:a")], minimum_allocation_seconds=80, allocation_reserve_percent=50.0
    )
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({}, {}))
    assert not is_valid
    msg = next(e for e in errors if "cap" in e)
    assert "Driven by: minimum_allocation_seconds requires 80s" in msg
    # Both discretionary levers are offered, reserve first.
    assert "To fix: lower allocation_reserve_percent and/or lower minimum_allocation_seconds." in msg


# ---------------------------------------------------------------------------
# Invariant 2: new_allocation >= 28-day consumed usage
# ---------------------------------------------------------------------------


def test_validate_allocations_usage_floor_passes() -> None:
    """new_allocation equal to consumed_seconds passes invariant 3."""
    inst = _make_instance("crn:a", 200, consumed=100)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(current=200, new=100, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert is_valid, errors


def test_validate_allocations_usage_floor_violation() -> None:
    """new allocation below consumed_seconds is rejected."""
    inst = _make_instance("crn:a", 200, consumed=150)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(current=200, new=100, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert not is_valid
    assert any("28-day usage" in e for e in errors)


def test_validate_allocations_usage_floor_skipped_when_disabled() -> None:
    """With enforce_usage_floor=False, new_allocation below consumed_seconds is not an error."""
    inst = _make_instance("crn:a", 200, consumed=150)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], enforce_usage_floor=False)
    chg = AllocationChange(current=200, new=100, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert is_valid, errors


def test_floor_ignores_consumed_seconds_when_disabled() -> None:
    """With enforce_usage_floor=False, _floor() never returns the consumed_seconds source."""
    inst = _make_instance("crn:a", 200, consumed=150)
    optimizer = AllocationOptimizer(
        _make_account(1000, inst), [_make_config("crn:a")], minimum_allocation_seconds=60, enforce_usage_floor=False
    )
    floor = optimizer._floor(inst)
    assert floor.source == "minimum_allocation_seconds"
    assert floor.value == 60


def test_usage_floor_warnings_empty_when_enforced() -> None:
    """usage_floor_warnings is always empty when enforce_usage_floor=True (errors instead)."""
    inst = _make_instance("crn:a", 200, consumed=150)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(current=200, new=100, reason="t")
    assert optimizer.usage_floor_warnings(OptimizationResult({"crn:a": chg}, {})) == []


def test_usage_floor_warnings_reported_when_disabled() -> None:
    """usage_floor_warnings surfaces instances below usage when enforce_usage_floor=False."""
    inst = _make_instance("crn:a", 200, consumed=150)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], enforce_usage_floor=False)
    chg = AllocationChange(current=200, new=100, reason="t")
    warnings = optimizer.usage_floor_warnings(OptimizationResult({"crn:a": chg}, {}))
    assert len(warnings) == 1
    assert "28-day usage" in warnings[0]


def test_usage_floor_warnings_none_when_no_breach() -> None:
    """usage_floor_warnings is empty when disabled but no instance falls below usage."""
    inst = _make_instance("crn:a", 200, consumed=100)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], enforce_usage_floor=False)
    assert optimizer.usage_floor_warnings(OptimizationResult({}, {})) == []


# ---------------------------------------------------------------------------
# Invariant 3: new_allocation >= minimum_allocation_seconds
# ---------------------------------------------------------------------------


def test_validate_allocations_minimum_floor_passes() -> None:
    """new_allocation equal to minimum_allocation_seconds passes invariant 4."""
    inst = _make_instance("crn:a", 200)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], minimum_allocation_seconds=60)
    chg = AllocationChange(current=200, new=60, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert is_valid, errors


def test_validate_allocations_minimum_floor_violation() -> None:
    """new allocation below minimum_allocation_seconds is rejected."""
    inst = _make_instance("crn:a", 200)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], minimum_allocation_seconds=60)
    chg = AllocationChange(current=200, new=30, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert not is_valid
    assert any("minimum" in e for e in errors)


# ---------------------------------------------------------------------------
# Invariant 4: new_allocation <= effective limit
# ---------------------------------------------------------------------------


def test_validate_allocations_limit_ceiling_passes() -> None:
    """new_allocation at inst.limit_seconds passes invariant 5."""
    inst = _make_instance("crn:a", 200, limit=500)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(current=200, new=500, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert is_valid, errors


def test_validate_allocations_limit_ceiling_violation() -> None:
    """new allocation above inst.limit_seconds is rejected."""
    inst = _make_instance("crn:a", 200, limit=500)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(current=200, new=600, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert not is_valid
    assert any("effective limit" in e for e in errors)


def test_validate_allocations_new_limit_takes_precedence() -> None:
    """A LimitChange overrides inst.limit_seconds for the ceiling check."""
    inst = _make_instance("crn:a", 200, limit=500)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    # limit_chg tightens the ceiling to 400; alloc_chg of 450 exceeds it
    alloc_chg = AllocationChange(current=200, new=450, reason="t")
    limit_chg = LimitChange(current=500, new=400)
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": alloc_chg}, {"crn:a": limit_chg}))
    assert not is_valid
    assert any("effective limit" in e for e in errors)


def test_validate_allocations_consumed_floor_beats_limit_ceiling() -> None:
    """When 28d consumed exceeds the limit, holding new_allocation at consumed is valid.

    The limit breach is unavoidable (invariant 3 forces new_allocation >= consumed),
    so invariant 5 must yield to avoid a non-actionable error.
    """
    inst = _make_instance("crn:a", 600, consumed=600, limit=500)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")])
    chg = AllocationChange(current=600, new=600, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert is_valid, errors


def test_validate_allocations_gratuitous_breach_above_floor_still_fires() -> None:
    """new allocation above max(consumed, minimum) AND above limit is gratuitous and still errors."""
    inst = _make_instance("crn:a", 600, consumed=600, limit=500)
    optimizer = AllocationOptimizer(_make_account(2000, inst), [_make_config("crn:a")])
    # consumed=600 forces a floor of 600; 700 exceeds both that floor and the limit.
    chg = AllocationChange(current=600, new=700, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
    assert not is_valid
    assert any("effective limit" in e for e in errors)


# ---------------------------------------------------------------------------
# Invariant 5: no archiving
# ---------------------------------------------------------------------------


def test_validate_allocations_no_archive_violation() -> None:
    """new_allocation == 0 is rejected regardless of minimum_allocation_seconds."""
    inst = _make_instance("crn:a", 200)
    optimizer = AllocationOptimizer(_make_account(1000, inst), [_make_config("crn:a")], minimum_allocation_seconds=0)
    chg = AllocationChange(current=200, new=0, reason="t")
    is_valid, errors = optimizer.validate_allocations(OptimizationResult({"crn:a": chg}, {}))
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
    # Reason should attribute the floor to the config knob, not 28d usage. (An
    # inactive instance always has consumed_seconds=0 — any positive consumption
    # contributes to activity_score — so the inactive branch sources its floor
    # from minimum_allocation_seconds.)
    assert "config minimum" in result.allocation_changes["crn:a"].reason


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
        start_date=datetime(2026, 4, 15, tzinfo=timezone.utc),
        net_grant_seconds=300,
        end_date=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    cfg = InstanceConfig(
        name="a",
        crn="crn:a",
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
    limit_chg = result.limit_changes["crn:a"]
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
    assert any(c.new == 300 for c in result.limit_changes.values())


def test_no_config_limit_means_no_ceiling_and_no_limit_change() -> None:
    inst = _active_instance("crn:a", allocation=100, consumed_24h=10)
    cfg = _make_config("crn:a")  # no target_limit_seconds, no grants

    optimizer = AllocationOptimizer(_make_account(5_000, inst), [cfg])
    result = optimizer.optimize()
    projected = _projected(result, optimizer.account)

    assert projected["crn:a"] == 5_000  # the entire pool flows to it
    assert not result.limit_changes


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


def test_reserve_is_hard_fraction_of_budget() -> None:
    """The headline contract: a 20% reserve on a 100s budget caps total allocation at 80s."""
    inst = _active_instance("crn:a", allocation=10, consumed=0, consumed_24h=10)
    cfg = _make_config("crn:a")
    account = _make_account(100, inst)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60, allocation_reserve_percent=20.0)

    result = optimizer.optimize()
    total = sum(_projected(result, account).values()) + account.unmanaged_allocation_seconds
    assert total <= 80

    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors


def test_reserve_reduces_allocation_and_validator_agrees() -> None:
    """Reserve pulls headroom out of the pool; result still passes validation."""
    inst = _active_instance("crn:a", allocation=200, consumed=100, consumed_24h=10)
    cfg = _make_config("crn:a")
    account = _make_account(5_000_000, inst)

    with_reserve = AllocationOptimizer(account, [cfg], allocation_reserve_percent=20.0).optimize()
    without_reserve = AllocationOptimizer(account, [cfg], allocation_reserve_percent=0.0).optimize()

    a_with = with_reserve.allocation_changes["crn:a"].new
    a_without = without_reserve.allocation_changes["crn:a"].new
    assert a_with < a_without

    optimizer = AllocationOptimizer(account, [cfg], allocation_reserve_percent=20.0)
    is_valid, errors = optimizer.validate_allocations(with_reserve)
    assert is_valid, errors


def test_reserve_with_capped_active_keeps_leftover_reserved() -> None:
    """When water-fill caps an active instance, the leftover stays reserved (not redistributed).

    The budget-based reserve is withheld from the pool before water-fill runs, so a
    capped instance's surplus never re-enters the budget — total projected
    allocation must respect the reserve cap.
    """
    # budget=1_000_000, single active instance with limit=300, consumed=100, alloc=200.
    # raw_pool = 999_800 + (200 - 100) = 999_900. reserve_amount = int(1_000_000 * 0.30)
    # = 300_000 → distributable = 699_900.
    # Water-fill caps the instance at limit=300 (room=200 from floor=100).
    # Total = 300 + 0 unmanaged = 300, well below budget − reserve (700_000). Validates.
    inst = _active_instance("crn:a", allocation=200, consumed=100, limit=300, consumed_24h=10)
    cfg = _make_config("crn:a")
    account = _make_account(1_000_000, inst)
    optimizer = AllocationOptimizer(account, [cfg], allocation_reserve_percent=30.0)

    result = optimizer.optimize()
    projected = _projected(result, account)
    assert projected["crn:a"] == 300

    is_valid, errors = optimizer.validate_allocations(result)
    assert is_valid, errors


def test_reserve_preserves_headroom_for_unmanaged_instances() -> None:
    """With a reserve set, the optimizer leaves room on the account for unconfigured instances.

    Documented use case in README: when most instances are unmanaged, the
    configured ones can claim every spare second. A reserve carves out
    headroom that stays unallocated.
    """
    # budget=10_000, one managed (alloc=100, very active) and one unmanaged (alloc=100).
    # unallocated = 9800. raw_pool = 9800 + (100 - floor 60) = 9840.
    # Without reserve, managed claws all of it → projected = 60 + 9840 = 9900.
    # With 50% reserve, reserve_amount = int(10_000 * 0.5) = 5000 →
    # distributable = 9840 - 5000 = 4840 → projected = 60 + 4840 = 4900.
    managed = _active_instance("crn:m", allocation=100, consumed_24h=10)
    unmanaged = _active_instance("crn:u", allocation=100, consumed_24h=5)
    account = _make_account(10_000, managed, unmanaged)

    no_reserve = AllocationOptimizer(account, [_make_config("crn:m")]).optimize()
    with_reserve = AllocationOptimizer(account, [_make_config("crn:m")], allocation_reserve_percent=50.0).optimize()

    no_reserve_proj = _projected(no_reserve, account)["crn:m"]
    with_reserve_proj = _projected(with_reserve, account)["crn:m"]

    # Unmanaged is untouched in both runs.
    assert "crn:u" not in no_reserve.allocation_changes
    assert "crn:u" not in with_reserve.allocation_changes
    # Reserve leaves additional account-level headroom (managed claims less).
    assert with_reserve_proj < no_reserve_proj
    # Sanity: 50% reserve leaves at least ~half the redistributable pool unallocated.
    total_with_reserve = with_reserve_proj + unmanaged.allocation_seconds
    assert total_with_reserve <= 5_100


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
    crns_changed = set(result.allocation_changes)

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
