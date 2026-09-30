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

from .models import DiscoveredInstances, InstanceConfig, InstanceNameDrift, NetGrant
from .plan import Plan, plan_from_name


def parse_utc_datetime(s: str, *, provenance: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"{provenance}: {s!r} must include a UTC offset, e.g. {s}+00:00")
    return dt.astimezone(timezone.utc)


def parse_net_grant_dates(grant: dict, *, provenance: str) -> tuple[datetime, datetime]:
    """Parse a net-grant's start/end dates, defaulting end to start + 28 days."""
    start = parse_utc_datetime(grant["start_date"], provenance=f"{provenance}.start_date")
    end = (
        parse_utc_datetime(grant["end_date"], provenance=f"{provenance}.end_date")
        if "end_date" in grant
        else start + timedelta(days=28)
    )
    return start, end


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
        required_fields = ["account_id", "plan", "minimum_allocation_seconds", "instances"]

        for field in required_fields:
            if field not in self.config_data:
                raise ValueError(f"Missing required field in config: {field}")

        if not isinstance(self.config_data["instances"], list):
            raise TypeError("'instances' must be a list")

        reserve = self.config_data.get("allocation_reserve_percent", 0.0)
        if not (0.0 <= float(reserve) < 100.0):
            raise ValueError("allocation_reserve_percent must be in range [0, 100)")

        # Eagerly parse to surface validation errors at load time.
        _ = self.plan
        _ = self.instance_configs

    @property
    def account_id(self) -> str:
        return self.config_data["account_id"]

    @cached_property
    def plan(self) -> Plan:
        return plan_from_name(self.config_data["plan"])

    @property
    def minimum_allocation_seconds(self) -> int:
        return self.config_data["minimum_allocation_seconds"]

    @property
    def allocation_reserve_percent(self) -> float:
        return float(self.config_data.get("allocation_reserve_percent", 0.0))

    @cached_property
    def instance_configs(self) -> list[InstanceConfig]:
        """Parse and return the list of instance configs."""
        configs: list[InstanceConfig] = []

        for entry in self.config_data["instances"]:
            for field in ("name", "crn"):
                if field not in entry:
                    raise ValueError(f"Instance config missing required field: {field}")

            inst = entry["name"]
            if not inst:
                raise ValueError("instances[].name cannot be empty")
            if not entry["crn"]:
                raise ValueError(f"instances[{inst}].crn cannot be empty")

            net_grants = []
            for i, grant_data in enumerate(entry.get("net_grants", [])):
                grant_provenance = f"instances[{inst}].net_grants[{i}]"
                grant_start, grant_end = parse_net_grant_dates(grant_data, provenance=grant_provenance)
                if grant_end <= grant_start:
                    raise ValueError(f"{grant_provenance}: end_date must be after start_date")
                if grant_data["net_grant_seconds"] <= 0:
                    raise ValueError(f"{grant_provenance}: net_grant_seconds must be positive")
                net_grants.append(
                    NetGrant(
                        start_date=grant_start,
                        net_grant_seconds=grant_data["net_grant_seconds"],
                        end_date=grant_end,
                    )
                )

            target_limit_seconds = entry.get("limit_seconds")
            if net_grants and target_limit_seconds is None:
                raise ValueError(f"instances[{inst}]: limit_seconds is required when net_grants is set")

            config = InstanceConfig(
                name=entry["name"],
                crn=entry["crn"],
                target_limit_seconds=target_limit_seconds,
                net_grants=tuple(net_grants),
            )
            configs.append(config)

        return configs

    def validate_instances_against_api(self, discovered: DiscoveredInstances) -> list[InstanceNameDrift]:
        """Verify every configured instance exists in the discovered set.

        Raises ValueError if any configured CRN is not found or is archived.

        Returns a list of name drifts for configured instances whose CRN matches a live
        instance but whose name no longer matches the live API name.
        """
        active_by_crn = {d.crn: d.name for d in discovered.active}
        archived_by_crn = {d.crn: d.name for d in discovered.archived}
        all_by_crn = {**active_by_crn, **archived_by_crn}

        unrecognized = [cfg for cfg in self.instance_configs if cfg.crn not in all_by_crn]
        archived = [cfg for cfg in self.instance_configs if cfg.crn in archived_by_crn]

        errors = []
        if unrecognized:
            bullets = "\n".join(f"  - {cfg.name}, {cfg.crn}" for cfg in unrecognized)
            errors.append(
                f"Config file contains instances not found in account "
                f"{self.account_id} on plan {self.plan.value}:\n{bullets}\n"
                "(run `qauvern update` to fix automatically)"
            )
        if archived:
            bullets = "\n".join(f"  - {cfg.name}, {cfg.crn}" for cfg in archived)
            errors.append(
                f"Config file contains archived instances:\n{bullets}\n(run `qauvern update` to fix automatically)"
            )
        if errors:
            raise ValueError("\n\n".join(errors))

        return [
            InstanceNameDrift(crn=cfg.crn, config_name=cfg.name, api_name=active_by_crn[cfg.crn])
            for cfg in self.instance_configs
            if cfg.name != active_by_crn[cfg.crn]
        ]
