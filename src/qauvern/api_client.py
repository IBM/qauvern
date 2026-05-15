# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""API client for IBM Quantum and Resource Controller services."""

import os
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests

from .models import Account, Instance, InstanceUsage

# Known plan ID to name mappings
# These are common IBM Quantum service plans
PLAN_NAMES = {
    # In staging
    "91b2c828-2952-4f05-aed8-bedf92c6c480": "Internal",
    "7f666d17-7893-47d8-b9e5-e8b5c0b5c5c5": "Premium",
    "5304b575-3cff-4455-90dc-ae4367762093": "Paygo",
}

PLAN_IDS_BY_NAME = {name.lower(): plan_id for plan_id, name in PLAN_NAMES.items()}

QUANTUM_COMPUTING_RESOURCE_ID = "b6049020-80f4-11eb-a0f7-e35ec9b4054f"


def get_plan_name(plan_id: str | None) -> str:
    """Get friendly plan name from plan ID.

    Args:
        plan_id: The plan UUID (can be None)

    Returns:
        Friendly plan name if known, otherwise truncated plan ID
    """
    if not plan_id:
        return "Unknown"

    # Check if we have a friendly name for this plan
    if plan_id in PLAN_NAMES:
        return PLAN_NAMES[plan_id]

    # Return first 8 characters of UUID for unknown plans
    return plan_id[:8] if len(plan_id) >= 8 else plan_id


def get_plan_id(plan_name_or_id: str) -> str:
    """Resolve a plan name or UUID to a plan UUID.

    Accepts either a friendly name (internal, premium, paygo) or a raw UUID.
    """
    lower = plan_name_or_id.lower()
    if lower in PLAN_IDS_BY_NAME:
        return PLAN_IDS_BY_NAME[lower]

    if re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        plan_name_or_id,
        re.IGNORECASE,
    ):
        return plan_name_or_id

    known = ", ".join(PLAN_IDS_BY_NAME.keys())
    raise ValueError(f"Unknown plan '{plan_name_or_id}'. Known plans: {known}. Or pass a raw plan UUID.")


