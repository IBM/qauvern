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
from functools import cached_property
from typing import Literal

from .limit_resolver import LimitBreakdown, resolve_limit
from .models import AccountPlan, AllocationChange, InstanceConfig, InstanceState, LimitChange, OptimizationResult

FloorSource = Literal["consumed_seconds", "minimum_allocation_seconds"]
CeilingSource = Literal["consumed_seconds", "effective_limit"]


@dataclass(frozen=True)
class Floor:
    """The minimum allocation we'll pin an instance to, and why.

    While the account is under its allocation budget, an instance's allocation
    never drops below its `consumed_seconds`.

    Once the account is over its allocation budget (see AllocationOptimizer),
    the invariant inverts: allocation is capped at usage (see `Ceiling`) so that
    fairness stays >= 1.0. The floor is then `minimum_allocation_seconds`, except
    that it drops to `consumed_seconds` when usage is below the minimum, since
    honoring the minimum would leave fairness below 1.0. Unused instances keep
    the full minimum: their fairness is 0 whatever the allocation, and the
    minimum is a buffer for their first runs.

    Instances that reached their limit (see `AllocationOptimizer.limit_reached_crns`)
    are held at this same floor, since anything above it would go to an
    instance that cannot run. The floor never depends on the instance's
    limit, so `redistribution_pool()` does not need usage data to resolve limits.

    `minimum_allocation_seconds` is a qauvern-level config knob that the
    user can lower. Under budget, ties go to `consumed_seconds` so the user
    sees the unfixable source first. Over budget, ties go to
    `minimum_allocation_seconds`, since lowering it lowers the floor.
    """

    value: int
    source: FloorSource


@dataclass(frozen=True)
class Ceiling:
    """The maximum allocation water-fill may award an instance, and why.

    Under budget, the ceiling is the instance's effective limit (`source =
    "effective_limit"`), above which it cannot run.

    Over budget, the ceiling is instead the instance's 28-day usage (`source
    = "consumed_seconds"`), so that every instance with usage keeps fairness
    >= 1.0. This usage-based ceiling never exceeds the effective limit,
    because an instance whose usage reached its limit is excluded from
    water-fill entirely rather than given a `Ceiling` (see
    `AllocationOptimizer.limit_reached_crns`).
    """

    value: int
    source: CeilingSource


