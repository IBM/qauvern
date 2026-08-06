# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for the `qauvern analyze` command and its pure helpers."""

import csv
import io
import json

from qauvern.commands.analyze import (
    CSV_COLUMNS,
    AnalyzeReport,
    format_analyze_csv,
    format_analyze_json,
    format_analyze_table,
)
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
    )


def _no_changes_setup():
    """One instance already at its floor — optimizer produces no changes."""
    inst = _make_instance(CRN_A, allocation=60)
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()
    return account, result, [cfg], optimizer


def _report(account, result, cfgs, optimizer, *, plan: Plan = Plan.PAYGO) -> AnalyzeReport:
    return AnalyzeReport.from_optimizer(account, result, plan, cfgs, optimizer)


# ---------------------------------------------------------------------------
# AnalyzeReport.from_optimizer
# ---------------------------------------------------------------------------


def test_from_optimizer_captures_validation_errors() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=200, reason="Active")},
        limit_changes={},
    )

    report = _report(account, result, [cfg], optimizer)
    assert report.validation_errors  # non-empty


def test_from_optimizer_no_validation_errors_when_valid() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    report = _report(account, result, cfgs, optimizer)
    assert report.validation_errors == ()


def test_from_optimizer_usage_floor_warning_when_disabled() -> None:
    inst = _make_instance(CRN_A, allocation=200, consumed=150)
    account = _make_account((inst,), budget=1000)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], enforce_usage_floor=False)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=200, new=100, reason="t")},
        limit_changes={},
    )

    report = _report(account, result, [cfg], optimizer)
    assert report.validation_errors == ()
    assert report.usage_floor_warnings
    assert "28-day usage" in report.usage_floor_warnings[0]


def test_from_optimizer_pool_zero_when_reserve_zero() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    report = _report(account, result, cfgs, optimizer)
    assert report.allocation_reserve_percent == 0
    assert report.redistribution_pool_seconds == 0


def test_from_optimizer_pool_populated_when_reserve_set() -> None:
    inst = _make_instance(CRN_A, allocation=60)
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60, allocation_reserve_percent=10.0)
    result = optimizer.optimize()

    report = _report(account, result, [cfg], optimizer)
    assert report.allocation_reserve_percent == 10.0
    expected_pool, _ = optimizer.redistribution_pool()
    assert report.redistribution_pool_seconds == expected_pool


# ---------------------------------------------------------------------------
# format_analyze_table — no changes
# ---------------------------------------------------------------------------


def test_table_no_changes_footer() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_table(_report(account, result, cfgs, optimizer))
    assert "No optimization recommendations" in output
    assert "To apply" not in output


def test_table_no_validation_errors_block_by_default() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_table(_report(account, result, cfgs, optimizer))
    assert "VALIDATION ERRORS" not in output


# ---------------------------------------------------------------------------
# format_analyze_table — has changes
# ---------------------------------------------------------------------------


def test_table_footer_shows_change_counts() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=80, reason="Inactive")},
        limit_changes={},
    )

    output = format_analyze_table(_report(account, result, [cfg], optimizer))
    assert "Total changes: 1 (1 allocation, 0 limit)" in output
    assert "To apply these recommendations, run: qauvern optimize" in output


def test_table_footer_counts_both_allocation_and_limit_changes() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=80, reason="Inactive")},
        limit_changes={CRN_A: LimitChange(current=None, new=3600)},
    )

    output = format_analyze_table(_report(account, result, [cfg], optimizer))
    assert "Total changes: 2 (1 allocation, 1 limit)" in output


# ---------------------------------------------------------------------------
# format_analyze_table — validation errors
# ---------------------------------------------------------------------------


def test_table_validation_errors_block_appears_when_invalid() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=200, reason="Active")},
        limit_changes={},
    )

    output = format_analyze_table(_report(account, result, [cfg], optimizer))
    assert "VALIDATION ERRORS" in output


# ---------------------------------------------------------------------------
# format_analyze_table — conditional lines
# ---------------------------------------------------------------------------


