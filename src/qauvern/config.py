# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Configuration file parser for qauvern."""

from datetime import datetime, timedelta, timezone
from functools import cached_property
from pathlib import Path

import yaml

from .models import InstanceConfig, NetGrant
from .plan import Plan, plan_from_name


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class ConfigParser:
    """Parser for YAML configuration files."""

    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        with open(self.config_path) as f:
            self.config_data: dict = yaml.safe_load(f)
        self._validate_config()

    def _validate_config(self) -> None:
        """Validate the configuration structure."""
        required_fields = ["account_id", "plan", "balance_period", "instances"]

        for field in required_fields:
            if field not in self.config_data:
                raise ValueError(f"Missing required field in config: {field}")

        if not isinstance(self.config_data["instances"], list):
            raise ValueError("'instances' must be a list")

        reserve = self.config_data.get("allocation_reserve_percent", 0.0)
        if not (0.0 <= float(reserve) < 100.0):
            raise ValueError("allocation_reserve_percent must be in range [0, 100)")

        balance_period = self.config_data["balance_period"]
        if "start_date" not in balance_period or "end_date" not in balance_period:
            raise ValueError("balance_period must contain start_date and end_date")

        # Eagerly parse to surface validation errors at load time.
        self.plan
        self.instance_configs

    @property
    def account_id(self) -> str:
        return self.config_data["account_id"]

    @cached_property
    def plan(self) -> Plan:
        return plan_from_name(self.config_data["plan"])

    @property
    def minimum_allocation_seconds(self) -> int:
        return self.config_data.get("minimum_allocation_seconds", 60)

    @property
    def allocation_reserve_percent(self) -> float:
        return float(self.config_data.get("allocation_reserve_percent", 0.0))

    @cached_property
    def balance_period(self) -> dict[str, datetime]:
        period = self.config_data["balance_period"]
        return {
            "start_date": _parse_utc(period["start_date"]),
            "end_date": _parse_utc(period["end_date"]),
        }

    @cached_property
    def instance_configs(self) -> list[InstanceConfig]:
        """Parse and return the list of instance configs."""
        configs: list[InstanceConfig] = []
        period = self.balance_period

        for entry in self.config_data["instances"]:
            for field in ("name", "crn"):
                if field not in entry:
                    raise ValueError(f"Instance config missing required field: {field}")

            start_date = _parse_utc(entry["start_date"]) if "start_date" in entry else period["start_date"]
            end_date = _parse_utc(entry["end_date"]) if "end_date" in entry else period["end_date"]

            net_grants = []
            for grant_data in entry.get("net_grants", []):
                grant_start = _parse_utc(grant_data["start_date"])
                grant_end = (
                    _parse_utc(grant_data["end_date"]) if "end_date" in grant_data else grant_start + timedelta(days=28)
                )
                net_grants.append(
                    NetGrant(
                        start_date=grant_start,
                        net_grant_seconds=grant_data["net_grant_seconds"],
                        end_date=grant_end,
                    )
                )

            target_usage_seconds = entry.get("target_usage_seconds")
            limit_seconds = entry.get("limit_seconds")

            config = InstanceConfig(
                name=entry["name"],
                crn=entry["crn"],
                target_usage_seconds=target_usage_seconds,
                start_date=start_date,
                end_date=end_date,
                target_limit_seconds=limit_seconds,
                net_grants=tuple(net_grants),
            )

            if limit_seconds is not None and target_usage_seconds is not None:
                if limit_seconds < target_usage_seconds:
                    raise ValueError(
                        f"Instance '{entry['name']}': limit_seconds ({limit_seconds}) "
                        f"must be >= target_usage_seconds ({target_usage_seconds})"
                    )
            configs.append(config)

        return configs
