# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Pure helpers for the `qauvern update` command.

Reconcile a YAML configuration document against the live API, mutating the
document in place and returning a structured summary of the changes.

The document is a `ruamel.yaml` round-trip mapping so that user comments,
field ordering, and unrelated keys (e.g. ``allocation_reserve_percent``,
per-instance ``start_date``/``end_date``) survive the rewrite.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from ..config import parse_utc_datetime
from ..models import DiscoveredInstance, DiscoveredInstances
from ..region import Region, extract_region_from_crn


@dataclass(frozen=True)
class UpdateActions:
    """Which reconciliation actions to perform. Each defaults to True."""

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


@dataclass(frozen=True)
class UpdateSummary:
    expired_net_grants: tuple[ExpiredGrant, ...] = ()
    added_instances: tuple[DiscoveredInstance, ...] = ()
    renamed_instances: tuple[InstanceRename, ...] = ()
    removed_instances: tuple[RemovedInstance, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.expired_net_grants or self.added_instances or self.renamed_instances or self.removed_instances)


def _grant_end_date(grant: dict, *, provenance: str) -> datetime:
    """Mirror ``ConfigParser.instance_configs`` net-grant end-date defaulting."""
    if "end_date" in grant:
        return parse_utc_datetime(grant["end_date"], provenance=f"{provenance}.end_date")
    start = parse_utc_datetime(grant["start_date"], provenance=f"{provenance}.start_date")
    return start + timedelta(days=28)


def _expire_net_grants(doc_instances: list, now: datetime) -> list[ExpiredGrant]:
    expired: list[ExpiredGrant] = []
    for entry in doc_instances:
        grants = entry.get("net_grants")
        if not grants:
            continue
        kept = []
        for i, grant in enumerate(grants):
            provenance = f"instances[{entry.get('name', '?')}].net_grants[{i}]"
            end_date = _grant_end_date(grant, provenance=provenance)
            if end_date <= now:
                expired.append(
                    ExpiredGrant(
                        instance_name=entry.get("name", ""),
                        crn=entry.get("crn", ""),
                        start_date=parse_utc_datetime(grant["start_date"], provenance=f"{provenance}.start_date"),
                        end_date=end_date,
                    )
                )
            else:
                kept.append(grant)
        if not kept:
            del entry["net_grants"]
        elif len(kept) != len(grants):
            entry["net_grants"] = kept
    return expired


def _remove_instances(doc_instances: list, archived_crns: set[str], known_crns: set[str]) -> list[RemovedInstance]:
    removed: list[RemovedInstance] = []
    survivors = []
    for entry in doc_instances:
        crn = entry.get("crn", "")
        name = entry.get("name", "")
        if crn in archived_crns:
            removed.append(RemovedInstance(crn=crn, name=name, reason="archived"))
        elif crn not in known_crns:
            removed.append(RemovedInstance(crn=crn, name=name, reason="missing"))
        else:
            survivors.append(entry)
    doc_instances[:] = survivors
    return removed


def _fix_names(
    doc_instances: list,
    active_by_crn: dict[str, str],
    region: Region | None,
) -> list[InstanceRename]:
    renames: list[InstanceRename] = []
    for entry in doc_instances:
        crn = entry.get("crn", "")
        if crn not in active_by_crn:
            continue
        if region is not None and extract_region_from_crn(crn) != region:
            continue
        api_name = active_by_crn[crn]
        old_name = entry.get("name", "")
        if api_name != old_name:
            renames.append(InstanceRename(crn=crn, old_name=old_name, new_name=api_name))
            entry["name"] = api_name
    return renames


def _new_instance_entry(inst: DiscoveredInstance, fallback_name: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": inst.name or fallback_name,
        "crn": inst.crn,
    }
    if inst.limit_seconds is not None:
        entry["limit_seconds"] = inst.limit_seconds
    return entry


def _add_instances(
    doc_instances: list,
    active: tuple[DiscoveredInstance, ...],
    region: Region | None,
) -> list[DiscoveredInstance]:
    existing_crns = {entry.get("crn", "") for entry in doc_instances}
    candidates = [
        inst
        for inst in active
        if inst.crn not in existing_crns and (region is None or extract_region_from_crn(inst.crn) == region)
    ]
    sorted_candidates = sorted(candidates, key=lambda x: (x.name == "", x.name))
    start_index = len(doc_instances) + 1
    for offset, inst in enumerate(sorted_candidates):
        doc_instances.append(_new_instance_entry(inst, fallback_name=f"Instance {start_index + offset}"))
    return sorted_candidates


def compute_update(
    doc: Any,
    discovered: DiscoveredInstances,
    *,
    now: datetime,
    region: Region | None = None,
    actions: UpdateActions = UpdateActions(),
) -> UpdateSummary:
    """Reconcile a config document against the discovered API state in place.

    ``doc`` is mutated; the returned summary describes what changed. The
    function does not perform I/O or call the API. Use ``ruamel.yaml`` (round-trip
    mode) to load the document so user comments are preserved.

    The ``region`` filter restricts *additions* and *renames* to instances in
    that region. Existing config entries are still subject to expiration and
    removal regardless of region — otherwise ``--region us-east`` would
    silently leave behind stale entries from other regions.
    """
    doc_instances = doc.get("instances")
    if doc_instances is None:
        raise ValueError("Config is missing required 'instances' field")

    summary = UpdateSummary()

    if actions.remove_instances:
        archived_crns = {d.crn for d in discovered.archived}
        known_crns = archived_crns | {d.crn for d in discovered.active}
        removed = _remove_instances(doc_instances, archived_crns, known_crns)
        summary = _replace_summary(summary, removed_instances=tuple(removed))

    if actions.expire_net_grants:
        expired = _expire_net_grants(doc_instances, now)
        summary = _replace_summary(summary, expired_net_grants=tuple(expired))

    if actions.fix_names:
        active_by_crn = {d.crn: d.name for d in discovered.active}
        renames = _fix_names(doc_instances, active_by_crn, region)
        summary = _replace_summary(summary, renamed_instances=tuple(renames))

    if actions.add_instances:
        added = _add_instances(doc_instances, discovered.active, region)
        summary = _replace_summary(summary, added_instances=tuple(added))

    return summary


def _replace_summary(summary: UpdateSummary, **changes: Any) -> UpdateSummary:
    return replace(summary, **changes)
