# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Optimization algorithm for IBM Quantum instance allocation."""

from datetime import date

from .limit_resolver import LimitResolver
from .models import Account, InstanceState, InstanceConfig, OptimizationRecommendation, OptimizationResult


class AllocationOptimizer:
    """Optimizer for quantum instance allocations."""

    def __init__(
        self,
        account: Account,
        instance_configs: list[InstanceConfig],
        minimum_allocation_seconds: int = 60,
        allocation_reserve_percent: float = 0.0,
        today: date | None = None,
    ):
        """Initialize the optimizer.

        Args:
            account: Account with instances to optimize
            instance_configs: List of instance configs with allocation constraints
            minimum_allocation_seconds: Minimum allocation to maintain for each instance (default: 60 seconds)
            allocation_reserve_percent: Fraction of available seconds to hold back from redistribution
            today: Date to use for limit override resolution (defaults to date.today())
        """
        self.account = account
        self.instance_configs = instance_configs
        self.minimum_allocation_seconds = minimum_allocation_seconds
        self.allocation_reserve_percent = allocation_reserve_percent
        self.today = today or date.today()
        self._config_by_crn = {config.crn: config for config in instance_configs}
        self._limit_resolver = LimitResolver()

    def _config_for(self, instance: InstanceState) -> InstanceConfig | None:
        """Get the instance config for a runtime instance."""
        return self._config_by_crn.get(instance.crn)

    def _consumption_for(self, config: InstanceConfig) -> int:
        """Calculate total consumption for an instance config."""
        for instance in self.account.instances:
            if instance.crn == config.crn:
                return instance.consumed_seconds
        return 0

    def optimize(self) -> OptimizationResult:
        """Compute allocation and limit recommendations based on core algorithm.

        Core Algorithm (from Design.md):
        1. Get detailed usage for all instances
        2. Allocation can never be reduced below current 28d usage (system constraint)
        3. Create composite activity score using exponential weighting:
           - Each bucket's usage/days is multiplied by bias^exponent
           - Exponents: 24h=5.0, 3d=4.0, 7d=3.0, 14d=2.0, 28d=1.0
           - With bias=2.0, creates strong recency bias (24h has 16x weight vs 28d)
        4. Instances with score=0 → minimal allocation
        5. Temporarily reduce active instances to their 28-day usage to free up allocation
        6. Redistribute all available allocation to active instances proportionally by activity score
        7. Resolve effective limits via LimitResolver (net grants, rolloff)

        Returns:
            OptimizationResult with recommendations; no changes are applied
        """
        recommendations: list[OptimizationRecommendation] = []

        # Step 1: Categorize instances by activity score
        print("\n=== Step 1: Categorizing Instances ===")
        instance_scores = {}
        inactive = []  # score = 0
        active = []  # score > 0

        for instance in self.account.instances:
            config = self._config_for(instance)
            if not config:
                continue

            # Use the activity_score property from Instance model
            score = instance.activity_score
            instance_scores[instance.crn] = score

            if score == 0:
                inactive.append(instance)
            else:
                active.append(instance)

        print(f"  Inactive instances (score=0): {len(inactive)}")
        print(f"  Active instances (score>0): {len(active)}")

        # Step 2: Reduce allocation for inactive instances (score = 0)
        # Allocation cannot go below current 28d usage
        print("\n=== Step 2: Processing Inactive Instances ===")
        freed_allocation = 0
        inactive_reduced = 0
        inactive_increased = 0
        for instance in inactive:
            # Minimum is the greater of: minimum_allocation_seconds or current 28d usage
            min_allocation = max(self.minimum_allocation_seconds, instance.consumed_seconds)
            if instance.allocation_seconds > min_allocation:
                freed = instance.allocation_seconds - min_allocation
                freed_allocation += freed
                recommendations.append(
                    OptimizationRecommendation(
                        instance_crn=instance.crn,
                        current_allocation=instance.allocation_seconds,
                        new_allocation=min_allocation,
                        reason=f"No recent activity (score=0), reducing to minimum (cannot go below 28d usage: {instance.consumed_seconds}s)",
                    )
                )
                inactive_reduced += 1
            elif instance.allocation_seconds < min_allocation:
                # Need to increase allocation to meet minimum (28d usage floor)
                additional = min_allocation - instance.allocation_seconds
                recommendations.append(
                    OptimizationRecommendation(
                        instance_crn=instance.crn,
                        current_allocation=instance.allocation_seconds,
                        new_allocation=min_allocation,
                        reason=f"Allocation below 28d usage floor, increasing to minimum: {instance.consumed_seconds}s",
                    )
                )
                freed_allocation -= additional  # This reduces available allocation
                inactive_increased += 1
        print(f"  Inactive instances reduced: {inactive_reduced}")
        print(f"  Inactive instances increased (below 28d floor): {inactive_increased}")
        print(f"  Allocation freed from inactive: {freed_allocation}s")

        # Step 3: Temporarily reduce active instances to their 28-day usage
        # This frees up all allocation not already consumed this period
        print("\n=== Step 3: Reducing Active Instances to 28-Day Usage ===")
        active_freed = 0
        for instance in active:
            # Reduce to 28-day usage (cannot go below this anyway)
            min_allocation = max(self.minimum_allocation_seconds, instance.consumed_seconds)
            if instance.allocation_seconds > min_allocation:
                freed = instance.allocation_seconds - min_allocation
                freed_allocation += freed
                active_freed += freed
        print(f"  Allocation freed from active instances: {active_freed}s")

        # Step 4: Calculate total allocation to distribute
        # Use ALL available allocation (freed + account available)
        print("\n=== Step 4: Calculating Total Available Allocation ===")
        reserve_factor = 1.0 - (self.allocation_reserve_percent / 100.0)
        total_to_allocate = int((freed_allocation + self.account.available_seconds) * reserve_factor)
        print(f"  Freed from inactive: {freed_allocation - active_freed}s")
        print(f"  Freed from active (temporary reduction): {active_freed}s")
        print(f"  Account available: {self.account.available_seconds}s")
        print(f"  Total to allocate: {total_to_allocate}s")

        # Step 5: Distribute allocation to active instances
        print("\n=== Step 5: Distributing to Active Instances ===")
        if active and total_to_allocate > 0:
            # All active instances have been reduced to 28-day usage in Step 3
            # Now distribute all available allocation proportionally by activity score
            print("  Distributing all available allocation by activity score")
            print(f"    Total to distribute: {total_to_allocate}s")

            # Calculate total score across all active instances
            total_score = sum(instance_scores[inst.crn] for inst in active)
            print(f"    Total activity score: {total_score:.1f}")

            if total_score > 0:
                # Sort by score (highest first) for better distribution
                active_sorted = sorted(active, key=lambda x: instance_scores[x.crn], reverse=True)

                active_recommendations = 0
                for instance in active_sorted:
                    config = self._config_for(instance)
                    if not config:
                        continue

                    # Calculate proportional share based on activity score
                    score = instance_scores[instance.crn]
                    proportional_share = int((score / total_score) * total_to_allocate)

                    # Start from 28-day usage floor
                    min_allocation = max(self.minimum_allocation_seconds, instance.consumed_seconds)
                    new_allocation = min_allocation + proportional_share

                    # Cap allocation at limit_seconds if set
                    if instance.limit_seconds is not None:
                        if new_allocation > instance.limit_seconds:
                            new_allocation = instance.limit_seconds

                    # Only create recommendation if allocation changes
                    if new_allocation != instance.allocation_seconds:
                        recommendations.append(
                            OptimizationRecommendation(
                                instance_crn=instance.crn,
                                current_allocation=instance.allocation_seconds,
                                new_allocation=new_allocation,
                                reason=f"Active instance (activity score: {score:.1f}, fairness: {instance.fairness:.2f})",
                            )
                        )
                        active_recommendations += 1

                print(f"    Recommendations for active instances: {active_recommendations}")
        else:
            print("  No active instances or no allocation to distribute")

        # Step 6: Resolve effective limits for each instance
        print("\n=== Step 6: Resolving Limits ===")
        limit_updates = 0
        for instance in self.account.instances:
            config = self._config_for(instance)
            if not config:
                continue

            new_limit = self._limit_resolver.resolve(config, instance, self.today)

            if new_limit is not None and new_limit != instance.limit_seconds:
                existing_rec = next((rec for rec in recommendations if rec.instance_crn == instance.crn), None)
                if existing_rec:
                    existing_rec.new_limit = new_limit
                else:
                    recommendations.append(
                        OptimizationRecommendation(
                            instance_crn=instance.crn,
                            current_allocation=instance.allocation_seconds,
                            new_allocation=instance.allocation_seconds,
                            reason="Updating limit via LimitResolver",
                            new_limit=new_limit,
                        )
                    )
                limit_updates += 1
        print(f"  Limit updates: {limit_updates}")

        print("\n=== Optimization Complete ===")
        print(f"Total recommendations: {len(recommendations)}")

        return OptimizationResult(tuple(recommendations))

    def validate_allocations(self, result: OptimizationResult) -> tuple[bool, list[str]]:
        """Check that applying `result` would not exceed the account cap.

        The check includes allocation held by instances missing from
        `self.account.instances` via `Account.unconfigured_allocation_seconds`,
        so the cap math is correct when only a subset of instances are loaded.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        projected = {inst.crn: inst.allocation_seconds for inst in self.account.instances}
        for rec in result.recommendations:
            projected[rec.instance_crn] = rec.new_allocation

        total_allocated = sum(projected.values()) + self.account.unconfigured_allocation_seconds
        if total_allocated > self.account.target_usage_seconds:
            errors.append(
                f"Total instance allocations ({total_allocated}s) exceeds "
                f"account target ({self.account.target_usage_seconds}s)"
            )

        return len(errors) == 0, errors
