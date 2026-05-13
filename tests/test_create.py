# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for the create instance command and related helpers."""

from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner, Result

from qauvern.api_client import get_plan_id
from qauvern.cli import main, parse_seconds
from tests.mock_api import MockIBMQuantumAPIClient


def test_plain_integer() -> None:
    assert parse_seconds("96000") == 96000


def test_seconds_suffix() -> None:
    assert parse_seconds("500s") == 500


def test_minutes() -> None:
    assert parse_seconds("30m") == 1800


def test_hours() -> None:
    assert parse_seconds("10h") == 36000


def test_days() -> None:
    assert parse_seconds("1d") == 86400


def test_fractional_days() -> None:
    assert parse_seconds("2.5d") == 216000


def test_fractional_hours() -> None:
    assert parse_seconds("1.5h") == 5400


def test_qau() -> None:
    assert parse_seconds("1qau") == 96000


def test_fractional_qau() -> None:
    assert parse_seconds("0.5qau") == 48000


def test_case_insensitive() -> None:
    assert parse_seconds("10H") == 36000


def test_whitespace_stripped() -> None:
    assert parse_seconds("  10h  ") == 36000


def test_invalid_raises() -> None:
    with pytest.raises(click.BadParameter):
        parse_seconds("abc")


def test_empty_suffix_raises() -> None:
    with pytest.raises(click.BadParameter):
        parse_seconds("h")


def test_friendly_name_internal() -> None:
    result = get_plan_id("internal")
    assert result == "91b2c828-2952-4f05-aed8-bedf92c6c480"


def test_friendly_name_premium() -> None:
    result = get_plan_id("premium")
    assert result == "7f666d17-7893-47d8-b9e5-e8b5c0b5c5c5"


def test_friendly_name_paygo() -> None:
    result = get_plan_id("paygo")
    assert result == "5304b575-3cff-4455-90dc-ae4367762093"


def test_get_plan_id_case_insensitive() -> None:
    assert get_plan_id("PREMIUM") == get_plan_id("premium")
    assert get_plan_id("Internal") == get_plan_id("internal")


def test_raw_uuid_passthrough() -> None:
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert get_plan_id(uuid) == uuid


def test_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="Unknown plan"):
        get_plan_id("nonexistent")


def test_invalid_uuid_raises() -> None:
    with pytest.raises(ValueError, match="Unknown plan"):
        get_plan_id("not-a-uuid")


@pytest.fixture
def mock_client() -> MockIBMQuantumAPIClient:
    return MockIBMQuantumAPIClient()


def test_basic_creation(mock_client: MockIBMQuantumAPIClient) -> None:
    result = mock_client.create_instance(
        name="test-instance",
        target="us-east",
        resource_group="rg-123",
        resource_plan_id="plan-123",
    )
    assert result["name"] == "test-instance"
    assert result["state"] == "active"
    assert "id" in result
    assert "us-east" in result["id"]


def test_creation_with_allocation(mock_client: MockIBMQuantumAPIClient) -> None:
    result = mock_client.create_instance(
        name="test-instance",
        target="eu-de",
        resource_group="rg-123",
        resource_plan_id="plan-123",
        allocation_seconds=96000,
    )
    crn = result["id"]
    assert mock_client.instances[crn].allocation_seconds == 96000


def test_instance_stored_in_mock(mock_client: MockIBMQuantumAPIClient) -> None:
    result = mock_client.create_instance(
        name="stored-instance",
        target="us-east",
        resource_group="rg-123",
        resource_plan_id="plan-123",
    )
    crn = result["id"]
    assert crn in mock_client.instances
    assert mock_client.instances[crn].name == "stored-instance"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def create_mock_client() -> MockIBMQuantumAPIClient:
    return MockIBMQuantumAPIClient()


def _invoke(
    runner: CliRunner,
    mock_client: MockIBMQuantumAPIClient,
    args: list[str],
    override_client: MockIBMQuantumAPIClient | None = None,
) -> Result:
    client = override_client or mock_client
    target = "qauvern.cli.IBMQuantumAPIClient"
    with patch(target, return_value=client):
        return runner.invoke(main, ["create"] + args)


def test_create_success(runner: CliRunner, create_mock_client: MockIBMQuantumAPIClient) -> None:
    result = _invoke(
        runner,
        create_mock_client,
        [
            "my-instance",
            "--target",
            "us-east",
            "--plan",
            "premium",
            "--resource-group",
            "rg-123",
            "--api-key",
            "test-key",
        ],
    )
    assert result.exit_code == 0
    assert "my-instance" in result.output
    assert "created successfully" in result.output