class AllocationOptimizer:
    """Optimizer for quantum instance allocations."""

    def __init__(
        self,
        account: AccountPlan,
        instance_configs: list[InstanceConfig],
        minimum_allocation_seconds: int = 60,
        allocation_reserve_percent: float = 0.0,
        today: date | None = None,
    ):
        """Initialize the optimizer.

        Once the account is over its allocation budget (`AccountPlan.over_allocation_budget`),
        the optimizer switches to the over-budget regime: see `Floor`, `Ceiling`, and
        `limit_reached_crns`. The account *limit* plays no part: it is a separate, higher
        ceiling above which nothing runs at all.

        Args:
            account: AccountPlan with instances to optimize
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

    @cached_property
    def limit_breakdowns(self) -> dict[str, LimitBreakdown | None]:
        """Effective config-side limit breakdown per managed instance, resolved lazily.

        Attribution reads `instance_state.usage`, which raises when the instance
        wasn't enriched with usage data (e.g. `cli.show`'s `detailed_usage=None`
        path) — computed lazily so callers that never touch this property are
        unaffected.
        """
        return {inst.crn: resolve_limit(self._configs[inst.crn], inst, self.today) for inst in self._managed}

    @cached_property
    def effective_limits(self) -> dict[str, int | None]:
        """Effective limit per managed instance: the resolved config limit, else the live IQP limit.

        The resolved value wins so that a limit raised on this run (e.g. by a new
        net grant) already counts, even though IQP still has the old one.
        """
        return {
            inst.crn: breakdown.total
            if (breakdown := self.limit_breakdowns[inst.crn]) is not None
            else inst.limit_seconds
            for inst in self._managed
        }

    @cached_property
    def limit_reached_crns(self) -> frozenset[str]:
        """Managed instances that cannot run because their usage reached their effective limit.

        They are held at their floor and excluded from water-fill. In the
        over-budget regime, that frees the allocation they held for instances
        that can run. Under budget, their floor of `consumed_seconds` already
        leaves them no room, so excluding them changes nothing.
        """
        return frozenset(
            inst.crn
            for inst in self._managed
            if (limit := self.effective_limits[inst.crn]) is not None and inst.consumed_seconds >= limit
        )

    @cached_property
    def _managed(self) -> list[InstanceState]:
        return [inst for inst in self.account.instances if inst.crn in self._configs]

    def _floor(self, instance: InstanceState) -> Floor:
        # Never 0, which IQP treats as archiving the instance, even if the config
        # sets minimum_allocation_seconds to 0.
        minimum = Floor(max(1, self.minimum_allocation_seconds), "minimum_allocation_seconds")
        consumed = Floor(instance.consumed_seconds, "consumed_seconds")
        if not self.account.over_allocation_budget:
            return consumed if consumed.value >= minimum.value else minimum
        return consumed if 0 < consumed.value < minimum.value else minimum

    def _ceiling(self, instance: InstanceState) -> Ceiling | None:
        if self.account.over_allocation_budget:
            return Ceiling(instance.consumed_seconds, "consumed_seconds")
        limit = self.effective_limits[instance.crn]
        return Ceiling(limit, "effective_limit") if limit is not None else None

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
        raw_pool = max(
            0,
            self.account.unallocated_seconds
            + sum(inst.allocation_seconds - self._floor(inst).value for inst in self._managed),
        )
        reserve_amount = int(self.account.allocation_budget_seconds * self.allocation_reserve_percent / 100)
        distributable = max(0, raw_pool - reserve_amount)
        return distributable, reserve_amount

    def optimize(self) -> OptimizationResult:
        """Compute allocation and limit recommendations.

        Algorithm:
        1. Resolve effective limit for each managed instance via resolve_limit.
        2. Categorize active (activity_score > 0) vs inactive (score == 0).
           Instances whose usage reached their effective limit are treated as
           inactive.
        3. Pin every managed instance at its floor (see `_floor`). Inactive
           instances stay there.
        4. Build the redistribution pool from unallocated headroom plus what
           managed instances hold above their floor, then withhold the
           budget-based reserve (allocation_budget * reserve_percent / 100) so
           total allocation stays under budget * (1 - reserve_percent / 100).
        5. Water-fill the pool across active instances proportional to activity
           score, up to each instance's ceiling (see `_ceiling`). Instances that
           hit their ceiling drop out of the round and the leftover flows to the
           rest. Leftover after all active instances are capped stays unallocated.
        6. Emit AllocationChange / LimitChange where the projected value differs
           from the live state.
        """

        # First, set all instances to their floor. This sometimes increases the allocation
        # to ensure that we meet invariants like >=28-day consumption. Otherwise, it often
        # frees up allocation so that we can redistribute it later based on the activity score.
        new_alloc: dict[str, int] = {inst.crn: self._floor(inst).value for inst in self._managed}

        pool, _ = self.redistribution_pool()

        active = [inst for inst in self._managed if inst.activity_score > 0 and inst.crn not in self.limit_reached_crns]
        if active and pool > 0:
            caps = {
                inst.crn: ceiling.value if (ceiling := self._ceiling(inst)) is not None else None for inst in active
            }
            self._water_fill(active, pool, caps, new_alloc)

        allocation_changes: dict[str, AllocationChange] = {}
        for inst in self._managed:
            projected = new_alloc[inst.crn]
            if projected != inst.allocation_seconds:
                allocation_changes[inst.crn] = AllocationChange(
                    current=inst.allocation_seconds,
                    new=projected,
                    reason=self._reason_for(inst, projected),
                )

        limit_changes = {
            inst.crn: LimitChange(current=inst.limit_seconds, new=limit_breakdown.total)
            for inst in self._managed
            if (limit_breakdown := self.limit_breakdowns[inst.crn]) is not None
            and limit_breakdown.total != inst.limit_seconds
        }

        return OptimizationResult(allocation_changes, limit_changes)

    def _water_fill(
        self,
        active: list[InstanceState],
        pool: int,
        caps: dict[str, int | None],
        new_alloc: dict[str, int],
    ) -> None:
        """Distribute `pool` seconds across `active` proportional to activity score, capping at `caps`.

        Instances that hit their cap drop out of the candidate set and their
        leftover share flows to the remaining candidates in the next round.
        """
        scores = {inst.crn: inst.activity_score for inst in active}
        candidates = list(active)
        remaining = pool

        while remaining > 0 and candidates:
            # Drop candidates with no room (new_alloc >= cap) before computing
            # total_score. This can be true from the first round: new_alloc starts
            # at each instance's floor, which can reach the cap, e.g. if
            # target_limit_seconds was tightened below 28-day usage, or when an
            # over-budget instance has less usage than minimum_allocation_seconds.
            candidates = [inst for inst in candidates if (cap := caps[inst.crn]) is None or cap > new_alloc[inst.crn]]
            if not candidates:
                break

            total_score = sum(scores[inst.crn] for inst in candidates)
            if total_score <= 0:
                break

            awarded = 0
            still_active: list[InstanceState] = []
            for inst in candidates:
                share = int((scores[inst.crn] / total_score) * remaining)
                cap = caps[inst.crn]
                room = (cap - new_alloc[inst.crn]) if cap is not None else share
                give = max(0, min(share, room))
                new_alloc[inst.crn] += give
                awarded += give
                if cap is None or new_alloc[inst.crn] < cap:
                    still_active.append(inst)

            remaining -= awarded
            candidates = still_active
            if awarded == 0:
                # Every remaining candidate hit rounding-to-zero on its share this
                # round, so break.
                break

    def _reason_for(self, inst: InstanceState, projected: int) -> str:
        floor = self._floor(inst)
        floor_label = "28d usage" if floor.source == "consumed_seconds" else "config minimum"
        if inst.crn in self.limit_reached_crns:
            return f"Limit reached — pinned to {floor_label}"
        if inst.activity_score == 0:
            return f"Inactive — pinned to {floor_label}"
        ceiling = self._ceiling(inst)
        suffix = ""
        if ceiling is not None and floor.value >= ceiling.value:
            # Water-fill had no room, e.g. over budget with no 28-day usage.
            suffix = f" — pinned to {floor_label}"
        elif ceiling is not None and projected >= ceiling.value:
            suffix = " — capped at 28d usage" if ceiling.source == "consumed_seconds" else " — capped at limit"
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

        In the over-budget regime, 28-day usage never pushes a floor above the
        config minimum, so rather than blaming usage, the message names the regime
        with the total the floors require.
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

        floors = [self._floor(inst) for inst in self._managed]
        floor_total = sum(f.value for f in floors)
        unmanaged = self.account.unmanaged_allocation_seconds

        # Split the floor by source so the message names what's driving the breach:
        # consumed_seconds is an IBM Quantum reality, minimum_allocation_seconds is a
        # config knob the user can lower.
        consumed_bucket = sum(f.value for f in floors if f.source == "consumed_seconds")
        min_alloc_bucket = floor_total - consumed_bucket

        drivers: list[str] = []
        if self.account.over_allocation_budget:
            drivers.append(
                f"the account is over its allocation budget, and floors require {floor_total}s "
                "(each instance's minimum_allocation_seconds, or its 28-day usage if lower)"
            )
        else:
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
        # Over budget, every floor is at most the minimum, so lowering it always helps.
        if min_alloc_bucket > 0 or self.account.over_allocation_budget:
            fixes.append("lower minimum_allocation_seconds")

        message = f"Total instance allocations ({total_allocated}s) exceed {cap_expr} by {over}s."
        if drivers:
            message += f" Driven by: {'; '.join(drivers)}."
        if fixes:
            message += f" To fix: {' and/or '.join(fixes)}."
        return message

    def over_budget_warnings(self, result: OptimizationResult) -> list[str]:
        """Warn about instances the over-budget regime can't fully accommodate.

        Over budget, allocation is capped at 28-day usage so fairness stays >= 1.0.
        That leaves two exceptions worth surfacing:

        - Usage below minimum_allocation_seconds: the minimum is not honored.
        - Allocation above usage, i.e. fairness below 1.0: in optimize() output,
          only instances with no usage, whose fairness is 0 whatever the allocation.

        Always empty under budget, where these situations are validate_allocations()
        errors instead. Callers should print these to stderr; they aren't a reason
        to block applying changes.
        """
        if not self.account.over_allocation_budget:
            return []
        warnings = []
        for inst in self._managed:
            alloc_chg = result.allocation_changes.get(inst.crn)
            new_alloc = alloc_chg.new if alloc_chg is not None else inst.allocation_seconds
            if new_alloc < self.minimum_allocation_seconds:
                warnings.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is below minimum_allocation_seconds "
                    f"({self.minimum_allocation_seconds}s) to keep fairness >= 1.0 while the account is over "
                    "its allocation budget"
                )
            elif new_alloc > inst.consumed_seconds:
                warnings.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is above 28-day usage "
                    f"({inst.consumed_seconds}s), so fairness is below 1.0 while the account is over its "
                    "allocation budget"
                )
        return warnings

    def validate_allocations(self, result: OptimizationResult) -> tuple[bool, list[str]]:
        """Check that `result` satisfies all allocation invariants.

        Checks (in order):
        1. Total projected allocation fits under the effective budget =  account budget − reserve.
        2. Each managed instance's new_allocation >= its 28-day consumed usage. In the
           over-budget regime this inverts: new_allocation <= 28-day consumed usage,
           unless the floor forces it higher (an instance with no usage).
        3. Each managed instance's new_allocation >= minimum_allocation_seconds. In the
           over-budget regime, this relaxes to 28-day usage when usage is lower but
           nonzero (see `Floor`).
        4. Each managed instance's new_allocation <= its effective limit (if set),
           unless the floor forces it higher, since a tightened limit below the floor
           is an unavoidable, non-actionable breach.
        5. No managed instance's new_allocation is 0 (archiving is not allowed).

        Unmanaged instances (those not in self.account.instances) contribute their
        current allocation to the total-cap check via AccountPlan.unmanaged_allocation_seconds.
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
        for inst in self._managed:
            alloc_chg = result.allocation_changes.get(inst.crn)
            new_alloc = alloc_chg.new if alloc_chg is not None else inst.allocation_seconds
            floor = self._floor(inst)

            # Invariant 2: allocation >= 28-day usage, or <= 28-day usage when over
            # budget. The latter only fires when the breach exceeds the floor, like
            # invariant 4, so an unused instance's minimum isn't reported.
            if not self.account.over_allocation_budget and new_alloc < inst.consumed_seconds:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is below "
                    f"28-day usage ({inst.consumed_seconds}s)"
                )
            if self.account.over_allocation_budget and new_alloc > inst.consumed_seconds and new_alloc > floor.value:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) exceeds 28-day usage "
                    f"({inst.consumed_seconds}s) while the account is over its allocation budget"
                )

            # Invariant 3: allocation >= minimum_allocation_seconds, relaxed to 28-day
            # usage when over budget. A 0 result is left to invariant 5.
            usage_relaxes_minimum = self.account.over_allocation_budget and floor.source == "consumed_seconds"
            if usage_relaxes_minimum and new_alloc < inst.consumed_seconds:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is below 28-day usage "
                    f"({inst.consumed_seconds}s), which replaces minimum ({self.minimum_allocation_seconds}s) "
                    "while the account is over its allocation budget"
                )
            if not usage_relaxes_minimum and new_alloc < self.minimum_allocation_seconds:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) is below "
                    f"minimum ({self.minimum_allocation_seconds}s)"
                )

            # Invariant 4: allocation <= effective limit (limit_changes take precedence).
            # The floor wins: only fire when the breach exceeds it, so a limit
            # tightened below the floor doesn't surface as a separate, unactionable
            # error.
            limit_chg = result.limit_changes.get(inst.crn)
            effective_limit = limit_chg.new if limit_chg is not None else inst.limit_seconds
            if effective_limit is not None and new_alloc > effective_limit and new_alloc > floor.value:
                errors.append(
                    f"Instance {inst.crn}: new_allocation ({new_alloc}s) exceeds effective limit ({effective_limit}s)"
                )

            # Invariant 5: no archiving
            if new_alloc == 0:
                errors.append(f"Instance {inst.crn}: new_allocation is 0 (archiving not allowed)")

        return len(errors) == 0, errors
