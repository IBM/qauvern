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

from datetime import datetime
from typing import Any

import pytest

from qauvern.models import Account, Instance, InstanceConfig, InstanceDetailedUsage, NetGrant


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

_USAGE_FIELD_NAMES = {"consumed_14day", "consumed_7day", "consumed_3day", "consumed_24h", "daily_usage"}


def _instance(**kwargs: Any) -> Instance:
    usage_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k in _USAGE_FIELD_NAMES}
    if "detailed_usage" not in kwargs:
        kwargs["detailed_usage"] = InstanceDetailedUsage(
            consumed_14day=usage_kwargs.get("consumed_14day", 0),
            consumed_7day=usage_kwargs.get("consumed_7day", 0),
            consumed_3day=usage_kwargs.get("consumed_3day", 0),
            consumed_24h=usage_kwargs.get("consumed_24h", 0),
            daily_usage=usage_kwargs.get("daily_usage", {}),
        )
    return Instance(crn="crn:test:1", name="Test", allocation_seconds=100000, **kwargs)


# -------------------------------------------------------------------
# NetGrant — validation
# -------------------------------------------------------------------


def test_net_grant_zero_seconds_raises() -> None:
    with pytest.raises(ValueError, match="net_grant_seconds must be positive"):
        NetGrant(start_date=datetime(2026, 5, 1), net_grant_seconds=0, end_date=datetime(2026, 5, 29))


def test_net_grant_negative_raises() -> None:
    with pytest.raises(ValueError, match="net_grant_seconds must be positive"):
        NetGrant(start_date=datetime(2026, 5, 1), net_grant_seconds=-100, end_date=datetime(2026, 5, 29))


def test_net_grant_end_date_before_start_raises() -> None:
    with pytest.raises(ValueError, match="end_date must be after start_date"):
        NetGrant(
            start_date=datetime(2026, 5, 1),
            net_grant_seconds=86400,
            end_date=datetime(2026, 4, 30),
        )


def test_net_grant_end_date_equals_start_raises() -> None:
    with pytest.raises(ValueError, match="end_date must be after start_date"):
        NetGrant(
            start_date=datetime(2026, 5, 1),
            net_grant_seconds=86400,
            end_date=datetime(2026, 5, 1),
        )


# -------------------------------------------------------------------
# InstanceConfig — validation
# -------------------------------------------------------------------


def test_instance_config_invalid_allocation() -> None:
    with pytest.raises(ValueError, match="target_usage_seconds must be positive"):
        InstanceConfig(
            name="Test",
            crn="crn:test:1",
            target_usage_seconds=-1000,
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 12, 31),
        )


def test_instance_config_invalid_dates() -> None:
    with pytest.raises(ValueError, match="start_date must be before end_date"):
        InstanceConfig(
            name="Test",
            crn="crn:test:1",
            target_usage_seconds=1000,
            start_date=datetime(2026, 12, 31),
            end_date=datetime(2026, 1, 1),
        )


def test_instance_config_empty_crn() -> None:
    with pytest.raises(ValueError, match="crn cannot be empty"):
        InstanceConfig(
            name="Test",
            crn="",
            target_usage_seconds=1000,
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 12, 31),
        )


# -------------------------------------------------------------------
# Instance — validation
# -------------------------------------------------------------------


def test_instance_detailed_usage() -> None:
    instance = Instance(
        name="",
        crn="",
        allocation_seconds=100,
        detailed_usage=None,
    )
    with pytest.raises(AssertionError):
        instance.usage.consumed_14day

    instance.detailed_usage = InstanceDetailedUsage(
        consumed_14day=14, consumed_7day=7, consumed_3day=3, consumed_24h=24, daily_usage={}
    )
    assert instance.usage.consumed_14day == 14


# -------------------------------------------------------------------
# Instance — fairness
# -------------------------------------------------------------------


def test_instance_fairness_calculation() -> None:
    assert _instance(consumed_seconds=50000).fairness == 0.5


def test_instance_fairness_zero_allocation() -> None:
    instance = Instance(crn="crn:test:1", name="Test", allocation_seconds=0, consumed_seconds=1000)
    assert instance.fairness == float("inf")


# -------------------------------------------------------------------
# Instance — activity_score
# -------------------------------------------------------------------


def test_activity_score_zero_when_no_usage() -> None:
    assert _instance().activity_score == 0.0


def test_activity_score_single_bucket() -> None:
    """24h usage contributes consumed_24h * bias^5 (= 32x)."""
    assert _instance(consumed_24h=100).activity_score == 100 * (2.0**5)


def test_activity_score_recent_outweighs_old() -> None:
    """Same per-day rate in 24h window scores higher than in 28d window."""
    recent = _instance(consumed_24h=100)
    old = _instance(consumed_seconds=100 * 28)  # same average daily rate over 28d
    assert recent.activity_score > old.activity_score


# -------------------------------------------------------------------
# Instance — exhausted
# -------------------------------------------------------------------


def test_exhausted_no_target() -> None:
    """target_usage_seconds=0 means no cap — never exhausted regardless of consumption."""
    assert not _instance(consumed_balance_period=999999).exhausted


def test_exhausted_under_target() -> None:
    assert not _instance(target_usage_seconds=1000, consumed_balance_period=999).exhausted


def test_exhausted_at_target() -> None:
    """Boundary: >= means exactly hitting the target counts as exhausted."""
    assert _instance(target_usage_seconds=1000, consumed_balance_period=1000).exhausted


def test_exhausted_over_target() -> None:
    assert _instance(target_usage_seconds=1000, consumed_balance_period=1001).exhausted


# -------------------------------------------------------------------
# Account — validation and utilization
# -------------------------------------------------------------------


def test_account_utilization() -> None:
    instance = Instance(crn="crn:test:1", name="Test", allocation_seconds=1000000, consumed_seconds=250000)
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        available_seconds=0,
        limit_seconds=None,
        instances=(instance,),
    )
    assert account.utilization == 25.0


def test_account_consumed_seconds_is_sum_of_instances() -> None:
    i1 = Instance(crn="crn:test:1", name="A", allocation_seconds=500000, consumed_seconds=100000)
    i2 = Instance(crn="crn:test:2", name="B", allocation_seconds=500000, consumed_seconds=150000)
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        available_seconds=0,
        limit_seconds=None,
        instances=(i1, i2),
    )
    assert account.consumed_seconds == 250000
