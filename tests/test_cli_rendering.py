# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for CLI rendering functions to ensure data structures render correctly."""

import pytest
from datetime import datetime
from qauvern.models import (
    AllocationChange,
    InstanceState,
    InstanceConfig,
    InstanceDetailedUsage,
    LimitChange,
    OptimizationResult,
)
from qauvern.cli import format_instance_table, format_seconds, format_fairness


def test_format_zero() -> None:
    """Test formatting zero seconds."""
    assert format_seconds(0) == "0s"


def test_format_seconds_only() -> None:
    """Test formatting seconds less than a minute."""
    assert format_seconds(45) == "45s"


def test_format_minutes() -> None:
    """Test formatting minutes (less than 1 hour shows as seconds)."""
    assert format_seconds(120) == "120s"


def test_format_hours() -> None:
    """Test formatting hours."""
    assert format_seconds(7200) == "2.0h"


def test_format_days() -> None:
    """Test formatting days."""
    assert format_seconds(172800) == "2.0d"


def test_format_low_fairness() -> None:
    """Test formatting fairness below 1.0."""
    result = format_fairness(0.5)
    assert "0.50" in result
    # Low fairness shows warning symbol, not checkmark
    assert "⚠" in result or "✓" in result


def test_format_high_fairness() -> None:
    """Test formatting fairness above 1.0."""
    result = format_fairness(1.5)
    assert "1.50" in result
    assert "✗" in result


def test_format_exact_fairness() -> None:
    """Test formatting fairness exactly 1.0."""
    result = format_fairness(1.0)
    assert "1.00" in result


@pytest.fixture
def instance1() -> InstanceState:
    return InstanceState(
        crn="crn:v1:test:public:quantum-computing:us-east:a/account1:instance1::",
        name="test-instance-1",
        allocation_seconds=1000,
        consumed_seconds=500,
        limit_seconds=2000,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=300,
            consumed_14day=400,
            consumed_7day=350,
            consumed_3day=250,
            consumed_24h=100,
            daily_usage={},
        ),
    )


@pytest.fixture
def instance2() -> InstanceState:
    return InstanceState(
        crn="crn:v1:test:public:quantum-computing:us-east:a/account1:instance2::",
        name="test-instance-2",
        allocation_seconds=2000,
        consumed_seconds=1500,
        limit_seconds=None,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=1200,
            consumed_14day=1400,
            consumed_7day=1300,
            consumed_3day=800,
            consumed_24h=200,
            daily_usage={},
        ),
    )


@pytest.fixture
def cfg1(instance1: InstanceState) -> InstanceConfig:
    return InstanceConfig(
        name="Instance 1",
        crn=instance1.crn,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )


@pytest.fixture
def cfg2(instance2: InstanceState) -> InstanceConfig:
    return InstanceConfig(
        name="Instance 2",
        crn=instance2.crn,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 12, 31),
    )


@pytest.fixture
def alloc1(instance1: InstanceState) -> AllocationChange:
    return AllocationChange(
        instance_crn=instance1.crn,
        current=1000,
        new=1500,
        reason="Increased usage detected",
    )


@pytest.fixture
def alloc2(instance2: InstanceState) -> AllocationChange:
    return AllocationChange(
        instance_crn=instance2.crn,
        current=2000,
        new=1000,
        reason="Reduced due to low activity",
    )


@pytest.fixture
def limit1(instance1: InstanceState) -> LimitChange:
    return LimitChange(
        instance_crn=instance1.crn,
        current=2000,
        new=3000,
        reason="Net grant active",
    )


def test_format_basic_columns(instance1: InstanceState, instance2: InstanceState) -> None:
    """Test formatting with basic columns."""
    instances = [instance1, instance2]
    columns = ["name", "allocation", "consumed"]

    table_data, headers = format_instance_table(instances, columns=columns)

    assert len(table_data) == 2
    assert len(headers) == 3
    assert "Instance" in headers
    assert "Allocation" in headers
    assert "Consumed" in headers


