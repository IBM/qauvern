# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for formatting helpers and table builders."""

import pytest
from datetime import datetime
from qauvern.formatting import (
    format_fairness,
    format_instance_analysis_table,
    format_instance_summary_table,
    format_limit_display,
    format_optimize_changes_table,
    format_reserve_summary,
    format_seconds,
)
from qauvern.models import (
    AllocationChange,
    InstanceConfig,
    InstanceDetailedUsage,
    InstanceState,
    LimitChange,
    OptimizationResult,
)


# -------------------------------------------------------------------
# format_seconds
# -------------------------------------------------------------------


def test_format_zero() -> None:
    assert format_seconds(0) == "0s"


def test_format_seconds_only() -> None:
    assert format_seconds(45) == "45s"


def test_format_minutes() -> None:
    assert format_seconds(120) == "120s"


def test_format_hours() -> None:
    assert format_seconds(7200) == "2.0h"


def test_format_days() -> None:
    assert format_seconds(172800) == "2.0d"


# -------------------------------------------------------------------
# format_fairness
# -------------------------------------------------------------------


def test_format_low_fairness() -> None:
    result = format_fairness(0.5)
    assert "0.50" in result
    assert "⚠" in result or "✓" in result


def test_format_high_fairness() -> None:
    result = format_fairness(1.5)
    assert "1.50" in result
    assert "✗" in result


def test_format_exact_fairness() -> None:
    result = format_fairness(1.0)
    assert "1.00" in result


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------


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


# -------------------------------------------------------------------
# format_instance_summary_table
# -------------------------------------------------------------------


def test_summary_basic_columns(instance1: InstanceState, instance2: InstanceState) -> None:
    table_data, headers = format_instance_summary_table([instance1, instance2])
    assert headers == ["Instance", "Allocation", "Consumed", "Utilization", "Limit", "Fairness"]
    assert len(table_data) == 2


def test_summary_drops_cur_prefix(instance1: InstanceState) -> None:
    """The summary table shows `Limit`, not `Cur Limit` (no New Limit column to disambiguate from)."""
    _, headers = format_instance_summary_table([instance1])
    assert "Limit" in headers
    assert "Cur Limit" not in headers


def test_summary_empty() -> None:
    table_data, headers = format_instance_summary_table([])
    assert table_data == []
    assert len(headers) == 6


def test_summary_zero_allocation() -> None:
    inst = InstanceState(
        crn="crn:zero",
        name="zero-alloc",
        allocation_seconds=0,
        consumed_seconds=0,
        limit_seconds=None,
        detailed_usage=InstanceDetailedUsage(
            consumed_balance_period=0,
            consumed_14day=0,
            consumed_7day=0,
            consumed_3day=0,
            consumed_24h=0,
            daily_usage={},
        ),
    )
    table_data, _ = format_instance_summary_table([inst])
    assert "0.0%" in table_data[0]


# -------------------------------------------------------------------
# format_instance_analysis_table
# -------------------------------------------------------------------


def test_analysis_headers(instance1: InstanceState, cfg1: InstanceConfig) -> None:
    _, headers = format_instance_analysis_table([instance1], instance_configs=[cfg1])
    assert headers == [
        "Instance",
        "Period",
        "28d",
        "14d",
        "7d",
        "3d",
        "24h",
        "Allocation",
        "Cur Limit",
        "New Limit",
        "Recommended",
        "Change",
        "Reason",
    ]


def test_analysis_increase(
    instance1: InstanceState,
    cfg1: InstanceConfig,
    alloc1: AllocationChange,
) -> None:
    table_data, _ = format_instance_analysis_table(
        [instance1],
        instance_configs=[cfg1],
        alloc_map={instance1.crn: alloc1},
    )
    row = table_data[0]
    assert format_seconds(1500) in row
    assert any(cell.startswith("+") for cell in row if isinstance(cell, str))
    assert alloc1.reason[:30] in row


def test_analysis_decrease_uses_minus_sign(
    instance2: InstanceState,
    cfg2: InstanceConfig,
    alloc2: AllocationChange,
) -> None:
    """Negative deltas in the analysis change column are prefixed with '-'."""
    table_data, _ = format_instance_analysis_table(
        [instance2],
        instance_configs=[cfg2],
        alloc_map={instance2.crn: alloc2},
    )
    row = table_data[0]
    assert any(cell.startswith("-") and cell != "-" for cell in row if isinstance(cell, str))


def test_analysis_no_change(instance1: InstanceState, cfg1: InstanceConfig) -> None:
    table_data, _ = format_instance_analysis_table([instance1], instance_configs=[cfg1])
    row = table_data[0]
    assert "No change" in row


def test_analysis_limit_only_uses_limit_reason(
    instance1: InstanceState,
    cfg1: InstanceConfig,
    limit1: LimitChange,
) -> None:
    """When only a limit change exists, the reason cell falls back to the limit's reason."""
    table_data, _ = format_instance_analysis_table(
        [instance1],
        instance_configs=[cfg1],
        limit_map={instance1.crn: limit1},
    )
    row = table_data[0]
    assert limit1.reason[:30] in row


