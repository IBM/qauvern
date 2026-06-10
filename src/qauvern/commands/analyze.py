# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

from dataclasses import dataclass

from tabulate import tabulate

from ..formatting import format_instance_analysis_table, format_reserve_summary, format_seconds
from ..models import Account, InstanceConfig, OptimizationResult
from ..optimizer import AllocationOptimizer
from ..plan import Plan


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