def test_table_reserve_summary_appears_when_reserve_percent_set() -> None:
    inst = _make_instance(CRN_A, allocation=60)
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60, allocation_reserve_percent=10.0)
    result = optimizer.optimize()

    output = format_analyze_table(_report(account, result, [cfg], optimizer))
    assert "Reserve: 10.0%" in output


def test_table_no_reserve_line_when_zero_percent() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_table(_report(account, result, cfgs, optimizer))
    assert "Reserve:" not in output


def test_table_unmanaged_allocation_line_appears_when_nonzero() -> None:
    inst = _make_instance(CRN_A, allocation=60)
    # budget=100, unallocated=0, configured=60 → unmanaged=40
    account = _make_account((inst,), budget=100, unallocated=0)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    output = format_analyze_table(_report(account, result, [cfg], optimizer))
    assert "Held by unconfigured instances" in output


def test_table_no_unmanaged_line_when_zero() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_table(_report(account, result, cfgs, optimizer))
    assert "Held by unconfigured instances" not in output


def test_table_limit_shown_as_unlimited_when_none() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_table(_report(account, result, cfgs, optimizer))
    assert "Limit: Unlimited" in output


def test_table_limit_shown_when_set() -> None:
    inst = _make_instance(CRN_A, allocation=60)
    account = _make_account((inst,), budget=60, limit=3600)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    output = format_analyze_table(_report(account, result, [cfg], optimizer))
    assert "Limit: 1.0h" in output


def test_table_plan_name_and_instance_count_appear() -> None:
    inst = _make_instance(CRN_A, allocation=60, name="My Instance")
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A, name="My Instance")
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    output = format_analyze_table(_report(account, result, [cfg], optimizer))
    assert "Plan: paygo" in output
    assert "Configured instances analyzed: 1" in output
    assert "My Instance" in output


# ---------------------------------------------------------------------------
# format_analyze_csv
# ---------------------------------------------------------------------------


def _parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert reader.fieldnames is not None
    return list(reader.fieldnames), rows


def test_csv_header_matches_documented_columns() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_csv(_report(account, result, cfgs, optimizer))
    headers, _ = _parse_csv(output)
    assert tuple(headers) == CSV_COLUMNS


def test_csv_one_row_per_instance() -> None:
    inst_a = _make_instance(CRN_A, allocation=60, name="A")
    crn_b = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:inst-b::"
    inst_b = _make_instance(crn_b, allocation=60, name="B")
    account = _make_account((inst_a, inst_b), budget=120)
    cfgs = [_make_config(CRN_A, name="A"), _make_config(crn_b, name="B")]
    optimizer = AllocationOptimizer(account, cfgs, minimum_allocation_seconds=60)
    result = optimizer.optimize()

    output = format_analyze_csv(_report(account, result, cfgs, optimizer))
    _, rows = _parse_csv(output)
    assert [r["name"] for r in rows] == ["A", "B"]


def test_csv_unchanged_instance_keeps_current_values_and_no_reason() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_csv(_report(account, result, cfgs, optimizer))
    _, rows = _parse_csv(output)
    row = rows[0]
    assert row["current_allocation"] == "60"
    assert row["new_allocation"] == "60"
    assert row["allocation_delta"] == "0"
    assert row["allocation_reason"] == ""


def test_csv_changed_allocation_reflects_delta_and_reason() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=80, reason="Inactive instance")},
        limit_changes={},
    )

    output = format_analyze_csv(_report(account, result, [cfg], optimizer))
    _, rows = _parse_csv(output)
    row = rows[0]
    assert row["current_allocation"] == "100"
    assert row["new_allocation"] == "80"
    assert row["allocation_delta"] == "-20"
    assert row["allocation_reason"] == "Inactive instance"


def test_csv_unset_limit_stays_empty_when_unchanged() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_csv(_report(account, result, cfgs, optimizer))
    _, rows = _parse_csv(output)
    row = rows[0]
    assert row["current_limit"] == ""
    assert row["new_limit"] == ""
    assert row["limit_delta"] == ""


def test_csv_existing_limit_carried_forward_when_unchanged() -> None:
    inst = _make_instance(CRN_A, allocation=60, limit=3600)
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    output = format_analyze_csv(_report(account, result, [cfg], optimizer))
    _, rows = _parse_csv(output)
    row = rows[0]
    assert row["current_limit"] == "3600"
    # Unchanged: new_limit still set so it doesn't look like an unset.
    assert row["new_limit"] == "3600"
    assert row["limit_delta"] == ""


