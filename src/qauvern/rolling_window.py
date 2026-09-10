# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Shared constants and predicates for IQP's 28-day rolling usage window.

We deliberately use 29 calendar dates for a "28-day window": `window_start(today)
<= day <= today` is inclusive on both ends. Whole-day buckets can't represent
a sub-day window boundary exactly, so we round in the generous
direction rather than the stingy one, since this module backs logic (net-grant
carryover, rolloff) whose purpose is to avoid under-crediting users.
"""

from collections.abc import Iterable
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import NetGrant

ROLLING_WINDOW_DAYS = 28

# Floor for how far back to fetch per-day usage from the API. Grants can need more;
# `daily_usage_lookback_days` works out how much.
DAILY_USAGE_LOOKBACK_DAYS = 60


def window_start(today: date) -> date:
    """Return the oldest date still inside the rolling window ending on `today`."""
    return today - timedelta(days=ROLLING_WINDOW_DAYS)


def grant_still_credits(grant: "NetGrant", today: date) -> bool:
    """Return whether `grant` can still credit usage inside today's rolling window.

    True as soon as the grant has started, and until its end date has fully
    rolled out of the window — i.e. until `end_date` is on or before
    `window_start(today)`.
    """
    return grant.start_date.date() <= today and grant.end_date.date() > window_start(today)


def grant_removable_on(end_date: date) -> date:
    """Return the first date on which a grant ending on `end_date` can no longer credit.

    That is the first `today` for which `grant_still_credits(grant, today)`
    is false, i.e. `end_date + ROLLING_WINDOW_DAYS`.
    """
    return end_date + timedelta(days=ROLLING_WINDOW_DAYS)


def daily_usage_lookback_days(grants: Iterable["NetGrant"], today: date) -> int:
    """Return how many days of per-day usage `attribute_usage` needs for `grants`.

    Budgets are consumed over a grant's full active period, so attribution needs
    every day back to a still-crediting grant's `start_date` — a 90-day grant needs
    ~117 days. A missing day makes spent budget look unspent and inflates the limit,
    so this follows the longest grant rather than a fixed constant. Never returns
    less than `DAILY_USAGE_LOOKBACK_DAYS`.
    """
    starts = [(today - grant.start_date.date()).days for grant in grants if grant_still_credits(grant, today)]
    return max([DAILY_USAGE_LOOKBACK_DAYS, *starts])
