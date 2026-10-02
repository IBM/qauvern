# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""End-to-end tests of the over-budget regime through the CLI and the mock API."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from qauvern.cli import main
from tests.mock_api import MockIBMQuantumAPIClient

CRN_AT_LIMIT = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:at-limit::"
CRN_RUNNING = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:running::"

CONFIG_TEXT = f"""\
account_id: acct-1
plan: internal
minimum_allocation_seconds: 60
instances:
  - name: AtLimit
    crn: '{CRN_AT_LIMIT}'
  - name: Running
    crn: '{CRN_RUNNING}'
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_TEXT)
    return path


def _over_budget_client() -> MockIBMQuantumAPIClient:
    """Plan-wide usage of 1200s against a 1000s budget; AtLimit cannot run."""
    client = MockIBMQuantumAPIClient()
    client.setup_account(account_id="acct-1", allocation_budget_seconds=1000)
    client.setup_instance(
        CRN_AT_LIMIT, "AtLimit", 500, consumed_seconds=500, limit_seconds=500, account_id="acct-1", consumed_24h=1
    )
    client.setup_instance(CRN_RUNNING, "Running", 500, consumed_seconds=700, account_id="acct-1", consumed_24h=1)
    return client


def _invoke(client: MockIBMQuantumAPIClient, *args: str) -> Result:
    with patch("qauvern.cli.IBMQuantumAPIClient", return_value=client):
        return CliRunner().invoke(main, [*args, "--api-key", "k"])


def test_optimize_applies_changes_over_budget(config_path: Path) -> None:
    client = _over_budget_client()

    result = _invoke(client, "optimize", "--config", str(config_path), "-y")

    assert result.exit_code == 0, result.output
    assert client.instances[CRN_AT_LIMIT].allocation_seconds == 60
    assert client.instances[CRN_RUNNING].allocation_seconds == 700


def test_show_reports_over_budget(config_path: Path) -> None:
    result = _invoke(_over_budget_client(), "show", "--config", str(config_path))

    assert result.exit_code == 0, result.output
    assert "Over allocation budget" in result.stdout


def test_show_omits_over_budget_line_under_budget(config_path: Path) -> None:
    client = MockIBMQuantumAPIClient()
    client.setup_account(account_id="acct-1", allocation_budget_seconds=10_000)
    client.setup_instance(CRN_AT_LIMIT, "AtLimit", 500, consumed_seconds=100, account_id="acct-1")
    client.setup_instance(CRN_RUNNING, "Running", 500, consumed_seconds=100, account_id="acct-1")

    result = _invoke(client, "show", "--config", str(config_path))

    assert result.exit_code == 0, result.output
    assert "Over allocation budget" not in result.stdout
