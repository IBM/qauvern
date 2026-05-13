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

import pytest

from qauvern.models import Account, Instance, InstanceUsage, NetGrant, Project


def test_project_creation() -> None:
    """Test creating a valid project."""
    start = datetime(2026, 1, 1)
    end = datetime(2026, 12, 31)

    project = Project(
        name="Test Project",
        crn="crn:test:1",
        target_usage_seconds=1000000,
        start_date=start,
        end_date=end,
    )

    assert project.name == "Test Project"
    assert project.crn == "crn:test:1"
    assert project.target_usage_seconds == 1000000


def test_project_invalid_allocation() -> None:
    """Test that negative allocation raises error."""
    start = datetime(2026, 1, 1)
    end = datetime(2026, 12, 31)

    with pytest.raises(ValueError, match="target_usage_seconds must be positive"):
        Project(
            name="Test",
            crn="crn:test:1",
            target_usage_seconds=-1000,
            start_date=start,
            end_date=end,
        )


def test_project_no_target_usage_seconds() -> None:
    """Test that project can be created without target_usage_seconds."""
    project = Project(
        name="Test",
        crn="crn:test:1",
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )
    assert project.target_usage_seconds is None


def test_project_invalid_dates() -> None:
    """Test that invalid date range raises error."""
    start = datetime(2026, 12, 31)
    end = datetime(2026, 1, 1)

    with pytest.raises(ValueError, match="start_date must be before end_date"):
        Project(
            name="Test",
            crn="crn:test:1",
            target_usage_seconds=1000,
            start_date=start,
            end_date=end,
        )


def test_project_empty_crn() -> None:
    """Test that empty CRN raises error."""
    start = datetime(2026, 1, 1)
    end = datetime(2026, 12, 31)

    with pytest.raises(ValueError, match="crn cannot be empty"):
        Project(
            name="Test",
            crn="",
            target_usage_seconds=1000,
            start_date=start,
            end_date=end,
        )


def test_instance_creation() -> None:
    """Test creating a valid instance."""
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
    """Test fairness calculation."""
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=100000,
        consumed_seconds=50000,
    )

    assert instance.fairness == 0.5


def test_instance_fairness_zero_allocation() -> None:
    """Test fairness with zero allocation."""
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=0,
        consumed_seconds=1000,
    )

    assert instance.fairness == float("inf")


def test_instance_remaining_limit() -> None:
    """Test remaining limit calculation."""
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=100000,
        limit_seconds=150000,
        consumed_seconds=50000,
    )

    assert instance.remaining_limit == 100000


def test_instance_no_limit() -> None:
    """Test instance without limit."""
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=100000,
        consumed_seconds=50000,
    )

    assert instance.remaining_limit is None


def test_account_creation() -> None:
    """Test creating a valid account."""
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
    """Test available seconds from API."""
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        consumed_seconds=300000,
        available_seconds=700000,
    )

    assert account.available_seconds == 700000


def test_account_utilization() -> None:
    """Test utilization percentage calculation."""
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        consumed_seconds=250000,
    )

    assert account.utilization == 25.0


def test_account_add_instance() -> None:
    """Test adding instances to account."""
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
    )

    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=100000,
    )

    account.add_instance(instance)
    assert len(account.instances) == 1
    assert account.instances[0].crn == "crn:test:1"


def test_account_get_instance_by_crn() -> None:
    """Test getting instance by CRN."""
    account = Account(
        account_id="test-account",
        plan_id="test-plan",
        target_usage_seconds=1000000,
    )

    instance1 = Instance(crn="crn:test:1", name="Test1", allocation_seconds=100000)
    instance2 = Instance(crn="crn:test:2", name="Test2", allocation_seconds=200000)

    account.add_instance(instance1)
    account.add_instance(instance2)

    found = account.get_instance_by_crn("crn:test:2")
    assert found is not None
    assert found.name == "Test2"

    not_found = account.get_instance_by_crn("crn:test:3")
    assert not_found is None


def test_usage_fairness_calculation() -> None:
    """Test fairness calculation in usage."""
    usage = InstanceUsage(
        crn="crn:test:1",
        consumed_seconds=50000,
        allocation_seconds=100000,
    )

    assert usage.fairness == 0.5


def test_usage_fairness_zero_allocation() -> None:
    """Test fairness with zero allocation."""
    usage = InstanceUsage(
        crn="crn:test:1",
        consumed_seconds=1000,
        allocation_seconds=0,
    )

    assert usage.fairness == float("inf")


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


def test_project_without_limit_fields() -> None:
    """Test Project defaults when limit fields are not provided."""
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
    """Test Project with project_limit_seconds set."""
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


def test_not_in_debt_when_consumed_below_limit() -> None:
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=10000,
        consumed_seconds=5000,
        limit_seconds=6000,
    )
    assert instance.in_debt is False


def test_in_debt_when_consumed_exceeds_limit() -> None:
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=10000,
        consumed_seconds=7000,
        limit_seconds=6000,
    )
    assert instance.in_debt is True


def test_not_in_debt_when_no_limit() -> None:
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=10000,
        consumed_seconds=99999,
        limit_seconds=None,
    )
    assert instance.in_debt is False


def test_not_in_debt_at_exact_limit() -> None:
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=10000,
        consumed_seconds=6000,
        limit_seconds=6000,
    )
    assert instance.in_debt is False


def test_daily_usage_default_empty() -> None:
    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=10000,
    )
    assert instance.daily_usage == {}


def test_daily_usage_accepts_date_keyed_dict() -> None:
    from datetime import date

    instance = Instance(
        crn="crn:test:1",
        name="Test",
        allocation_seconds=10000,
        daily_usage={date(2026, 4, 1): 3600, date(2026, 4, 2): 7200},
    )
    assert instance.daily_usage[date(2026, 4, 1)] == 3600


def test_account_default_reserve() -> None:
    """Test Account defaults allocation_reserve_percent to 0.0."""
    account = Account(
        account_id="test",
        plan_id="test-plan",
        target_usage_seconds=1000000,
    )
    assert account.allocation_reserve_percent == 0.0


def test_account_with_reserve() -> None:
    """Test Account accepts allocation_reserve_percent."""
    account = Account(
        account_id="test",
        plan_id="test-plan",
        target_usage_seconds=1000000,
        allocation_reserve_percent=20.0,
    )
    assert account.allocation_reserve_percent == 20.0
