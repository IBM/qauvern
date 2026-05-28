# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Pure helpers for the `qauvern configure` command."""

from collections.abc import Sequence
import io

import yaml

from ..formatting import format_fairness, format_limit, format_seconds
from ..models import InstanceState
from ..plan import Plan


def build_configure_yaml(
    account_id: str,
    plan: Plan,
    instances: Sequence[InstanceState],
    balance_start: str,
    balance_end: str,
) -> str:
    config = {
        "account_id": account_id,
        "plan": plan.value,
        "balance_period": {
            "start_date": balance_start,
            "end_date": balance_end,
        },
        "instances": [
            {
                "name": inst.name or f"Instance {i}",
                "crn": inst.crn,
                "target_usage_seconds": inst.allocation_seconds or 96000,
                **({"limit_seconds": inst.limit_seconds} if inst.limit_seconds is not None else {}),
            }
            for i, inst in enumerate(sorted(instances, key=lambda x: (x.name == "", x.name)), 1)
        ],
    }

    out = io.StringIO()
    out.write("# Auto-generated configuration file\n\n")
    yaml.dump(config, out, default_flow_style=False, sort_keys=False)
    return out.getvalue()


def build_instance_summary_table(instances: Sequence[InstanceState]) -> tuple[list[list[str]], list[str]]:
    """Build the rows and headers for the post-configure instance summary."""
    headers = ["Instance Name", "Allocation", "Limit", "Consumed", "Fairness"]
    rows = [
        [
            inst.name[:40],
            format_seconds(inst.allocation_seconds),
            format_limit(inst.limit_seconds),
            format_seconds(inst.consumed_seconds),
            format_fairness(inst.fairness),
        ]
        for inst in sorted(instances, key=lambda x: x.name)
    ]
    return rows, headers
