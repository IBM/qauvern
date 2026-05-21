# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Shared display formatters."""

import click


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


def format_fairness(fairness: float) -> str:
    """Format fairness value with color indicators."""
    if fairness < 0.5:
        return click.style(f"{fairness:.2f} ✓", fg="green")
    elif fairness < 1.0:
        return click.style(f"{fairness:.2f} ⚠", fg="yellow")
    else:
        return click.style(f"{fairness:.2f} ✗", fg="red")
