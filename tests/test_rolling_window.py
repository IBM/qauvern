# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for the shared rolling-window predicates.

The window convention is inclusive on both ends: window_start(today) <= day
<= today spans 29 calendar dates for a "28-day window".
"""

from datetime import date, datetime, timedelta, timezone

from qauvern.models import NetGrant
from qauvern.rolling_window import (
    DAILY_USAGE_LOOKBACK_DAYS,
    ROLLING_WINDOW_DAYS,
    daily_usage_lookback_days,
    grant_removable_on,
    grant_still_credits,
    window_start,
)


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def make_grant(start: date, end: date, net_grant_seconds: int = 100) -> NetGrant:
    return NetGrant(start_date=_dt(start), net_grant_seconds=net_grant_seconds, end_date=_dt(end))


# -------------------------------------------------------------------
# window_start
# -------------------------------------------------------------------


def test_window_start_is_28_days_before_today() -> None:
    assert window_start(date(2026, 3, 29)) == date(2026, 3, 1)


def test_window_spans_29_inclusive_dates() -> None:
    today = date(2026, 3, 29)
    assert (today - window_start(today)).days + 1 == 29


# -------------------------------------------------------------------
# grant_still_credits
# -------------------------------------------------------------------


def test_grant_credits_while_active() -> None:
    grant = make_grant(date(2026, 3, 1), date(2026, 3, 29))
    assert grant_still_credits(grant, date(2026, 3, 15)) is True


def test_grant_does_not_credit_before_it_starts() -> None:
    grant = make_grant(date(2026, 3, 1), date(2026, 3, 29))
    assert grant_still_credits(grant, date(2026, 2, 28)) is False


def test_grant_starting_today_credits_today() -> None:
    grant = make_grant(date(2026, 3, 1), date(2026, 3, 29))
    assert grant_still_credits(grant, date(2026, 3, 1)) is True


def test_expired_grant_still_credits_inside_window() -> None:
    grant = make_grant(date(2026, 3, 1), date(2026, 3, 29))
    # window_start(2026-04-01) == 2026-03-04, which is before end_date -> still credits.
    assert grant_still_credits(grant, date(2026, 4, 1)) is True


def test_expired_grant_stops_crediting_once_end_date_rolls_out_of_window() -> None:
    grant = make_grant(date(2026, 3, 1), date(2026, 3, 29))
    boundary_today = date(2026, 3, 29) + timedelta(days=ROLLING_WINDOW_DAYS)
    assert grant_still_credits(grant, boundary_today) is False
    assert grant_still_credits(grant, boundary_today - timedelta(days=1)) is True


def test_degenerate_zero_length_grant_still_counts_as_relevant() -> None:
    """A start == end grant never has an active day, but that's attribute_usage's
    concern (it will credit 0 usage) — grant_still_credits only checks whether the
    grant has started and hasn't yet rolled out of the window."""
    grant = make_grant(date(2026, 3, 1), date(2026, 3, 1))
    assert grant_still_credits(grant, date(2026, 3, 1)) is True


# -------------------------------------------------------------------
# grant_removable_on
# -------------------------------------------------------------------


def test_grant_removable_on_is_end_date_plus_window() -> None:
    grant = make_grant(date(2026, 3, 1), date(2026, 3, 29))
    assert grant_removable_on(grant.end_date.date()) == date(2026, 3, 29) + timedelta(days=ROLLING_WINDOW_DAYS)


def test_grant_removable_on_agrees_with_grant_still_credits() -> None:
    """grant_removable_on must be the exact first date grant_still_credits flips to False."""
    grant = make_grant(date(2026, 3, 1), date(2026, 3, 29))
    prune_on = grant_removable_on(grant.end_date.date())
    assert grant_still_credits(grant, prune_on) is False
    assert grant_still_credits(grant, prune_on - timedelta(days=1)) is True


# ---------------------------------------------------------------------------
# daily_usage_lookback_days
# ---------------------------------------------------------------------------


def _grant(start: date, end: date, seconds: int = 1000) -> NetGrant:
    return NetGrant(
        start_date=datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
        net_grant_seconds=seconds,
        end_date=datetime(end.year, end.month, end.day, tzinfo=timezone.utc),
    )


def test_lookback_falls_back_to_the_constant_without_grants() -> None:
    assert daily_usage_lookback_days([], date(2026, 6, 1)) == DAILY_USAGE_LOOKBACK_DAYS


def test_lookback_ignores_short_grants_already_covered_by_the_constant() -> None:
    """A default 28-day grant needs 55 days at its last crediting day — under the floor."""
    end = date(2026, 4, 1)
    grant = _grant(end - timedelta(days=ROLLING_WINDOW_DAYS), end)
    today = grant_removable_on(end) - timedelta(days=1)
    assert (today - grant.start_date.date()).days == 55
    assert daily_usage_lookback_days([grant], today) == DAILY_USAGE_LOOKBACK_DAYS


def test_lookback_reaches_back_to_a_long_grants_start_date() -> None:
    """A 90-day grant needs its whole active period, which the constant does not cover."""
    start, end = date(2026, 3, 1), date(2026, 5, 30)
    grant = _grant(start, end)
    today = grant_removable_on(end) - timedelta(days=1)
    assert daily_usage_lookback_days([grant], today) == (today - start).days
    assert daily_usage_lookback_days([grant], today) > DAILY_USAGE_LOOKBACK_DAYS


def test_lookback_follows_the_earliest_still_crediting_grant() -> None:
    today = date(2026, 6, 1)
    long_grant = _grant(date(2026, 2, 1), date(2026, 6, 15))
    short_grant = _grant(date(2026, 5, 20), date(2026, 6, 17))
    assert daily_usage_lookback_days([short_grant, long_grant], today) == (today - date(2026, 2, 1)).days


def test_lookback_ignores_grants_that_can_no_longer_credit() -> None:
    """A rolled-off grant is excluded from attribution, so its history is not needed."""
    today = date(2026, 9, 1)
    rolled_off = _grant(date(2026, 1, 1), date(2026, 3, 1))
    assert not grant_still_credits(rolled_off, today)
    assert daily_usage_lookback_days([rolled_off], today) == DAILY_USAGE_LOOKBACK_DAYS
