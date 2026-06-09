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


def extract_region_from_crn(crn: str) -> Region:
    """Extract the region from a CRN.

    CRN format: crn:v1:bluemix:public:quantum-computing:REGION:a/ACCOUNT_ID:INSTANCE_ID::

    Returns Region.US_EAST if the CRN cannot be parsed. Raises AssertionError for
    well-formed CRNs whose region is not yet in the Region enum.
    """
    parts = crn.split(":")
    if len(parts) < 6:
        return Region.US_EAST
    region_str = parts[5]
    if region_str not in {r.value for r in Region}:
        raise AssertionError(
            f"Unrecognized region {region_str!r} in CRN. Please open an issue at https://github.com/IBM/qauvern/issues"
        )
    return Region(region_str)
