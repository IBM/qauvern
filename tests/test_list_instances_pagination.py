# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for list_instances pagination in IBMQuantumAPIClient."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from qauvern.api_client import IBMQuantumAPIClient


def _make_response(resources: list[dict], next_url: str | None = None) -> MagicMock:
    """Build a mock requests.Response for a Resource Controller list page."""
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    body: dict = {"resources": resources}
    if next_url:
        body["next_url"] = next_url
    resp.json.return_value = body
    return resp


def _resource(crn: str, name: str, account_id: str = "acct-1") -> dict:
    return {"id": crn, "name": name, "account_id": account_id}


@pytest.fixture
def client() -> IBMQuantumAPIClient:
    with patch.object(IBMQuantumAPIClient, "_obtain_iam_token"):
        return IBMQuantumAPIClient(api_key="test-key")


def test_single_page_no_next_url(client: IBMQuantumAPIClient) -> None:
    """Single page with no next_url returns all instances on that page."""
    page = _make_response(
        [
            _resource("crn:1", "inst-1"),
            _resource("crn:2", "inst-2"),
        ]
    )
    with patch.object(client.session, "get", return_value=page) as mock_get:
        result = client.list_instances("acct-1")
    assert len(result) == 2
    assert result[0].crn == "crn:1"
    assert result[1].crn == "crn:2"
    assert mock_get.call_count == 1


def test_two_pages(client: IBMQuantumAPIClient) -> None:
    """Two pages are both fetched and combined."""
    page1 = _make_response(
        [_resource("crn:1", "inst-1")],
        next_url="https://resource-controller.cloud.ibm.com/v2/resource_instances?start=token-abc",
    )
    page2 = _make_response([_resource("crn:2", "inst-2")])
    with patch.object(client.session, "get", side_effect=[page1, page2]) as mock_get:
        result = client.list_instances("acct-1")
    assert len(result) == 2
    assert [i.crn for i in result] == ["crn:1", "crn:2"]
    assert mock_get.call_count == 2
    # Second call must include the start token
    assert mock_get.call_args_list[1].kwargs["params"]["start"] == "token-abc"


def test_three_pages(client: IBMQuantumAPIClient) -> None:
    """Three pages are fully traversed."""
    page1 = _make_response(
        [_resource("crn:1", "inst-1")],
        next_url="https://resource-controller.cloud.ibm.com/v2/resource_instances?start=tok-1",
    )
    page2 = _make_response(
        [_resource("crn:2", "inst-2")],
        next_url="https://resource-controller.cloud.ibm.com/v2/resource_instances?start=tok-2",
    )
    page3 = _make_response([_resource("crn:3", "inst-3")])
    with patch.object(client.session, "get", side_effect=[page1, page2, page3]) as mock_get:
        result = client.list_instances("acct-1")
    assert len(result) == 3
    assert mock_get.call_count == 3


def test_http_error_on_second_page_raises(client: IBMQuantumAPIClient) -> None:
    """An HTTP error on the second page raises immediately, not a partial list."""
    page1 = _make_response(
        [_resource("crn:1", "inst-1")],
        next_url="https://resource-controller.cloud.ibm.com/v2/resource_instances?start=tok-x",
    )
    error_resp = MagicMock()
    error_resp.ok = False
    error_resp.status_code = 500
    error_resp.json.return_value = {"message": "internal error"}
    with patch.object(client.session, "get", side_effect=[page1, error_resp]):
        with pytest.raises(requests.HTTPError):
            client.list_instances("acct-1")


def test_account_id_filter_across_pages(client: IBMQuantumAPIClient) -> None:
    """Account ID is passed as server-side filter param."""
    page1 = _make_response(
        [
            _resource("crn:1", "mine", account_id="acct-1"),
            _resource("crn:2", "also-mine", account_id="acct-1"),
        ],
        next_url="https://resource-controller.cloud.ibm.com/v2/resource_instances?start=tok-y&account_id=acct-1",
    )
    page2 = _make_response(
        [
            _resource("crn:3", "third-mine", account_id="acct-1"),
        ]
    )
    with patch.object(client.session, "get", side_effect=[page1, page2]) as mock_get:
        result = client.list_instances("acct-1")
    assert len(result) == 3
    assert {i.crn for i in result} == {"crn:1", "crn:2", "crn:3"}
    # Verify account_id was passed in params on first call
    assert mock_get.call_args_list[0].kwargs["params"]["account_id"] == "acct-1"
    # Verify account_id was passed in params on second call
    assert mock_get.call_args_list[1].kwargs["params"]["account_id"] == "acct-1"
