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
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class NetGrant:
    """An additive time budget boost above limit_seconds."""

    start_date: datetime
    net_grant_seconds: int
    end_date: datetime

    def __post_init__(self) -> None:
        if self.net_grant_seconds <= 0:
            raise ValueError("net_grant_seconds must be positive")
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")


@dataclass(frozen=True)
class InstanceConfig:
    """Configuration for a single quantum instance, keyed by CRN."""

    name: str
    crn: str
    start_date: datetime
    end_date: datetime
    target_usage_seconds: int | None = None
    limit_seconds: int | None = None
    net_grants: tuple[NetGrant, ...] = ()

    def __post_init__(self) -> None:
        if self.target_usage_seconds is not None and self.target_usage_seconds <= 0:
            raise ValueError("target_usage_seconds must be positive")
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        if not self.crn:
            raise ValueError("crn cannot be empty")


@dataclass(frozen=True)
class InstanceIdentifier:
    """Minimal identity record from the Resource Controller API."""

    crn: str
    name: str


@dataclass(frozen=True)
class Instance:
    """Base instance state from the IBM Quantum API.

    Carries only the fields the API returns for a single instance, plus the
    28-day rolling consumption. Detailed analytics data lives on
    :class:`InstanceUsage` and is paired with this in :class:`ResolvedInstance`.
    """

    crn: str
    name: str
    allocation_seconds: int
    limit_seconds: int | None = None
    consumed_seconds: int = 0  # 28-day rolling window

    @property
    def fairness(self) -> float:
        """Calculate fairness value for this instance."""
        if self.allocation_seconds > 0:
            return self.consumed_seconds / self.allocation_seconds
        return float("inf") if self.consumed_seconds > 0 else 0.0


@dataclass(frozen=True)
class InstanceUsage:
    """Detailed usage data for one instance, fetched from analytics endpoints.

    Constructed once during enrichment so downstream consumers can rely on the
    full set of buckets being present rather than mutating an Instance in place.
    """

    consumed_balance_period: int = 0  # Usage since balance period start
    consumed_14day: int = 0
    consumed_7day: int = 0
    consumed_3day: int = 0
    consumed_24h: int = 0
    daily_usage: dict[date, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedInstance:
    """An :class:`Instance` paired with its :class:`InstanceConfig` and resolved
    :class:`InstanceUsage`.

    This is the type the optimizer and limit resolver operate on — by
    construction, every field needed to compute ``activity_score`` /
    ``exhausted`` / limit overrides is present.
    """

    instance: Instance
    config: InstanceConfig
    usage: InstanceUsage

    # ---- delegations to the underlying records ----
    @property
    def crn(self) -> str:
        return self.instance.crn

    @property
    def name(self) -> str:
        return self.instance.name

    @property
    def allocation_seconds(self) -> int:
        return self.instance.allocation_seconds

    @property
    def limit_seconds(self) -> int | None:
        return self.instance.limit_seconds

    @property
    def consumed_seconds(self) -> int:
        return self.instance.consumed_seconds

    @property
    def fairness(self) -> float:
        return self.instance.fairness

    @property
    def target_usage_seconds(self) -> int | None:
        return self.config.target_usage_seconds

    @property
    def consumed_balance_period(self) -> int:
        return self.usage.consumed_balance_period

    @property
    def consumed_14day(self) -> int:
        return self.usage.consumed_14day

    @property
    def consumed_7day(self) -> int:
        return self.usage.consumed_7day

    @property
    def consumed_3day(self) -> int:
        return self.usage.consumed_3day

    @property
    def consumed_24h(self) -> int:
        return self.usage.consumed_24h

    @property
    def daily_usage(self) -> dict[date, int]:
        return self.usage.daily_usage

    # ---- derived values ----
    @property
    def activity_score(self) -> float:
        """Composite activity score with exponential weighting.

        Each time bucket is multiplied by ``bias`` raised to an exponent:
        24h=5.0, 3d=4.0, 7d=3.0, 14d=2.0, 28d=1.0 (with bias=2.0, 24h carries
        16x the weight of 28d).
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
        if self.instance.consumed_seconds > 0:
            score += (self.instance.consumed_seconds / 28.0) * (bias**1.0)
        return score

    @property
    def exhausted(self) -> bool:
        """True when the instance has used its full configured target."""
        target = self.config.target_usage_seconds
        if target is None or target <= 0:
            return False
        return self.usage.consumed_balance_period >= target


@dataclass(frozen=True)
class Account:
    """IBM Cloud account with instances for a specific plan."""

    account_id: str
    plan_id: str
    target_usage_seconds: int
    available_seconds: int
    limit_seconds: int | None
    instances: tuple[Instance, ...]

    @cached_property
    def consumed_seconds(self) -> int:
        return sum(i.consumed_seconds for i in self.instances)

    @property
    def utilization(self) -> float:
        if self.target_usage_seconds > 0:
            return (self.consumed_seconds / self.target_usage_seconds) * 100
        return 0.0


@dataclass(frozen=True)
class ResolvedAccount:
    """An :class:`Account` whose instances have been paired with their configs
    and detailed usage.

    Returned by ``enrich_instances_with_usage_data`` and consumed by the
    optimizer / limit resolver.
    """

    account_id: str
    plan_id: str
    target_usage_seconds: int
    available_seconds: int
    limit_seconds: int | None
    instances: tuple[ResolvedInstance, ...]

    @cached_property
    def consumed_seconds(self) -> int:
        return sum(r.consumed_seconds for r in self.instances)

    @property
    def utilization(self) -> float:
        if self.target_usage_seconds > 0:
            return (self.consumed_seconds / self.target_usage_seconds) * 100
        return 0.0


@dataclass
class OptimizationRecommendation:
    """Represents a single optimization recommendation for an instance."""

    instance_crn: str
    current_allocation: int
    new_allocation: int
    reason: str
    new_limit: int | None = None

    @property
    def change(self) -> int:
        """Calculate the change in allocation."""
        return self.new_allocation - self.current_allocation


@dataclass
class OptimizationResult:
    """Results from optimization algorithm."""

    account: ResolvedAccount
    instance_configs: list[InstanceConfig]
    recommendations: list[OptimizationRecommendation]

    @property
    def reductions(self) -> list[OptimizationRecommendation]:
        """Get recommendations that reduce allocation (change < 0)."""
        return [rec for rec in self.recommendations if rec.change < 0]

    @property
    def additions(self) -> list[OptimizationRecommendation]:
        """Get recommendations that increase allocation (change > 0)."""
        return [rec for rec in self.recommendations if rec.change > 0]

    def add_recommendation(self, instance_crn: str, current_allocation: int, new_allocation: int, reason: str) -> None:
        """Add a recommendation to the results."""
        self.recommendations.append(
            OptimizationRecommendation(
                instance_crn=instance_crn,
                current_allocation=current_allocation,
                new_allocation=new_allocation,
                reason=reason,
            )
        )
