# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for the `qauvern update` command and its pure helpers."""

import io
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result
from ruamel.yaml import YAML

from qauvern.cli import main
from qauvern.commands.update import (
    ExpiredGrant,
    InstanceRename,
    RemovedInstance,
    UpdateActions,
    UpdateSummary,
    compute_update,
    format_update_summary,
)
from qauvern.config import ConfigParser
from qauvern.models import DiscoveredInstance, DiscoveredInstances
from tests.mock_api import MockIBMQuantumAPIClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


US_CRN_A = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:us-a::"
US_CRN_B = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:us-b::"
US_CRN_C = "crn:v1:bluemix:public:quantum-computing:us-east:a/acc:us-c::"
EU_CRN_A = "crn:v1:bluemix:public:quantum-computing:eu-de:a/acc:eu-a::"


def _load_yaml(text: str):
    return YAML(typ="rt").load(io.StringIO(text))


def _dump_yaml(doc) -> str:
    yaml_rt = YAML(typ="rt")
    out = io.StringIO()
    yaml_rt.dump(doc, out)
    return out.getvalue()


def _discovered(active=(), archived=()) -> DiscoveredInstances:
    return DiscoveredInstances(active=tuple(active), archived=tuple(archived))


def _disc(crn: str, name: str = "Live", limit: int | None = None) -> DiscoveredInstance:
    return DiscoveredInstance(crn=crn, name=name, allocation_seconds=36000, limit_seconds=limit)


BASE_HEADER = """\
# user comment that must survive
account_id: acct-1
plan: internal
minimum_allocation_seconds: 60
allocation_reserve_percent: 5.0
balance_period:
  start_date: '2026-01-01T00:00:00+00:00'
  end_date: '2026-12-31T23:59:59+00:00'
"""


# ---------------------------------------------------------------------------
# compute_update — pure logic
# ---------------------------------------------------------------------------


