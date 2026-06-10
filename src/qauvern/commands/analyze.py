# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import csv
import io
from dataclasses import dataclass

from tabulate import tabulate

from ..formatting import format_instance_analysis_table, format_reserve_summary, format_seconds
from ..models import Account, InstanceConfig, OptimizationResult
from ..optimizer import AllocationOptimizer
from ..plan import Plan

CSV_COLUMNS: tuple[str, ...] = (
    "name",
    "crn",
    "current_allocation",
    "new_allocation",
    "allocation_delta",
    "allocation_reason",
    "current_limit",
    "new_limit",
    "limit_delta",
    "consumed_balance_period",
    "consumed_28d",
    "consumed_14d",
    "consumed_7d",
    "consumed_3d",
    "consumed_24h",
    "fairness",
    "activity_score",
)


@dataclass(frozen=True)
class AnalyzeReport:
    """Everything a formatter needs to render `analyze` output."""

    plan: Plan
    account: Account
    result: OptimizationResult
    instance_configs: tuple[InstanceConfig, ...]
    validation_errors: tuple[str, ...]
    allocation_reserve_percent: float
    redistribution_pool_seconds: int

    @classmethod
    def from_optimizer(
        cls,
        account: Account,
        result: OptimizationResult,
        plan: Plan,
        instance_configs: list[InstanceConfig],
        optimizer: AllocationOptimizer,
    ) -> "AnalyzeReport":
        _, errors = optimizer.validate_allocations(result)
        pool_seconds = 0
        if optimizer.allocation_reserve_percent > 0:
            pool_seconds, _ = optimizer.redistribution_pool()
        return cls(
            plan=plan,
            account=account,
            result=result,
            instance_configs=tuple(instance_configs),
            validation_errors=tuple(errors),
            allocation_reserve_percent=optimizer.allocation_reserve_percent,
            redistribution_pool_seconds=pool_seconds,
        )


def format_analyze_table(report: AnalyzeReport) -> str:
    """Render the report as the human-readable table (default `--format table`)."""
    account = report.account
    result = report.result
    lines: list[str] = []

    if report.validation_errors:
        lines += ["", "=" * 80, "VALIDATION ERRORS", "=" * 80]
        for error in report.validation_errors:
            lines.append(f"❌ {error}")

    total_balance_consumed = sum(inst.usage.consumed_balance_period for inst in account.instances)
    limit_str = format_seconds(account.limit_seconds) if account.limit_seconds else "Unlimited"

    lines += [
        "",
        "=" * 80,
        "ACCOUNT PLAN ALLOCATION SUMMARY",
        "=" * 80,
        f"Plan: {report.plan.value}",
        f"Allocation budget: {format_seconds(account.allocation_budget_seconds)}",
        f"Unallocated: {format_seconds(account.unallocated_seconds)}",
        f"Consumed (Balance Period, configured): {format_seconds(total_balance_consumed)}",
        f"Consumed (28-day, configured): {format_seconds(account.consumed_seconds)}",
    ]

    if account.unmanaged_allocation_seconds > 0:
        lines.append(
            f"Held by unconfigured instances: {format_seconds(account.unmanaged_allocation_seconds)} "
            "(not modified; counted against cap)"
        )

    if report.allocation_reserve_percent > 0:
        lines.append(format_reserve_summary(report.redistribution_pool_seconds, report.allocation_reserve_percent))

    lines += [
        f"Limit: {limit_str}",
        f"Configured instances analyzed: {len(report.instance_configs)}",
        "",
        "=" * 80,
        "INSTANCE ANALYSIS",
        "=" * 80,
    ]

    table_data, headers = format_instance_analysis_table(
        account.instances,
        alloc_map=result.allocation_changes,
        limit_map=result.limit_changes,
    )
    lines.append(tabulate(table_data, headers=headers, tablefmt="grid"))

    total_changes = len(result.allocation_changes) + len(result.limit_changes)
    if total_changes:
        lines += [
            "",
            f"Total changes: {total_changes} ({len(result.allocation_changes)} allocation, {len(result.limit_changes)} limit)",
            "",
            "To apply these recommendations, run: qauvern optimize",
        ]
    else:
        lines += ["", "✓ No optimization recommendations. Allocations are optimal."]

    return "\n".join(lines)


def format_analyze_csv(report: AnalyzeReport) -> str:
    """Render the report's per-instance rows as CSV.

    Account-level info is intentionally omitted — CSV is a flat row-based
    format, and consumers wanting account context should use `--format json`
    or `--format table`. Validation errors, when present, are not encoded in
    the CSV body (the CLI logs them to stderr instead).
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()

    alloc_map = report.result.allocation_changes
    limit_map = report.result.limit_changes
    for inst in report.account.instances:
        alloc = alloc_map.get(inst.crn)
        limit_rec = limit_map.get(inst.crn)

        new_allocation = alloc.new if alloc is not None else inst.allocation_seconds
        allocation_delta = new_allocation - inst.allocation_seconds

        new_limit = limit_rec.new if limit_rec is not None else inst.limit_seconds
        if limit_rec is not None and inst.limit_seconds is not None:
            limit_delta: int | str = limit_rec.new - inst.limit_seconds
        else:
            limit_delta = ""

        writer.writerow(
            {
                "name": inst.name,
                "crn": inst.crn,
                "current_allocation": inst.allocation_seconds,
                "new_allocation": new_allocation,
                "allocation_delta": allocation_delta,
                "allocation_reason": alloc.reason if alloc is not None else "",
                "current_limit": inst.limit_seconds if inst.limit_seconds is not None else "",
                "new_limit": new_limit if new_limit is not None else "",
                "limit_delta": limit_delta,
                "consumed_balance_period": inst.usage.consumed_balance_period,
                "consumed_28d": inst.consumed_seconds,
                "consumed_14d": inst.usage.consumed_14day,
                "consumed_7d": inst.usage.consumed_7day,
                "consumed_3d": inst.usage.consumed_3day,
                "consumed_24h": inst.usage.consumed_24h,
                "fairness": f"{inst.fairness:.6f}",
                "activity_score": f"{inst.activity_score:.6f}",
            }
        )
    return buf.getvalue()
