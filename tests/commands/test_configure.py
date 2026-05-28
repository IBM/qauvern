# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for the `qauvern configure` command and its pure helpers."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner, Result
from datetime import timezone

from qauvern.cli import main
from qauvern.commands.configure import build_configure_yaml, build_instance_summary_table
from qauvern.config import ConfigParser
from qauvern.models import InstanceState
from qauvern.plan import Plan
from tests.mock_api import MockIBMQuantumAPIClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instance(
    crn: str = "crn:test:i-1",
    name: str = "Instance 1",
    allocation_seconds: int = 36000,
    consumed_seconds: int = 0,
    limit_seconds: int | None = None,
) -> InstanceState:
    return InstanceState(
        crn=crn,
        name=name,
        allocation_seconds=allocation_seconds,
        limit_seconds=limit_seconds,
        consumed_seconds=consumed_seconds,
        detailed_usage=None,
    )


# ---------------------------------------------------------------------------
# build_configure_yaml
# ---------------------------------------------------------------------------


def test_build_configure_yaml_multiple_instances() -> None:
    instances = [
        _make_instance(crn="crn:c", name=""),
        _make_instance(crn="crn:b", name="B"),
        _make_instance(crn="crn:a", name="A"),
    ]
    parsed = yaml.safe_load(build_configure_yaml("acct", Plan.INTERNAL, instances, "s", "e"))
    assert [p["name"] for p in parsed["instances"]] == ["A", "B", "Instance 3"]
    assert [p["crn"] for p in parsed["instances"]] == ["crn:a", "crn:b", "crn:c"]


def test_build_configure_yaml_omits_target_usage_seconds() -> None:
    instances = [_make_instance(allocation_seconds=12345)]
    parsed = yaml.safe_load(build_configure_yaml("acct", Plan.INTERNAL, instances, "s", "e"))
    assert "target_usage_seconds" not in parsed["instances"][0]


def test_build_configure_yaml_includes_limit_seconds_when_set() -> None:
    instances = [_make_instance(limit_seconds=50000)]
    parsed = yaml.safe_load(build_configure_yaml("acct", Plan.INTERNAL, instances, "s", "e"))
    assert parsed["instances"][0]["limit_seconds"] == 50000


def test_build_configure_yaml_omits_limit_seconds_when_none() -> None:
    instances = [_make_instance(limit_seconds=None)]
    parsed = yaml.safe_load(build_configure_yaml("acct", Plan.INTERNAL, instances, "s", "e"))
    assert "limit_seconds" not in parsed["instances"][0]


def test_build_configure_yaml_emits_plan_name() -> None:
    parsed = yaml.safe_load(build_configure_yaml("acct", Plan.PAYGO, [_make_instance()], "s", "e"))
    assert parsed["plan"] == "paygo"


