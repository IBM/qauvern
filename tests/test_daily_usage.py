# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for get_daily_usage using mock client (no live API calls)."""

from datetime import date

import pytest

from tests.mock_api import MockIBMQuantumAPIClient


@pytest.fixture
def daily_usage_client() -> MockIBMQuantumAPIClient:
    client = MockIBMQuantumAPIClient()
    client.setup_account("acct-1", allocation_budget_seconds=1000000)
    client.setup_instance("crn:test:1", "Test", 100000, account_id="acct-1")
    return client


def test_returns_empty_dict_when_no_daily_usage_set(daily_usage_client: MockIBMQuantumAPIClient) -> None:
    result = daily_usage_client.get_daily_usage(
        "crn:test:1",
        "acct-1",
        date(2026, 4, 1),
        date(2026, 4, 10),
    )
    assert result == {}


def test_returns_usage_within_date_range(daily_usage_client: MockIBMQuantumAPIClient) -> None:
    daily_usage_client.setup_daily_usage(
        "crn:test:1",
        {
            date(2026, 4, 1): 3600,
            date(2026, 4, 2): 7200,
            date(2026, 4, 5): 1800,
        },
    )
    result = daily_usage_client.get_daily_usage(
        "crn:test:1",
        "acct-1",
        date(2026, 4, 1),
        date(2026, 4, 3),
    )
    assert result == {date(2026, 4, 1): 3600, date(2026, 4, 2): 7200}


def test_end_date_is_exclusive(daily_usage_client: MockIBMQuantumAPIClient) -> None:
    daily_usage_client.setup_daily_usage(
        "crn:test:1",
        {
            date(2026, 4, 9): 5000,
            date(2026, 4, 10): 9000,
        },
    )
    result = daily_usage_client.get_daily_usage(
        "crn:test:1",
        "acct-1",
        date(2026, 4, 1),
        date(2026, 4, 10),
    )
    assert date(2026, 4, 9) in result
    assert date(2026, 4, 10) not in result


def test_excludes_days_before_start_date(daily_usage_client: MockIBMQuantumAPIClient) -> None:
    daily_usage_client.setup_daily_usage(
        "crn:test:1",
        {
            date(2026, 3, 31): 9000,
            date(2026, 4, 1): 3600,
        },
    )
    result = daily_usage_client.get_daily_usage(
        "crn:test:1",
        "acct-1",
        date(2026, 4, 1),
        date(2026, 4, 10),
    )
    assert date(2026, 3, 31) not in result
    assert date(2026, 4, 1) in result
