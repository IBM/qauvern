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

from ..models import DiscoveredInstance
from ..plan import Plan


def build_configure_yaml(
    account_id: str,
    plan: Plan,
    instances: Sequence[DiscoveredInstance],
) -> str:
    config = {
        "account_id": account_id,
        "plan": plan.value,
        "minimum_allocation_seconds": 60,
        "instances": [
            {
                "name": inst.name or f"Instance {i}",
                "crn": inst.crn,
                **({"limit_seconds": inst.limit_seconds} if inst.limit_seconds is not None else {}),
            }
            for i, inst in enumerate(sorted(instances, key=lambda x: (x.name == "", x.name)), 1)
        ],
    }

    out = io.StringIO()
    out.write("# Auto-generated configuration file\n\n")
    yaml.dump(config, out, default_flow_style=False, sort_keys=False)
    return out.getvalue()
