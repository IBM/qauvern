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
from datetime import datetime as dt, timezone

import pytest
import yaml

from qauvern.config import ConfigParser
from qauvern.models import InstanceNameDrift
from tests.mock_api import MockIBMQuantumAPIClient


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _write_config(content: str) -> str:
    """Write config content to a temp file and return path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return f.name


def _to_yaml(data: dict) -> str:
    return yaml.dump(data, default_flow_style=False)


# -------------------------------------------------------------------
# Loading and validation
# -------------------------------------------------------------------


def test_file_not_found_raises() -> None:
    """Test that loading a nonexistent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Configuration file not found"):
        ConfigParser("/nonexistent/path/config.yaml")


def test_missing_required_field_raises() -> None:
    """Test that each missing top-level required field raises ValueError."""
    base = {
        "account_id": "acc-1",
        "plan": "internal",
        "minimum_allocation_seconds": 60,
        "instances": [],
    }
    for field in ["account_id", "plan", "minimum_allocation_seconds", "instances"]:
        config = {k: v for k, v in base.items() if k != field}
        path = _write_config(_to_yaml(config))
        try:
            with pytest.raises(ValueError, match=field):
                ConfigParser(path)
        finally:
            os.unlink(path)


def test_instances_key_not_a_list_raises() -> None:
    """Test that the 'instances' YAML key being a non-list raises ValueError."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances: "not-a-list"
""")
    try:
        with pytest.raises(ValueError, match="instances"):
            ConfigParser(path)
    finally:
        os.unlink(path)


def test_instance_missing_required_field_raises() -> None:
    """Test that an instance entry missing 'name' or 'crn' raises ValueError."""
    for missing in ["name", "crn"]:
        fields = {"name": "Project A", "crn": "crn:test:1"}
        del fields[missing]
        entry_yaml = "\n".join(f'    {k}: "{v}"' for k, v in fields.items())
        path = _write_config(f"""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  -
{entry_yaml}
""")
        try:
            with pytest.raises(ValueError, match=missing):
                ConfigParser(path)
        finally:
            os.unlink(path)


# -------------------------------------------------------------------
# Top-level fields
# -------------------------------------------------------------------


def test_invalid_plan_raises() -> None:
    """Test that an unrecognized plan name raises ValueError listing known plans."""
    path = _write_config("""
account_id: "acc-1"
plan: "flex"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
""")
    try:
        with pytest.raises(ValueError, match="Unknown plan 'flex'"):
            ConfigParser(path)
    finally:
        os.unlink(path)


def test_minimum_allocation_seconds_parsed() -> None:
    """Test that explicit minimum_allocation_seconds is parsed from config."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 300
instances:
  - name: "Project A"
    crn: "crn:test:1"
""")
    try:
        parser = ConfigParser(path)
        assert parser.minimum_allocation_seconds == 300
    finally:
        os.unlink(path)


def test_allocation_reserve_percent_defaults_to_zero() -> None:
    """Test that allocation_reserve_percent defaults to 0.0 when absent."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
""")
    try:
        parser = ConfigParser(path)
        assert parser.allocation_reserve_percent == 0.0
    finally:
        os.unlink(path)


def test_allocation_reserve_percent_parsed() -> None:
    """Test that allocation_reserve_percent is parsed from config."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
allocation_reserve_percent: 20
instances:
  - name: "Project A"
    crn: "crn:test:1"
""")
    try:
        parser = ConfigParser(path)
        assert parser.allocation_reserve_percent == 20.0
    finally:
        os.unlink(path)


def test_reserve_percent_out_of_range_raises() -> None:
    """Test that allocation_reserve_percent >= 100 raises ValueError."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
allocation_reserve_percent: 100
instances:
  - name: "Project A"
    crn: "crn:test:1"
""")
    try:
        with pytest.raises(ValueError, match="allocation_reserve_percent"):
            ConfigParser(path)
    finally:
        os.unlink(path)


# -------------------------------------------------------------------
# Instance configs
# -------------------------------------------------------------------


def test_limit_seconds_parsed() -> None:
    """Test that limit_seconds YAML key parses to limit_seconds."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
    limit_seconds: 50000
""")
    try:
        parser = ConfigParser(path)
        assert parser.instance_configs[0].target_limit_seconds == 50000
    finally:
        os.unlink(path)


# -------------------------------------------------------------------
# Net grants
# -------------------------------------------------------------------


