# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Display formatters and table builders for the CLI."""

from collections.abc import Sequence

import click

from .models import (
    AllocationChange,
    InstanceState,
    LimitChange,
)


def format_seconds(seconds: int) -> str:
    """Format seconds into a human-readable string."""
    seconds = abs(seconds)
    hours = seconds / 3600
    if hours < 1:
        return f"{seconds}s"
    elif hours < 24:
        return f"{hours:.1f}h"
    else:
        days = hours / 24
        return f"{days:.1f}d"


def format_optional_seconds(seconds: int | None) -> str:
    """Format a seconds value, returning '-' when None."""
    if seconds is None:
        return "-"
    return format_seconds(seconds)


def format_fairness(fairness: float) -> str:
    """Format fairness value with color indicators."""
    if fairness < 0.5:
        return click.style(f"{fairness:.2f} ✓", fg="green")
    elif fairness < 1.0:
        return click.style(f"{fairness:.2f} ⚠", fg="yellow")
    else:
        return click.style(f"{fairness:.2f} ✗", fg="red")


def format_reserve_summary(distributable_pool: int, reserve_percent: float) -> str:
    """Format account reserve summary line.

    `distributable_pool` is the post-reserve seconds available to redistribute
    (AllocationOptimizer.redistribution_pool()[0]) — i.e. the raw pool already
    scaled by `1 - reserve_percent/100`.
    """
    return f"Reserve: {reserve_percent:.1f}%   Available for rebalancing: {format_seconds(distributable_pool)}"


def parse_seconds(value: str) -> int:
    """Parse a human-friendly time string into seconds.

    Accepts plain integers (as seconds), suffixed values (10h, 30m, 2.5d, 96000s),
    or QAU units (1qau = 96000 seconds).
    """
    value = value.strip().lower()

    try:
        return int(value)
    except ValueError:
        pass

    suffixes = {
        "qau": 96000,
        "d": 86400,
        "h": 3600,
        "m": 60,
        "s": 1,
    }

    for suffix, multiplier in suffixes.items():
        if value.endswith(suffix):
            numeric_part = value[: -len(suffix)]
            try:
                return int(float(numeric_part) * multiplier)
            except ValueError:
                pass

    raise click.BadParameter(
        f"Cannot parse '{value}' as a time duration. Use plain seconds, or a suffix: 30m, 10h, 2.5d, 1qau"
    )


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[:max_len]


def format_instance_summary_table(
    instances: Sequence[InstanceState],
) -> tuple[list[list[str]], list[str]]:
    """Build the summary table used by `show` and `instances`.

    Columns: Instance, Allocation, Consumed, Utilization, Limit, Fairness.
    """
    headers = ["Instance", "Allocation", "Consumed", "Utilization", "Limit", "Fairness"]
    rows = []
    for inst in instances:
        if inst.allocation_seconds > 0:
            util = (inst.consumed_seconds / inst.allocation_seconds) * 100
            util_str = f"{util:.1f}%"
        else:
            util_str = "0.0%"
        rows.append(
            [
                _truncate(inst.name, 35),
                format_seconds(inst.allocation_seconds),
                format_seconds(inst.consumed_seconds),
                util_str,
                format_optional_seconds(inst.limit_seconds),
                format_fairness(inst.fairness),
            ]
        )
    return rows, headers


def _format_change_arrow(current: int | None, new: int | None) -> str:
    """Render a value transition as `cur → new (±delta)` or just `cur` when unchanged."""
    if current == new:
        return format_optional_seconds(current)
    arrow = f"{format_optional_seconds(current)} → {format_optional_seconds(new)}"
    if current is None or new is None:
        return arrow
    delta = new - current
    sign = "+" if delta > 0 else "-"
    return f"{arrow} ({sign}{format_seconds(delta)})"


def format_instance_analysis_table(
    instances: Sequence[InstanceState],
    alloc_map: dict[str, AllocationChange] | None = None,
    limit_map: dict[str, LimitChange] | None = None,
    include_usage: bool = True,
) -> tuple[list[list[str]], list[str]]:
    """Build the per-instance analysis/changes table."""
    headers = ["Instance"]
    if include_usage:
        headers += ["Period", "28d", "14d", "7d", "3d", "24h"]
    headers += ["Allocation", "Limit", "Reason"]
    alloc_map = alloc_map or {}
    limit_map = limit_map or {}

    rows = []
    for inst in instances:
        alloc = alloc_map.get(inst.crn)
        limit_rec = limit_map.get(inst.crn)

        new_alloc_seconds = alloc.new if alloc is not None else inst.allocation_seconds
        allocation = _format_change_arrow(inst.allocation_seconds, new_alloc_seconds)

        new_limit_seconds = limit_rec.new if limit_rec is not None else inst.limit_seconds
        limit = _format_change_arrow(inst.limit_seconds, new_limit_seconds)

        if alloc is not None:
            reason = _truncate(alloc.reason, 60)
        elif limit_rec is not None:
            reason = _truncate(limit_rec.reason, 60)
        else:
            reason = "No change"

        row = [_truncate(inst.name, 35)]
        if include_usage:
            row += [
                format_seconds(inst.usage.consumed_balance_period),
                format_seconds(inst.consumed_seconds),
                format_seconds(inst.usage.consumed_14day),
                format_seconds(inst.usage.consumed_7day),
                format_seconds(inst.usage.consumed_3day),
                format_seconds(inst.usage.consumed_24h),
            ]
        row += [allocation, limit, reason]
        rows.append(row)
    return rows, headers
