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
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests

from .models import Account, AccountAllocation, Instance, InstanceIdentifier
from .plan import Plan, plan_id_for

QUANTUM_COMPUTING_RESOURCE_ID = "b6049020-80f4-11eb-a0f7-e35ec9b4054f"


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

    def _request(
        self,
        method: str,
        url: str,
        *,
        crn: str | None = None,
        account_id: str | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Send a request, attaching Service-CRN/Account-Id headers and checking for errors."""
        headers = dict(kwargs.pop("headers", None) or {})
        if crn is not None:
            headers["Service-CRN"] = crn
        if account_id is not None:
            headers["Account-Id"] = account_id
        response = self.session.request(method, url, headers=headers or None, **kwargs)
        if response.ok:
            return response

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

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """Like _request, but parses and returns the JSON response body."""
        response = self._request(method, url, **kwargs)
        try:
            return response.json()
        except Exception as e:
            details = (
                f"Invalid JSON response from {url}.\n"
                f"Status Code: {response.status_code}\n"
                f"Content-Type: {response.headers.get('Content-Type', 'Not specified')}\n"
                f"Response length: {len(response.text)} bytes\n"
                f"Response body: {response.text[:500]}"
            )
            raise Exception(details) from e

    def get_account(self, account_id: str, plan: Plan) -> AccountAllocation:
        """Get account allocation for a specific plan."""
        plan_id = plan_id_for(plan)
        url = f"{self.base_url}/v1/accounts/{account_id}"
        data = self._request_json("GET", url, params={"plan_id": plan_id})

        plans = data.get("plans", [])
        if not plans:
            raise ValueError(f"No plan found for plan {plan.value} (plan_id {plan_id})")

        api_plan = plans[0]
        return AccountAllocation(
            account_id=account_id,
            plan_id=plan_id,
            target_usage_seconds=api_plan.get("usage_allocation_seconds", 0),
            available_seconds=api_plan.get("unallocated_usage_seconds", 0),
            limit_seconds=api_plan.get("usage_limit_seconds"),
        )

    def get_instance(self, instance_crn: str) -> Instance:
        """Get instance configuration including allocation and limits.

        Args:
            instance_crn: The CRN of the service instance

        Returns:
            Instance object with current configuration
        """
        url = f"{self._get_regional_base_url(instance_crn)}/v1/instance"
        data = self._request_json("GET", url, crn=instance_crn)

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
        url = f"{self._get_regional_base_url(instance_crn)}/v1/instances/usage"
        data = self._request_json("GET", url, crn=instance_crn)
        return int(data.get("usage_consumed_seconds", 0))

    def get_instance_usage_seconds(
        self, instance_crn: str, start_date: datetime, end_date: datetime, account_id: str
    ) -> int:
        """Get usage analytics for an instance with custom date range.

        Args:
            instance_crn: The CRN of the service instance
            start_date: Start date for usage query
            end_date: End date for usage query
            account_id: The account ID (used as subscription_id)

        Returns:
            Consumed seconds in the given date range
        """
        url = f"{self._get_regional_base_url(instance_crn)}/v1/analytics/usage"
        params = {
            "instance": instance_crn,  # Pass full CRN directly (requests will handle array serialization)
            "interval_start": (start_date.isoformat() + "Z" if not start_date.tzinfo else start_date.isoformat()),
            "interval_end": (end_date.isoformat() + "Z" if not end_date.tzinfo else end_date.isoformat()),
        }
        data = self._request_json("GET", url, account_id=account_id, params=params)

        # The analytics endpoint returns usage in MILLISECONDS, convert to seconds
        usage_ms = data.get("usage", 0)
        return int(usage_ms / 1000) if usage_ms else 0

    def get_rolling_window_seconds(self, instance_crn: str, account_id: str, days: int = 28) -> int:
        """Get usage in seconds for the rolling window period.

        Args:
            instance_crn: The CRN of the service instance
            account_id: The account ID (used as subscription_id)
            days: Number of days in the rolling window (default: 28)

        Returns:
            Consumed seconds in the rolling window
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        return self.get_instance_usage_seconds(instance_crn, start_date, end_date, account_id)

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

        return {
            "consumed_14day": self.get_instance_usage_seconds(
                instance_crn, end_date - timedelta(days=14), end_date, account_id
            ),
            "consumed_7day": self.get_instance_usage_seconds(
                instance_crn, end_date - timedelta(days=7), end_date, account_id
            ),
            "consumed_3day": self.get_instance_usage_seconds(
                instance_crn, end_date - timedelta(days=3), end_date, account_id
            ),
            "consumed_24h": self.get_instance_usage_seconds(
                instance_crn, end_date - timedelta(hours=24), end_date, account_id
            ),
        }

    def get_daily_usage(self, instance_crn: str, account_id: str, start_date: date, end_date: date) -> dict[date, int]:
        """Get per-day usage for rolloff calculations via analytics grouped endpoint.

        Calls /v1/analytics/usage_grouped_by_date?group_by=instance.
        end_date is exclusive (half-open interval).

        Returns:
            Dict mapping date -> seconds consumed on that day
        """
        url = f"{self._get_regional_base_url(instance_crn)}/v1/analytics/usage_grouped_by_date"
        params = {
            "group_by": "instance",
            "instance": instance_crn,
            "interval_start": datetime.combine(start_date, datetime.min.time()).isoformat() + "Z",
            "interval_end": datetime.combine(end_date, datetime.min.time()).isoformat() + "Z",
        }
        data = self._request_json("GET", url, account_id=account_id, params=params)

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
        # Allocation updates go through the Resource Controller; the CRN is URL-encoded into the path.
        url = f"{self.resource_controller_url}/v2/resource_instances/{quote(instance_crn, safe='')}"
        # The API broker expects usage_allocation_seconds as a string, not an integer
        payload = {"parameters": {"usage_allocation_seconds": str(allocation_seconds)}}
        self._request("PATCH", url, json=payload)
        return True

    def update_instance_limit(self, instance_crn: str, limit_seconds: int | None) -> bool:
        """Update the limit for an instance.

        Args:
            instance_crn: The CRN of the service instance
            limit_seconds: New limit in seconds (None to remove limit)

        Returns:
            True if successful
        """
        url = f"{self._get_regional_base_url(instance_crn)}/v1/instances/configuration"
        # API expects instance_limit field, not limit_seconds
        payload = {"instance_limit": limit_seconds}
        self._request("PUT", url, crn=instance_crn, json=payload)
        return True

    def create_instance(
        self,
        name: str,
        target: str,
        resource_group: str,
        plan: Plan,
        allocation_seconds: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new IBM Quantum service instance via Resource Controller API."""
        url = f"{self.resource_controller_url}/v2/resource_instances"
        payload: dict[str, Any] = {
            "name": name,
            "target": target,
            "resource_group": resource_group,
            "resource_plan_id": plan_id_for(plan),
            "resource_id": QUANTUM_COMPUTING_RESOURCE_ID,
        }

        if allocation_seconds is not None:
            payload["parameters"] = {"usage_allocation_seconds": str(allocation_seconds)}

        if tags:
            payload["tags"] = tags

        return self._request_json("POST", url, json=payload)

    def list_instances(self, account_id: str, plan: Plan) -> list[InstanceIdentifier]:
        url = f"{self.resource_controller_url}/v2/resource_instances"
        params: dict[str, Any] = {
            "resource_id": QUANTUM_COMPUTING_RESOURCE_ID,
            "account_id": account_id,
            "resource_plan_id": plan_id_for(plan),
        }

        instances = []
        while True:
            data = self._request_json("GET", url, params=params)
            for resource in data.get("resources", []):
                crn = resource.get("id")
                name = resource.get("name", "")
                if crn:
                    instances.append(InstanceIdentifier(crn=crn, name=name))

            next_url = data.get("next_url")
            if not next_url:
                break
            parsed = urlparse(next_url)
            start_token = parse_qs(parsed.query).get("start", [None])[0]
            if not start_token:
                break
            params["start"] = start_token

        return instances

    def get_account_with_instances(
        self,
        account_id: str,
        plan: Plan,
        instance_crns: Iterable[str],
    ) -> Account:
        """Get account allocation plus the specified instances, populated with full data.

        Callers supply the CRNs to populate. For the "every instance on the
        account/plan" path, call `list_instances` first and pass its CRNs in
        (see `configure`). Most commands pull CRNs from the user's config file.
        """
        alloc = self.get_account(account_id, plan)
        instances = []
        for crn in instance_crns:
            try:
                full = self.get_instance(crn)
                consumed = self.get_rolling_window_seconds(crn, account_id)
                instances.append(
                    Instance(
                        crn=crn,
                        name=full.name,
                        allocation_seconds=full.allocation_seconds,
                        limit_seconds=full.limit_seconds,
                        plan=full.plan,
                        consumed_seconds=consumed,
                    )
                )
            except Exception as e:
                print(f"Warning: Could not fetch full data for instance `{crn}`, so skipping: {e}")
        return Account(
            account_id=alloc.account_id,
            plan_id=alloc.plan_id,
            target_usage_seconds=alloc.target_usage_seconds,
            available_seconds=alloc.available_seconds,
            limit_seconds=alloc.limit_seconds,
            instances=tuple(instances),
        )
