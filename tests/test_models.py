# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for data models."""

from typing import Any

import pytest

from qauvern.models import Account, InstanceDetailedUsage, InstanceState

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _instance(**kwargs: Any) -> InstanceState:
    return InstanceState(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=100000,
        limit_seconds=kwargs.get("limit_seconds"),
        consumed_seconds=kwargs.get("consumed_seconds", 0),
        detailed_usage=kwargs.get("detailed_usage", None),
    )


def _usage(**kwargs: Any) -> InstanceDetailedUsage:
    return InstanceDetailedUsage(
        consumed_14day=kwargs.get("consumed_14day", 0),
        consumed_7day=kwargs.get("consumed_7day", 0),
        consumed_3day=kwargs.get("consumed_3day", 0),
        consumed_24h=kwargs.get("consumed_24h", 0),
        daily_usage=kwargs.get("daily_usage", {}),
    )


# -------------------------------------------------------------------
# Instance — validation
# -------------------------------------------------------------------


def test_instance_detailed_usage() -> None:
    instance = InstanceState(
        name="", crn="", allocation_seconds=100, detailed_usage=None, consumed_seconds=0, limit_seconds=None
    )
    with pytest.raises(AssertionError):
        _ = instance.usage.consumed_14day

    instance.detailed_usage = _usage(consumed_14day=14)
    assert instance.usage.consumed_14day == 14


# -------------------------------------------------------------------
# Instance — fairness
# -------------------------------------------------------------------


def test_instance_fairness_calculation() -> None:
    assert _instance(consumed_seconds=50000).fairness == 0.5


def test_instance_fairness_zero_allocation() -> None:
    instance = InstanceState(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=0,
        consumed_seconds=1000,
        limit_seconds=None,
        detailed_usage=None,
    )
    assert instance.fairness == float("inf")


# -------------------------------------------------------------------
# Instance — activity_score
# -------------------------------------------------------------------


def test_activity_score_zero_when_no_usage() -> None:
    assert _instance(detailed_usage=_usage()).activity_score == 0.0


def test_activity_score_single_bucket() -> None:
    """24h usage contributes consumed_24h * bias^5 (= 32x)."""
    assert _instance(detailed_usage=_usage(consumed_24h=100)).activity_score == 100 * (2.0**5)


def test_activity_score_recent_outweighs_old() -> None:
    """Same per-day rate in 24h window scores higher than in 28d window."""
    recent = _instance(detailed_usage=_usage(consumed_24h=100))
    old = _instance(consumed_seconds=100 * 28, detailed_usage=_usage())  # same average daily rate over 28d
    assert recent.activity_score > old.activity_score


# -------------------------------------------------------------------
# Account — validation
# -------------------------------------------------------------------


def test_account_consumed_seconds_is_sum_of_instances() -> None:
    i1 = InstanceState(
        crn="crn:test:1",
        name="A",
        allocation_seconds=500000,
        consumed_seconds=100000,
        limit_seconds=None,
        detailed_usage=None,
    )
    i2 = InstanceState(
        crn="crn:test:2",
        name="B",
        allocation_seconds=500000,
        consumed_seconds=150000,
        limit_seconds=None,
        detailed_usage=None,
    )
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        allocation_budget_seconds=1000000,
        unallocated_seconds=0,
        limit_seconds=None,
        instances=(i1, i2),
    )
    assert account.consumed_seconds == 250000


def test_account_unmanaged_allocation_seconds() -> None:
    """target − available − sum(loaded allocations) = allocation held by unloaded instances."""
    loaded = InstanceState(
        crn="crn:test:1",
        name="Loaded",
        allocation_seconds=4,
        consumed_seconds=0,
        limit_seconds=None,
        detailed_usage=None,
    )
    # target=10, available=1, loaded holds 4, so 5 must be on instances not loaded.
    partial = Account(
        account_id="test-account",
        plan_id="test-plan",
        allocation_budget_seconds=10,
        unallocated_seconds=1,
        limit_seconds=None,
        instances=(loaded,),
    )
    assert partial.unmanaged_allocation_seconds == 5

    # When every instance is present (target − available == sum of allocations) it is 0.
    fully_loaded = Account(
        account_id="test-account",
        plan_id="test-plan",
        allocation_budget_seconds=10,
        unallocated_seconds=6,
        limit_seconds=None,
        instances=(loaded,),
    )
    assert fully_loaded.unmanaged_allocation_seconds == 0

    # An inconsistent snapshot (configured + available > target) is clamped at 0
    # rather than reported as negative — negative would falsely create cap headroom.
    inconsistent = Account(
        account_id="test-account",
        plan_id="test-plan",
        allocation_budget_seconds=10,
        unallocated_seconds=8,
        limit_seconds=None,
        instances=(loaded,),
    )
    assert inconsistent.unmanaged_allocation_seconds == 0