class IBMQuantumAPIClient:
    """Client for IBM Quantum API with IAM token authentication."""

    def __init__(
        self,
        api_key: str | None = None,
        iam_token: str | None = None,
        base_url: str | None = None,
        staging: bool = False,
    ):
        """Initialize the API client.

        Args:
            api_key: IBM Cloud API key for IAM authentication (defaults to IBMCLOUD_API_KEY env var)
            iam_token: Pre-obtained IAM token (optional, will be obtained if not provided)
            base_url: Base URL for the API (defaults to production, us-east region)
            staging: If True, use staging environment (test.cloud.ibm.com instead of cloud.ibm.com)
        """
        self.api_key = api_key or os.getenv("IBMCLOUD_API_KEY")
        self.iam_token = iam_token
        self.staging = staging

        # Set IAM token URL based on staging flag
        if staging:
            self.iam_token_url = "https://iam.test.cloud.ibm.com/identity/token"
        else:
            self.iam_token_url = "https://iam.cloud.ibm.com/identity/token"

        if not self.api_key and not self.iam_token:
            raise ValueError(
                "Either API key must be provided/set in IBMCLOUD_API_KEY env var, or IAM token must be provided"
            )

        # Set base URLs based on staging flag
        if staging:
            self.base_url = base_url or "https://quantum.test.cloud.ibm.com/api"
            self.resource_controller_url = "https://resource-controller.test.cloud.ibm.com"
        else:
            self.base_url = base_url or "https://quantum.cloud.ibm.com/api"
            self.resource_controller_url = "https://resource-controller.cloud.ibm.com"

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "IBM-API-Version": "2026-02-15"})

        # Obtain IAM token if not provided
        if not self.iam_token:
            self._obtain_iam_token()
        else:
            self._set_authorization_header()

    @staticmethod
    def _extract_region_from_crn(crn: str) -> str:
        """Extract the region from a CRN.

        CRN format: crn:v1:bluemix:public:quantum-computing:REGION:a/ACCOUNT_ID:INSTANCE_ID::

        Args:
            crn: The Cloud Resource Name

        Returns:
            The region string (e.g., 'us-east', 'eu-de')
        """
        # CRN format: crn:version:cname:ctype:service-name:location:scope:service-instance:resource-type:resource
        parts = crn.split(":")
        if len(parts) >= 6:
            return parts[5]  # Region is the 6th component (index 5)
        return "us-east"  # Default to us-east if parsing fails

    def _get_regional_base_url(self, crn: str) -> str:
        """Get the appropriate regional base URL for a given CRN.

        Args:
            crn: The Cloud Resource Name

        Returns:
            The regional base URL (e.g., 'https://eu-de.quantum.cloud.ibm.com/api')
        """
        region = self._extract_region_from_crn(crn)

        # Determine the domain based on staging flag
        domain = "test.cloud.ibm.com" if self.staging else "cloud.ibm.com"

        # us-east is the default region and uses the main endpoint
        if region == "us-east":
            return f"https://quantum.{domain}/api"
        else:
            # Other regions use region-specific endpoints
            return f"https://{region}.quantum.{domain}/api"

    def _obtain_iam_token(self) -> None:
        """Obtain an IAM token from IBM Cloud IAM service."""
        if not self.api_key:
            raise ValueError("API key is required to obtain IAM token")

        response = requests.post(
            self.iam_token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                "apikey": self.api_key,
            },
        )
        response.raise_for_status()

        token_data = response.json()
        self.iam_token = token_data["access_token"]
        self._set_authorization_header()

    def _set_authorization_header(self) -> None:
        """Set the Authorization header with the IAM token."""
        self.session.headers.update({"Authorization": f"Bearer {self.iam_token}"})

    def refresh_token(self) -> None:
        """Refresh the IAM token.

        Call this method if you receive authentication errors, as IAM tokens expire.
        """
        self._obtain_iam_token()

    def get_account(self, account_id: str, plan_id: str) -> Account:
        """Get account information including allocation for a specific plan.

        Args:
            account_id: The IBM Cloud account ID
            plan_id: The plan ID to get allocation for

        Returns:
            Account object with allocation information for the specified plan
        """
        url = f"{self.base_url}/v1/accounts/{account_id}"
        params = {"plan_id": plan_id}
        response = self.session.get(url, params=params)
        response.raise_for_status()

        data = response.json()
        # API returns plans array, extract the first (and only) plan
        plans = data.get("plans", [])
        if not plans:
            raise ValueError(f"No plan found for plan_id {plan_id}")

        plan = plans[0]
        return Account(
            account_id=account_id,
            plan_id=plan_id,
            target_usage_seconds=plan.get("usage_allocation_seconds", 0),
            consumed_seconds=0,  # Will be calculated from instances
            available_seconds=plan.get("unallocated_usage_seconds", 0),
            limit_seconds=plan.get("usage_limit_seconds"),
        )

    def get_instance(self, instance_crn: str) -> Instance:
        """Get instance configuration including allocation and limits.

        Args:
            instance_crn: The CRN of the service instance

        Returns:
            Instance object with current configuration
        """
        # Use the /v1/instance endpoint with Service-CRN header
        # Use regional base URL based on the CRN's region
        base_url = self._get_regional_base_url(instance_crn)
        url = f"{base_url}/v1/instance"
        headers = {"Service-CRN": instance_crn}
        response = self.session.get(url, headers=headers)

        # Check for HTTP errors
        if not response.ok:
            error_msg = f"HTTP {response.status_code} for {url}"
            try:
                error_data = response.json()
                if "message" in error_data:
                    error_msg = f"{error_msg}: {error_data['message']}"
            except Exception:
                error_msg = f"{error_msg}: {response.text[:200]}"
            raise requests.HTTPError(error_msg)

        # Parse JSON response
        try:
            data = response.json()
        except ValueError as e:
            # Provide detailed error information
            error_details = f"Invalid JSON response from {url}.\n"
            error_details += f"Status Code: {response.status_code}\n"
            error_details += f"Content-Type: {response.headers.get('Content-Type', 'Not specified')}\n"
            error_details += f"Response length: {len(response.text)} bytes\n"
            error_details += f"Response body: {response.text[:500]}"
            raise ValueError(error_details) from e
        # Map API response fields to Instance model
        # API returns: instance_limit_seconds, usage_allocation_seconds, backends, plan_id
        return Instance(
            crn=instance_crn,
            name=data.get("name", ""),
            allocation_seconds=int(data.get("usage_allocation_seconds", 0)),
            limit_seconds=(
                int(float(data.get("instance_limit_seconds", 0))) if data.get("instance_limit_seconds") else None
            ),
            consumed_seconds=0,  # Will be populated by get_instance_usage
            plan=data.get("plan_id", ""),
        )

    def get_instance_usage_28d(self, instance_crn: str) -> int:
        """Get 28-day rolling window usage for an instance using /v1/instances/usage endpoint.

        This endpoint does not require admin privileges and returns usage for the last 28 days.

        Args:
            instance_crn: The CRN of the service instance

        Returns:
            Consumed seconds in the 28-day window
        """
        # Use the /v1/instances/usage endpoint with Service-CRN header
        base_url = self._get_regional_base_url(instance_crn)
        url = f"{base_url}/v1/instances/usage"
        headers = {"Service-CRN": instance_crn}
        response = self.session.get(url, headers=headers)

        # Check for HTTP errors
        if not response.ok:
            error_msg = f"HTTP {response.status_code} for {url}"
            try:
                error_data = response.json()
                if "message" in error_data:
                    error_msg = f"{error_msg}: {error_data['message']}"
            except Exception:
                error_msg = f"{error_msg}: {response.text[:200]}"
            raise requests.HTTPError(error_msg)

        # Parse JSON response
        try:
            data = response.json()
        except ValueError as e:
            error_details = f"Invalid JSON response from {url}.\n"
            error_details += f"Status Code: {response.status_code}\n"
            error_details += f"Content-Type: {response.headers.get('Content-Type', 'Not specified')}\n"
            error_details += f"Response length: {len(response.text)} bytes\n"
            error_details += f"Response body: {response.text[:500]}"
            raise ValueError(error_details) from e

        # Return consumed seconds from the 28-day window
        return int(data.get("usage_consumed_seconds", 0))

    def get_instance_usage(
        self, instance_crn: str, start_date: datetime, end_date: datetime, account_id: str
    ) -> InstanceUsage:
        """Get usage analytics for an instance with custom date range.

        Args:
            instance_crn: The CRN of the service instance
            start_date: Start date for usage query
            end_date: End date for usage query
            account_id: The account ID (used as subscription_id)

        Returns:
            InstanceUsage object with consumption data
        """
        # Use the /v1/analytics/usage endpoint with instance as query parameter
        # Use regional base URL based on the CRN's region
        base_url = self._get_regional_base_url(instance_crn)
        url = f"{base_url}/v1/analytics/usage"

        # Format dates as ISO 8601 strings and pass instance CRN as query parameter
        params = {
            "instance": instance_crn,  # Pass full CRN directly (requests will handle array serialization)
            "interval_start": (start_date.isoformat() + "Z" if not start_date.tzinfo else start_date.isoformat()),
            "interval_end": (end_date.isoformat() + "Z" if not end_date.tzinfo else end_date.isoformat()),
        }

        # Add Account-Id header for analytics endpoint
        headers = {"Account-Id": account_id}

        # Session already has Authorization header from _ensure_authenticated
        response = self.session.get(url, params=params, headers=headers)

        # Check for HTTP errors
        if not response.ok:
            error_msg = f"HTTP {response.status_code} for {url}"
            try:
                error_data = response.json()
                if "message" in error_data:
                    error_msg = f"{error_msg}: {error_data['message']}"
            except Exception:
                error_msg = f"{error_msg}: {response.text[:200]}"
            raise requests.HTTPError(error_msg)

        # Parse JSON response
        try:
            data = response.json()
        except ValueError as e:
            raise ValueError(f"Invalid JSON response from {url}. Response starts with: {response.text[:200]}") from e

        # Map API response fields from analytics endpoint
        # The analytics endpoint returns usage in MILLISECONDS, convert to seconds
        usage_ms = data.get("usage", 0)
        usage_seconds = int(usage_ms / 1000) if usage_ms else 0

        return InstanceUsage(
            crn=instance_crn,
            consumed_seconds=usage_seconds,
            allocation_seconds=0,  # Analytics endpoint doesn't return allocation
            limit_seconds=None,  # Analytics endpoint doesn't return limit
        )

    def get_rolling_window_usage(self, instance_crn: str, account_id: str, days: int = 28) -> InstanceUsage:
        """Get usage for the rolling window period.

        Args:
            instance_crn: The CRN of the service instance
            account_id: The account ID (used as subscription_id)
            days: Number of days in the rolling window (default: 28)

        Returns:
            InstanceUsage object for the rolling window
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        return self.get_instance_usage(instance_crn, start_date, end_date, account_id)

    def get_detailed_usage(self, instance_crn: str, account_id: str) -> dict:
        """Get detailed usage data for multiple time periods using analytics endpoint.

        This method fetches usage data for 14-day, 7-day, 3-day, and 24-hour periods.
        Requires admin privileges to access the analytics endpoint.

        Args:
            instance_crn: The CRN of the service instance
            account_id: The account ID for analytics authentication

        Returns:
            Dictionary with keys: consumed_14day, consumed_7day, consumed_3day, consumed_24h
        """
        end_date = datetime.now()

        # Fetch usage for different time periods
        usage_14d = self.get_instance_usage(instance_crn, end_date - timedelta(days=14), end_date, account_id)
        usage_7d = self.get_instance_usage(instance_crn, end_date - timedelta(days=7), end_date, account_id)
        usage_3d = self.get_instance_usage(instance_crn, end_date - timedelta(days=3), end_date, account_id)
        usage_24h = self.get_instance_usage(instance_crn, end_date - timedelta(hours=24), end_date, account_id)

        return {
            "consumed_14day": usage_14d.consumed_seconds,
            "consumed_7day": usage_7d.consumed_seconds,
            "consumed_3day": usage_3d.consumed_seconds,
            "consumed_24h": usage_24h.consumed_seconds,
        }

    def get_daily_usage(self, instance_crn: str, account_id: str, start_date: date, end_date: date) -> dict[date, int]:
        """Get per-day usage for rolloff calculations via analytics grouped endpoint.

        Calls /v1/analytics/usage_grouped_by_date?group_by=instance.
        end_date is exclusive (half-open interval).

        Returns:
            Dict mapping date -> seconds consumed on that day
        """
        base_url = self._get_regional_base_url(instance_crn)
        url = f"{base_url}/v1/analytics/usage_grouped_by_date"
        params = {
            "group_by": "instance",
            "instance": instance_crn,
            "interval_start": datetime.combine(start_date, datetime.min.time()).isoformat() + "Z",
            "interval_end": datetime.combine(end_date, datetime.min.time()).isoformat() + "Z",
        }
        headers = {"Account-Id": account_id}
        response = self.session.get(url, params=params, headers=headers)

        if not response.ok:
            error_msg = f"HTTP {response.status_code} for {url}"
            try:
                error_data = response.json()
                if "message" in error_data:
                    error_msg = f"{error_msg}: {error_data['message']}"
            except Exception:
                error_msg = f"{error_msg}: {response.text[:200]}"
            raise requests.HTTPError(error_msg)

        try:
            data = response.json()
        except ValueError as e:
            raise ValueError(f"Invalid JSON response from {url}: {response.text[:200]}") from e

        result: dict[date, int] = {}
        for entry in data.get("data", []):
            day = date.fromisoformat(entry["interval_start"][:10])
            usage_ms = entry.get("usage", 0) or 0
            result[day] = int(usage_ms / 1000)
        return result

    def update_instance_allocation(self, instance_crn: str, allocation_seconds: int) -> bool:
        """Update the allocation for an instance.

        Args:
            instance_crn: The CRN of the service instance
            allocation_seconds: New allocation in seconds

        Returns:
            True if successful
        """
        # Note: Allocation updates are done through IBM Cloud Resource Controller API
        # The CRN needs to be URL-encoded when used in the path for Resource Controller
        url = f"{self.resource_controller_url}/v2/resource_instances/{quote(instance_crn, safe='')}"
        # The API broker expects usage_allocation_seconds as a string, not an integer
        payload = {"parameters": {"usage_allocation_seconds": str(allocation_seconds)}}

        response = self.session.patch(url, json=payload)

        # Provide detailed error message for debugging
        if not response.ok:
            error_msg = f"HTTP {response.status_code} for {url}"
            try:
                error_data = response.json()
                if "errors" in error_data:
                    error_msg = f"{error_msg}: {error_data['errors']}"
                elif "message" in error_data:
                    error_msg = f"{error_msg}: {error_data['message']}"
                else:
                    error_msg = f"{error_msg}: {error_data}"
            except Exception:
                error_msg = f"{error_msg}: {response.text[:500]}"
            raise requests.HTTPError(error_msg)

        return True

    def update_instance_limit(self, instance_crn: str, limit_seconds: int | None) -> bool:
        """Update the limit for an instance.

        Args:
            instance_crn: The CRN of the service instance
            limit_seconds: New limit in seconds (None to remove limit)

        Returns:
            True if successful
        """
        # Use the /v1/instances/configuration endpoint with Service-CRN header
        # Use regional base URL based on the CRN's region
        base_url = self._get_regional_base_url(instance_crn)
        url = f"{base_url}/v1/instances/configuration"
        headers = {"Service-CRN": instance_crn}
        # API expects instance_limit field, not limit_seconds
        payload = {"instance_limit": limit_seconds}

        response = self.session.put(url, json=payload, headers=headers)
        response.raise_for_status()
        return True

    def create_instance(
        self,
        name: str,
        target: str,
        resource_group: str,
        resource_plan_id: str,
        allocation_seconds: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new IBM Quantum service instance via Resource Controller API.

        Args:
            name: Instance name
            target: Deployment region (e.g., "us-east", "eu-de")
            resource_group: Resource group ID
            resource_plan_id: Plan UUID
            allocation_seconds: Optional initial allocation in seconds
            tags: Optional list of tags

        Returns:
            Dict with the created instance data from the API response.
        """
        url = f"{self.resource_controller_url}/v2/resource_instances"
        payload: dict[str, Any] = {
            "name": name,
            "target": target,
            "resource_group": resource_group,
            "resource_plan_id": resource_plan_id,
            "resource_id": QUANTUM_COMPUTING_RESOURCE_ID,
        }

        if allocation_seconds is not None:
            payload["parameters"] = {"usage_allocation_seconds": str(allocation_seconds)}

        if tags:
            payload["tags"] = tags

        response = self.session.post(url, json=payload)

        if not response.ok:
            error_msg = f"HTTP {response.status_code} creating instance"
            try:
                error_data = response.json()
                if "errors" in error_data:
                    error_msg = f"{error_msg}: {error_data['errors']}"
                elif "message" in error_data:
                    error_msg = f"{error_msg}: {error_data['message']}"
                else:
                    error_msg = f"{error_msg}: {error_data}"
            except Exception:
                error_msg = f"{error_msg}: {response.text[:500]}"
            raise requests.HTTPError(error_msg)

        return response.json()

    def get_instance_name_from_crn(self, instance_crn: str) -> str:
        """Get the friendly name of an instance from its CRN using Resource Controller API.

        Args:
            instance_crn: The CRN of the service instance

        Returns:
            The friendly name of the instance, or empty string if not found
        """
        # Use IBM Cloud Resource Controller API to get instance details
        url = f"{self.resource_controller_url}/v2/resource_instances/{quote(instance_crn, safe='')}"

        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            return data.get("name", "")
        except Exception:
            return ""

    def list_instances(self, account_id: str) -> list[Instance]:
        """List all quantum computing instances for an account using Resource Controller API.

        Args:
            account_id: The IBM Cloud account ID

        Returns:
            List of Instance objects with CRN and name populated
        """
        url = f"{self.resource_controller_url}/v2/resource_instances"
        params: dict[str, Any] = {
            "resource_id": QUANTUM_COMPUTING_RESOURCE_ID,
            "account_id": account_id,
        }

        instances = []
        while True:
            response = self.session.get(url, params=params)

            if not response.ok:
                error_msg = f"HTTP {response.status_code} for {url}"
                try:
                    error_data = response.json()
                    if "message" in error_data:
                        error_msg = f"{error_msg}: {error_data['message']}"
                except Exception:
                    error_msg = f"{error_msg}: {response.text[:200]}"
                raise requests.HTTPError(error_msg)

            try:
                data = response.json()
            except ValueError as e:
                raise ValueError(
                    f"Invalid JSON response from {url}. Response starts with: {response.text[:200]}"
                ) from e

            for resource in data.get("resources", []):
                crn = resource.get("id")
                name = resource.get("name", "")
                if crn:
                    instances.append(
                        Instance(
                            crn=crn,
                            name=name,
                            allocation_seconds=0,
                            limit_seconds=None,
                            consumed_seconds=0,
                        )
                    )

            next_url = data.get("next_url")
            if not next_url:
                break
            parsed = urlparse(next_url)
            start_token = parse_qs(parsed.query).get("start", [None])[0]
            if not start_token:
                break
            params["start"] = start_token

        return instances

    def get_account_with_instances(self, account_id: str, plan_id: str) -> Account:
        """Get account with instances filtered by plan, populated with full data.

        Args:
            account_id: The IBM Cloud account ID
            plan_id: The plan ID to filter instances by

        Returns:
            Account object with instances list populated with full allocation, usage, and plan data
            Only includes instances that match the specified plan_id
            Account-level allocation comes from API, consumed is calculated from matching instances
        """
        # Get account allocation from API (this is the account/plan allocation)
        account = self.get_account(account_id, plan_id)

        instances = self.list_instances(account_id)

        # Fetch full instance data for each instance (allocation, usage, plan)
        # and filter by plan_id
        total_consumed = 0

        for instance in instances:
            try:
                # Get full instance data from regional Quantum API
                full_instance = self.get_instance(instance.crn)
                # Update the instance with full data
                instance.allocation_seconds = full_instance.allocation_seconds
                instance.limit_seconds = full_instance.limit_seconds
                instance.plan = full_instance.plan

                # Only include instances that match the specified plan_id
                if instance.plan != plan_id:
                    continue

                # Get usage data
                usage = self.get_rolling_window_usage(instance.crn, account_id)
                instance.consumed_seconds = usage.consumed_seconds

                # Add to consumed total
                total_consumed += instance.consumed_seconds

                account.add_instance(instance)
            except Exception as e:
                # If we can't fetch full data, skip this instance
                print(f"Warning: Could not fetch full data for instance {instance.name}: {e}")

        # Update account consumed from sum of instance usage
        account.consumed_seconds = total_consumed

        return account
