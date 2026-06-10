# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML

from ..config import ConfigParser, parse_net_grant_dates
from ..models import DiscoveredInstance, DiscoveredInstances
from ..plan import Plan, plan_from_name


@dataclass(frozen=True)
class UpdateActions:
    """Which reconciliation actions to perform."""

    expire_net_grants: bool = True
    add_instances: bool = True
    fix_names: bool = True
    remove_instances: bool = True


@dataclass(frozen=True)
class ExpiredGrant:
    instance_name: str
    crn: str
    start_date: datetime
    end_date: datetime


@dataclass(frozen=True)
class InstanceRename:
    crn: str
    old_name: str
    new_name: str


@dataclass(frozen=True)
class RemovedInstance:
    crn: str
    name: str
    reason: Literal["archived", "missing"]


@dataclass
class UpdateSummary:
    expired_net_grants: list[ExpiredGrant] = field(default_factory=list)
    added_instances: list[DiscoveredInstance] = field(default_factory=list)
    renamed_instances: list[InstanceRename] = field(default_factory=list)
    removed_instances: list[RemovedInstance] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.expired_net_grants or self.added_instances or self.renamed_instances or self.removed_instances)


def _expire_net_grants(doc_instances: list, now: datetime, summary: UpdateSummary) -> None:
    for entry in doc_instances:
        grants = entry.get("net_grants")
        if not grants:
            continue
        kept = []
        for i, grant in enumerate(grants):
            provenance = f"instances[{entry.get('name', '?')}].net_grants[{i}]"
            start_date, end_date = parse_net_grant_dates(grant, provenance=provenance)
            if end_date <= now:
                summary.expired_net_grants.append(
                    ExpiredGrant(
                        instance_name=entry.get("name", ""),
                        crn=entry.get("crn", ""),
                        start_date=start_date,
                        end_date=end_date,
                    )
                )
            else:
                kept.append(grant)
        if not kept:
            del entry["net_grants"]
        elif len(kept) != len(grants):
            entry["net_grants"] = kept


def _remove_instances(
    doc_instances: list, archived_crns: set[str], known_crns: set[str], summary: UpdateSummary
) -> None:
    survivors = []
    for entry in doc_instances:
        crn = entry.get("crn", "")
        name = entry.get("name", "")
        if crn in archived_crns:
            summary.removed_instances.append(RemovedInstance(crn=crn, name=name, reason="archived"))
        elif crn not in known_crns:
            summary.removed_instances.append(RemovedInstance(crn=crn, name=name, reason="missing"))
        else:
            survivors.append(entry)
    doc_instances[:] = survivors


def _fix_names(doc_instances: list, active_by_crn: dict[str, str], summary: UpdateSummary) -> None:
    for entry in doc_instances:
        crn = entry.get("crn", "")
        if crn not in active_by_crn:
            continue
        api_name = active_by_crn[crn]
        old_name = entry.get("name", "")
        if api_name != old_name:
            summary.renamed_instances.append(InstanceRename(crn=crn, old_name=old_name, new_name=api_name))
            entry["name"] = api_name


def _new_instance_entry(inst: DiscoveredInstance, fallback_name: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": inst.name or fallback_name,
        "crn": inst.crn,
    }
    if inst.limit_seconds is not None:
        entry["limit_seconds"] = inst.limit_seconds
    return entry


def _add_instances(doc_instances: list, active: tuple[DiscoveredInstance, ...], summary: UpdateSummary) -> None:
    existing_crns = {entry.get("crn", "") for entry in doc_instances}
    candidates = [inst for inst in active if inst.crn not in existing_crns]
    sorted_candidates = sorted(candidates, key=lambda x: (x.name == "", x.name))
    start_index = len(doc_instances) + 1
    for offset, inst in enumerate(sorted_candidates):
        doc_instances.append(_new_instance_entry(inst, fallback_name=f"Instance {start_index + offset}"))
    summary.added_instances.extend(sorted_candidates)


def compute_update(
    doc: Any,
    discovered: DiscoveredInstances,
    *,
    now: datetime,
    actions: UpdateActions = UpdateActions(),
) -> UpdateSummary:
    """Reconcile a config document against the discovered API state in place.

    `doc` is mutated; the returned summary describes what changed.
    """
    doc_instances = doc.get("instances")
    if doc_instances is None:
        raise ValueError("Config is missing required 'instances' field")

    summary = UpdateSummary()

    if actions.remove_instances:
        archived_crns = {d.crn for d in discovered.archived}
        known_crns = archived_crns | {d.crn for d in discovered.active}
        _remove_instances(doc_instances, archived_crns, known_crns, summary)

    if actions.expire_net_grants:
        _expire_net_grants(doc_instances, now, summary)

    if actions.fix_names:
        active_by_crn = {d.crn: d.name for d in discovered.active}
        _fix_names(doc_instances, active_by_crn, summary)

    if actions.add_instances:
        _add_instances(doc_instances, discovered.active, summary)

    return summary


def format_update_summary(summary: UpdateSummary) -> str:
    """Render an `UpdateSummary` as a human-readable multi-line string."""
    if summary.is_empty:
        return "No changes needed."

    lines = ["Planned changes:"]

    if summary.removed_instances:
        lines.append(f"  Remove ({len(summary.removed_instances)}):")
        for r in summary.removed_instances:
            lines.append(f"    - {r.name or '(unnamed)'} [{r.reason}] ({r.crn})")

    if summary.expired_net_grants:
        lines.append(f"  Expired net_grants ({len(summary.expired_net_grants)}):")
        for g in summary.expired_net_grants:
            lines.append(f"    - {g.instance_name or '(unnamed)'}: {g.start_date.date()} → {g.end_date.date()}")

    if summary.renamed_instances:
        lines.append(f"  Rename ({len(summary.renamed_instances)}):")
        for rn in summary.renamed_instances:
            lines.append(f'    - "{rn.old_name}" → "{rn.new_name}" ({rn.crn})')

    if summary.added_instances:
        lines.append(f"  Add ({len(summary.added_instances)}):")
        for a in summary.added_instances:
            lines.append(f"    - {a.name or '(unnamed)'} ({a.crn})")

    return "\n".join(lines)


def _make_yaml() -> YAML:
    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    return yaml_rt


@dataclass(frozen=True)
class LoadedConfig:
    doc: Any
    account_id: str
    plan: Plan


def load_config_doc(config_path: Path) -> LoadedConfig:
    """Load a YAML config in round-trip mode, returning the doc + key fields.

    Bypasses `ConfigParser` because `update` is meant to fix exactly the
    drift that would cause `ConfigParser` to raise. We still need
    `account_id`` and `plan` to call the API, so pull them directly.
    """
    yaml_rt = _make_yaml()
    with open(config_path) as f:
        doc = yaml_rt.load(f)
    if doc is None or "account_id" not in doc or "plan" not in doc:
        raise ValueError(f"Config file {config_path} is missing required fields (account_id, plan)")
    return LoadedConfig(doc=doc, account_id=doc["account_id"], plan=plan_from_name(doc["plan"]))


def write_config_doc(config_path: Path, doc: Any) -> None:
    yaml_rt = _make_yaml()
    with open(config_path, "w") as f:
        yaml_rt.dump(doc, f)


def validate_written_config(config_path: Path) -> str | None:
    """Re-parse the written file with ``ConfigParser``. Return any error message."""
    try:
        ConfigParser(str(config_path))
    except Exception as e:
        return str(e)
    return None
