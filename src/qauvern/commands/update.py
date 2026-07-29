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

from ..config import parse_net_grant_dates
from ..models import DiscoveredInstance, DiscoveredInstances


@dataclass(frozen=True)
class UpdateActions:
    """Which reconciliation actions to perform."""

    expire_net_grants: bool = True
    add_instances: bool = True
    fix_names: bool = True
    remove_instances: bool = True
    add_missing_limits: bool = True


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


@dataclass(frozen=True)
class LimitAdded:
    crn: str
    name: str
    limit_seconds: int


@dataclass
class UpdateSummary:
    expired_net_grants: list[ExpiredGrant] = field(default_factory=list)
    added_instances: list[DiscoveredInstance] = field(default_factory=list)
    renamed_instances: list[InstanceRename] = field(default_factory=list)
    removed_instances: list[RemovedInstance] = field(default_factory=list)
    added_limits: list[LimitAdded] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.expired_net_grants
            or self.added_instances
            or self.renamed_instances
            or self.removed_instances
            or self.added_limits
        )


def _expire_net_grants(doc_instances: list, now: datetime, summary: UpdateSummary) -> None:
    for entry in doc_instances:
        grants = entry.get("net_grants")
        if not grants:
            continue
        kept = []
        for i, grant in enumerate(grants):
            provenance = f"instances[{entry['name']}].net_grants[{i}]"
            start_date, end_date = parse_net_grant_dates(grant, provenance=provenance)
            if end_date <= now:
                summary.expired_net_grants.append(
                    ExpiredGrant(
                        instance_name=entry["name"],
                        crn=entry["crn"],
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
        crn = entry["crn"]
        name = entry["name"]
        if crn in archived_crns:
            summary.removed_instances.append(RemovedInstance(crn=crn, name=name, reason="archived"))
        elif crn not in known_crns:
            summary.removed_instances.append(RemovedInstance(crn=crn, name=name, reason="missing"))
        else:
            survivors.append(entry)
    doc_instances[:] = survivors


def _fix_names(doc_instances: list, active_by_crn: dict[str, str], summary: UpdateSummary) -> None:
    for entry in doc_instances:
        crn = entry["crn"]
        if crn not in active_by_crn:
            continue
        api_name = active_by_crn[crn]
        old_name = entry["name"]
        if api_name != old_name:
            summary.renamed_instances.append(InstanceRename(crn=crn, old_name=old_name, new_name=api_name))
            entry["name"] = api_name


def _add_missing_limits(
    doc_instances: list, active_by_crn: dict[str, DiscoveredInstance], summary: UpdateSummary
) -> None:
    for entry in doc_instances:
        if "limit_seconds" in entry:
            continue
        crn = entry["crn"]
        live = active_by_crn.get(crn)
        if live is None or live.limit_seconds is None:
            continue
        entry["limit_seconds"] = live.limit_seconds
        summary.added_limits.append(LimitAdded(crn=crn, name=entry["name"], limit_seconds=live.limit_seconds))


def _new_instance_entry(inst: DiscoveredInstance) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": inst.name,
        "crn": inst.crn,
    }
    if inst.limit_seconds is not None:
        entry["limit_seconds"] = inst.limit_seconds
    return entry


def _add_instances(doc_instances: list, active: tuple[DiscoveredInstance, ...], summary: UpdateSummary) -> None:
    existing_crns = {entry["crn"] for entry in doc_instances}
    candidates = sorted(
        [inst for inst in active if inst.crn not in existing_crns],
        key=lambda x: x.name,
    )
    for inst in candidates:
        doc_instances.append(_new_instance_entry(inst))
    summary.added_instances.extend(candidates)


def compute_update(
    doc: Any,
    discovered: DiscoveredInstances,
    *,
    now: datetime,
    actions: UpdateActions | None = None,
) -> UpdateSummary:
    """Reconcile a config document against the discovered API state in place.

    `doc` is mutated; the returned summary describes what changed.
    """
    if actions is None:
        actions = UpdateActions()

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

    active_by_crn = {d.crn: d for d in discovered.active}

    if actions.fix_names:
        _fix_names(doc_instances, {crn: d.name for crn, d in active_by_crn.items()}, summary)

    if actions.add_missing_limits:
        _add_missing_limits(doc_instances, active_by_crn, summary)

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
            lines.append(f"    - {r.name} [{r.reason}] ({r.crn})")

    if summary.expired_net_grants:
        lines.append(f"  Expired net_grants ({len(summary.expired_net_grants)}):")
        for g in summary.expired_net_grants:
            lines.append(f"    - {g.instance_name}: {g.start_date.date()} → {g.end_date.date()}")

    if summary.renamed_instances:
        lines.append(f"  Rename ({len(summary.renamed_instances)}):")
        for rn in summary.renamed_instances:
            lines.append(f'    - "{rn.old_name}" → "{rn.new_name}" ({rn.crn})')

    if summary.added_limits:
        lines.append(f"  Add limit_seconds ({len(summary.added_limits)}):")
        for la in summary.added_limits:
            lines.append(f"    - {la.name}: {la.limit_seconds} ({la.crn})")

    if summary.added_instances:
        lines.append(f"  Add ({len(summary.added_instances)}):")
        for a in summary.added_instances:
            lines.append(f"    - {a.name} ({a.crn})")

    return "\n".join(lines)


def _make_yaml() -> YAML:
    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    return yaml_rt


def load_config_doc(config_path: Path) -> Any:
    """Load a YAML config in round-trip mode for in-place mutation."""
    yaml_rt = _make_yaml()
    with open(config_path) as f:
        return yaml_rt.load(f)


def write_config_doc(config_path: Path, doc: Any) -> None:
    yaml_rt = _make_yaml()
    with open(config_path, "w") as f:
        yaml_rt.dump(doc, f)
