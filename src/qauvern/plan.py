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


_PLAN_IDS: dict[Plan, str] = {
    Plan.INTERNAL: "91b2c828-2952-4f05-aed8-bedf92c6c480",
    Plan.PREMIUM: "7f666d17-7893-47d8-bf9d-2b2389fc4dfc",
    Plan.PAYGO: "5304b575-3cff-4455-90dc-ae4367762093",
}


def plan_id_for(plan: Plan) -> str:
    """Return the IBM Cloud resource_plan_id for a plan."""
    return _PLAN_IDS[plan]


def plan_from_name(name: str) -> Plan:
    """Parse a user-supplied plan name (case-insensitive) into a Plan.

    Raises ValueError with the known plan names if the input is unrecognized.
    """
    try:
        return Plan(name.lower())
    except ValueError:
        known = ", ".join(p.value for p in Plan)
        raise ValueError(f"Unknown plan '{name}'. Known plans: {known}.") from None
