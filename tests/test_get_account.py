# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for IBMQuantumAPIClient.get_account and get_plan_usage_seconds."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from qauvern.api_client import IBMQuantumAPIClient
from qauvern.plan import Plan


def _make_response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = body
    return resp


@pytest.fixture
def client() -> IBMQuantumAPIClient:
    with patch.object(IBMQuantumAPIClient, "_obtain_iam_token"):
        return IBMQuantumAPIClient(api_key="test-key")  # pragma: allowlist-secret


def test_get_plan_usage_seconds_filters_by_plan_not_instance(client: IBMQuantumAPIClient) -> None:
    end = datetime(2026, 3, 29, tzinfo=timezone.utc)
    start = end - timedelta(days=28)
    with patch.object(client.session, "request", return_value=_make_response({"usage": 1_234_567})) as req:
        assert client.get_plan_usage_seconds(Plan.PREMIUM, start, end, "acct-1") == 1234

    (method, url), kwargs = req.call_args
    assert (method, url) == ("GET", "https://quantum.cloud.ibm.com/api/v1/analytics/usage")
    assert kwargs["params"] == {
        "plan": "premium",
        "interval_start": start.isoformat(),
        "interval_end": end.isoformat(),
    }
    assert kwargs["headers"] == {"Account-Id": "acct-1"}


def test_get_account_populates_plan_wide_consumed_seconds(client: IBMQuantumAPIClient) -> None:
    account_body = {
        "plans": [{"usage_allocation_seconds": 1000, "unallocated_usage_seconds": 100, "usage_limit_seconds": None}]
    }
    responses = [_make_response(account_body), _make_response({"usage": 1_200_000})]
    with patch.object(client.session, "request", side_effect=responses) as req:
        account = client.get_account("acct-1", Plan.PAYGO, [])

    assert account.consumed_seconds == 1200
    assert account.configured_consumed_seconds == 0
    params = req.call_args_list[1].kwargs["params"]
    window = datetime.fromisoformat(params["interval_end"]) - datetime.fromisoformat(params["interval_start"])
    assert window == timedelta(days=28)
