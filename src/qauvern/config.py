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

from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .models import NetGrant, Project


class ConfigParser:
    """Parser for YAML configuration files."""

    def __init__(self, config_path: str):
        """Initialize the config parser.

        Args:
            config_path: Path to the YAML configuration file
        """
        self.config_path = Path(config_path)
        self.config_data: dict = {}
        self._projects_cache: list[Project] = []
        self._projects_loaded = False

    def load(self) -> None:
        """Load and parse the configuration file."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")

        with open(self.config_path) as f:
            self.config_data = yaml.safe_load(f)

        self._validate_config()
        # Validate project constraints during load
        self.get_projects()

    def _validate_config(self) -> None:
        """Validate the configuration structure."""
        required_fields = ["account_id", "plan_id", "balance_period", "projects"]

        for field in required_fields:
            if field not in self.config_data:
                raise ValueError(f"Missing required field in config: {field}")

        if not isinstance(self.config_data["projects"], list):
            raise ValueError("'projects' must be a list")

        reserve = self.config_data.get("allocation_reserve_percent", 0.0)
        if not (0.0 <= float(reserve) < 100.0):
            raise ValueError("allocation_reserve_percent must be in range [0, 100)")

        balance_period = self.config_data["balance_period"]
        if "start_date" not in balance_period or "end_date" not in balance_period:
            raise ValueError("balance_period must contain start_date and end_date")

    def get_account_id(self) -> str:
        """Get the account ID from config."""
        return self.config_data["account_id"]

    def get_plan_id(self) -> str:
        """Get the plan ID from config."""
        return self.config_data["plan_id"]

    def get_minimum_allocation_seconds(self) -> int:
        """Get the global minimum allocation in seconds.

        Returns:
            Minimum allocation in seconds, defaults to 60 (1 minute) if not specified
        """
        return self.config_data.get("minimum_allocation_seconds", 60)

    def get_allocation_reserve_percent(self) -> float:
        """Get the allocation reserve percent from config. Defaults to 0.0."""
        return float(self.config_data.get("allocation_reserve_percent", 0.0))

    def get_balance_period(self) -> dict[str, datetime]:
        """Get the balance period dates."""
        period = self.config_data["balance_period"]
        return {
            "start_date": datetime.fromisoformat(period["start_date"]),
            "end_date": datetime.fromisoformat(period["end_date"]),
        }

    def get_projects(self) -> list[Project]:
        """Parse and return the list of projects.

        Note: Each project corresponds to exactly one service instance (CRN).
        """
        if self._projects_loaded:
            return self._projects_cache

        projects = []
        period = self.get_balance_period()

        for proj_data in self.config_data["projects"]:
            # Validate required fields
            required = ["name", "crn"]
            for field in required:
                if field not in proj_data:
                    raise ValueError(f"Project missing required field: {field}")

            # Use project-specific dates if provided, otherwise use balance period
            start_date = (
                datetime.fromisoformat(proj_data["start_date"]) if "start_date" in proj_data else period["start_date"]
            )
            end_date = datetime.fromisoformat(proj_data["end_date"]) if "end_date" in proj_data else period["end_date"]

            # Parse optional project_limit_seconds
            project_limit_seconds = proj_data.get("project_limit_seconds")

            # Parse optional net_grants
            net_grants = []
            for grant_data in proj_data.get("net_grants", []):
                grant_start = datetime.fromisoformat(grant_data["start_date"])
                grant_end = (
                    datetime.fromisoformat(grant_data["end_date"])
                    if "end_date" in grant_data
                    else grant_start + timedelta(days=28)
                )
                grant = NetGrant(
                    start_date=grant_start,
                    net_grant_seconds=grant_data["net_grant_seconds"],
                    end_date=grant_end,
                )
                net_grants.append(grant)

            target_usage_seconds = proj_data.get("target_usage_seconds")

            project = Project(
                name=proj_data["name"],
                crn=proj_data["crn"],
                target_usage_seconds=target_usage_seconds,
                start_date=start_date,
                end_date=end_date,
                description=proj_data.get("description"),
                project_limit_seconds=project_limit_seconds,
                net_grants=net_grants,
            )

            # Validate limit constraints
            if project_limit_seconds is not None and target_usage_seconds is not None:
                if project_limit_seconds < target_usage_seconds:
                    raise ValueError(
                        f"Project '{proj_data['name']}': project_limit_seconds ({project_limit_seconds}) "
                        f"must be >= target_usage_seconds ({target_usage_seconds})"
                    )
            projects.append(project)

        self._projects_cache = projects
        self._projects_loaded = True
        return projects

    def get_api_config(self) -> dict[str, str]:
        """Get API configuration if present."""
        return self.config_data.get("api", {})


def load_config(config_path: str) -> ConfigParser:
    """Load and return a configuration parser.

    Args:
        config_path: Path to the YAML configuration file

    Returns:
        Loaded ConfigParser instance
    """
    parser = ConfigParser(config_path)
    parser.load()
    return parser