def test_no_net_grants_defaults_to_empty_tuple() -> None:
    """Test that an entry with no net_grants has empty tuple."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
""")
    try:
        parser = ConfigParser(path)
        assert parser.instance_configs[0].net_grants == ()
    finally:
        os.unlink(path)


def test_net_grant_parsed() -> None:
    """Test that net_grants are parsed from config."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00+00:00"
        net_grant_seconds: 180000
""")
    try:
        parser = ConfigParser(path)
        assert len(parser.instance_configs[0].net_grants) == 1
        assert parser.instance_configs[0].net_grants[0].net_grant_seconds == 180000
    finally:
        os.unlink(path)


def test_multiple_net_grants_parsed() -> None:
    """Test that multiple net_grants are parsed."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00+00:00"
        net_grant_seconds: 180000
      - start_date: "2026-06-01T00:00:00+00:00"
        net_grant_seconds: 96000
""")
    try:
        parser = ConfigParser(path)
        assert len(parser.instance_configs[0].net_grants) == 2
        assert parser.instance_configs[0].net_grants[1].net_grant_seconds == 96000
    finally:
        os.unlink(path)


def test_net_grant_zero_seconds_raises() -> None:
    """Test that net_grant_seconds <= 0 raises ValueError naming the offending instance/grant."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00+00:00"
        net_grant_seconds: 180000
      - start_date: "2026-06-01T00:00:00+00:00"
        net_grant_seconds: 0
""")
    try:
        with pytest.raises(
            ValueError, match=r"instances\[Project A\]\.net_grants\[1\]: net_grant_seconds must be positive"
        ):
            ConfigParser(path)
    finally:
        os.unlink(path)


def test_net_grant_end_date_not_after_start_raises() -> None:
    """Test that net_grant end_date <= start_date raises ValueError naming the offending grant."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00+00:00"
        end_date: "2026-04-30T00:00:00+00:00"
        net_grant_seconds: 180000
""")
    try:
        with pytest.raises(
            ValueError, match=r"instances\[Project A\]\.net_grants\[0\]: end_date must be after start_date"
        ):
            ConfigParser(path)
    finally:
        os.unlink(path)


def test_instance_empty_crn_raises() -> None:
    """Test that an empty crn raises ValueError naming the instance."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: ""
""")
    try:
        with pytest.raises(ValueError, match=r"instances\[Project A\]\.crn cannot be empty"):
            ConfigParser(path)
    finally:
        os.unlink(path)


def test_net_grants_require_limit_seconds() -> None:
    """Test that net_grants without limit_seconds raises ValueError naming the instance."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
    net_grants:
      - start_date: "2026-05-01T00:00:00+00:00"
        net_grant_seconds: 180000
""")
    try:
        with pytest.raises(
            ValueError, match=r"instances\[Project A\]: limit_seconds is required when net_grants is set"
        ):
            ConfigParser(path)
    finally:
        os.unlink(path)


def test_net_grant_end_date_parsed() -> None:
    """Test that explicit end_date is parsed from config."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00+00:00"
        end_date: "2026-06-15T00:00:00+00:00"
        net_grant_seconds: 180000
""")
    try:
        parser = ConfigParser(path)
        assert parser.instance_configs[0].net_grants[0].end_date == dt(2026, 6, 15, tzinfo=timezone.utc)
    finally:
        os.unlink(path)


def test_net_grant_no_end_date_defaults_28_days() -> None:
    """Test that missing end_date defaults to start_date + 28 days."""
    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00+00:00"
        net_grant_seconds: 180000
""")
    try:
        parser = ConfigParser(path)
        assert parser.instance_configs[0].net_grants[0].end_date == dt(2026, 5, 29, tzinfo=timezone.utc)
    finally:
        os.unlink(path)


# -------------------------------------------------------------------
# validate_instances_against_api
# -------------------------------------------------------------------


def _two_instance_config() -> str:
    return """
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances:
  - name: "Project A"
    crn: "crn:test:1"
  - name: "Project B"
    crn: "crn:test:2"
