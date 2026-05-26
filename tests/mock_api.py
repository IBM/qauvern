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

from datetime import date, datetime, timedelta
from typing import Any

from qauvern.models import Account, Instance
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

        # Mock data storage
        self.accounts: dict[str, Account] = {}
        self.instances: dict[str, Instance] = {}
        self.usage_data: dict[str, dict] = {}
        self.daily_usage_data: dict[str, dict[date, int]] = {}

    def setup_account(
        self,
        account_id: str,
        target_usage_seconds: int,
        consumed_seconds: int = 0,
        available_seconds: int = 0,
    ) -> Account:
        """Setup a mock account for testing."""
        account = Account(
            plan_id="test-plan",
            account_id=account_id,
            target_usage_seconds=target_usage_seconds,
            consumed_seconds=consumed_seconds,
            available_seconds=available_seconds,
        )
        self.accounts[account_id] = account
        return account

    def setup_instance(
        self,
        crn: str,
        name: str,
        allocation_seconds: int,
        consumed_seconds: int = 0,
        limit_seconds: int | None = None,
        account_id: str | None = None,
    ) -> Instance:
        """Setup a mock instance for testing."""
        instance = Instance(
            crn=crn,
            name=name,
            allocation_seconds=allocation_seconds,
            limit_seconds=limit_seconds,
            consumed_seconds=consumed_seconds,
        )
        self.instances[crn] = instance

        # Add to account if specified
        if account_id and account_id in self.accounts:
            self.accounts[account_id].add_instance(instance)

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

    def get_account(self, account_id: str) -> Account:
        """Get mock account information."""
        if account_id not in self.accounts:
            raise ValueError(f"Account {account_id} not found in mock data")
        return self.accounts[account_id]

    def get_instance(self, instance_crn: str) -> Instance:
        """Get mock instance configuration."""
        if instance_crn not in self.instances:
            raise ValueError(f"Instance {instance_crn} not found in mock data")
        return self.instances[instance_crn]

    def get_instance_usage_seconds(
        self, instance_crn: str, start_date: datetime, end_date: datetime, account_id: str
    ) -> int:
        """Get mock usage analytics for an instance."""
        key = f"{instance_crn}:{start_date.isoformat()}:{end_date.isoformat()}"

        if key in self.usage_data:
            return self.usage_data[key]["total_seconds"]

        # Default to instance's consumed_seconds if no specific usage data
        return self.get_instance(instance_crn).consumed_seconds

    def get_rolling_window_seconds(self, instance_crn: str, account_id: str, days: int = 28) -> int:
        """Get mock usage in seconds for the rolling window period."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        return self.get_instance_usage_seconds(instance_crn, start_date, end_date, account_id)

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

    def list_instances(self, account_id: str, plan: Plan | None = None) -> list[Instance]:
        """List mock instances for an account.

        `plan` is accepted to match the real client signature; the mock does
        not filter by plan since each test scenario sets up instances directly.
        """
        if account_id not in self.accounts:
            raise ValueError(f"Account {account_id} not found")

        return self.accounts[account_id].instances

    def get_account_with_instances(self, account_id: str, plan: Plan | None = None) -> Account:
        """Get mock account with all instances populated.

        `plan` is accepted to match the real client signature; the mock does
        not filter by plan since each test scenario sets up instances directly.
        """
        return self.get_account(account_id)

    def create_instance(
        self,
        name: str,
        target: str,
        resource_group: str,
        plan: Plan,
        allocation_seconds: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Mock creating an instance."""
        plan_uuid = plan_id_for(plan)
        crn = f"crn:v1:bluemix:public:quantum-computing:{target}:a/mock-account:{name}::"
        instance = Instance(
            crn=crn,
            name=name,
            allocation_seconds=allocation_seconds or 0,
            limit_seconds=None,
            consumed_seconds=0,
            plan=plan_uuid,
        )
        self.instances[crn] = instance
        return {
            "id": crn,
            "name": name,
            "state": "active",
            "region_id": target,
            "resource_plan_id": plan_uuid,
        }


def create_test_scenario_basic() -> MockIBMQuantumAPIClient:
    """Create a basic test scenario with account and instances.

    Returns:
        Configured mock API client
    """
    client = MockIBMQuantumAPIClient()

    # Setup account with 30 QAU = 30 * 1600 * 60 = 2,880,000 seconds
    client.setup_account(account_id="test-account-123", target_usage_seconds=2880000, consumed_seconds=500000)

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

    # Setup account with 10 QAU = 960,000 seconds
    client.setup_account(account_id="test-account-456", target_usage_seconds=960000, consumed_seconds=200000)

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
