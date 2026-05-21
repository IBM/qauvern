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

from datetime import date, datetime
from typing import Any

import pytest

from qauvern.models import Account, Instance, NetGrant, Project


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _instance(**kwargs: Any) -> Instance:
    return Instance(crn="crn:test:1", name="Test", allocation_seconds=100000, **kwargs)


# -------------------------------------------------------------------
# NetGrant
# -------------------------------------------------------------------


def test_net_grant_construction() -> None:
    grant = NetGrant(
        start_date=datetime(2026, 5, 1),
        net_grant_seconds=86400,
        end_date=datetime(2026, 5, 29),
    )
    assert grant.start_date == datetime(2026, 5, 1)
    assert grant.net_grant_seconds == 86400


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
# Project
# -------------------------------------------------------------------


def test_project_creation() -> None:
    project = Project(
        name="Test Project",
        crn="crn:test:1",
        target_usage_seconds=1000000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )
    assert project.name == "Test Project"
    assert project.crn == "crn:test:1"
    assert project.target_usage_seconds == 1000000


def test_project_no_target_usage_seconds() -> None:
    project = Project(
        name="Test",
        crn="crn:test:1",
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )
    assert project.target_usage_seconds is None


def test_project_without_limit_fields() -> None:
    p = Project(
        name="A",
        crn="crn:test:1",
        target_usage_seconds=30000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )
    assert p.project_limit_seconds is None
    assert p.net_grants == []


def test_project_with_limit_seconds() -> None:
    p = Project(
        name="A",
        crn="crn:test:1",
        target_usage_seconds=30000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
        project_limit_seconds=50000,
    )
    assert p.project_limit_seconds == 50000


def test_project_accepts_net_grants() -> None:
    grant = NetGrant(start_date=datetime(2026, 5, 1), net_grant_seconds=86400, end_date=datetime(2026, 5, 29))
    p = Project(
        name="A",
        crn="crn:test:1",
        target_usage_seconds=30000,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
        project_limit_seconds=50000,
        net_grants=[grant],
    )
    assert len(p.net_grants) == 1
    assert p.net_grants[0].net_grant_seconds == 86400


def test_project_invalid_allocation() -> None:
    with pytest.raises(ValueError, match="target_usage_seconds must be positive"):
        Project(
            name="Test",
            crn="crn:test:1",
            target_usage_seconds=-1000,
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 12, 31),
        )


def test_project_invalid_dates() -> None:
    with pytest.raises(ValueError, match="start_date must be before end_date"):
        Project(
            name="Test",
            crn="crn:test:1",
            target_usage_seconds=1000,
            start_date=datetime(2026, 12, 31),
            end_date=datetime(2026, 1, 1),
        )


def test_project_empty_crn() -> None:
    with pytest.raises(ValueError, match="crn cannot be empty"):
        Project(
            name="Test",
            crn="",
            target_usage_seconds=1000,
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 12, 31),
        )


# -------------------------------------------------------------------
# Instance — creation and fairness
# -------------------------------------------------------------------


def test_instance_creation() -> None:
    instance = Instance(
        crn="crn:test:instance-1",
        name="Test Instance",
        allocation_seconds=100000,
        limit_seconds=150000,
        consumed_seconds=50000,
    )
    assert instance.crn == "crn:test:instance-1"
    assert instance.allocation_seconds == 100000
    assert instance.consumed_seconds == 50000


def test_instance_fairness_calculation() -> None:
    assert _instance(consumed_seconds=50000).fairness == 0.5


def test_instance_fairness_zero_allocation() -> None:
    instance = Instance(crn="crn:test:1", name="Test", allocation_seconds=0, consumed_seconds=1000)
    assert instance.fairness == float("inf")


def test_daily_usage_default_empty() -> None:
    assert _instance().daily_usage == {}


def test_daily_usage_accepts_date_keyed_dict() -> None:
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=10000,
        daily_usage={date(2026, 4, 1): 3600, date(2026, 4, 2): 7200},
    )
    assert instance.daily_usage[date(2026, 4, 1)] == 3600


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
# Account
# -------------------------------------------------------------------


def test_account_creation() -> None:
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        consumed_seconds=500000,
    )
    assert account.account_id == "test-account"
    assert account.target_usage_seconds == 1000000
    assert account.consumed_seconds == 500000


def test_account_available_seconds() -> None:
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        consumed_seconds=300000,
        available_seconds=700000,
    )
    assert account.available_seconds == 700000


def test_account_utilization() -> None:
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        consumed_seconds=250000,
    )
    assert account.utilization == 25.0


def test_account_add_instance() -> None:
    account = Account(account_id="test-account", plan_id="test-plan", target_usage_seconds=1000000)
    account.add_instance(_instance())
    assert len(account.instances) == 1
    assert account.instances[0].crn == "crn:test:1"


def test_account_default_reserve() -> None:
    account = Account(account_id="test", plan_id="test-plan", target_usage_seconds=1000000)
    assert account.allocation_reserve_percent == 0.0


def test_account_with_reserve() -> None:
    account = Account(
        account_id="test",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        allocation_reserve_percent=20.0,
    )
    assert account.allocation_reserve_percent == 20.0