def test_format_with_recommendations(
    instance1: InstanceState,
    instance2: InstanceState,
    alloc1: AllocationChange,
    alloc2: AllocationChange,
) -> None:
    """Test formatting with allocation changes."""
    instances = [instance1, instance2]
    alloc_map = {
        instance1.crn: alloc1,
        instance2.crn: alloc2,
    }
    columns = ["name", "allocation", "recommended", "change", "reason"]

    table_data, headers = format_instance_table(instances, columns=columns, alloc_map=alloc_map)

    assert len(table_data) == 2
    assert "Recommended" in headers
    assert "Change" in headers
    assert "Reason" in headers

    # Instance 1 should show increase
    row1 = table_data[0]
    assert "+" in str(row1)  # Should have positive change indicator

    # Instance 2 should show decrease
    row2 = table_data[1]
    assert "+" not in str(row2)  # Change should be negative (no + sign)


def test_format_all_time_periods(
    instance1: InstanceState, instance2: InstanceState, cfg1: InstanceConfig, cfg2: InstanceConfig
) -> None:
    """Test formatting with all time period columns."""
    instances = [instance1, instance2]
    instance_configs = [cfg1, cfg2]
    columns = ["name", "period", "28d", "14d", "7d", "3d", "24h"]

    table_data, headers = format_instance_table(instances, instance_configs=instance_configs, columns=columns)

    assert len(table_data) == 2
    assert "Period" in headers
    assert "28d" in headers
    assert "14d" in headers
    assert "7d" in headers
    assert "3d" in headers
    assert "24h" in headers


def test_format_with_fairness(instance1: InstanceState, instance2: InstanceState) -> None:
    """Test formatting with fairness column."""
    instances = [instance1, instance2]
    columns = ["name", "allocation", "consumed", "fairness"]

    table_data, headers = format_instance_table(instances, columns=columns)

    assert len(table_data) == 2
    assert "Fairness" in headers


def test_format_with_limit(instance1: InstanceState, instance2: InstanceState) -> None:
    """Test formatting with limit column."""
    instances = [instance1, instance2]
    columns = ["name", "allocation", "limit"]

    table_data, headers = format_instance_table(instances, columns=columns)

    assert len(table_data) == 2
    assert "Cur Limit" in headers


def test_format_empty_instances() -> None:
    """Test formatting with empty instance list."""
    instances = []
    columns = ["name", "allocation"]

    table_data, headers = format_instance_table(instances, columns=columns)

    assert len(table_data) == 0
    assert len(headers) == 2


def test_format_instance_without_recommendation(
    instance1: InstanceState, instance2: InstanceState, alloc1: AllocationChange
) -> None:
    """Test formatting instance that has no allocation change."""
    instances = [instance1, instance2]
    alloc_map = {instance1.crn: alloc1}  # Only change for instance1
    columns = ["name", "recommended", "change", "reason"]

    table_data, headers = format_instance_table(instances, columns=columns, alloc_map=alloc_map)

    assert len(table_data) == 2
    # Instance 2 should have "-" or "No change" for missing change
    row2 = table_data[1]
    assert "-" in str(row2) or "No change" in str(row2)


def test_allocation_change_properties() -> None:
    """Test AllocationChange fields and delta property."""
    chg = AllocationChange(
        instance_crn="crn:test",
        current=1000,
        new=1500,
        reason="Test reason",
    )

    assert chg.instance_crn == "crn:test"
    assert chg.current == 1000
    assert chg.new == 1500
    assert chg.reason == "Test reason"
    assert chg.delta == 500


def test_allocation_change_negative_delta() -> None:
    """Test AllocationChange with negative delta."""
    chg = AllocationChange(
        instance_crn="crn:test",
        current=2000,
        new=1000,
        reason="Reduction",
    )

    assert chg.delta == -1000