"""


def test_validate_instances_against_api_passes_when_all_configs_match() -> None:
    """All configured CRNs are present on the API and names match → no drift."""
    client = MockIBMQuantumAPIClient()
    client.setup_account("acc-1", allocation_budget_seconds=0)
    client.setup_instance("crn:test:1", "Project A", allocation_seconds=0, account_id="acc-1")
    client.setup_instance("crn:test:2", "Project B", allocation_seconds=0, account_id="acc-1")
    # Extra instance on the API that's not in config — fine, only configs must be a subset.
    client.setup_instance("crn:test:3", "Project C", allocation_seconds=0, account_id="acc-1")

    path = _write_config(_two_instance_config())
    try:
        parser = ConfigParser(path)
        assert parser.validate_instances_against_api(client.discover_instances()) == []
    finally:
        os.unlink(path)


def test_validate_instances_against_api_raises_on_unrecognized_crn() -> None:
    """A config CRN that's not on the API → ValueError naming the instance."""
    client = MockIBMQuantumAPIClient()
    client.setup_account("acc-1", allocation_budget_seconds=0)
    client.setup_instance("crn:test:1", "Project A", allocation_seconds=0, account_id="acc-1")
    # crn:test:2 is in the config but NOT on the API.

    path = _write_config(_two_instance_config())
    try:
        parser = ConfigParser(path)
        with pytest.raises(ValueError) as excinfo:
            parser.validate_instances_against_api(client.discover_instances())
        assert str(excinfo.value) == (
            "Config file contains instances not found in account acc-1 on plan internal:\n"
            "  - Project B, crn:test:2\n"
            "(run `qauvern update` to fix automatically)"
        )
    finally:
        os.unlink(path)


def test_validate_instances_against_api_passes_with_empty_config_instances() -> None:
    """A config with no instances has nothing to mismatch."""
    client = MockIBMQuantumAPIClient()
    client.setup_account("acc-1", allocation_budget_seconds=0)

    path = _write_config("""
account_id: "acc-1"
plan: "internal"
minimum_allocation_seconds: 60
instances: []
""")
    try:
        parser = ConfigParser(path)
        assert parser.validate_instances_against_api(client.discover_instances()) == []
    finally:
        os.unlink(path)


def test_validate_instances_against_api_returns_drift_when_name_differs() -> None:
    """All configured names differ from live names → all collected in returned list."""
    client = MockIBMQuantumAPIClient()
    client.setup_account("acc-1", allocation_budget_seconds=0)
    client.setup_instance("crn:test:1", "Alpha", allocation_seconds=0, account_id="acc-1")
    client.setup_instance("crn:test:2", "Beta", allocation_seconds=0, account_id="acc-1")

    path = _write_config(_two_instance_config())
    try:
        parser = ConfigParser(path)
        drifts = parser.validate_instances_against_api(client.discover_instances())
        assert drifts == [
            InstanceNameDrift(crn="crn:test:1", config_name="Project A", api_name="Alpha"),
            InstanceNameDrift(crn="crn:test:2", config_name="Project B", api_name="Beta"),
        ]
        assert [str(d) for d in drifts] == [
            '"Project A" -> "Alpha" (crn: crn:test:1)',
            '"Project B" -> "Beta" (crn: crn:test:2)',
        ]
    finally:
        os.unlink(path)


def test_validate_instances_against_api_unrecognized_takes_priority_over_drift() -> None:
    """Unrecognized CRN raises before drift is reported, even if other configs are drifted."""
    client = MockIBMQuantumAPIClient()
    client.setup_account("acc-1", allocation_budget_seconds=0)
    # crn:test:1 is on the API but with a different name (would-be drift).
    client.setup_instance("crn:test:1", "Alpha", allocation_seconds=0, account_id="acc-1")
    # crn:test:2 is not on the API at all — unrecognized.

    path = _write_config(_two_instance_config())
    try:
        parser = ConfigParser(path)
        with pytest.raises(ValueError) as excinfo:
            parser.validate_instances_against_api(client.discover_instances())
        assert "Project B, crn:test:2" in str(excinfo.value)
    finally:
        os.unlink(path)


def test_validate_instances_against_api_raises_on_archived_instance() -> None:
    """A config CRN that is archived → ValueError naming the archived instance."""
    client = MockIBMQuantumAPIClient()
    client.setup_account("acc-1", allocation_budget_seconds=0)
    client.setup_instance("crn:test:1", "Project A", allocation_seconds=0, account_id="acc-1")
    client.setup_instance("crn:test:2", "Project B", allocation_seconds=0, account_id="acc-1", archived=True)

    path = _write_config(_two_instance_config())
    try:
        parser = ConfigParser(path)
        with pytest.raises(ValueError) as excinfo:
            parser.validate_instances_against_api(client.discover_instances())
        assert "archived" in str(excinfo.value)
        assert "Project B, crn:test:2" in str(excinfo.value)
    finally:
        os.unlink(path)
