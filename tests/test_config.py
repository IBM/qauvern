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
from datetime import datetime as dt

import pytest
import yaml

from qauvern.config import ConfigParser


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
        "plan_id": "plan-1",
        "balance_period": {"start_date": "2026-01-01T00:00:00", "end_date": "2026-12-31T23:59:59"},
        "instances": [],
    }
    for field in ["account_id", "plan_id", "balance_period", "instances"]:
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
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances: "not-a-list"
""")
    try:
        with pytest.raises(ValueError, match="instances"):
            ConfigParser(path)
    finally:
        os.unlink(path)


def test_balance_period_missing_dates_raises() -> None:
    """Test that balance_period missing start_date or end_date raises ValueError."""
    for missing in ["start_date", "end_date"]:
        dates = {"start_date": "2026-01-01T00:00:00", "end_date": "2026-12-31T23:59:59"}
        del dates[missing]
        period_yaml = "\n".join(f'  {k}: "{v}"' for k, v in dates.items())
        path = _write_config(f"""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
{period_yaml}
instances: []
""")
        try:
            with pytest.raises(ValueError, match="balance_period"):
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
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
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


def test_minimum_allocation_seconds_defaults_to_60() -> None:
    """Test that minimum_allocation_seconds defaults to 60 when absent."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
""")
    try:
        parser = ConfigParser(path)
        assert parser.minimum_allocation_seconds == 60
    finally:
        os.unlink(path)


def test_minimum_allocation_seconds_parsed() -> None:
    """Test that explicit minimum_allocation_seconds is parsed from config."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
minimum_allocation_seconds: 300
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
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
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
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
plan_id: "plan-1"
allocation_reserve_percent: 20
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
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
plan_id: "plan-1"
allocation_reserve_percent: 100
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
""")
    try:
        with pytest.raises(ValueError, match="allocation_reserve_percent"):
            ConfigParser(path)
    finally:
        os.unlink(path)


# -------------------------------------------------------------------
# Instance configs
# -------------------------------------------------------------------


def test_instance_inherits_balance_period_dates() -> None:
    """Test that instance configs without explicit dates inherit from balance_period."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
""")
    try:
        parser = ConfigParser(path)
        cfg = parser.instance_configs[0]
        assert cfg.start_date == dt(2026, 1, 1)
        assert cfg.end_date == dt(2026, 12, 31, 23, 59, 59)
    finally:
        os.unlink(path)


def test_instance_date_overrides_balance_period() -> None:
    """Test that entry-level start/end dates override the balance period."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    start_date: "2026-03-01T00:00:00"
    end_date: "2026-09-30T23:59:59"
""")
    try:
        parser = ConfigParser(path)
        cfg = parser.instance_configs[0]
        assert cfg.start_date == dt(2026, 3, 1)
        assert cfg.end_date == dt(2026, 9, 30, 23, 59, 59)
    finally:
        os.unlink(path)


def test_instance_without_target_usage_seconds() -> None:
    """Test that entry without target_usage_seconds parses with None."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    limit_seconds: 50000
""")
    try:
        parser = ConfigParser(path)
        assert parser.instance_configs[0].target_usage_seconds is None
    finally:
        os.unlink(path)


def test_limit_seconds_parsed() -> None:
    """Test that limit_seconds YAML key parses to limit_seconds."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    limit_seconds: 50000
""")
    try:
        parser = ConfigParser(path)
        assert parser.instance_configs[0].limit_seconds == 50000
    finally:
        os.unlink(path)


def test_limit_below_target_raises() -> None:
    """Test that limit_seconds < target_usage_seconds raises ValueError."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 50000
    limit_seconds: 30000
""")
    try:
        with pytest.raises(ValueError, match="limit_seconds"):
            ConfigParser(path)
    finally:
        os.unlink(path)


# -------------------------------------------------------------------
# Net grants
# -------------------------------------------------------------------


def test_no_net_grants_defaults_to_empty_tuple() -> None:
    """Test that an entry with no net_grants has empty tuple."""
    path = _write_config("""
account_id: "acc-1"
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
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
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
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
plan_id: "plan-1"
balance_period:
  start_date: "2026-01-01T00:00:00"
  end_date: "2026-12-31T23:59:59"
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
        net_grant_seconds: 180000
      - start_date: "2026-06-01T00:00:00"
        net_grant_seconds: 96000
""")
    try:
        parser = ConfigParser(path)
        assert len(parser.instance_configs[0].net_grants) == 2
        assert parser.instance_configs[0].net_grants[1].net_grant_seconds == 96000
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
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
        net_grant_seconds: 0
""")
    try:
        with pytest.raises(ValueError, match="net_grant_seconds"):
            ConfigParser(path)
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
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
        end_date: "2026-06-15T00:00:00"
        net_grant_seconds: 180000
""")
    try:
        parser = ConfigParser(path)
        assert parser.instance_configs[0].net_grants[0].end_date == dt(2026, 6, 15)
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
instances:
  - name: "Project A"
    crn: "crn:test:1"
    target_usage_seconds: 30000
    limit_seconds: 50000
    net_grants:
      - start_date: "2026-05-01T00:00:00"
        net_grant_seconds: 180000
""")
    try:
        parser = ConfigParser(path)
        assert parser.instance_configs[0].net_grants[0].end_date == dt(2026, 5, 29)
    finally:
        os.unlink(path)
