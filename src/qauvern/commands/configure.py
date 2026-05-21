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
    """Build the full contents of an auto-generated configuration YAML file.

    The output includes a header comment block describing how to customize the
    file, the YAML body itself, and a footer block listing details of every
    instance found in the account.
    """
    config = {
        "account_id": account_id,
        "balance_period": {
            "start_date": balance_start,
            "end_date": balance_end,
        },
        "projects": [
            {
                "name": f"Project {i}",
                "crn": inst.crn,
                "target_usage_seconds": inst.allocation_seconds or 96000,
            }
            for i, inst in enumerate(instances, 1)
        ],
    }

    out = io.StringIO()
    out.write("# qauvern configuration\n")
    out.write("# Auto-generated configuration file\n")
    out.write("#\n")
    out.write("# IMPORTANT: This is a base configuration with all instances\n")
    out.write("# grouped into a single project. You should customize this by:\n")
    out.write("#\n")
    out.write("# 1. Creating separate projects for different teams/purposes\n")
    out.write("# 2. Assigning instance CRNs to appropriate projects\n")
    out.write("# 3. Setting appropriate target_usage_seconds for each project\n")
    out.write("# 4. Adjusting balance period dates as needed\n")
    out.write("#\n")
    out.write(f"# Account: {account_id}\n")
    out.write(f"# Instances Found: {len(instances)}\n")
    out.write("#\n")
    out.write("# Note: Each project corresponds to exactly one service instance.\n")
    out.write("#\n\n")

    yaml.dump(config, out, default_flow_style=False, sort_keys=False)

    out.write("\n# Instance Details:\n")
    out.write("# The following instances were found in your account:\n")
    out.write("#\n")
    for inst in instances:
        out.write(f"# - {inst.name or 'Unnamed'}\n")
        out.write(f"#   CRN: {inst.crn}\n")
        out.write(f"#   Allocation: {format_seconds(inst.allocation_seconds)}\n")
        out.write(f"#   Consumed: {format_seconds(inst.consumed_seconds)}\n")
        if inst.limit_seconds:
            out.write(f"#   Limit: {format_seconds(inst.limit_seconds)}\n")
        out.write(f"#   Fairness: {inst.fairness:.2f}\n")
        out.write("#\n")

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
