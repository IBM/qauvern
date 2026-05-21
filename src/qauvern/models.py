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

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class NetGrant:
    """An additive time budget boost above project_limit_seconds."""

    start_date: datetime
    net_grant_seconds: int
    end_date: datetime

    def __post_init__(self) -> None:
        if self.net_grant_seconds <= 0:
            raise ValueError("net_grant_seconds must be positive")
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")


@dataclass
class Project:
    """Represents a quantum project with allocation constraints.

    Note: A project corresponds to exactly one service instance (CRN).
    Projects and instances are conceptually the same thing.
    """

    name: str
    crn: str
    start_date: datetime
    end_date: datetime
    target_usage_seconds: int | None = None
    project_limit_seconds: int | None = None
    net_grants: list[NetGrant] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate project data."""
        if self.target_usage_seconds is not None and self.target_usage_seconds <= 0:
            raise ValueError("target_usage_seconds must be positive")
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        if not self.crn:
            raise ValueError("crn cannot be empty")


@dataclass
class Instance:
    """Represents a quantum service instance."""

    crn: str
    name: str
    allocation_seconds: int
    limit_seconds: int | None = None
    consumed_seconds: int = 0  # Usage in 28-day rolling window
    plan: str | None = None
    target_usage_seconds: int = 0  # Target usage from project configuration
    consumed_balance_period: int = 0  # Usage since balance period start
    consumed_14day: int = 0  # Usage in last 14 days
    consumed_7day: int = 0  # Usage in last 7 days
    consumed_3day: int = 0  # Usage in last 3 days
    consumed_24h: int = 0  # Usage in last 24 hours
    daily_usage: dict[date, int] = field(default_factory=dict)

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
        if self.consumed_24h > 0:
            score += (self.consumed_24h / 1.0) * (bias**5.0)
        if self.consumed_3day > 0:
            score += (self.consumed_3day / 3.0) * (bias**4.0)
        if self.consumed_7day > 0:
            score += (self.consumed_7day / 7.0) * (bias**3.0)
        if self.consumed_14day > 0:
            score += (self.consumed_14day / 14.0) * (bias**2.0)
        if self.consumed_seconds > 0:
            score += (self.consumed_seconds / 28.0) * (bias**1.0)
        return score

    @property
    def exhausted(self) -> bool:
        """Check if instance has exhausted its target usage for the balance period.

        Returns:
            True if consumed_balance_period exceeds target_usage_seconds, False otherwise
        """
        if self.target_usage_seconds > 0:
            return self.consumed_balance_period >= self.target_usage_seconds
        return False


@dataclass
class Account:
    """Represents an IBM Cloud account with quantum allocation.

    Note: Allocations are at the Account + Plan level.
    This Account object represents allocation for a specific plan within the account.
    """

    account_id: str
    plan_id: str
    target_usage_seconds: int
    consumed_seconds: int = 0
    available_seconds: int = 0  # From API's unallocated_usage_seconds
    limit_seconds: int | None = None  # From API's usage_limit_seconds
    instances: list[Instance] = field(default_factory=list)
    allocation_reserve_percent: float = 0.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.allocation_reserve_percent < 100.0):
            raise ValueError("allocation_reserve_percent must be in range [0, 100)")

    @property
    def utilization(self) -> float:
        """Calculate utilization percentage."""
        if self.target_usage_seconds > 0:
            return (self.consumed_seconds / self.target_usage_seconds) * 100
        return 0.0

    def add_instance(self, instance: Instance) -> None:
        """Add an instance to the account."""
        self.instances.append(instance)


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

    account: Account
    projects: list[Project]
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