def test_configure_yaml_round_trips(tmp_path: Path) -> None:
    """The auto-generated YAML loads cleanly with no manual edits, and still
    loads after a user customizes it."""
    from datetime import datetime

    instances = [_make_instance(crn="crn:test:rt", allocation_seconds=36000)]
    text = build_configure_yaml(
        "acct", Plan.INTERNAL, instances, "2026-01-01T00:00:00+00:00", "2026-12-31T23:59:59+00:00"
    )

    path = tmp_path / "config.yaml"
    path.write_text(text)

    # First: the unedited generator output parses with all expected values.
    cfg = ConfigParser(str(path))
    assert cfg.account_id == "acct"
    assert cfg.plan == Plan.INTERNAL
    assert cfg.balance_period["start_date"] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert cfg.balance_period["end_date"] == datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert len(cfg.instance_configs) == 1
    assert cfg.instance_configs[0].crn == "crn:test:rt"
    assert cfg.instance_configs[0].target_usage_seconds is None

    # Then: simulate the user editing the file (changing plan, tweaking the
    # instance, adding net_grants, setting a custom minimum) and confirm it
    # still parses with the new values.
    parsed = yaml.safe_load(text)
    parsed["plan"] = "premium"
    parsed["minimum_allocation_seconds"] = 120
    parsed["instances"][0]["target_usage_seconds"] = 50000
    parsed["instances"][0]["limit_seconds"] = 80000
    parsed["instances"][0]["net_grants"] = [
        {"start_date": "2026-05-01T00:00:00+00:00", "net_grant_seconds": 180000},
    ]
    path.write_text(yaml.dump(parsed))

    cfg = ConfigParser(str(path))
    assert cfg.plan == Plan.PREMIUM
    assert cfg.minimum_allocation_seconds == 120
    assert cfg.instance_configs[0].target_usage_seconds == 50000
    assert cfg.instance_configs[0].target_limit_seconds == 80000
    assert len(cfg.instance_configs[0].net_grants) == 1
    assert cfg.instance_configs[0].net_grants[0].net_grant_seconds == 180000
    assert cfg.instance_configs[0].net_grants[0].end_date == datetime(2026, 5, 29, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# build_instance_summary_table
# ---------------------------------------------------------------------------


def test_summary_table_headers() -> None:
    _rows, headers = build_instance_summary_table([_make_instance()])
    assert headers == ["Instance Name", "Allocation", "Limit", "Consumed", "Fairness"]


def test_summary_table_sorted_by_name() -> None:
    instances = [
        _make_instance(crn="crn:c", name="Charlie"),
        _make_instance(crn="crn:a", name="Alpha"),
        _make_instance(crn="crn:b", name="Bravo"),
    ]
    rows, _ = build_instance_summary_table(instances)
    assert len(rows) == 3
    assert [row[0] for row in rows] == ["Alpha", "Bravo", "Charlie"]


def test_summary_table_truncates_long_names() -> None:
    long_name = "x" * 80
    rows, _ = build_instance_summary_table([_make_instance(name=long_name)])
    assert rows[0][0] == "x" * 40


def test_summary_table_uses_format_seconds() -> None:
    rows, _ = build_instance_summary_table([_make_instance(allocation_seconds=36000, consumed_seconds=18000)])
    assert rows[0][1] == "10.0h"
    assert rows[0][3] == "5.0h"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _invoke_configure(runner: CliRunner, mock_client: MockIBMQuantumAPIClient, args: list[str]) -> Result:
    with patch("qauvern.cli.IBMQuantumAPIClient", return_value=mock_client):
        return runner.invoke(main, ["configure", *args])


def test_configure_happy_path(runner: CliRunner, tmp_path: Path) -> None:
    mock_client = MockIBMQuantumAPIClient()
    mock_client.setup_account(account_id="acct-1", target_usage_seconds=0)
    mock_client.setup_instance(
        crn="crn:test:i-1",
        name="My Instance",
        allocation_seconds=36000,
        limit_seconds=72000,
        account_id="acct-1",
    )

    output = tmp_path / "config.yaml"
    result = _invoke_configure(
        runner,
        mock_client,
        [
            "--account-id",
            "acct-1",
            "--plan",
            "internal",
            "--api-key",
            "test-key",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output.exists()

    parsed = yaml.safe_load(output.read_text())
    assert parsed["account_id"] == "acct-1"
    assert parsed["plan"] == "internal"
    assert len(parsed["instances"]) == 1
    instance = parsed["instances"][0]
    assert instance["crn"] == "crn:test:i-1"
    assert "target_usage_seconds" not in instance
    assert instance["limit_seconds"] == 72000

    assert "Configuration file created" in result.output
    assert "My Instance" in result.output


def test_configure_empty_instances_exits_with_error(runner: CliRunner, tmp_path: Path) -> None:
    mock_client = MockIBMQuantumAPIClient()
    mock_client.setup_account(account_id="acct-empty", target_usage_seconds=0)

    output = tmp_path / "config.yaml"
    result = _invoke_configure(
        runner,
        mock_client,
        [
            "--account-id",
            "acct-empty",
            "--plan",
            "internal",
            "--api-key",
            "test-key",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert "No instances found" in result.output
    assert not output.exists()
