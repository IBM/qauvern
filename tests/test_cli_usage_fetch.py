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

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from qauvern.cli import main
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
