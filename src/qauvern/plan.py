# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""IBM Quantum service plans supported by qauvern.

Plans are a closed set: the optimizer's logic depends on plan-specific
allocation semantics, so unsupported plans (e.g. Flex) are intentionally
excluded.
"""

from enum import Enum


class Plan(str, Enum):
    INTERNAL = "internal"
    PREMIUM = "premium"
    PAYGO = "paygo"


_STAGING_PLAN_IDS: dict[Plan, str] = {
    Plan.INTERNAL: "91b2c828-2952-4f05-aed8-bedf92c6c480",
    Plan.PREMIUM: "7f666d17-7893-47d8-bf9d-2b2389fc4dfc",
    Plan.PAYGO: "5304b575-3cff-4455-90dc-ae4367762093",
}

_PROD_PLAN_IDS: dict[Plan, str] = {
    Plan.INTERNAL: "91b2c828-2952-4f05-aed8-bedf92c6c480",
    Plan.PREMIUM: "7f666d17-7893-47d8-bf9d-2b2389fc4dfc",
    Plan.PAYGO: "5304b575-3cff-4455-90dc-ae4367762093",
}


def _ids(staging: bool) -> dict[Plan, str]:
    return _STAGING_PLAN_IDS if staging else _PROD_PLAN_IDS


def plan_id_for(plan: Plan, *, staging: bool) -> str:
    """Return the IBM Cloud resource_plan_id for a plan in the given environment."""
    return _ids(staging)[plan]


def plan_from_id(plan_id: str, *, staging: bool) -> Plan | None:
    """Reverse-lookup a Plan from a resource_plan_id, or None if unknown."""
    for plan, pid in _ids(staging).items():
        if pid == plan_id:
            return plan
    return None


def plan_from_name(name: str) -> Plan:
    """Parse a user-supplied plan name (case-insensitive) into a Plan.

    Raises ValueError with the known plan names if the input is unrecognized.
    """
    try:
        return Plan(name.lower())
    except ValueError:
        known = ", ".join(p.value for p in Plan)
        raise ValueError(f"Unknown plan '{name}'. Known plans: {known}.") from None