def test_csv_limit_change_from_unset_records_new_value() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={},
        limit_changes={CRN_A: LimitChange(current=None, new=7200)},
    )

    output = format_analyze_csv(_report(account, result, [cfg], optimizer))
    _, rows = _parse_csv(output)
    row = rows[0]
    assert row["current_limit"] == ""
    assert row["new_limit"] == "7200"
    assert row["limit_delta"] == ""  # delta undefined when going from unset


def test_csv_limit_change_with_existing_records_delta() -> None:
    inst = _make_instance(CRN_A, allocation=100, limit=3600)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={},
        limit_changes={CRN_A: LimitChange(current=3600, new=7200)},
    )

    output = format_analyze_csv(_report(account, result, [cfg], optimizer))
    _, rows = _parse_csv(output)
    row = rows[0]
    assert row["current_limit"] == "3600"
    assert row["new_limit"] == "7200"
    assert row["limit_delta"] == "3600"


def test_csv_no_account_level_columns_leak() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_csv(_report(account, result, cfgs, optimizer))
    headers, _ = _parse_csv(output)
    for forbidden in ("allocation_budget", "unallocated", "unmanaged_allocation"):
        assert forbidden not in headers


def test_csv_round_trips_through_dictreader() -> None:
    inst = _make_instance(CRN_A, allocation=100, consumed=10, consumed_24h=5)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    output = format_analyze_csv(_report(account, result, [cfg], optimizer))
    headers, rows = _parse_csv(output)
    assert len(rows) == 1
    assert set(rows[0].keys()) == set(headers)


def test_csv_validation_errors_not_in_body() -> None:
    """Validation errors don't leak into CSV rows — CLI logs them to stderr."""
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=200, reason="Active")},
        limit_changes={},
    )

    report = _report(account, result, [cfg], optimizer)
    assert report.validation_errors  # precondition
    output = format_analyze_csv(report)
    # Body is exactly header + one row per instance — no error block prepended/appended.
    headers, rows = _parse_csv(output)
    assert tuple(headers) == CSV_COLUMNS
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# format_analyze_json
# ---------------------------------------------------------------------------


def test_json_top_level_keys_present() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    payload = json.loads(format_analyze_json(_report(account, result, cfgs, optimizer)))
    assert set(payload.keys()) == {
        "plan",
        "account",
        "reserve",
        "validation_errors",
        "usage_floor_warnings",
        "instances",
    }


def test_json_round_trips() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    output = format_analyze_json(_report(account, result, cfgs, optimizer))
    # Must be valid JSON.
    json.loads(output)


def test_json_plan_value_is_string() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    payload = json.loads(format_analyze_json(_report(account, result, cfgs, optimizer, plan=Plan.PAYGO)))
    assert payload["plan"] == "paygo"


def test_json_account_includes_unmanaged_allocation() -> None:
    inst = _make_instance(CRN_A, allocation=60)
    # budget=100, configured=60 → unmanaged=40
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    payload = json.loads(format_analyze_json(_report(account, result, [cfg], optimizer)))
    assert payload["account"]["unmanaged_allocation_seconds"] == 40
    assert payload["account"]["allocation_budget_seconds"] == 100


def test_json_reserve_zero_when_unset() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    payload = json.loads(format_analyze_json(_report(account, result, cfgs, optimizer)))
    assert payload["reserve"]["percent"] == 0
    assert payload["reserve"]["distributable_pool_seconds"] == 0


def test_json_reserve_populated_when_set() -> None:
    inst = _make_instance(CRN_A, allocation=60)
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60, allocation_reserve_percent=10.0)
    result = optimizer.optimize()

    payload = json.loads(format_analyze_json(_report(account, result, [cfg], optimizer)))
    assert payload["reserve"]["percent"] == 10.0
    expected_pool, _ = optimizer.redistribution_pool()
    assert payload["reserve"]["distributable_pool_seconds"] == expected_pool


