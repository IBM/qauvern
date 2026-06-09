# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Unit tests for region utilities."""

import pytest

from qauvern.region import extract_region_from_crn


def test_extract_us_east() -> None:
    crn = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc123:inst456::"
    assert extract_region_from_crn(crn) == "us-east"


def test_extract_eu_de() -> None:
    crn = "crn:v1:bluemix:public:quantum-computing:eu-de:a/acc123:inst456::"
    assert extract_region_from_crn(crn) == "eu-de"


def test_truncated_crn_defaults_to_us_east() -> None:
    assert extract_region_from_crn("crn:v1:only:five") == "us-east"


def test_empty_crn_defaults_to_us_east() -> None:
    assert extract_region_from_crn("") == "us-east"


def test_unrecognized_region_raises_assertion_error() -> None:
    unknown_crn = "crn:v1:bluemix:public:quantum-computing:in-south:a/acc:inst::"
    with pytest.raises(AssertionError, match="github.com/IBM/qauvern/issues"):
        extract_region_from_crn(unknown_crn)
