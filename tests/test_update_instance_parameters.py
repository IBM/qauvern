# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Regression tests: every PATCH must re-send both parameter fields together.

The Resource Controller replaces the whole `parameters` object on PATCH, so an update
that omits a field silently wipes it.
"""

from unittest.mock import MagicMock, patch

import pytest

from qauvern.api_client import IBMQuantumAPIClient


@pytest.fixture
def client() -> IBMQuantumAPIClient:
    with patch.object(IBMQuantumAPIClient, "_obtain_iam_token"):
        return IBMQuantumAPIClient(api_key="test-key")


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = {}
    return resp


def test_patch_carries_both_fields(client: IBMQuantumAPIClient) -> None:
    with patch.object(client.session, "request", return_value=_ok_response()) as send:
        client.update_instance_parameters(
            "crn:v1:bluemix:public:quantum-computing:us-east:a/x:y::",
            allocation_seconds=7200,
            limit_seconds=3600,
        )
    assert send.call_count == 1
    sent = send.call_args.kwargs["json"]["parameters"]
    assert sent == {
        "usage_allocation_seconds": "7200",
        "instance_limit_seconds": "3600",
    }


def test_limit_none_serializes_as_null(client: IBMQuantumAPIClient) -> None:
    """A None limit means 'no limit' — must remain JSON null, not the string 'None'."""
    with patch.object(client.session, "request", return_value=_ok_response()) as send:
        client.update_instance_parameters(
            "crn:test",
            allocation_seconds=10,
            limit_seconds=None,
        )
    sent = send.call_args.kwargs["json"]["parameters"]
    assert sent["instance_limit_seconds"] is None
