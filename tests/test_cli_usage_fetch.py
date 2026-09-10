# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests that usage-fetch failures abort `analyze`/`optimize` instead of degrading silently."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from qauvern.cli import main
from qauvern.rolling_window import DAILY_USAGE_LOOKBACK_DAYS
from tests.mock_api import MockIBMQuantumAPIClient

CRN_A = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:inst-a::"
CRN_B = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:inst-b::"

CONFIG_TEXT = f"""\
account_id: acct-1
plan: internal
minimum_allocation_seconds: 60
instances:
  - name: Instance A
    crn: '{CRN_A}'
  - name: Instance B
    crn: '{CRN_B}'
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_client_with_instances() -> MockIBMQuantumAPIClient:
    client = MockIBMQuantumAPIClient()
    client.setup_account(account_id="acct-1", allocation_budget_seconds=200000)
    client.setup_instance(crn=CRN_A, name="Instance A", allocation_seconds=100000, account_id="acct-1")
    client.setup_instance(crn=CRN_B, name="Instance B", allocation_seconds=100000, account_id="acct-1")
    return client


def _invoke(runner: CliRunner, client: MockIBMQuantumAPIClient, command: str, config_path: Path) -> Result:
    args = [command, "--config", str(config_path), "--api-key", "k"]
    if command == "optimize":
        args.append("-y")
    with patch("qauvern.cli.IBMQuantumAPIClient", return_value=client):
        return runner.invoke(main, args)


@pytest.mark.parametrize("command", ["analyze", "optimize"])
def test_usage_fetch_failure_aborts_run(runner: CliRunner, tmp_path: Path, command: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT)

    client = _mock_client_with_instances()
    with patch.object(client, "get_detailed_usage", side_effect=RuntimeError("boom")):
        result = _invoke(runner, client, command, config_path)

    assert result.exit_code != 0
    assert "Instance A" in result.output
    assert "boom" in result.output
    # No allocation changes were applied to either instance.
    assert client.instances[CRN_A].allocation_seconds == 100000
    assert client.instances[CRN_B].allocation_seconds == 100000


@pytest.mark.parametrize("command", ["analyze", "optimize"])
def test_daily_usage_fetch_failure_aborts_run(runner: CliRunner, tmp_path: Path, command: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEXT)

    client = _mock_client_with_instances()
    detailed = {"consumed_14day": 0, "consumed_7day": 0, "consumed_3day": 0, "consumed_24h": 0}
    with (
        patch.object(client, "get_detailed_usage", return_value=detailed),
        patch.object(client, "get_daily_usage", side_effect=RuntimeError("daily boom")),
    ):
        result = _invoke(runner, client, command, config_path)

    assert result.exit_code != 0
    assert "daily boom" in result.output


# ---------------------------------------------------------------------------
# Per-day usage lookback follows the configured grants
# ---------------------------------------------------------------------------


def test_daily_usage_lookback_widens_for_a_long_grant(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    """A 90-day grant must be fetched back to its start_date, not just the 60-day floor.

    A day missing from `daily_usage` reads as unspent budget and inflates the limit.
    Dates are relative to today so the grant stays in its post-expiry crediting tail.
    """
    today = datetime.now(timezone.utc).date()
    grant_start = today - timedelta(days=100)
    grant_end = today - timedelta(days=10)  # expired, but still inside the window
    config_text = f"""\
account_id: acct-1
plan: internal
minimum_allocation_seconds: 60
instances:
  - name: Instance A
    crn: '{CRN_A}'
    limit_seconds: 1000
    net_grants:
      - start_date: '{grant_start.isoformat()}T00:00:00+00:00'
        end_date: '{grant_end.isoformat()}T00:00:00+00:00'
        net_grant_seconds: 5000
  - name: Instance B
    crn: '{CRN_B}'
"""

    client = _mock_client_with_instances()
    requested: dict[str, date] = {}
    original = client.get_daily_usage

    def _record(instance_crn: str, account_id: str, start_date: date, end_date: date) -> dict[date, int]:
        requested[instance_crn] = start_date
        return original(instance_crn, account_id, start_date, end_date)

    monkeypatch.setattr(client, "get_daily_usage", _record)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text)
    result = _invoke(runner, client, "analyze", config_path)
    assert result.exit_code == 0, result.output

    # Instance A reaches back to the grant's start_date; B has no grants, so it
    # stays on the DAILY_USAGE_LOOKBACK_DAYS floor.
    assert requested[CRN_A] == grant_start
    assert requested[CRN_B] == today - timedelta(days=DAILY_USAGE_LOOKBACK_DAYS)
