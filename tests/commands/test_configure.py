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

from qauvern.cli import main
from qauvern.commands.configure import build_configure_yaml, build_instance_summary_table
from qauvern.config import load_config
from qauvern.models import Instance
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
) -> Instance:
    return Instance(
        crn=crn,
        name=name,
        allocation_seconds=allocation_seconds,
        limit_seconds=limit_seconds,
        consumed_seconds=consumed_seconds,
    )


# ---------------------------------------------------------------------------
# build_configure_yaml
# ---------------------------------------------------------------------------


def test_build_configure_yaml_multiple_instances() -> None:
    instances = [
        _make_instance(crn="crn:a", name="A"),
        _make_instance(crn="crn:b", name="B"),
        _make_instance(crn="crn:c", name="C"),
    ]
    parsed = yaml.safe_load(build_configure_yaml("acct", instances, "s", "e"))
    assert [p["name"] for p in parsed["projects"]] == ["Instance 1", "Instance 2", "Instance 3"]
    assert [p["crn"] for p in parsed["projects"]] == ["crn:a", "crn:b", "crn:c"]


def test_build_configure_yaml_uses_allocation_for_target() -> None:
    instances = [_make_instance(allocation_seconds=12345)]
    parsed = yaml.safe_load(build_configure_yaml("acct", instances, "s", "e"))
    assert parsed["projects"][0]["target_usage_seconds"] == 12345


def test_build_configure_yaml_defaults_target_when_allocation_is_zero() -> None:
    instances = [_make_instance(allocation_seconds=0)]
    parsed = yaml.safe_load(build_configure_yaml("acct", instances, "s", "e"))
    assert parsed["projects"][0]["target_usage_seconds"] == 96000


def test_build_configure_yaml_header() -> None:
    instances = [_make_instance(), _make_instance(crn="crn:test:i-2")]
    text = build_configure_yaml("acct-XYZ", instances, "s", "e")
    assert "# Account: acct-XYZ" in text
    assert "# Instances Found: 2" in text


def test_build_configure_yaml_footer_lists_each_instance() -> None:
    instances = [
        _make_instance(
            crn="crn:foo",
            name="Foo",
            allocation_seconds=3600,
            consumed_seconds=1800,
            limit_seconds=7200,
        ),
        _make_instance(crn="crn:bar", name="Bar", allocation_seconds=0, consumed_seconds=0),
    ]
    text = build_configure_yaml("acct", instances, "s", "e")

    footer = text.split("# Instance Details:")[1]

    foo_block = footer.split("# - Foo")[1].split("# -")[0]
    assert "#   CRN: crn:foo" in foo_block
    assert "#   Allocation: 1.0h" in foo_block
    assert "#   Consumed: 1800s" in foo_block
    assert "#   Limit: 2.0h" in foo_block

    bar_block = footer.split("# - Bar")[1]
    assert "#   CRN: crn:bar" in bar_block
    assert "Limit:" not in bar_block  # no limit line when limit_seconds is None


def test_build_configure_yaml_footer_falls_back_to_unnamed() -> None:
    instances = [_make_instance(name="", crn="crn:noname")]
    text = build_configure_yaml("acct", instances, "s", "e")
    footer = text.split("# Instance Details:")[1]
    assert "# - Unnamed" in footer
    assert "#   CRN: crn:noname" in footer


def test_build_configure_yaml_round_trips_through_load_config(tmp_path: Path) -> None:
    """The auto-generated YAML, plus the fields a user must add by hand, loads cleanly."""
    from datetime import datetime

    instances = [_make_instance(crn="crn:test:rt", allocation_seconds=36000)]
    text = build_configure_yaml("acct", instances, "2026-01-01T00:00:00", "2026-12-31T23:59:59")

    parsed = yaml.safe_load(text)
    parsed["plan_id"] = "91b2c828-2952-4f05-aed8-bedf92c6c480"  # internal plan
    parsed["projects"][0]["start_date"] = "2026-01-01T00:00:00"
    parsed["projects"][0]["end_date"] = "2026-12-31T23:59:59"

    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(parsed))

    cfg = load_config(str(path))
    assert cfg.account_id == "acct"
    assert cfg.balance_period["start_date"] == datetime(2026, 1, 1, 0, 0, 0)
    assert cfg.balance_period["end_date"] == datetime(2026, 12, 31, 23, 59, 59)
    assert len(cfg.instance_configs) == 1
    assert cfg.instance_configs[0].crn == "crn:test:rt"
    assert cfg.instance_configs[0].target_usage_seconds == 36000


# ---------------------------------------------------------------------------
# build_instance_summary_table
# ---------------------------------------------------------------------------


def test_summary_table_headers() -> None:
    _rows, headers = build_instance_summary_table([_make_instance()])
    assert headers == ["Instance Name", "Allocation", "Consumed", "Fairness"]


def test_summary_table_preserves_order_and_count() -> None:
    instances = [
        _make_instance(crn="crn:a", name="Alpha"),
        _make_instance(crn="crn:b", name="Bravo"),
        _make_instance(crn="crn:c", name="Charlie"),
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
    assert rows[0][2] == "5.0h"


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
        account_id="acct-1",
    )

    output = tmp_path / "config.yaml"
    result = _invoke_configure(
        runner,
        mock_client,
        [
            "--account-id",
            "acct-1",
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
    assert len(parsed["projects"]) == 1
    assert parsed["projects"][0]["crn"] == "crn:test:i-1"

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
            "--api-key",
            "test-key",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert "No instances found" in result.output
    assert not output.exists()
