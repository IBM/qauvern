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
from .models import Account, AllocationChange, InstanceConfig, InstanceState, LimitChange, OptimizationResult


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

    def _floor(self, instance: InstanceState) -> int:
        return max(self.minimum_allocation_seconds, instance.consumed_seconds)

    def optimize(self) -> OptimizationResult:
        """Compute allocation and limit recommendations.

        Algorithm:
        1. Resolve effective limit for each managed instance via LimitResolver.
        2. Categorize active (activity_score > 0) vs inactive (score == 0).
        3. Pin every managed instance at floor = max(minimum_allocation_seconds,
           consumed_seconds). Inactive instances stay there.
        4. Build the redistribution pool from unallocated headroom plus what
           managed instances hold above their floor; scale by reserve_percent.
        5. Water-fill the pool across active instances proportional to activity
           score. Instances that hit their effective limit drop out of the round
           and the leftover flows to the rest. Leftover after all active
           instances are capped stays unallocated.
        6. Emit AllocationChange / LimitChange where the projected value differs
           from the live state.
        """
        managed = [inst for inst in self.account.instances if inst.crn in self._configs]

        resolved_limits: dict[str, int | None] = {
            inst.crn: resolve_limit(self._configs[inst.crn], inst, self.today) for inst in managed
        }
        effective_limits: dict[str, int | None] = {
            inst.crn: resolved_limits[inst.crn] if resolved_limits[inst.crn] is not None else inst.limit_seconds
            for inst in managed
        }

        # First, set all instances to their floor. This sometimes increases the allocation
        # to ensure that we meet invariants like >=28-day consumption. Otherwise, it often
        # frees up allocation so that we can redistribute it later based on the activity score.
        new_alloc: dict[str, int] = {inst.crn: self._floor(inst) for inst in managed}

        # Pool: unallocated headroom + (alloc - floor) summed across managed instances.
        # Negative contributions (instances below their floor) reduce the pool because
        # bumping them up to floor consumes budget. Reserve scales the whole pool.
        delta_above_floor = sum(inst.allocation_seconds - self._floor(inst) for inst in managed)
        raw_pool = self.account.unallocated_seconds + delta_above_floor
        pool = max(0, int(raw_pool * (1 - self.allocation_reserve_percent / 100)))

        active = [inst for inst in managed if inst.activity_score > 0]
        if active and pool > 0:
            self._water_fill(active, pool, effective_limits, new_alloc)

        allocation_changes: list[AllocationChange] = []
        for inst in managed:
            projected = new_alloc[inst.crn]
            if projected != inst.allocation_seconds:
                allocation_changes.append(
                    AllocationChange(
                        instance_crn=inst.crn,
                        current=inst.allocation_seconds,
                        new=projected,
                        reason=self._reason_for(inst, projected, effective_limits[inst.crn]),
                    )
                )

        limit_changes = tuple(
            LimitChange(
                instance_crn=inst.crn,
                current=inst.limit_seconds,
                new=new_limit,
                reason="Resolved from config (limit_seconds and any active net grants)",
            )
            for inst in managed
            if (new_limit := resolved_limits[inst.crn]) is not None and new_limit != inst.limit_seconds
        )

        return OptimizationResult(tuple(allocation_changes), limit_changes)

    def _water_fill(
        self,
        active: list[InstanceState],
        pool: int,
        effective_limits: dict[str, int | None],
        new_alloc: dict[str, int],
    ) -> None:
        """Distribute `pool` seconds across `active` proportional to activity score, capping at effective limit.

        Instances that hit their effective limit drop out of the candidate set and
        their leftover share flows to the remaining candidates in the next round.
        """
        scores = {inst.crn: inst.activity_score for inst in active}
        candidates = list(active)
        remaining = pool

        while remaining > 0 and candidates:
            total_score = sum(scores[inst.crn] for inst in candidates)
            if total_score <= 0:
                break

            awarded = 0
            still_active: list[InstanceState] = []
            for inst in candidates:
                share = int((scores[inst.crn] / total_score) * remaining)
                limit = effective_limits[inst.crn]
                room = (limit - new_alloc[inst.crn]) if limit is not None else share
                give = max(0, min(share, room))
                new_alloc[inst.crn] += give
                awarded += give
                if limit is None or new_alloc[inst.crn] < limit:
                    still_active.append(inst)

            remaining -= awarded
            candidates = still_active
            if awarded == 0:
                # Either every remaining candidate is at its cap, or rounding left
                # nothing distributable this round. Either way, no further progress.
                break

    def _reason_for(self, inst: InstanceState, projected: int, effective_limit: int | None) -> str:
        if inst.activity_score == 0:
            return f"No recent activity (score=0); pinning at floor (28d usage: {inst.consumed_seconds}s)"
        capped = effective_limit is not None and projected >= effective_limit
        suffix = " (capped at effective limit)" if capped else ""
        return f"Active (activity score: {inst.activity_score:.1f}, fairness: {inst.fairness:.2f}){suffix}"

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
