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
from .models import OptimizationResult, ResolvedAccount, ResolvedInstance


class AllocationOptimizer:
    """Optimizer for quantum instance allocations."""

    def __init__(
        self,
        resolved_account: ResolvedAccount,
        minimum_allocation_seconds: int = 60,
        allocation_reserve_percent: float = 0.0,
        today: date | None = None,
    ):
        """Initialize the optimizer.

        Args:
            resolved_account: Account with instances paired to configs and detailed usage.
            minimum_allocation_seconds: Minimum allocation to maintain for each instance (default: 60 seconds)
            allocation_reserve_percent: Fraction of available seconds to hold back from redistribution
            today: Date to use for limit override resolution (defaults to date.today())
        """
        self.account = resolved_account
        self.minimum_allocation_seconds = minimum_allocation_seconds
        self.allocation_reserve_percent = allocation_reserve_percent
        self.today = today or date.today()
        self._limit_resolver = LimitResolver()

    @property
    def instance_configs(self) -> list:
        """Configs for each resolved instance, in order."""
        return [r.config for r in self.account.instances]

    def _remaining_for(self, resolved: ResolvedInstance) -> int | None:
        """How much more this instance is allowed to consume this period.

        Returns ``None`` when the config has no ``target_usage_seconds`` (uncapped).
        """
        target = resolved.config.target_usage_seconds
        if target is None:
            return None
        return max(0, target - resolved.consumed_seconds)

    def _get_active_instances(self, threshold_seconds: int = 3600) -> list[ResolvedInstance]:
        """Resolved instances with at least ``threshold_seconds`` of 28-day usage."""
        return [r for r in self.account.instances if r.consumed_seconds >= threshold_seconds]

    def _get_inactive_instances(self, threshold_seconds: int = 3600) -> list[ResolvedInstance]:
        """Resolved instances with less than ``threshold_seconds`` of 28-day usage."""
        return [r for r in self.account.instances if r.consumed_seconds < threshold_seconds]

    def analyze(self) -> OptimizationResult:
        """Analyze current allocations and provide recommendations based on core algorithm.

        Core Algorithm (from Design.md):
        1. Get detailed usage for all instances
        2. Allocation can never be reduced below current 28d usage (system constraint)
        3. Exhausted instances (used all allotted time) → allocation=0, limit=1
        4. Create composite activity score using exponential weighting:
           - Each bucket's usage/days is multiplied by bias^exponent
           - Exponents: 24h=5.0, 3d=4.0, 7d=3.0, 14d=2.0, 28d=1.0
           - With bias=2.0, creates strong recency bias (24h has 16x weight vs 28d)
        5. Instances with score=0 → minimal allocation
        6. Temporarily reduce active instances to their 28-day usage to free up allocation
        7. Redistribute all available allocation to active instances proportionally by activity score

        Returns:
            OptimizationResult with recommendations but no changes applied
        """
        result = OptimizationResult(
            account=self.account,
            instance_configs=self.instance_configs,
            recommendations=[],
        )

        # Step 1: Categorize instances by activity score
        print("\n=== Step 1: Categorizing Instances ===")
        instance_scores: dict[str, float] = {}
        exhausted: list[ResolvedInstance] = []
        inactive: list[ResolvedInstance] = []  # score = 0
        active: list[ResolvedInstance] = []  # score > 0

        for resolved in self.account.instances:
            if resolved.exhausted:
                exhausted.append(resolved)
                continue

            score = resolved.activity_score
            instance_scores[resolved.crn] = score

            if score == 0:
                inactive.append(resolved)
            else:
                active.append(resolved)

        print(f"  Exhausted instances: {len(exhausted)}")
        print(f"  Inactive instances (score=0): {len(inactive)}")
        print(f"  Active instances (score>0): {len(active)}")

        # Step 2: Handle exhausted instances - set allocation=0, limit=1
        print("\n=== Step 2: Handling Exhausted Instances ===")
        exhausted_count = 0
        for resolved in exhausted:
            if resolved.allocation_seconds > 0 or (resolved.limit_seconds is None or resolved.limit_seconds != 1):
                result.add_recommendation(
                    instance_crn=resolved.crn,
                    current_allocation=resolved.allocation_seconds,
                    new_allocation=0,
                    reason="Instance has exhausted allocation for accounting period",
                )
                exhausted_count += 1
        print(f"  Recommendations for exhausted instances: {exhausted_count}")

        # Step 3: Reduce allocation for inactive instances (score = 0)
        # Allocation cannot go below current 28d usage
        print("\n=== Step 3: Processing Inactive Instances ===")
        freed_allocation = 0
        inactive_reduced = 0
        inactive_increased = 0
        for resolved in inactive:
            # Minimum is the greater of: minimum_allocation_seconds or current 28d usage
            min_allocation = max(self.minimum_allocation_seconds, resolved.consumed_seconds)
            if resolved.allocation_seconds > min_allocation:
                freed = resolved.allocation_seconds - min_allocation
                freed_allocation += freed
                result.add_recommendation(
                    instance_crn=resolved.crn,
                    current_allocation=resolved.allocation_seconds,
                    new_allocation=min_allocation,
                    reason=f"No recent activity (score=0), reducing to minimum (cannot go below 28d usage: {resolved.consumed_seconds}s)",
                )
                inactive_reduced += 1
            elif resolved.allocation_seconds < min_allocation:
                # Need to increase allocation to meet minimum (28d usage floor)
                additional = min_allocation - resolved.allocation_seconds
                result.add_recommendation(
                    instance_crn=resolved.crn,
                    current_allocation=resolved.allocation_seconds,
                    new_allocation=min_allocation,
                    reason=f"Allocation below 28d usage floor, increasing to minimum: {resolved.consumed_seconds}s",
                )
                freed_allocation -= additional  # This reduces available allocation
                inactive_increased += 1
        print(f"  Inactive instances reduced: {inactive_reduced}")
        print(f"  Inactive instances increased (below 28d floor): {inactive_increased}")
        print(f"  Allocation freed from inactive: {freed_allocation}s")

        # Step 4: Temporarily reduce active instances to their 28-day usage
        # This frees up all allocation not already consumed this period
        print("\n=== Step 4: Reducing Active Instances to 28-Day Usage ===")
        active_freed = 0
        for resolved in active:
            min_allocation = max(self.minimum_allocation_seconds, resolved.consumed_seconds)
            if resolved.allocation_seconds > min_allocation:
                freed = resolved.allocation_seconds - min_allocation
                freed_allocation += freed
                active_freed += freed
        print(f"  Allocation freed from active instances: {active_freed}s")

        # Step 5: Calculate total allocation to distribute
        # Use ALL available allocation (freed + account available)
        print("\n=== Step 5: Calculating Total Available Allocation ===")
        reserve_factor = 1.0 - (self.allocation_reserve_percent / 100.0)
        total_to_allocate = int((freed_allocation + self.account.available_seconds) * reserve_factor)
        print(f"  Freed from inactive: {freed_allocation - active_freed}s")
        print(f"  Freed from active (temporary reduction): {active_freed}s")
        print(f"  Account available: {self.account.available_seconds}s")
        print(f"  Total to allocate: {total_to_allocate}s")

        # Step 6: Distribute allocation to active instances in two phases
        print("\n=== Step 6: Distributing to Active Instances ===")
        if active and total_to_allocate > 0:
            # All active instances have been reduced to 28-day usage in Step 4
            # Now distribute all available allocation proportionally by activity score
            print("  Distributing all available allocation by activity score")
            print(f"    Total to distribute: {total_to_allocate}s")

            total_score = sum(instance_scores[r.crn] for r in active)
            print(f"    Total activity score: {total_score:.1f}")

            if total_score > 0:
                # Sort by score (highest first) for better distribution
                active_sorted = sorted(active, key=lambda r: instance_scores[r.crn], reverse=True)

                active_recommendations = 0
                for resolved in active_sorted:
                    # Calculate how much more this instance could use
                    config_remaining = self._remaining_for(resolved)
                    if config_remaining is not None and config_remaining <= 0:
                        continue

                    # Calculate proportional share based on activity score
                    score = instance_scores[resolved.crn]
                    proportional_share = int((score / total_score) * total_to_allocate)

                    # Start from 28-day usage floor
                    min_allocation = max(self.minimum_allocation_seconds, resolved.consumed_seconds)
                    new_allocation = min_allocation + proportional_share

                    # Cap allocation: use config target if available, otherwise limit_seconds
                    if config_remaining is not None:
                        max_allocation = resolved.allocation_seconds + config_remaining
                        if new_allocation > max_allocation:
                            new_allocation = max_allocation
                    elif resolved.limit_seconds is not None:
                        if new_allocation > resolved.limit_seconds:
                            new_allocation = resolved.limit_seconds

                    # Only create recommendation if allocation changes
                    if new_allocation != resolved.allocation_seconds:
                        result.add_recommendation(
                            instance_crn=resolved.crn,
                            current_allocation=resolved.allocation_seconds,
                            new_allocation=new_allocation,
                            reason=f"Active instance (activity score: {score:.1f}, fairness: {resolved.fairness:.2f})",
                        )
                        active_recommendations += 1

                print(f"    Recommendations for active instances: {active_recommendations}")
        else:
            print("  No active instances or no allocation to distribute")

        print("\n=== Analysis Complete ===")
        print(f"Total recommendations: {len(result.recommendations)}")

        return result

    def optimize(self) -> OptimizationResult:
        """Optimize allocations and calculate new limits.

        This method calculates optimal allocations but does not apply them.
        Use the apply() method to actually update the instances.

        Returns:
            OptimizationResult with optimized allocations
        """
        result = self.analyze()

        # Calculate new limits for each instance using LimitResolver
        for resolved in self.account.instances:
            new_limit = self._limit_resolver.resolve(resolved, self.today)

            if new_limit is not None and new_limit != resolved.limit_seconds:
                existing_rec = next((rec for rec in result.recommendations if rec.instance_crn == resolved.crn), None)
                if existing_rec:
                    existing_rec.new_limit = new_limit
                else:
                    result.add_recommendation(
                        instance_crn=resolved.crn,
                        current_allocation=resolved.allocation_seconds,
                        new_allocation=resolved.allocation_seconds,
                        reason="Updating limit via LimitResolver",
                    )
                    result.recommendations[-1].new_limit = new_limit

        return result

    def validate_allocations(self) -> tuple[bool, list[str]]:
        """Validate that current allocations are within constraints.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        # Check that sum of allocations doesn't exceed account target
        total_allocated = sum(r.allocation_seconds for r in self.account.instances)
        if total_allocated > self.account.target_usage_seconds:
            errors.append(
                f"Total instance allocations ({total_allocated}s) exceeds "
                f"account target ({self.account.target_usage_seconds}s)"
            )

        # Check that each instance config's allocation doesn't exceed its target.
        # Deduplicate by CRN so multiple instances sharing a CRN produce one error.
        seen_crns: set[str] = set()
        for resolved in self.account.instances:
            target = resolved.config.target_usage_seconds
            if target is None or resolved.crn in seen_crns:
                continue
            seen_crns.add(resolved.crn)
            allocated = sum(r.allocation_seconds for r in self.account.instances if r.crn == resolved.crn)
            if allocated > target:
                errors.append(
                    f"Instance '{resolved.config.name}' total allocation ({allocated}s) exceeds target ({target}s)"
                )

        return len(errors) == 0, errors
