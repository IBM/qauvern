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
import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from tabulate import tabulate

from ..formatting import format_instance_analysis_table, format_reserve_summary, format_seconds
from ..limit_resolver import LimitBreakdown
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
    "limit_base",
    "limit_grant_funded",
    "limit_unspent_grant",
    "limit_overage",
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
    usage_floor_warnings: tuple[str, ...]
    allocation_reserve_percent: float
    redistribution_pool_seconds: int
    limit_breakdowns: dict[str, LimitBreakdown | None]
    preview_date: date

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
        warnings = optimizer.usage_floor_warnings(result)
        pool_seconds = 0
        if optimizer.allocation_reserve_percent > 0:
            pool_seconds, _ = optimizer.redistribution_pool()
        return cls(
            plan=plan,
            account=account,
            result=result,
            instance_configs=tuple(instance_configs),
            validation_errors=tuple(errors),
            usage_floor_warnings=tuple(warnings),
            allocation_reserve_percent=optimizer.allocation_reserve_percent,
            redistribution_pool_seconds=pool_seconds,
            limit_breakdowns=optimizer.limit_breakdowns,
            preview_date=optimizer.today,
        )


def _shaped_by_grant(breakdown: LimitBreakdown) -> bool:
    """Whether any net-grant term contributes to this instance's effective limit."""
    return (
        breakdown.grant_funded_seconds > 0
        or breakdown.unspent_grant_seconds > 0
        or breakdown.pre_boost_overage_seconds > 0
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

    if report.usage_floor_warnings:
        lines += ["", "=" * 80, "WARNINGS", "=" * 80]
        for warning in report.usage_floor_warnings:
            lines.append(f"⚠ {warning}")

    limit_str = format_seconds(account.limit_seconds) if account.limit_seconds else "Unlimited"

    lines += [
        "",
        "=" * 80,
        "ACCOUNT PLAN ALLOCATION SUMMARY",
        "=" * 80,
        f"Plan: {report.plan.value}",
        f"Preview date: {report.preview_date.isoformat()}",
        f"Allocation budget: {format_seconds(account.allocation_budget_seconds)}",
        f"Unallocated: {format_seconds(account.unallocated_seconds)}",
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

    lines += _format_limit_breakdown_section(report)

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


def _breakdown_payload(breakdown: LimitBreakdown | None) -> dict[str, Any] | None:
    """JSON view of an effective-limit breakdown; None when the config sets no limit."""
    if breakdown is None:
        return None
    return {
        "base_seconds": breakdown.base_seconds,
        "grant_funded_seconds": breakdown.grant_funded_seconds,
        "unspent_grant_seconds": breakdown.unspent_grant_seconds,
        "pre_boost_overage_seconds": breakdown.pre_boost_overage_seconds,
        "boost_start_date": breakdown.boost_start_date.isoformat() if breakdown.boost_start_date else None,
        "total_seconds": breakdown.total,
    }


def _format_limit_breakdown_section(report: AnalyzeReport) -> list[str]:
    """Render the per-instance effective-limit terms, or nothing when no grant applies.

    Instances with no grant shaping their limit are skipped: their effective limit is
    just `limit_seconds`, already in the instance table.
    """
    rows: list[list[str]] = []
    for inst in report.account.instances:
        breakdown = report.limit_breakdowns.get(inst.crn)
        if breakdown is None or not _shaped_by_grant(breakdown):
            continue
        rows.append(
            [
                inst.name,
                format_seconds(breakdown.base_seconds),
                format_seconds(breakdown.grant_funded_seconds),
                format_seconds(breakdown.unspent_grant_seconds),
                format_seconds(breakdown.pre_boost_overage_seconds),
                format_seconds(breakdown.total),
            ]
        )
    if not rows:
        return []

    headers = ["Instance", "Base", "Grant-funded usage", "Unspent grant", "Pre-boost overage", "Effective limit"]
    return [
        "",
        "=" * 80,
        "LIMIT BREAKDOWN",
        "=" * 80,
        tabulate(rows, headers=headers, tablefmt="grid"),
        "Grant-funded usage keeps the minutes a grant paid for out of the base limit, and",
        "decays as they leave the 28-day window. Unspent grant is forfeit at end_date.",
    ]


def format_analyze_json(report: AnalyzeReport) -> str:
    """Render the report as structured JSON for scripts.

    Includes account-level info plus per-instance rows with pre-computed
    deltas for allocations and limits. The new allocation/limit value is
    always emitted (equal to the current value when unchanged) so consumers
    don't read a missing field as "unset".
    """
    account = report.account
    alloc_map = report.result.allocation_changes
    limit_map = report.result.limit_changes

    instances: list[dict[str, Any]] = []
    for inst in account.instances:
        alloc = alloc_map.get(inst.crn)
        limit_rec = limit_map.get(inst.crn)

        new_allocation = alloc.new if alloc is not None else inst.allocation_seconds
        new_limit = limit_rec.new if limit_rec is not None else inst.limit_seconds
        limit_delta: int | None = (
            new_limit - inst.limit_seconds if new_limit is not None and inst.limit_seconds is not None else None
        )
        fairness = inst.fairness
        if not math.isfinite(fairness):
            fairness = None

        instances.append(
            {
                "name": inst.name,
                "crn": inst.crn,
                "current_allocation_seconds": inst.allocation_seconds,
                "new_allocation_seconds": new_allocation,
                "allocation_delta_seconds": new_allocation - inst.allocation_seconds,
                "allocation_change_reason": alloc.reason if alloc is not None else None,
                "current_limit_seconds": inst.limit_seconds,
                "new_limit_seconds": new_limit,
                "limit_delta_seconds": limit_delta,
                "consumed_28day_seconds": inst.consumed_seconds,
                "consumed_14day_seconds": inst.usage.consumed_14day,
                "consumed_7day_seconds": inst.usage.consumed_7day,
                "consumed_3day_seconds": inst.usage.consumed_3day,
                "consumed_24h_seconds": inst.usage.consumed_24h,
                "fairness": fairness,
                "activity_score": inst.activity_score,
                "limit_breakdown": _breakdown_payload(report.limit_breakdowns.get(inst.crn)),
            }
        )

    payload = {
        "plan": report.plan.value,
        "preview_date": report.preview_date.isoformat(),
        "account": {
            "account_id": account.account_id,
            "allocation_budget_seconds": account.allocation_budget_seconds,
            "unallocated_seconds": account.unallocated_seconds,
            "consumed_seconds": account.consumed_seconds,
            "limit_seconds": account.limit_seconds,
            "unmanaged_allocation_seconds": account.unmanaged_allocation_seconds,
        },
        "reserve": {
            "percent": report.allocation_reserve_percent,
            "distributable_pool_seconds": report.redistribution_pool_seconds,
        },
        "validation_errors": list(report.validation_errors),
        "usage_floor_warnings": list(report.usage_floor_warnings),
        "instances": instances,
    }
    return json.dumps(payload, indent=2)


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
        breakdown = report.limit_breakdowns.get(inst.crn)
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
                "consumed_28d": inst.consumed_seconds,
                "consumed_14d": inst.usage.consumed_14day,
                "consumed_7d": inst.usage.consumed_7day,
                "consumed_3d": inst.usage.consumed_3day,
                "consumed_24h": inst.usage.consumed_24h,
                "fairness": f"{inst.fairness:.6f}",
                "activity_score": f"{inst.activity_score:.6f}",
                "limit_base": breakdown.base_seconds if breakdown is not None else "",
                "limit_grant_funded": breakdown.grant_funded_seconds if breakdown is not None else "",
                "limit_unspent_grant": breakdown.unspent_grant_seconds if breakdown is not None else "",
                "limit_overage": breakdown.pre_boost_overage_seconds if breakdown is not None else "",
            }
        )
    return buf.getvalue()
