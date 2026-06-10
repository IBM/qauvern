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
from qauvern.formatting import (
    format_fairness,
    format_instance_analysis_table,
    format_instance_summary_table,
    format_reserve_summary,
    format_seconds,
)
from qauvern.models import (
    AllocationChange,
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
def alloc1(instance1: InstanceState) -> AllocationChange:
    return AllocationChange(
        current=1000,
        new=1500,
        reason="Increased usage detected",
    )


@pytest.fixture
def alloc2(instance2: InstanceState) -> AllocationChange:
    return AllocationChange(
        current=2000,
        new=1000,
        reason="Reduced due to low activity",
    )


@pytest.fixture
def limit1(instance1: InstanceState) -> LimitChange:
    return LimitChange(
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


def test_analysis_headers(instance1: InstanceState) -> None:
    _, headers = format_instance_analysis_table([instance1])
    assert headers == [
        "Instance",
        "Period",
        "28d",
        "14d",
        "7d",
        "3d",
        "24h",
        "Allocation",
        "Limit",
        "Reason",
    ]


def test_analysis_allocation_inlines_delta_on_change(
    instance1: InstanceState,
    alloc1: AllocationChange,
) -> None:
    """Allocation cell renders `cur → new (+delta)` when changing."""
    table_data, headers = format_instance_analysis_table(
        [instance1],
        alloc_map={instance1.crn: alloc1},
    )
    alloc_cell = table_data[0][headers.index("Allocation")]
    assert "→" in alloc_cell
    assert format_seconds(alloc1.current) in alloc_cell
    assert format_seconds(alloc1.new) in alloc_cell
    assert f"(+{format_seconds(alloc1.delta)})" in alloc_cell


def test_analysis_allocation_no_arrow_when_unchanged(instance1: InstanceState) -> None:
    table_data, headers = format_instance_analysis_table([instance1])
    alloc_cell = table_data[0][headers.index("Allocation")]
    assert "→" not in alloc_cell
    assert alloc_cell == format_seconds(instance1.allocation_seconds)


def test_analysis_limit_inlines_delta_on_change(instance1: InstanceState, limit1: LimitChange) -> None:
    table_data, headers = format_instance_analysis_table(
        [instance1],
        limit_map={instance1.crn: limit1},
    )
    limit_cell = table_data[0][headers.index("Limit")]
    assert "→" in limit_cell
    assert format_seconds(limit1.new) in limit_cell
    assert limit1.current is not None
    assert limit1.current is not None
    assert f"(+{format_seconds(limit1.new - limit1.current)})" in limit_cell


def test_analysis_increase(
    instance1: InstanceState,
    alloc1: AllocationChange,
) -> None:
    table_data, headers = format_instance_analysis_table(
        [instance1],
        alloc_map={instance1.crn: alloc1},
    )
    alloc_cell = table_data[0][headers.index("Allocation")]
    assert format_seconds(1500) in alloc_cell
    assert "(+" in alloc_cell
    assert alloc1.reason in table_data[0]


def test_analysis_decrease_uses_minus_sign(
    instance2: InstanceState,
    alloc2: AllocationChange,
) -> None:
    """Negative allocation deltas are prefixed with '-' in the inline delta."""
    table_data, headers = format_instance_analysis_table(
        [instance2],
        alloc_map={instance2.crn: alloc2},
    )
    alloc_cell = table_data[0][headers.index("Allocation")]
    assert "(-" in alloc_cell


def test_analysis_no_change(instance1: InstanceState) -> None:
    table_data, _ = format_instance_analysis_table([instance1])
    row = table_data[0]
    assert "No change" in row


def test_analysis_limit_only_uses_limit_reason(
    instance1: InstanceState,
    limit1: LimitChange,
) -> None:
    """When only a limit change exists, the reason cell falls back to the limit's reason."""
    table_data, _ = format_instance_analysis_table(
        [instance1],
        limit_map={instance1.crn: limit1},
    )
    row = table_data[0]
    assert limit1.reason in row


def test_analysis_reason_truncation_at_60_chars(instance1: InstanceState) -> None:
    long_reason = "x" * 100
    alloc = AllocationChange(current=1000, new=1500, reason=long_reason)
    table_data, headers = format_instance_analysis_table([instance1], alloc_map={instance1.crn: alloc})
    reason_cell = table_data[0][headers.index("Reason")]
    assert reason_cell == "x" * 60


# -------------------------------------------------------------------
# format_instance_analysis_table with include_usage=False (optimize view)
# -------------------------------------------------------------------


def test_optimize_table_drops_usage_columns(instance1: InstanceState, alloc1: AllocationChange) -> None:
    """include_usage=False yields the same shape as analyze minus the usage block."""
    _, headers = format_instance_analysis_table(
        [instance1],
        alloc_map={instance1.crn: alloc1},
        include_usage=False,
    )
    assert headers == ["Instance", "Allocation", "Limit", "Reason"]


def test_optimize_table_negative_delta_has_minus(instance2: InstanceState, alloc2: AllocationChange) -> None:
    """Issue #21: negative allocation deltas inline as `(-x)` in the Allocation cell."""
    rows, headers = format_instance_analysis_table(
        [instance2],
        alloc_map={instance2.crn: alloc2},
        include_usage=False,
    )
    alloc_cell = rows[0][headers.index("Allocation")]
    assert "(-" in alloc_cell


def test_optimize_table_positive_delta_has_plus(instance1: InstanceState, alloc1: AllocationChange) -> None:
    rows, headers = format_instance_analysis_table(
        [instance1],
        alloc_map={instance1.crn: alloc1},
        include_usage=False,
    )
    assert "(+" in rows[0][headers.index("Allocation")]


def test_optimize_table_allocation_only_shows_actual_limit(instance1: InstanceState, alloc1: AllocationChange) -> None:
    """When only allocation changes, the Limit cell shows the instance's real current limit (not a placeholder)."""
    rows, headers = format_instance_analysis_table(
        [instance1],
        alloc_map={instance1.crn: alloc1},
        include_usage=False,
    )
    assert rows[0][headers.index("Limit")] == format_seconds(instance1.limit_seconds or 0)
    assert "→" not in rows[0][headers.index("Limit")]


def test_optimize_table_allocation_arrow_on_change(instance1: InstanceState, alloc1: AllocationChange) -> None:
    rows, headers = format_instance_analysis_table(
        [instance1],
        alloc_map={instance1.crn: alloc1},
        include_usage=False,
    )
    alloc_cell = rows[0][headers.index("Allocation")]
    assert "→" in alloc_cell
    assert format_seconds(alloc1.current) in alloc_cell
    assert format_seconds(alloc1.new) in alloc_cell
    assert f"(+{format_seconds(alloc1.delta)})" in alloc_cell


def test_optimize_table_pulls_reason_from_allocation(instance1: InstanceState, alloc1: AllocationChange) -> None:
    rows, headers = format_instance_analysis_table(
        [instance1],
        alloc_map={instance1.crn: alloc1},
        include_usage=False,
    )
    assert rows[0][headers.index("Reason")] == alloc1.reason


def test_optimize_table_pulls_reason_from_limit_when_no_alloc(instance1: InstanceState, limit1: LimitChange) -> None:
    rows, headers = format_instance_analysis_table(
        [instance1],
        limit_map={instance1.crn: limit1},
        include_usage=False,
    )
    assert rows[0][headers.index("Reason")] == limit1.reason


def test_optimize_table_limit_only_row_shows_real_allocation(instance1: InstanceState, limit1: LimitChange) -> None:
    """A row with only a limit change shows the instance's actual current allocation (not a placeholder)."""
    rows, headers = format_instance_analysis_table(
        [instance1],
        limit_map={instance1.crn: limit1},
        include_usage=False,
    )
    assert len(rows) == 1
    alloc_cell = rows[0][headers.index("Allocation")]
    limit_cell = rows[0][headers.index("Limit")]
    assert alloc_cell == format_seconds(instance1.allocation_seconds)
    assert "→" not in alloc_cell
    assert "→" in limit_cell
    assert format_seconds(limit1.new) in limit_cell
    assert limit1.current is not None
    assert f"(+{format_seconds(limit1.new - limit1.current)})" in limit_cell


# -------------------------------------------------------------------
# AllocationChange / OptimizationResult
# -------------------------------------------------------------------


def test_allocation_change_delta() -> None:
    assert AllocationChange(current=1000, new=1500, reason="t").delta == 500
    assert AllocationChange(current=2000, new=1000, reason="t").delta == -1000


def test_result_partitions_by_delta_sign() -> None:
    result = OptimizationResult(
        allocation_changes={
            "crn:1": AllocationChange(current=2000, new=1000, reason="down"),
            "crn:2": AllocationChange(current=1000, new=2000, reason="up"),
            "crn:3": AllocationChange(current=1500, new=1500, reason="flat"),
        },
        limit_changes={},
    )
    assert list(result.decreases) == ["crn:1"]
    assert list(result.increases) == ["crn:2"]


# -------------------------------------------------------------------
# format_reserve_summary
# -------------------------------------------------------------------


def test_reserve_summary_contains_percent() -> None:
    line = format_reserve_summary(distributable_pool=400000, reserve_percent=20.0)
    assert "20.0%" in line


def test_reserve_summary_contains_available() -> None:
    line = format_reserve_summary(distributable_pool=400000, reserve_percent=20.0)
    assert format_seconds(400000) in line
