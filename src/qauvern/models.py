# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Data models for qauvern."""

from functools import cached_property
from dataclasses import dataclass
from datetime import date, datetime

from .region import Region, extract_region_from_crn


@dataclass(frozen=True)
class NetGrant:
    """An additive time budget boost above limit_seconds."""

    start_date: datetime
    net_grant_seconds: int
    end_date: datetime


@dataclass(frozen=True)
class InstanceConfig:
    """Configuration for a single quantum instance, keyed by CRN."""

    name: str
    crn: str
    target_limit_seconds: int | None = None
    net_grants: tuple[NetGrant, ...] = ()


@dataclass(frozen=True)
class DiscoveredInstance:
    crn: str
    name: str
    allocation_seconds: int
    limit_seconds: int | None


@dataclass(frozen=True)
class DiscoveredInstances:
    """Result of discover_instances, split into active and archived collections."""

    active: tuple[DiscoveredInstance, ...]
    archived: tuple[DiscoveredInstance, ...]

    def filter_by_region(self, region: Region | None) -> "DiscoveredInstances":
        if region is None:
            return self
        return DiscoveredInstances(
            active=tuple(i for i in self.active if extract_region_from_crn(i.crn) == region),
            archived=tuple(i for i in self.archived if extract_region_from_crn(i.crn) == region),
        )


@dataclass(frozen=True)
class InstanceNameDrift:
    """An instance whose configured name no longer matches the live API name."""

    crn: str
    config_name: str
    api_name: str

    def __str__(self) -> str:
        return f'"{self.config_name}" -> "{self.api_name}" (crn: {self.crn})'


@dataclass(frozen=True)
class InstanceDetailedUsage:
    consumed_14day: int
    consumed_7day: int
    consumed_3day: int
    consumed_24h: int
    daily_usage: dict[date, int]


@dataclass
class InstanceState:
    """Represents the live state of an instance, populated by the IBM APIs."""

    crn: str
    name: str
    allocation_seconds: int
    limit_seconds: int | None
    consumed_seconds: int  # Usage in 28-day rolling window
    detailed_usage: InstanceDetailedUsage | None
    # None means "ANY backend"; a tuple is an explicit allow-list.
    backends: tuple[str, ...] | None = None

    @property
    def usage(self) -> InstanceDetailedUsage:
        if self.detailed_usage is None:
            raise AssertionError("Instance.detailed_usage accessed before it was populated")
        return self.detailed_usage

    @property
    def fairness(self) -> float:
        """Calculate fairness value for this instance."""
        if self.allocation_seconds > 0:
            return self.consumed_seconds / self.allocation_seconds
        return float("inf") if self.consumed_seconds > 0 else 0.0

    @property
    def activity_score(self) -> float:
        """Calculate composite activity score based on usage across time periods.

        Uses exponential weighting where recent usage is weighted more heavily.
        Each time bucket is multiplied by bias raised to an exponent:
        - 24h usage: bias^5.0
        - 3d usage: bias^4.0
        - 7d usage: bias^3.0
        - 14d usage: bias^2.0
        - 28d usage: bias^1.0

        Returns:
            Composite activity score (higher = more active recently)
        """
        score: float = 0.0
        bias: float = 2.0
        if self.usage.consumed_24h > 0:
            score += (self.usage.consumed_24h / 1.0) * (bias**5.0)
        if self.usage.consumed_3day > 0:
            score += (self.usage.consumed_3day / 3.0) * (bias**4.0)
        if self.usage.consumed_7day > 0:
            score += (self.usage.consumed_7day / 7.0) * (bias**3.0)
        if self.usage.consumed_14day > 0:
            score += (self.usage.consumed_14day / 14.0) * (bias**2.0)
        if self.consumed_seconds > 0:
            score += (self.consumed_seconds / 28.0) * (bias**1.0)
        return score


@dataclass(frozen=True)
class Account:
    """IBM Cloud account with instances for a specific plan."""

    account_id: str
    plan_id: str
    allocation_budget_seconds: int
    unallocated_seconds: int
    limit_seconds: int | None
    instances: tuple[InstanceState, ...]

    @cached_property
    def consumed_seconds(self) -> int:
        return sum(i.consumed_seconds for i in self.instances)

    @cached_property
    def unmanaged_allocation_seconds(self) -> int:
        """Allocation held by instances not present in `self.instances`.

        Derived as `budget − unallocated − sum(loaded instance allocations)`.
        Lets the optimizer validate against the account-wide cap when only
        a subset of instances are loaded. Returns 0 when every instance is
        present.

        Clamped at 0: a negative result means the snapshot is internally
        inconsistent (e.g. allocations changed mid-fetch). Pretending there is
        spare cap headroom would let validation pass an over-cap projection,
        so we treat that as "no unmanaged allocation" instead.
        """
        configured = sum(i.allocation_seconds for i in self.instances)
        return max(0, self.allocation_budget_seconds - self.unallocated_seconds - configured)


@dataclass(frozen=True)
class AllocationChange:
    """A proposed change to a single instance's allocation."""

    current: int
    new: int
    reason: str

    @property
    def delta(self) -> int:
        return self.new - self.current


@dataclass(frozen=True)
class LimitChange:
    """A proposed change to a single instance's limit."""

    current: int | None
    new: int


@dataclass(frozen=True)
class OptimizationResult:
    """Results from the optimization algorithm.

    Both dicts are keyed by CRN — at most one entry per instance.
    """

    allocation_changes: dict[str, AllocationChange]
    limit_changes: dict[str, LimitChange]

    @property
    def decreases(self) -> dict[str, AllocationChange]:
        return {crn: c for crn, c in self.allocation_changes.items() if c.delta < 0}

    @property
    def increases(self) -> dict[str, AllocationChange]:
        return {crn: c for crn, c in self.allocation_changes.items() if c.delta > 0}
