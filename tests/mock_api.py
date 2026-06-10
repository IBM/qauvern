# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Mock API client for testing."""

import dataclasses
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from qauvern.models import Account, DiscoveredInstance, DiscoveredInstances, InstanceState
from qauvern.plan import Plan, plan_id_for


class MockIBMQuantumAPIClient:
    """Mock API client for testing without real API calls."""

    def __init__(
        self,
        api_key: str | None = None,
        iam_token: str | None = None,
        base_url: str | None = None,
    ):
        """Initialize the mock API client."""
        self.api_key = api_key or "mock-api-key"
        self.iam_token = iam_token or "mock-iam-token"
        self.base_url = base_url or "https://mock.quantum.cloud.ibm.com"

        self._account_params: dict[str, dict] = {}
        self._account_instances: dict[str, list[str]] = {}
        self._archived_crns: set[str] = set()
        self.instances: dict[str, InstanceState] = {}
        self.usage_data: dict[str, dict] = {}
        self.daily_usage_data: dict[str, dict[date, int]] = {}

    def _build_account(self, account_id: str) -> Account:
        if account_id not in self._account_params:
            raise ValueError(f"Account {account_id} not found in mock data")
        params = self._account_params[account_id]
        instances = tuple(
            self.instances[crn] for crn in self._account_instances.get(account_id, []) if crn in self.instances
        )
        return Account(account_id=account_id, plan_id="test-plan", limit_seconds=None, instances=instances, **params)

    def setup_account(
        self,
        account_id: str,
        allocation_budget_seconds: int,
        unallocated_seconds: int = 0,
    ) -> Account:
        """Setup a mock account for testing."""
        self._account_params[account_id] = {
            "allocation_budget_seconds": allocation_budget_seconds,
            "unallocated_seconds": unallocated_seconds,
        }
        self._account_instances.setdefault(account_id, [])
        return self._build_account(account_id)

    def setup_instance(
        self,
        crn: str,
        name: str,
        allocation_seconds: int,
        consumed_seconds: int = 0,
        limit_seconds: int | None = None,
        account_id: str | None = None,
        archived: bool = False,
    ) -> InstanceState:
        """Setup a mock instance for testing."""
        instance = InstanceState(
            crn=crn,
            name=name,
            allocation_seconds=allocation_seconds,
            limit_seconds=limit_seconds,
            consumed_seconds=consumed_seconds,
            detailed_usage=None,
        )
        self.instances[crn] = instance
        if archived:
            self._archived_crns.add(crn)
        if account_id:
            self._account_instances.setdefault(account_id, []).append(crn)
        return instance

    def setup_usage(self, instance_crn: str, start_date: datetime, end_date: datetime, consumed_seconds: int) -> None:
        """Setup mock usage data for an instance."""
        key = f"{instance_crn}:{start_date.isoformat()}:{end_date.isoformat()}"
        self.usage_data[key] = {
            "instance_crn": instance_crn,
            "start_date": start_date,
            "end_date": end_date,
            "total_seconds": consumed_seconds,
        }

    def setup_daily_usage(self, instance_crn: str, daily_data: dict[date, int]) -> None:
        """Setup per-day usage data for rolloff calculations."""
        self.daily_usage_data[instance_crn] = daily_data

    def get_daily_usage(self, instance_crn: str, account_id: str, start_date: date, end_date: date) -> dict[date, int]:
        """Return per-day usage for the half-open interval [start_date, end_date)."""
        all_days = self.daily_usage_data.get(instance_crn, {})
        return {d: s for d, s in all_days.items() if start_date <= d < end_date}

    def get_instance(self, discovered: DiscoveredInstance) -> InstanceState:
        """Get mock instance configuration."""
        if discovered.crn not in self.instances:
            raise ValueError(f"Instance {discovered.crn} not found in mock data")
        return self.instances[discovered.crn]

    def get_instance_usage_seconds(
        self, instance_crn: str, start_date: datetime, end_date: datetime, account_id: str
    ) -> int:
        """Get mock usage analytics for an instance."""
        key = f"{instance_crn}:{start_date.isoformat()}:{end_date.isoformat()}"

        if key in self.usage_data:
            return self.usage_data[key]["total_seconds"]

        # Default to instance's consumed_seconds if no specific usage data
        if instance_crn not in self.instances:
            raise ValueError(f"Instance {instance_crn} not found in mock data")
        return self.instances[instance_crn].consumed_seconds

    def update_instance_allocation(self, instance_crn: str, allocation_seconds: int) -> bool:
        """Update the allocation for a mock instance."""
        if instance_crn not in self.instances:
            raise ValueError(f"Instance {instance_crn} not found")

        self.instances[instance_crn].allocation_seconds = allocation_seconds
        return True

    def update_instance_limit(self, instance_crn: str, limit_seconds: int | None) -> bool:
        """Update the limit for a mock instance."""
        if instance_crn not in self.instances:
            raise ValueError(f"Instance {instance_crn} not found")

        self.instances[instance_crn].limit_seconds = limit_seconds
        return True

    def discover_instances(self, account_id: str, plan: Plan | None = None) -> DiscoveredInstances:
        """List mock instances for an account, split into live and archived.

        `plan` is accepted to match the real client signature; the mock does
        not filter by plan since each test scenario sets up instances directly.
        """
        if account_id not in self._account_params:
            raise ValueError(f"Account {account_id} not found")
        live = []
        archived = []
        for crn in self._account_instances.get(account_id, []):
            if crn not in self.instances:
                continue
            src = self.instances[crn]
            instance = DiscoveredInstance(
                crn=crn,
                name=src.name,
                allocation_seconds=src.allocation_seconds,
                limit_seconds=src.limit_seconds,
            )
            if crn in self._archived_crns:
                archived.append(instance)
            else:
                live.append(instance)
        return DiscoveredInstances(active=tuple(live), archived=tuple(archived))

    def get_account(
        self,
        account_id: str,
        plan: Plan | None = None,
        instances: Sequence[InstanceState] | None = None,
    ) -> Account:
        """Get mock account, optionally attaching the given instances directly."""
        account = self._build_account(account_id)
        if instances is not None:
            return dataclasses.replace(account, instances=tuple(instances))
        return account

    def create_instance(
        self,
        name: str,
        target: str,
        resource_group: str,
        plan: Plan,
        allocation_seconds: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        crn = f"crn:v1:bluemix:public:quantum-computing:{target}:a/mock-account:{name}::"
        instance = InstanceState(
            crn=crn,
            name=name,
            allocation_seconds=allocation_seconds or 0,
            limit_seconds=None,
            consumed_seconds=0,
            detailed_usage=None,
        )
        self.instances[crn] = instance
        return {
            "id": crn,
            "name": name,
            "state": "active",
            "region_id": target,
            "resource_plan_id": plan_id_for(plan),
        }


def create_test_scenario_basic() -> MockIBMQuantumAPIClient:
    """Create a basic test scenario with account and instances.

    Returns:
        Configured mock API client
    """
    client = MockIBMQuantumAPIClient()

    # Setup account with 2,880,000 seconds (800 hours)
    client.setup_account(account_id="test-account-123", allocation_budget_seconds=2880000)

    # Setup instances
    client.setup_instance(
        crn="crn:test:instance-1",
        name="Active Instance 1",
        allocation_seconds=800000,
        consumed_seconds=750000,  # High usage, fairness ~0.94
        limit_seconds=1000000,
        account_id="test-account-123",
    )

    client.setup_instance(
        crn="crn:test:instance-2",
        name="Active Instance 2",
        allocation_seconds=600000,
        consumed_seconds=300000,  # Medium usage, fairness 0.5
        limit_seconds=800000,
        account_id="test-account-123",
    )

    client.setup_instance(
        crn="crn:test:instance-3",
        name="Inactive Instance",
        allocation_seconds=500000,
        consumed_seconds=1000,  # Very low usage, fairness ~0.002
        limit_seconds=600000,
        account_id="test-account-123",
    )

    client.setup_instance(
        crn="crn:test:instance-4",
        name="Unused Instance",
        allocation_seconds=400000,
        consumed_seconds=0,  # No usage
        limit_seconds=500000,
        account_id="test-account-123",
    )

    return client


def create_test_scenario_overallocated() -> MockIBMQuantumAPIClient:
    """Create a test scenario where instances are over-allocated.

    Returns:
        Configured mock API client
    """
    client = MockIBMQuantumAPIClient()

    # Setup account with 960,000 seconds (~267 hours)
    client.setup_account(account_id="test-account-456", allocation_budget_seconds=960000)

    # Setup instances with total allocation exceeding account
    client.setup_instance(
        crn="crn:test:instance-a",
        name="Instance A",
        allocation_seconds=500000,
        consumed_seconds=100000,
        account_id="test-account-456",
    )

    client.setup_instance(
        crn="crn:test:instance-b",
        name="Instance B",
        allocation_seconds=600000,
        consumed_seconds=100000,
        account_id="test-account-456",
    )

    # Total allocation = 1,100,000 > 960,000 (over-allocated)

    return client
