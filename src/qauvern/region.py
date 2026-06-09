# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""IBM Quantum deployment regions supported by qauvern."""

from enum import Enum


class Region(str, Enum):
    US_EAST = "us-east"
    EU_DE = "eu-de"


def extract_region_from_crn(crn: str) -> str:
    """Extract the region from a CRN.

    CRN format: crn:v1:bluemix:public:quantum-computing:REGION:a/ACCOUNT_ID:INSTANCE_ID::

    Returns the region string, or 'us-east' if the CRN cannot be parsed.
    """
    parts = crn.split(":")
    if len(parts) >= 6:
        return parts[5]
    return "us-east"
