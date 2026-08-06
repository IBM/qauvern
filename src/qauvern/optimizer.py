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

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

from .limit_resolver import resolve_limit
from .models import Account, AllocationChange, InstanceConfig, InstanceState, LimitChange, OptimizationResult

FloorSource = Literal["consumed_seconds", "minimum_allocation_seconds"]


@dataclass(frozen=True)
class Floor:
    """The minimum allocation we'll pin an instance to, and why.

    `consumed_seconds` is the IBM Quantum hard floor — the API technically
    does not enforce it, but pinning to it closes a queue-priority exploit:
    dropping an instance's allocation below its usage frees allocation that
    didn't actually exist as spare capacity, which can then be handed to
    another instance to jump it to the front of the fair-share queue.
    `enforce_usage_floor=False` (see AllocationOptimizer) disables this floor
    for accounts where it's not enforceable — see `optimizer.enforce_usage_floor`.
    `minimum_allocation_seconds` is a qauvern-level config knob that the
    user can lower. Ties go to `consumed_seconds` so the user sees the
    unfixable source first.
    """

    value: int
    source: FloorSource


class AllocationOptimizer:
    """Optimizer for quantum instance allocations."""

    def __init__(
        self,
        account: Account,
        instance_configs: list[InstanceConfig],
        minimum_allocation_seconds: int = 60,
        allocation_reserve_percent: float = 0.0,
        enforce_usage_floor: bool = True,
        today: date | None = None,
    ):
        """Initialize the optimizer.

        Args:
            account: Account with instances to optimize
            instance_configs: List of instance configs with allocation constraints
            minimum_allocation_seconds: Minimum allocation to maintain for each instance (default: 60 seconds)
            allocation_reserve_percent: Fraction of available seconds to hold back from redistribution
            enforce_usage_floor: Whether allocation must stay >= 28-day consumed usage (default: True).
                Disabling this is only safe for accounts whose plan allows an instance's usage to
                exceed its allocation (e.g. via a limit set above allocation).
            today: Date to use for limit override resolution (defaults to today in UTC)
        """
        self.account = account
        self.instance_configs = instance_configs
        self.minimum_allocation_seconds = minimum_allocation_seconds
        self.allocation_reserve_percent = allocation_reserve_percent
        self.enforce_usage_floor = enforce_usage_floor
        self.today = today or datetime.now(timezone.utc).date()
        self._configs = {config.crn: config for config in instance_configs}

    def _floor(self, instance: InstanceState) -> Floor:
        if self.enforce_usage_floor and instance.consumed_seconds >= self.minimum_allocation_seconds:
            return Floor(instance.consumed_seconds, "consumed_seconds")
        return Floor(self.minimum_allocation_seconds, "minimum_allocation_seconds")

    def redistribution_pool(self) -> tuple[int, int]:
        """Return (distributable_pool, reserve_amount) in seconds.

        `reserve_amount` is a fixed slice of the *account budget* —
        `allocation_budget_seconds * reserve_percent / 100` — held back so total
        allocation never exceeds `budget * (1 - reserve%/100)`.

        `raw_pool` is the movable headroom: unallocated seconds + sum(allocation -
        floor) over managed instances. `distributable_pool` is whatever movable
        headroom survives after withholding the reserve, clamped at 0 (the reserve
        can swallow the entire pool). Unlike the reserve, it is not a fixed fraction
        of anything — it is what water-fill is actually allowed to award.
        """
        managed = [inst for inst in self.account.instances if inst.crn in self._configs]
        raw_pool = max(
            0,
            self.account.unallocated_seconds
            + sum(inst.allocation_seconds - self._floor(inst).value for inst in managed),
        )
        reserve_amount = int(self.account.allocation_budget_seconds * self.allocation_reserve_percent / 100)
        distributable = max(0, raw_pool - reserve_amount)
        return distributable, reserve_amount

    def optimize(self) -> OptimizationResult:
        """Compute allocation and limit recommendations.

        Algorithm:
        1. Resolve effective limit for each managed instance via LimitResolver.
        2. Categorize active (activity_score > 0) vs inactive (score == 0).
        3. Pin every managed instance at floor = max(minimum_allocation_seconds,
           consumed_seconds). Inactive instances stay there.
        4. Build the redistribution pool from unallocated headroom plus what
           managed instances hold above their floor, then withhold the
           budget-based reserve (allocation_budget * reserve_percent / 100) so
           total allocation stays under budget * (1 - reserve_percent / 100).
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
        floors: dict[str, Floor] = {inst.crn: self._floor(inst) for inst in managed}
        new_alloc: dict[str, int] = {crn: f.value for crn, f in floors.items()}

        pool, _ = self.redistribution_pool()

        active = [inst for inst in managed if inst.activity_score > 0]
        if active and pool > 0:
            self._water_fill(active, pool, effective_limits, new_alloc)

        allocation_changes: dict[str, AllocationChange] = {}
        for inst in managed:
            projected = new_alloc[inst.crn]
            if projected != inst.allocation_seconds:
                allocation_changes[inst.crn] = AllocationChange(
                    current=inst.allocation_seconds,
                    new=projected,
                    reason=self._reason_for(inst, projected, effective_limits[inst.crn]),
                )

        limit_changes = {
            inst.crn: LimitChange(current=inst.limit_seconds, new=new_limit)
            for inst in managed
            if (new_limit := resolved_limits[inst.crn]) is not None and new_limit != inst.limit_seconds
        }

        return OptimizationResult(allocation_changes, limit_changes)

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
            floor = self._floor(inst)
            label = "28d usage" if floor.source == "consumed_seconds" else "config minimum"
            return f"Inactive — pinned to {label}"
        capped = effective_limit is not None and projected >= effective_limit
        suffix = " — capped at limit" if capped else ""
        return f"Active (score {inst.activity_score:.1f}, fairness {inst.fairness:.2f}){suffix}"

    def _budget_breach_error(self, total_allocated: int, budget: int, reserve_amount: int) -> str:
        """Build the message for a projection that exceeds the effective budget.

        The cap is the effective budget = account budget − reserve (the reserve is
        0 when no buffer is configured, so this is just the plain budget cap). The
        message names the reserve in the cap breakdown only when one is set.

        `optimize()` can only breach the cap when the floors themselves — plus
        untouchable unmanaged allocation — already overflow it: water-fill never
        awards more than `budget − reserve − floor_total − unmanaged`, so its
        output is otherwise bounded by the cap. A breach with floors under the cap
        is therefore unreachable from real output (it requires a corrupt API
        snapshot or a hand-built result), so we don't special-case it — we just
        emit one diagnostic that names each driver (28-day usage, the config
        minimum, unmanaged instances) and offers the qauvern-owned knobs that can
        close the gap. Consumed usage and unmanaged allocation aren't user-fixable,
        so when neither knob is in play the message simply states the breach.
        """
        effective_budget = budget - reserve_amount
        over = total_allocated - effective_budget

        if reserve_amount > 0:
            cap_expr = (
                f"the {effective_budget}s cap ({budget}s account budget "
                f"− {reserve_amount}s reserve at {self.allocation_reserve_percent}%)"
            )
        else:
            cap_expr = f"the {budget}s account budget"

        floors = [self._floor(inst) for inst in self.account.instances if inst.crn in self._configs]
        floor_total = sum(f.value for f in floors)
        unmanaged = self.account.unmanaged_allocation_seconds

        # Split the floor by source so the message names what's driving the breach:
        # consumed_seconds is an IBM Quantum reality, minimum_allocation_seconds is a
        # config knob the user can lower.
        consumed_bucket = sum(f.value for f in floors if f.source == "consumed_seconds")
        min_alloc_bucket = floor_total - consumed_bucket

        drivers: list[str] = []
        if consumed_bucket > 0:
            drivers.append(f"28-day usage requires {consumed_bucket}s")
        if min_alloc_bucket > 0:
            drivers.append(f"minimum_allocation_seconds requires {min_alloc_bucket}s")
        if unmanaged > 0:
            drivers.append(f"unmanaged instances hold {unmanaged}s")

        # Only the qauvern-owned knobs are actionable.
        fixes: list[str] = []
        if reserve_amount > 0:
            fixes.append("lower allocation_reserve_percent")
        if min_alloc_bucket > 0:
            fixes.append("lower minimum_allocation_seconds")

        message = f"Total instance allocations ({total_allocated}s) exceed {cap_expr} by {over}s."
        if drivers:
            message += f" Driven by: {'; '.join(drivers)}."
        if fixes:
            message += f" To fix: {' and/or '.join(fixes)}."
        return message

    def usage_floor_warnings(self, result: OptimizationResult) -> list[str]:
        """Warn about instances whose new_allocation falls below 28-day usage.

        Only meaningful when `enforce_usage_floor` is False — with it True, this
        situation is a validate_allocations() error instead, not a warning. Callers
        should print these to stderr; they aren't a reason to block applying changes.
        """
        if self.enforce_usage_floor:
            return []
        warnings = []
        for inst in self.account.instances:
            if inst.crn not in self._configs:
                continue
            alloc_chg = result.allocation_changes.get(inst.crn)
            new_alloc = alloc_chg.new if alloc_chg is not None else inst.allocation_seconds
            if new_alloc < inst.consumed_seconds:
                warnings.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is below "
                    f"28-day usage ({inst.consumed_seconds}s) — enforce_usage_floor is disabled"
                )
        return warnings

    def validate_allocations(self, result: OptimizationResult) -> tuple[bool, list[str]]:
        """Check that `result` satisfies all allocation invariants.

        Checks (in order):
        1. Total projected allocation fits under the effective budget =  account budget − reserve.
        2. Each managed instance's new_allocation >= its 28-day consumed usage (skipped
           when `enforce_usage_floor` is False — see `usage_floor_warnings` instead).
        3. Each managed instance's new_allocation >= minimum_allocation_seconds.
        4. Each managed instance's new_allocation <= its effective limit (if set),
           unless invariants 2 or 3 force it higher: the floor max(consumed_seconds,
           minimum_allocation_seconds) takes precedence, since a tightened limit
           below that floor is an unavoidable, non-actionable breach.
        5. No managed instance's new_allocation is 0 (archiving is not allowed).

        Unmanaged instances (those not in self.account.instances) contribute their
        current allocation to the total-cap check via Account.unmanaged_allocation_seconds.
        Per-instance invariants (2–5) only apply to instances present in
        self.account.instances that also have a config.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        # Invariant 1: total projected allocation must fit under the effective
        # budget = account budget − reserve (the reserve is 0 when unset).
        _, reserve_amount = self.redistribution_pool()
        budget = self.account.allocation_budget_seconds
        effective_budget = budget - reserve_amount

        total_allocated = (
            sum(
                result.allocation_changes[inst.crn].new
                if inst.crn in result.allocation_changes
                else inst.allocation_seconds
                for inst in self.account.instances
            )
            + self.account.unmanaged_allocation_seconds
        )
        if total_allocated > effective_budget:
            errors.append(self._budget_breach_error(total_allocated, budget, reserve_amount))

        # Invariants 2–5: per managed instance
        for inst in self.account.instances:
            if inst.crn not in self._configs:
                continue
            alloc_chg = result.allocation_changes.get(inst.crn)
            new_alloc = alloc_chg.new if alloc_chg is not None else inst.allocation_seconds

            # Invariant 2: >= 28-day usage
            if self.enforce_usage_floor and new_alloc < inst.consumed_seconds:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is below "
                    f"28-day usage ({inst.consumed_seconds}s)"
                )

            # Invariant 3: >= minimum_allocation_seconds
            if new_alloc < self.minimum_allocation_seconds:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is below "
                    f"minimum ({self.minimum_allocation_seconds}s)"
                )

            # Invariant 4: <= effective limit (limit_changes take precedence).
            # Invariants 2 and 3 win: only fire when the breach exceeds the
            # floor they would force, so a limit tightened below that floor
            # doesn't surface as a separate, unactionable error.
            limit_chg = result.limit_changes.get(inst.crn)
            effective_limit = limit_chg.new if limit_chg is not None else inst.limit_seconds
            floor = max(self.minimum_allocation_seconds, inst.consumed_seconds if self.enforce_usage_floor else 0)
            if effective_limit is not None and new_alloc > effective_limit and new_alloc > floor:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) exceeds effective limit ({effective_limit}s)"
                )

            # Invariant 5: no archiving
            if new_alloc == 0:
                errors.append(f"Instance {inst.crn}: new_allocation is 0 (archiving not allowed)")

        return len(errors) == 0, errors
