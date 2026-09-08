# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for the mock API client itself, not application code."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from qauvern.cli import main
from tests.mock_api import MockIBMQuantumAPIClient

CRN = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:inst-a::"


@pytest.fixture
def client() -> MockIBMQuantumAPIClient:
    client = MockIBMQuantumAPIClient()
    client.setup_account("acct-1", allocation_budget_seconds=1000000)
    return client


# -------------------------------------------------------------------
# setup_instance's detailed-usage support
# -------------------------------------------------------------------


def test_setup_instance_detailed_usage_defaults_to_zero(client: MockIBMQuantumAPIClient) -> None:
    instance = client.setup_instance(CRN, "Test", allocation_seconds=100000, account_id="acct-1")

    assert instance.usage.consumed_14day == 0
    assert instance.usage.consumed_7day == 0
    assert instance.usage.consumed_3day == 0
    assert instance.usage.consumed_24h == 0
    assert instance.usage.daily_usage == {}


def test_setup_instance_detailed_usage_is_configurable(client: MockIBMQuantumAPIClient) -> None:
    instance = client.setup_instance(
        CRN,
        "Test",
        allocation_seconds=100000,
        account_id="acct-1",
        consumed_14day=1400,
        consumed_7day=700,
        consumed_3day=300,
        consumed_24h=100,
        daily_usage={date(2026, 4, 1): 3600},
    )

    assert instance.usage.consumed_14day == 1400
    assert instance.usage.consumed_7day == 700
    assert instance.usage.consumed_3day == 300
    assert instance.usage.consumed_24h == 100
    assert instance.usage.daily_usage == {date(2026, 4, 1): 3600}


def test_setup_instance_daily_usage_agrees_with_get_daily_usage(client: MockIBMQuantumAPIClient) -> None:
    client.setup_instance(
        CRN, "Test", allocation_seconds=100000, account_id="acct-1", daily_usage={date(2026, 4, 1): 3600}
    )

    assert client.get_daily_usage(CRN, "acct-1", date(2026, 3, 1), date(2026, 5, 1)) == {date(2026, 4, 1): 3600}


# -------------------------------------------------------------------
# get_detailed_usage
# -------------------------------------------------------------------


def test_get_detailed_usage_reflects_setup_instance(client: MockIBMQuantumAPIClient) -> None:
    client.setup_instance(
        CRN,
        "Test",
        allocation_seconds=100000,
        account_id="acct-1",
        consumed_14day=1400,
        consumed_7day=700,
        consumed_3day=300,
        consumed_24h=100,
    )

    assert client.get_detailed_usage(CRN, "acct-1") == {
        "consumed_14day": 1400,
        "consumed_7day": 700,
        "consumed_3day": 300,
        "consumed_24h": 100,
    }


def test_get_detailed_usage_raises_for_unknown_instance(client: MockIBMQuantumAPIClient) -> None:
    with pytest.raises(ValueError, match="not found"):
        client.get_detailed_usage("crn:test:missing", "acct-1")


# -------------------------------------------------------------------
# End-to-end: the CLI's usage-enrichment flow against the mock, no patching
# -------------------------------------------------------------------


def test_analyze_enriches_usage_against_mock_without_patching(client: MockIBMQuantumAPIClient, tmp_path: Path) -> None:
    yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).date()
    client.setup_instance(
        CRN,
        "Instance A",
        allocation_seconds=100000,
        account_id="acct-1",
        consumed_14day=1400,
        consumed_7day=700,
        consumed_3day=300,
        consumed_24h=100,
        daily_usage={yesterday: 3600},
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""\
account_id: acct-1
plan: internal
minimum_allocation_seconds: 60
instances:
  - name: Instance A
    crn: '{CRN}'
""")

    with patch("qauvern.cli.IBMQuantumAPIClient", return_value=client):
        runner = CliRunner()
        result = runner.invoke(main, ["analyze", "--config", str(config_path), "--api-key", "k"])

    assert result.exit_code == 0, result.output
    assert client.instances[CRN].usage.consumed_14day == 1400