def test_json_unchanged_instance_keeps_current_values() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    payload = json.loads(format_analyze_json(_report(account, result, cfgs, optimizer)))
    inst = payload["instances"][0]
    assert inst["current_allocation_seconds"] == 60
    # Always emit new value, even when unchanged, so consumers don't read None as "unset".
    assert inst["new_allocation_seconds"] == 60
    assert inst["allocation_delta_seconds"] == 0
    assert inst["allocation_change_reason"] is None


def test_json_changed_allocation_records_delta_and_reason() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=80, reason="Inactive instance")},
        limit_changes={},
    )

    payload = json.loads(format_analyze_json(_report(account, result, [cfg], optimizer)))
    inst_row = payload["instances"][0]
    assert inst_row["current_allocation_seconds"] == 100
    assert inst_row["new_allocation_seconds"] == 80
    assert inst_row["allocation_delta_seconds"] == -20
    assert inst_row["allocation_change_reason"] == "Inactive instance"


def test_json_unset_limit_stays_null_when_unchanged() -> None:
    account, result, cfgs, optimizer = _no_changes_setup()
    payload = json.loads(format_analyze_json(_report(account, result, cfgs, optimizer)))
    inst = payload["instances"][0]
    assert inst["current_limit_seconds"] is None
    assert inst["new_limit_seconds"] is None
    assert inst["limit_delta_seconds"] is None


def test_json_existing_limit_carried_forward_when_unchanged() -> None:
    inst = _make_instance(CRN_A, allocation=60, limit=3600)
    account = _make_account((inst,), budget=60)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = optimizer.optimize()

    payload = json.loads(format_analyze_json(_report(account, result, [cfg], optimizer)))
    row = payload["instances"][0]
    assert row["current_limit_seconds"] == 3600
    # Always emit new value, even when unchanged, so consumers don't read None as "unset".
    assert row["new_limit_seconds"] == 3600
    assert row["limit_delta_seconds"] == 0


def test_json_limit_change_from_unset_records_new_value() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={},
        limit_changes={CRN_A: LimitChange(current=None, new=7200)},
    )

    payload = json.loads(format_analyze_json(_report(account, result, [cfg], optimizer)))
    row = payload["instances"][0]
    assert row["current_limit_seconds"] is None
    assert row["new_limit_seconds"] == 7200
    # Delta is undefined when the prior limit was unset.
    assert row["limit_delta_seconds"] is None


def test_json_limit_change_with_existing_records_delta() -> None:
    inst = _make_instance(CRN_A, allocation=100, limit=3600)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={},
        limit_changes={CRN_A: LimitChange(current=3600, new=7200)},
    )

    payload = json.loads(format_analyze_json(_report(account, result, [cfg], optimizer)))
    row = payload["instances"][0]
    assert row["current_limit_seconds"] == 3600
    assert row["new_limit_seconds"] == 7200
    assert row["limit_delta_seconds"] == 3600


def test_json_validation_errors_populated_when_invalid() -> None:
    inst = _make_instance(CRN_A, allocation=100)
    account = _make_account((inst,), budget=100)
    cfg = _make_config(CRN_A)
    optimizer = AllocationOptimizer(account, [cfg], minimum_allocation_seconds=60)
    result = OptimizationResult(
        allocation_changes={CRN_A: AllocationChange(current=100, new=200, reason="Active")},
        limit_changes={},
    )

    payload = json.loads(format_analyze_json(_report(account, result, [cfg], optimizer)))
    assert payload["validation_errors"]


def test_json_one_entry_per_instance() -> None:
    inst_a = _make_instance(CRN_A, allocation=60, name="A")
    crn_b = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:inst-b::"
    inst_b = _make_instance(crn_b, allocation=60, name="B")
    account = _make_account((inst_a, inst_b), budget=120)
    cfgs = [_make_config(CRN_A, name="A"), _make_config(crn_b, name="B")]
    optimizer = AllocationOptimizer(account, cfgs, minimum_allocation_seconds=60)
    result = optimizer.optimize()

    payload = json.loads(format_analyze_json(_report(account, result, cfgs, optimizer)))
    assert [i["name"] for i in payload["instances"]] == ["A", "B"]
