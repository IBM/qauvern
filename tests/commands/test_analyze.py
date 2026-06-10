# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for the `qauvern analyze` command and its pure helper."""

from datetime import datetime, timezone

from qauvern.commands.analyze import format_analyze_output
from qauvern.models import (
    Account,
    AllocationChange,
    InstanceConfig,
    InstanceDetailedUsage,
    InstanceState,
    LimitChange,
    OptimizationResult,
)
from qauvern.optimizer import AllocationOptimizer
from qauvern.plan import Plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CRN_A = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:inst-a::"


def _make_instance(
    crn: str,
    allocation: int,
    *,
    name: str = "Instance",
    consumed: int = 0,
    limit: int | None = None,
    consumed_24h: int = 0,
) -> InstanceState:
    return InstanceState(
        crn=crn,
        name=name,
        allocation_seconds=allocation,
        limit_seconds=limit,
        consumed_seconds=consumed,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=0,
            consumed_14day=0,
            consumed_7day=0,
            consumed_3day=0,
            consumed_24h=consumed_24h,
            daily_usage={},
        ),
    )


def _make_account(
    instances: tuple[InstanceState, ...],
    budget: int,
    *,
    unallocated: int = 0,
    limit: int | None = None,
) -> Account:
    return Account(
        account_id="test-account",
        plan_id="test-plan",
        allocation_budget_seconds=budget,
        unallocated_seconds=unallocated,
        limit_seconds=limit,
        instances=instances,
    )


def _make_config(crn: str, *, name: str = "Instance") -> InstanceConfig:
    return InstanceConfig(
        name=name,
        crn=crn,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )


def _no_changes_setup():
    """One instance already at its floor — optimizer produces no changes."""
    inst = _make_instance(CRN_A, allocation=60)
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()
    return account, result, [cfg], optimizer


# ---------------------------------------------------------------------------
# format_analyze_output — no changes
# ---------------------------------------------------------------------------


def test_no_changes_footer() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_output(account, result, Plan.PAYGO, cfgs, optimizer)
    assert "No optimization recommendations" in output
    assert "To apply" not in output


def test_no_validation_errors_block_by_default() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_output(account, result, Plan.PAYGO, cfgs, optimizer)
    assert "VALIDATION ERRORS" not in output


# ---------------------------------------------------------------------------
# format_analyze_output — has changes
# ---------------------------------------------------------------------------


def test_footer_shows_change_counts() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=80, reason="Inactive")},
        limit_changes={},
    )

    output = format_analyze_output(account, result, Plan.PAYGO, [cfg], optimizer)
    assert "Total changes: 1 (1 allocation, 0 limit)" in output
    assert "To apply these recommendations, run: qauvern optimize" in output


def test_footer_counts_both_allocation_and_limit_changes() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=80, reason="Inactive")},
        limit_changes={CRN_A: LimitChange(current=None, new=3600)},
    )

    output = format_analyze_output(account, result, Plan.PAYGO, [cfg], optimizer)
    assert "Total changes: 2 (1 allocation, 1 limit)" in output


# ---------------------------------------------------------------------------
# format_analyze_output — validation errors
# ---------------------------------------------------------------------------


def test_validation_errors_block_appears_when_invalid() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=200, reason="Active")},
        limit_changes={},
    )

    output = format_analyze_output(account, result, Plan.PAYGO, [cfg], optimizer)
    assert "VALIDATION ERRORS" in output


# ---------------------------------------------------------------------------
# format_analyze_output — conditional lines
# ---------------------------------------------------------------------------


def test_reserve_summary_appears_when_reserve_percent_set() -> None:
    inst = _make_instance(CRN_A, allocation=60)
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60, allocation_reserve_percent=10.0)
    result = optimizer.optimize()

    output = format_analyze_output(account, result, Plan.PAYGO, [cfg], optimizer)
    assert "Reserve: 10.0%" in output


def test_no_reserve_line_when_zero_percent() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_output(account, result, Plan.PAYGO, cfgs, optimizer)
    assert "Reserve:" not in output


def test_unmanaged_allocation_line_appears_when_nonzero() -> None:
    inst = _make_instance(CRN_A, allocation=60)
    # budget=100, unallocated=0, configured=60 → unmanaged=40
    account = _make_account((inst,), budget=100, unallocated=0)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    output = format_analyze_output(account, result, Plan.PAYGO, [cfg], optimizer)
    assert "Held by unconfigured instances" in output


def test_no_unmanaged_line_when_zero() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_output(account, result, Plan.PAYGO, cfgs, optimizer)
    assert "Held by unconfigured instances" not in output


def test_limit_shown_as_unlimited_when_none() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_output(account, result, Plan.PAYGO, cfgs, optimizer)
    assert "Limit: Unlimited" in output


def test_limit_shown_when_set() -> None:
    inst = _make_instance(CRN_A, allocation=60)
    account = _make_account((inst,), budget=60, limit=3600)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    output = format_analyze_output(account, result, Plan.PAYGO, [cfg], optimizer)
    assert "Limit: 1.0h" in output


def test_plan_name_and_instance_count_appear() -> None:
    inst = _make_instance(CRN_A, allocation=60, name="My Instance")
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A, name="My Instance")
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    output = format_analyze_output(account, result, Plan.PAYGO, [cfg], optimizer)
    assert "Plan: paygo" in output
    assert "Configured instances analyzed: 1" in output
    assert "My Instance" in output
