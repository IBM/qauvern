# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for configure command."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from qauvern.config import load_config
from tests.mock_api import create_test_scenario_basic


def test_generate_config_from_account() -> None:
    """Test generating configuration from account data."""
    mock_client = create_test_scenario_basic()
    account = mock_client.get_account_with_instances("test-account-123")

    assert len(account.instances) == 4

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        output_path = Path(f.name)

    try:
        config = {
            "account_id": account.account_id,
            "balance_period": {
                "start_date": "2026-01-01T00:00:00",
                "end_date": "2026-12-31T23:59:59",
            },
            "projects": [
                {
                    "name": "Default Project",
                    "description": "Auto-generated project",
                    "crns": [inst.crn for inst in account.instances],
                    "target_usage_seconds": account.target_usage_seconds,
                }
            ],
        }

        with open(output_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        assert output_path.exists()

        with open(output_path) as f:
            loaded_config = yaml.safe_load(f)

        assert loaded_config["account_id"] == "test-account-123"
        assert len(loaded_config["projects"]) == 1
        assert len(loaded_config["projects"][0]["crns"]) == 4

    finally:
        if output_path.exists():
            output_path.unlink()


def test_config_includes_all_instances() -> None:
    """Test that generated config includes all instances."""
    mock_client = create_test_scenario_basic()
    account = mock_client.get_account_with_instances("test-account-123")

    config = {
        "account_id": account.account_id,
        "balance_period": {
            "start_date": "2026-01-01T00:00:00",
            "end_date": "2026-12-31T23:59:59",
        },
        "projects": [
            {
                "name": "Default Project",
                "crns": [inst.crn for inst in account.instances],
                "target_usage_seconds": account.target_usage_seconds,
            }
        ],
    }

    all_crns = config["projects"][0]["crns"]
    assert "crn:test:instance-1" in all_crns
    assert "crn:test:instance-2" in all_crns
    assert "crn:test:instance-3" in all_crns
    assert "crn:test:instance-4" in all_crns


def test_config_has_valid_structure() -> None:
    """Test that generated config has valid structure."""
    mock_client = create_test_scenario_basic()
    account = mock_client.get_account_with_instances("test-account-123")

    config = {
        "account_id": account.account_id,
        "balance_period": {
            "start_date": "2026-01-01T00:00:00",
            "end_date": "2026-12-31T23:59:59",
        },
        "projects": [
            {
                "name": "Default Project",
                "crns": [inst.crn for inst in account.instances],
                "target_usage_seconds": account.target_usage_seconds,
            }
        ],
    }

    assert "account_id" in config
    assert "balance_period" in config
    assert "projects" in config
    assert "start_date" in config["balance_period"]
    assert "end_date" in config["balance_period"]

    project = config["projects"][0]
    assert "name" in project
    assert "crns" in project
    assert "target_usage_seconds" in project
    assert isinstance(project["crns"], list)
    assert len(project["crns"]) > 0


def test_config_yaml_serialization() -> None:
    """Test that config can be serialized to YAML."""
    mock_client = create_test_scenario_basic()
    account = mock_client.get_account_with_instances("test-account-123")

    config = {
        "account_id": account.account_id,
        "balance_period": {
            "start_date": "2026-01-01T00:00:00",
            "end_date": "2026-12-31T23:59:59",
        },
        "projects": [
            {
                "name": "Default Project",
                "crns": [inst.crn for inst in account.instances],
                "target_usage_seconds": account.target_usage_seconds,
            }
        ],
    }

    yaml_str = yaml.dump(config, default_flow_style=False)
    assert yaml_str is not None
    assert len(yaml_str) > 0

    loaded = yaml.safe_load(yaml_str)
    assert loaded["account_id"] == config["account_id"]
    assert len(loaded["projects"]) == len(config["projects"])


def _write_config(content: str) -> str:
    """Write config content to a temp file and return path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return f.name


def test_project_limit_seconds_parsed() -> None:
    """Test that project_limit_seconds is parsed from config."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    project_limit_seconds: 50000
""")
    try:
        parser = load_config(path)
        projects = parser.projects
        assert projects[0].project_limit_seconds == 50000
    finally:
        os.unlink(path)


def test_net_grant_parsed() -> None:
    """Test that net_grants are parsed from config."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    project_limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
        net_grant_seconds: 180000
""")
    try:
        parser = load_config(path)
        projects = parser.projects
        assert len(projects[0].net_grants) == 1
        assert projects[0].net_grants[0].net_grant_seconds == 180000
    finally:
        os.unlink(path)


def test_multiple_net_grants_parsed() -> None:
    """Test that multiple net_grants are parsed."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    project_limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
        net_grant_seconds: 180000
      - start_date: "2026-06-01T00:00:00"
        net_grant_seconds: 96000
""")
    try:
        parser = load_config(path)
        projects = parser.projects
        assert len(projects[0].net_grants) == 2
        assert projects[0].net_grants[1].net_grant_seconds == 96000
    finally:
        os.unlink(path)


def test_no_net_grants_defaults_to_empty_list() -> None:
    """Test that project with no net_grants has empty list."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
""")
    try:
        parser = load_config(path)
        projects = parser.projects
        assert projects[0].net_grants == []
    finally:
        os.unlink(path)


def test_project_without_target_usage_seconds() -> None:
    """Test that project without target_usage_seconds parses with None."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    project_limit_seconds: 50000
""")
    try:
        parser = load_config(path)
        projects = parser.projects
        assert projects[0].target_usage_seconds is None
    finally:
        os.unlink(path)


def test_net_grant_zero_seconds_raises() -> None:
    """Test that net_grant_seconds <= 0 raises ValueError."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    project_limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
        net_grant_seconds: 0
""")
    try:
        with pytest.raises(ValueError, match="net_grant_seconds"):
            load_config(path)
    finally:
        os.unlink(path)


def test_net_grant_end_date_parsed() -> None:
    """Test that explicit end_date is parsed from config."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    project_limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
        end_date: "2026-06-15T00:00:00"
        net_grant_seconds: 180000
""")
    try:
        from datetime import datetime as dt

        parser = load_config(path)
        projects = parser.projects
        assert projects[0].net_grants[0].end_date == dt(2026, 6, 15)
    finally:
        os.unlink(path)


def test_net_grant_no_end_date_defaults_28_days() -> None:
    """Test that missing end_date defaults to start_date + 28 days."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    project_limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
        net_grant_seconds: 180000
""")
    try:
        from datetime import datetime as dt

        parser = load_config(path)
        projects = parser.projects
        assert projects[0].net_grants[0].end_date == dt(2026, 5, 29)
    finally:
        os.unlink(path)


def test_allocation_reserve_percent_parsed() -> None:
    """Test that allocation_reserve_percent is parsed from config."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
allocation_reserve_percent: 20
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
""")
    try:
        parser = load_config(path)
        assert parser.allocation_reserve_percent == 20.0
    finally:
        os.unlink(path)


def test_allocation_reserve_percent_defaults_to_zero() -> None:
    """Test that allocation_reserve_percent defaults to 0.0 when absent."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
""")
    try:
        parser = load_config(path)
        assert parser.allocation_reserve_percent == 0.0
    finally:
        os.unlink(path)


def test_project_limit_below_target_raises() -> None:
    """Test that project_limit_seconds < target_usage_seconds raises ValueError."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 50000
    project_limit_seconds: 30000
""")
    try:
        with pytest.raises(ValueError, match="project_limit_seconds"):
            load_config(path)
    finally:
        os.unlink(path)


def test_reserve_percent_out_of_range_raises() -> None:
    """Test that allocation_reserve_percent >= 100 raises ValueError."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
allocation_reserve_percent: 100
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
projects:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
""")
    try:
        with pytest.raises(ValueError, match="allocation_reserve_percent"):
            load_config(path)
    finally:
        os.unlink(path)
