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

import io

import yaml

from ..formatting import format_fairness, format_seconds
from ..models import Instance


def build_configure_yaml(
    account_id: str,
    instances: list[Instance],
    balance_start: str,
    balance_end: str,
) -> str:
    config = {
        "account_id": account_id,
        "balance_period": {
            "start_date": balance_start,
            "end_date": balance_end,
        },
        "instances": [
            {
                "name": inst.name or f"Instance {i}",
                "crn": inst.crn,
                "target_usage_seconds": inst.allocation_seconds or 96000,
            }
            for i, inst in enumerate(instances, 1)
        ],
    }

    out = io.StringIO()
    out.write("# Auto-generated configuration file\n\n")
    yaml.dump(config, out, default_flow_style=False, sort_keys=False)
    return out.getvalue()


def build_instance_summary_table(
    instances: list[Instance],
) -> tuple[list[list[str]], list[str]]:
    """Build the rows and headers for the post-configure instance summary."""
    headers = ["Instance Name", "Allocation", "Consumed", "Fairness"]
    rows = [
        [
            inst.name[:40],
            format_seconds(inst.allocation_seconds),
            format_seconds(inst.consumed_seconds),
            format_fairness(inst.fairness),
        ]
        for inst in instances
    ]
    return rows, headers