def test_analysis_new_limit_from_map(
    instance1: InstanceState,
    cfg1: InstanceConfig,
    limit1: LimitChange,
) -> None:
    table_data, _ = format_instance_analysis_table(
        [instance1],
        instance_configs=[cfg1],
        limit_map={instance1.crn: limit1},
    )
    assert format_seconds(limit1.new) in table_data[0]


# -------------------------------------------------------------------
# format_optimize_changes_table
# -------------------------------------------------------------------


def test_optimize_table_negative_delta_has_minus(instance2: InstanceState, alloc2: AllocationChange) -> None:
    """Issue #21: negative changes in the optimize table must be prefixed with '-'."""
    rows, headers = format_optimize_changes_table(
        allocation_changes=[alloc2],
        limit_changes=[],
        instance_map={instance2.crn: instance2.name},
    )
    assert headers == ["Instance Name", "Current", "New", "Change", "New Limit"]
    assert len(rows) == 1
    change_cell = rows[0][3]
    assert change_cell.startswith("-")
    assert change_cell != "-"


def test_optimize_table_positive_delta_has_plus(instance1: InstanceState, alloc1: AllocationChange) -> None:
    rows, _ = format_optimize_changes_table(
        allocation_changes=[alloc1],
        limit_changes=[],
        instance_map={instance1.crn: instance1.name},
    )
    assert rows[0][3].startswith("+")


def test_optimize_table_limit_only_row(instance1: InstanceState, limit1: LimitChange) -> None:
    """A row with only a limit change shows '-' for allocation columns and a real limit value."""
    rows, _ = format_optimize_changes_table(
        allocation_changes=[],
        limit_changes=[limit1],
        instance_map={instance1.crn: instance1.name},
    )
    assert len(rows) == 1
    name, current, new, change, new_limit = rows[0]
    assert name == instance1.name
    assert current == "-"
    assert new == "-"
    assert change == "-"
    assert new_limit == format_seconds(limit1.new)


def test_optimize_table_sorted_by_instance_name(
    instance1: InstanceState,
    instance2: InstanceState,
    alloc1: AllocationChange,
    alloc2: AllocationChange,
) -> None:
    rows, _ = format_optimize_changes_table(
        allocation_changes=[alloc2, alloc1],
        limit_changes=[],
        instance_map={instance1.crn: instance1.name, instance2.crn: instance2.name},
    )
    assert [r[0] for r in rows] == sorted([instance1.name, instance2.name])


# -------------------------------------------------------------------
# AllocationChange / OptimizationResult
# -------------------------------------------------------------------


def test_allocation_change_delta() -> None:
    assert AllocationChange(instance_crn="crn:test", current=1000, new=1500, reason="t").delta == 500
    assert AllocationChange(instance_crn="crn:test", current=2000, new=1000, reason="t").delta == -1000


def test_result_partitions_by_delta_sign() -> None:
    result = OptimizationResult(
        allocation_changes=(
            AllocationChange(instance_crn="crn:1", current=2000, new=1000, reason="down"),
            AllocationChange(instance_crn="crn:2", current=1000, new=2000, reason="up"),
            AllocationChange(instance_crn="crn:3", current=1500, new=1500, reason="flat"),
        ),
        limit_changes=(),
    )
    assert [c.instance_crn for c in result.decreases] == ["crn:1"]
    assert [c.instance_crn for c in result.increases] == ["crn:2"]


# -------------------------------------------------------------------
# format_limit_display
# -------------------------------------------------------------------


def test_limit_display_none_returns_dash() -> None:
    assert format_limit_display(None) == "-"


def test_limit_display_no_override() -> None:
    assert format_limit_display(30000) == format_seconds(30000)


def test_limit_display_with_grant_annotated() -> None:
    result = format_limit_display(30000, has_grant=True)
    assert "(+grant)" in result
    assert format_seconds(30000) in result


def test_limit_display_with_debt_shows_exclamation() -> None:
    result = format_limit_display(30000, in_debt=True)
    assert "!" in result


def test_limit_display_no_annotations() -> None:
    result = format_limit_display(30000)
    assert "grant" not in result.lower()
    assert "!" not in result


def test_limit_display_both_grant_and_debt() -> None:
    result = format_limit_display(30000, has_grant=True, in_debt=True)
    assert "(+grant)" in result
    assert "!" in result


# -------------------------------------------------------------------
# format_reserve_summary
# -------------------------------------------------------------------


def test_reserve_summary_contains_percent() -> None:
    line = format_reserve_summary(distributable_pool=400000, reserve_percent=20.0)
    assert "20.0%" in line


def test_reserve_summary_contains_available() -> None:
    line = format_reserve_summary(distributable_pool=400000, reserve_percent=20.0)
    assert format_seconds(400000) in line