def test_create_with_allocation(runner: CliRunner, create_mock_client: MockIBMQuantumAPIClient) -> None:
    result = _invoke(
        runner,
        create_mock_client,
        [
            "my-instance",
            "--target",
            "us-east",
            "--plan",
            "internal",
            "--resource-group",
            "rg-123",
            "--api-key",
            "test-key",
            "--allocation",
            "10h",
        ],
    )
    assert result.exit_code == 0
    assert "10.0h" in result.output


def test_create_with_limit(runner: CliRunner, create_mock_client: MockIBMQuantumAPIClient) -> None:
    result = _invoke(
        runner,
        create_mock_client,
        [
            "my-instance",
            "--target",
            "us-east",
            "--plan",
            "premium",
            "--resource-group",
            "rg-123",
            "--api-key",
            "test-key",
            "--limit",
            "50h",
        ],
    )
    assert result.exit_code == 0
    assert "Limit set successfully" in result.output


def test_create_limit_failure_still_succeeds(runner: CliRunner, create_mock_client: MockIBMQuantumAPIClient) -> None:
    mock = MockIBMQuantumAPIClient()

    def fail_limit(*args, **kwargs):
        raise Exception("limit API unavailable")

    mock.update_instance_limit = fail_limit  # ty: ignore[invalid-assignment]

    result = _invoke(
        runner,
        create_mock_client,
        [
            "my-instance",
            "--target",
            "us-east",
            "--plan",
            "premium",
            "--resource-group",
            "rg-123",
            "--api-key",
            "test-key",
            "--limit",
            "10h",
        ],
        override_client=mock,
    )
    assert result.exit_code == 0
    assert "created successfully" in result.output
    assert "limit could not be set" in result.output


def test_create_with_tags(runner: CliRunner, create_mock_client: MockIBMQuantumAPIClient) -> None:
    result = _invoke(
        runner,
        create_mock_client,
        [
            "my-instance",
            "--target",
            "us-east",
            "--plan",
            "premium",
            "--resource-group",
            "rg-123",
            "--api-key",
            "test-key",
            "--tag",
            "env:prod",
            "--tag",
            "team:quantum",
        ],
    )
    assert result.exit_code == 0
    assert "created successfully" in result.output


def test_create_with_raw_plan_uuid(runner: CliRunner, create_mock_client: MockIBMQuantumAPIClient) -> None:
    result = _invoke(
        runner,
        create_mock_client,
        [
            "my-instance",
            "--target",
            "us-east",
            "--plan",
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "--resource-group",
            "rg-123",
            "--api-key",
            "test-key",
        ],
    )
    assert result.exit_code == 0


def test_create_missing_required_option(runner: CliRunner, create_mock_client: MockIBMQuantumAPIClient) -> None:
    result = _invoke(
        runner,
        create_mock_client,
        [
            "my-instance",
            "--target",
            "us-east",
            "--api-key",
            "test-key",
        ],
    )
    assert result.exit_code != 0


def test_create_invalid_plan_name(runner: CliRunner, create_mock_client: MockIBMQuantumAPIClient) -> None:
    result = _invoke(
        runner,
        create_mock_client,
        [
            "my-instance",
            "--target",
            "us-east",
            "--plan",
            "bogus",
            "--resource-group",
            "rg-123",
            "--api-key",
            "test-key",
        ],
    )
    assert result.exit_code != 0
    assert "Unknown plan" in result.output


def test_create_api_failure(runner: CliRunner, create_mock_client: MockIBMQuantumAPIClient) -> None:
    mock = MockIBMQuantumAPIClient()

    def fail_create(*args, **kwargs):
        raise Exception("API error: 403 Forbidden")

    mock.create_instance = fail_create  # ty: ignore[invalid-assignment]

    result = _invoke(
        runner,
        create_mock_client,
        [
            "my-instance",
            "--target",
            "us-east",
            "--plan",
            "premium",
            "--resource-group",
            "rg-123",
            "--api-key",
            "test-key",
        ],
        override_client=mock,
    )
    assert result.exit_code != 0
    assert "API error" in result.output


def test_create_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["create", "--help"])
    assert result.exit_code == 0
    assert "Create a new IBM Quantum service instance" in result.output
    assert "--target" in result.output
    assert "--plan" in result.output
    assert "--allocation" in result.output
    assert "--limit" in result.output