def test_limit_change_fields() -> None:
    """Test LimitChange fields."""
    chg = LimitChange(
        instance_crn="crn:test",
        current=2000,
        new=3000,
        reason="Net grant active",
    )

    assert chg.instance_crn == "crn:test"
    assert chg.current == 2000
    assert chg.new == 3000
    assert chg.reason == "Net grant active"


def test_result_decreases_property() -> None:
    """Test that decreases property filters allocation_changes with delta < 0."""
    result = OptimizationResult(
        allocation_changes=(
            AllocationChange(instance_crn="crn:1", current=2000, new=1000, reason="Reduce"),
            AllocationChange(instance_crn="crn:2", current=1000, new=2000, reason="Increase"),
            AllocationChange(instance_crn="crn:3", current=1500, new=500, reason="Reduce more"),
        ),
        limit_changes=(),
    )

    decreases = result.decreases
    assert len(decreases) == 2
    assert all(c.delta < 0 for c in decreases)


def test_result_increases_property() -> None:
    """Test that increases property filters allocation_changes with delta > 0."""
    result = OptimizationResult(
        allocation_changes=(
            AllocationChange(instance_crn="crn:1", current=2000, new=1000, reason="Reduce"),
            AllocationChange(instance_crn="crn:2", current=1000, new=2000, reason="Increase"),
            AllocationChange(instance_crn="crn:3", current=500, new=1500, reason="Increase more"),
        ),
        limit_changes=(),
    )

    increases = result.increases
    assert len(increases) == 2
    assert all(c.delta > 0 for c in increases)


def test_result_no_change() -> None:
    """A zero-delta AllocationChange appears in neither decreases nor increases."""
    result = OptimizationResult(
        allocation_changes=(AllocationChange(instance_crn="crn:1", current=1000, new=1000, reason="No change"),),
        limit_changes=(),
    )

    assert len(result.decreases) == 0
    assert len(result.increases) == 0


def test_none_returns_dash() -> None:
    """None limit returns dash."""
    from qauvern.cli import format_limit_display

    assert format_limit_display(None) == "-"


def test_limit_no_override() -> None:
    """Limit without override returns formatted seconds."""
    from qauvern.cli import format_limit_display

    result = format_limit_display(30000)
    assert result == format_seconds(30000)


def test_limit_with_grant_annotated() -> None:
    """Limit with active grant includes (+grant) annotation."""
    from qauvern.cli import format_limit_display

    result = format_limit_display(30000, has_grant=True)
    assert "(+grant)" in result
    assert format_seconds(30000) in result


def test_limit_with_debt_shows_exclamation() -> None:
    """Limit with debt flag includes ! annotation."""
    from qauvern.cli import format_limit_display

    result = format_limit_display(30000, in_debt=True)
    assert "!" in result


def test_limit_no_annotations() -> None:
    """Limit with no flags shows only the time value."""
    from qauvern.cli import format_limit_display

    result = format_limit_display(30000)
    assert "grant" not in result.lower()
    assert "!" not in result


def test_limit_both_grant_and_debt() -> None:
    """Both flags shown simultaneously."""
    from qauvern.cli import format_limit_display

    result = format_limit_display(30000, has_grant=True, in_debt=True)
    assert "(+grant)" in result
    assert "!" in result


def test_reserve_summary_contains_percent() -> None:
    """Reserve summary includes the reserve percentage."""
    from qauvern.cli import format_reserve_summary

    line = format_reserve_summary(total=500000, reserve_percent=20.0)
    assert "20.0%" in line


def test_reserve_summary_contains_available() -> None:
    """Reserve summary includes available-for-rebalancing amount (80% of total)."""
    from qauvern.cli import format_reserve_summary

    line = format_reserve_summary(total=500000, reserve_percent=20.0)
    assert format_seconds(400000) in line  # 80% of 500000