def test_expire_net_grants_drops_past_grants_keeps_future() -> None:
    text = (
        BASE_HEADER
        + f"""\
instances:
  - name: A
    crn: '{US_CRN_A}'
    limit_seconds: 50000
    net_grants:
      - start_date: '2026-01-01T00:00:00+00:00'
        end_date: '2026-02-01T00:00:00+00:00'
        net_grant_seconds: 1000
      - start_date: '2026-06-01T00:00:00+00:00'
        end_date: '2026-07-01T00:00:00+00:00'
        net_grant_seconds: 2000
"""
    )
    doc = _load_yaml(text)
    summary = compute_update(
        doc,
        _discovered(active=(_disc(US_CRN_A, "A"),)),
        now=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert len(summary.expired_net_grants) == 1
    assert summary.expired_net_grants[0].instance_name == "A"
    grants = doc["instances"][0]["net_grants"]
    assert len(grants) == 1
    assert grants[0]["net_grant_seconds"] == 2000


def test_expire_net_grants_default_end_date_uses_28_day_window() -> None:
    text = (
        BASE_HEADER
        + f"""\
instances:
  - name: A
    crn: '{US_CRN_A}'
    limit_seconds: 50000
    net_grants:
      - start_date: '2026-01-01T00:00:00+00:00'
        net_grant_seconds: 1000
"""
    )
    doc = _load_yaml(text)
    summary = compute_update(
        doc,
        _discovered(active=(_disc(US_CRN_A, "A"),)),
        now=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    assert len(summary.expired_net_grants) == 1
    assert "net_grants" not in doc["instances"][0]


def test_expire_net_grants_drops_key_when_emptied() -> None:
    text = (
        BASE_HEADER
        + f"""\
instances:
  - name: A
    crn: '{US_CRN_A}'
    limit_seconds: 50000
    net_grants:
      - start_date: '2026-01-01T00:00:00+00:00'
        end_date: '2026-02-01T00:00:00+00:00'
        net_grant_seconds: 1000
"""
    )
    doc = _load_yaml(text)
    compute_update(doc, _discovered(active=(_disc(US_CRN_A, "A"),)), now=datetime(2026, 5, 1, tzinfo=timezone.utc))
    assert "net_grants" not in doc["instances"][0]


def test_remove_archived_and_missing_instances() -> None:
    text = (
        BASE_HEADER
        + f"""\
instances:
  - name: Live
    crn: '{US_CRN_A}'
  - name: Archived
    crn: '{US_CRN_B}'
  - name: Missing
    crn: '{US_CRN_C}'
"""
    )
    doc = _load_yaml(text)
    summary = compute_update(
        doc,
        _discovered(
            active=(_disc(US_CRN_A, "Live"),),
            archived=(_disc(US_CRN_B, "Archived"),),
        ),
        now=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    reasons = {(r.crn, r.reason) for r in summary.removed_instances}
    assert reasons == {(US_CRN_B, "archived"), (US_CRN_C, "missing")}
    crns = [entry["crn"] for entry in doc["instances"]]
    assert crns == [US_CRN_A]


def test_fix_names_renames_only_drifting_entries() -> None:
    text = (
        BASE_HEADER
        + f"""\
instances:
  - name: Old Name
    crn: '{US_CRN_A}'
  - name: Same
    crn: '{US_CRN_B}'
"""
    )
    doc = _load_yaml(text)
    summary = compute_update(
        doc,
        _discovered(active=(_disc(US_CRN_A, "New Name"), _disc(US_CRN_B, "Same"))),
        now=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert len(summary.renamed_instances) == 1
    assert summary.renamed_instances[0].crn == US_CRN_A
    assert summary.renamed_instances[0].new_name == "New Name"
    assert doc["instances"][0]["name"] == "New Name"
    assert doc["instances"][1]["name"] == "Same"


def test_add_instances_appends_with_optional_limit() -> None:
    text = (
        BASE_HEADER
        + f"""\
instances:
  - name: Existing
    crn: '{US_CRN_A}'
"""
    )
    doc = _load_yaml(text)
    summary = compute_update(
        doc,
        _discovered(
            active=(
                _disc(US_CRN_A, "Existing"),
                _disc(US_CRN_B, "B-named", limit=70000),
                _disc(US_CRN_C, "", limit=None),
            )
        ),
        now=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    crns = [entry["crn"] for entry in doc["instances"]]
    assert crns == [US_CRN_A, US_CRN_B, US_CRN_C]
    assert doc["instances"][1]["limit_seconds"] == 70000
    assert "limit_seconds" not in doc["instances"][2]
    # Unnamed gets a fallback name based on its index in the final list.
    assert doc["instances"][2]["name"] == "Instance 3"
    assert {a.crn for a in summary.added_instances} == {US_CRN_B, US_CRN_C}


def test_actions_flags_can_disable_each_step() -> None:
    text = (
        BASE_HEADER
        + f"""\
instances:
  - name: Old
    crn: '{US_CRN_A}'
    limit_seconds: 50000
    net_grants:
      - start_date: '2026-01-01T00:00:00+00:00'
        end_date: '2026-02-01T00:00:00+00:00'
        net_grant_seconds: 1000
  - name: Stale
    crn: '{US_CRN_C}'
"""
    )
    doc = _load_yaml(text)
    summary = compute_update(
        doc,
        _discovered(active=(_disc(US_CRN_A, "Renamed"), _disc(US_CRN_B, "B"))),
        now=datetime(2026, 5, 1, tzinfo=timezone.utc),
        actions=UpdateActions(
            expire_net_grants=False,
            add_instances=False,
            fix_names=False,
            remove_instances=False,
        ),
    )
    assert summary.is_empty
    assert len(doc["instances"]) == 2
    assert doc["instances"][0]["name"] == "Old"
    assert doc["instances"][0]["net_grants"][0]["net_grant_seconds"] == 1000


def test_round_trip_preserves_comments_and_unrelated_keys() -> None:
    text = (
        BASE_HEADER
        + f"""\
instances:
  - name: Kept
    crn: '{US_CRN_A}'
    # per-instance comment
    limit_seconds: 50000
    start_date: '2026-03-01T00:00:00+00:00'
    end_date: '2026-09-01T00:00:00+00:00'
"""
    )
    doc = _load_yaml(text)
    compute_update(
        doc,
        _discovered(active=(_disc(US_CRN_A, "Kept"),)),
        now=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    output = _dump_yaml(doc)
    assert "# user comment that must survive" in output
    assert "# per-instance comment" in output
    assert "allocation_reserve_percent: 5.0" in output
    assert "limit_seconds: 50000" in output
    assert "2026-03-01" in output
    assert "2026-09-01" in output


def test_compute_update_raises_when_instances_missing() -> None:
    doc = _load_yaml(BASE_HEADER + "")
    with pytest.raises(ValueError, match="instances"):
        compute_update(doc, _discovered(), now=datetime(2026, 5, 1, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# format_update_summary
# ---------------------------------------------------------------------------


def test_format_update_summary_empty() -> None:
    assert format_update_summary(UpdateSummary()) == "No changes needed."


def test_format_update_summary_all_sections() -> None:
    summary = UpdateSummary(
        removed_instances=[RemovedInstance(crn=US_CRN_B, name="Gone", reason="archived")],
        expired_net_grants=[
            ExpiredGrant(
                instance_name="A",
                crn=US_CRN_A,
                start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                end_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
        ],
        renamed_instances=[InstanceRename(crn=US_CRN_A, old_name="Old", new_name="New")],
        added_instances=[_disc(US_CRN_C, "Fresh")],
    )
    text = format_update_summary(summary)
    assert text.startswith("Planned changes:")
    assert "Remove (1)" in text
    assert "Gone" in text and "archived" in text
    assert "Expired net_grants (1)" in text
    assert "2026-01-01" in text and "2026-02-01" in text
    assert "Rename (1)" in text
    assert '"Old"' in text and '"New"' in text
    assert "Add (1)" in text
    assert "Fresh" in text


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _invoke_update(
    runner: CliRunner,
    mock_client: MockIBMQuantumAPIClient,
    args: list[str],
    *,
    input: str | None = "y\n",
) -> Result:
    with patch("qauvern.cli.IBMQuantumAPIClient", return_value=mock_client):
        return runner.invoke(main, ["update", *args], input=input)


def _write_config(path: Path, instances_block: str) -> None:
    path.write_text(BASE_HEADER + instances_block)


def test_update_happy_path_writes_corrected_file(runner: CliRunner, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        f"""\
instances:
  - name: Old Name
    crn: '{US_CRN_A}'
  - name: Archived
    crn: '{US_CRN_B}'
""",
    )

    mock_client = MockIBMQuantumAPIClient()
    mock_client.setup_account(account_id="acct-1", allocation_budget_seconds=0)
    mock_client.setup_instance(crn=US_CRN_A, name="Renamed A", allocation_seconds=36000, account_id="acct-1")
    mock_client.setup_instance(crn=US_CRN_B, name="Archived", allocation_seconds=0, account_id="acct-1", archived=True)
    mock_client.setup_instance(crn=US_CRN_C, name="New C", allocation_seconds=36000, account_id="acct-1")

    result = _invoke_update(runner, mock_client, ["--config", str(config_path), "--api-key", "k"])
    assert result.exit_code == 0, result.output

    cfg = ConfigParser(str(config_path))
    crns = {ic.crn for ic in cfg.instance_configs}
    assert crns == {US_CRN_A, US_CRN_C}
    name_by_crn = {ic.crn: ic.name for ic in cfg.instance_configs}
    assert name_by_crn[US_CRN_A] == "Renamed A"


def test_update_dry_run_does_not_write(runner: CliRunner, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    original = (
        BASE_HEADER
        + f"""\
instances:
  - name: Old Name
    crn: '{US_CRN_A}'
"""
    )
    config_path.write_text(original)

    mock_client = MockIBMQuantumAPIClient()
    mock_client.setup_account(account_id="acct-1", allocation_budget_seconds=0)
    mock_client.setup_instance(crn=US_CRN_A, name="Renamed", allocation_seconds=36000, account_id="acct-1")

    result = _invoke_update(runner, mock_client, ["--config", str(config_path), "--api-key", "k", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert config_path.read_text() == original
    assert "Dry run" in result.output


def test_update_no_add_no_remove_only_renames_and_expires(runner: CliRunner, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        f"""\
instances:
  - name: Old Name
    crn: '{US_CRN_A}'
    limit_seconds: 50000
    net_grants:
      - start_date: '2026-01-01T00:00:00+00:00'
        end_date: '2026-02-01T00:00:00+00:00'
        net_grant_seconds: 1000
  - name: Stale
    crn: '{US_CRN_C}'
""",
    )

    mock_client = MockIBMQuantumAPIClient()
    mock_client.setup_account(account_id="acct-1", allocation_budget_seconds=0)
    mock_client.setup_instance(crn=US_CRN_A, name="Renamed", allocation_seconds=36000, account_id="acct-1")
    mock_client.setup_instance(crn=US_CRN_B, name="New B", allocation_seconds=36000, account_id="acct-1")

    result = _invoke_update(
        runner,
        mock_client,
        ["--config", str(config_path), "--api-key", "k", "--no-add", "--no-remove"],
    )
    assert result.exit_code == 0, result.output

    yaml_rt = YAML(typ="rt")
    with open(config_path) as f:
        doc = yaml_rt.load(f)
    crns = [entry["crn"] for entry in doc["instances"]]
    assert crns == [US_CRN_A, US_CRN_C]  # nothing added, stale not removed
    assert doc["instances"][0]["name"] == "Renamed"
    assert "net_grants" not in doc["instances"][0]


def test_update_region_filters_adds(runner: CliRunner, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "instances: []\n")

    mock_client = MockIBMQuantumAPIClient()
    mock_client.setup_account(account_id="acct-1", allocation_budget_seconds=0)
    mock_client.setup_instance(crn=US_CRN_A, name="US A", allocation_seconds=36000, account_id="acct-1")
    mock_client.setup_instance(crn=EU_CRN_A, name="EU A", allocation_seconds=36000, account_id="acct-1")

    result = _invoke_update(
        runner, mock_client, ["--config", str(config_path), "--api-key", "k", "--region", "us-east"]
    )
    assert result.exit_code == 0, result.output

    yaml_rt = YAML(typ="rt")
    with open(config_path) as f:
        doc = yaml_rt.load(f)
    crns = [entry["crn"] for entry in doc["instances"]]
    assert crns == [US_CRN_A]


def test_update_region_removes_other_region_config_entries(runner: CliRunner, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        f"""\
instances:
  - name: US A
    crn: '{US_CRN_A}'
  - name: EU A
    crn: '{EU_CRN_A}'
""",
    )

    mock_client = MockIBMQuantumAPIClient()
    mock_client.setup_account(account_id="acct-1", allocation_budget_seconds=0)
    mock_client.setup_instance(crn=US_CRN_A, name="US A", allocation_seconds=36000, account_id="acct-1")
    mock_client.setup_instance(crn=EU_CRN_A, name="EU A", allocation_seconds=36000, account_id="acct-1")

    result = _invoke_update(
        runner, mock_client, ["--config", str(config_path), "--api-key", "k", "--region", "us-east"]
    )
    assert result.exit_code == 0, result.output

    yaml_rt = YAML(typ="rt")
    with open(config_path) as f:
        doc = yaml_rt.load(f)
    crns = [entry["crn"] for entry in doc["instances"]]
    assert crns == [US_CRN_A]


def test_update_aborts_on_negative_confirmation(runner: CliRunner, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    original = (
        BASE_HEADER
        + f"""\
instances:
  - name: Old Name
    crn: '{US_CRN_A}'
"""
    )
    config_path.write_text(original)

    mock_client = MockIBMQuantumAPIClient()
    mock_client.setup_account(account_id="acct-1", allocation_budget_seconds=0)
    mock_client.setup_instance(crn=US_CRN_A, name="Renamed", allocation_seconds=36000, account_id="acct-1")

    result = _invoke_update(runner, mock_client, ["--config", str(config_path), "--api-key", "k"], input="n\n")
    assert result.exit_code != 0
    assert config_path.read_text() == original


def test_update_no_changes_reports_already_up_to_date(runner: CliRunner, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        f"""\
instances:
  - name: Live
    crn: '{US_CRN_A}'
""",
    )

    mock_client = MockIBMQuantumAPIClient()
    mock_client.setup_account(account_id="acct-1", allocation_budget_seconds=0)
    mock_client.setup_instance(crn=US_CRN_A, name="Live", allocation_seconds=36000, account_id="acct-1")

    result = _invoke_update(runner, mock_client, ["--config", str(config_path), "--api-key", "k"])
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output
