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

from datetime import date, datetime, timezone

from .limit_resolver import resolve_limit
from .models import Account, AllocationChange, InstanceConfig, LimitChange, OptimizationResult


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
            today: Date to use for limit override resolution (defaults to today in UTC)
        """
        self.account = account
        self.instance_configs = instance_configs
        self.minimum_allocation_seconds = minimum_allocation_seconds
        self.allocation_reserve_percent = allocation_reserve_percent
        self.today = today or datetime.now(timezone.utc).date()
        self._configs = {config.crn: config for config in instance_configs}

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
        allocation_changes: list[AllocationChange] = []
        limit_changes: list[LimitChange] = []

        # Step 1: Categorize instances by activity score
        print("\n=== Step 1: Categorizing Instances ===")
        instance_scores = {}
        inactive = []  # score = 0
        active = []  # score > 0

        for instance in self.account.instances:
            config = self._configs.get(instance.crn)
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
                allocation_changes.append(
                    AllocationChange(
                        instance_crn=instance.crn,
                        current=instance.allocation_seconds,
                        new=min_allocation,
                        reason=f"No recent activity (score=0), reducing to minimum (cannot go below 28d usage: {instance.consumed_seconds}s)",
                    )
                )
                inactive_reduced += 1
            elif instance.allocation_seconds < min_allocation:
                # Need to increase allocation to meet minimum (28d usage floor)
                additional = min_allocation - instance.allocation_seconds
                allocation_changes.append(
                    AllocationChange(
                        instance_crn=instance.crn,
                        current=instance.allocation_seconds,
                        new=min_allocation,
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
        total_to_allocate = int((freed_allocation + self.account.unallocated_seconds) * reserve_factor)
        print(f"  Freed from inactive: {freed_allocation - active_freed}s")
        print(f"  Freed from active (temporary reduction): {active_freed}s")
        print(f"  Account available: {self.account.unallocated_seconds}s")
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
                    config = self._configs.get(instance.crn)
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

                    # Only create a change if allocation actually differs
                    if new_allocation != instance.allocation_seconds:
                        allocation_changes.append(
                            AllocationChange(
                                instance_crn=instance.crn,
                                current=instance.allocation_seconds,
                                new=new_allocation,
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
            config = self._configs.get(instance.crn)
            if not config:
                continue

            new_limit = resolve_limit(config, instance, self.today)

            if new_limit is not None and new_limit != instance.limit_seconds:
                limit_changes.append(
                    LimitChange(
                        instance_crn=instance.crn,
                        current=instance.limit_seconds,
                        new=new_limit,
                        reason="Updating limit via LimitResolver",
                    )
                )
                limit_updates += 1
        print(f"  Limit updates: {limit_updates}")

        print("\n=== Optimization Complete ===")
        print(f"Total changes: {len(allocation_changes)} allocation, {len(limit_changes)} limit")

        return OptimizationResult(tuple(allocation_changes), tuple(limit_changes))

    def validate_allocations(self, result: OptimizationResult) -> tuple[bool, list[str]]:
        """Check that `result` satisfies all allocation invariants.

        Checks (in order):
        1. Total projected allocation does not exceed the account budget.
        2. Total projected allocation respects the allocation_reserve_percent buffer.
        3. Each managed instance's new_allocation >= its 28-day consumed usage.
        4. Each managed instance's new_allocation >= minimum_allocation_seconds.
        5. Each managed instance's new_allocation <= its effective limit (if set),
           unless invariants 3 or 4 force it higher: the floor max(consumed_seconds,
           minimum_allocation_seconds) takes precedence, since a tightened limit
           below that floor is an unavoidable, non-actionable breach.
        6. No managed instance's new_allocation is 0 (archiving is not allowed).

        Unmanaged instances (those not in self.account.instances) contribute their
        current allocation to the total-cap check via Account.unmanaged_allocation_seconds.
        Per-instance invariants (3–6) only apply to instances present in
        self.account.instances that also have a config.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        alloc_by_crn = {c.instance_crn: c for c in result.allocation_changes}
        limit_by_crn = {c.instance_crn: c for c in result.limit_changes}

        # Invariant 1: total allocation cap
        total_allocated = (
            sum(
                alloc_by_crn[inst.crn].new if inst.crn in alloc_by_crn else inst.allocation_seconds
                for inst in self.account.instances
            )
            + self.account.unmanaged_allocation_seconds
        )
        if total_allocated > self.account.allocation_budget_seconds:
            errors.append(
                f"Total instance allocations ({total_allocated}s) exceeds "
                f"account budget ({self.account.allocation_budget_seconds}s)"
            )

        # Invariant 2: reserve buffer
        # available = unallocated headroom + what managed instances hold above their floors.
        # Mirrors the optimizer's Steps 2-3 redistribution pool.
        available = max(
            0,
            self.account.unallocated_seconds
            + sum(
                inst.allocation_seconds - max(self.minimum_allocation_seconds, inst.consumed_seconds)
                for inst in self.account.instances
                if inst.crn in self._configs
            ),
        )
        reserve_amount = int(available * self.allocation_reserve_percent / 100)
        effective_budget = self.account.allocation_budget_seconds - reserve_amount
        if reserve_amount > 0 and total_allocated > effective_budget:
            errors.append(
                f"Total instance allocations ({total_allocated}s) exceeds budget minus reserve ({effective_budget}s)"
            )

        # Invariants 3–6: per managed instance
        for inst in self.account.instances:
            if inst.crn not in self._configs:
                continue
            alloc_chg = alloc_by_crn.get(inst.crn)
            new_alloc = alloc_chg.new if alloc_chg is not None else inst.allocation_seconds

            # Invariant 3: >= 28-day usage
            if new_alloc < inst.consumed_seconds:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is below "
                    f"28-day usage ({inst.consumed_seconds}s)"
                )

            # Invariant 4: >= minimum_allocation_seconds
            if new_alloc < self.minimum_allocation_seconds:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is below "
                    f"minimum ({self.minimum_allocation_seconds}s)"
                )

            # Invariant 5: <= effective limit (limit_changes take precedence).
            # Invariants 3 and 4 win: only fire when the breach exceeds the
            # floor they would force, so a limit tightened below that floor
            # doesn't surface as a separate, unactionable error.
            limit_chg = limit_by_crn.get(inst.crn)
            effective_limit = limit_chg.new if limit_chg is not None else inst.limit_seconds
            floor = max(self.minimum_allocation_seconds, inst.consumed_seconds)
            if effective_limit is not None and new_alloc > effective_limit and new_alloc > floor:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) exceeds effective limit ({effective_limit}s)"
                )

            # Invariant 6: no archiving
            if new_alloc == 0:
                errors.append(f"Instance {inst.crn}: new_allocation is 0 (archiving not allowed)")

        return len(errors) == 0, errors
