# qauvern — IBM Quantum Load Balancer

## Terminology

* Rolling window - a backward looking 28 days of usage. The window rolls forward continuously. As clients use minutes in the quantum system, their available minutes for consumption decrease.
* Account - all quantum access is based on an IBM Cloud account. IBM admins set the account level allocation based on the contracted time the client has purchased.
* [Instance](https://quantum.cloud.ibm.com/docs/en/guides/instances.md) - A client will create one or more quantum service instances. Users get access to certain instances and they target a particular instance when submitting workloads. Each instance gets its own allocation and limits.
* [Limits](https://quantum.cloud.ibm.com/docs/en/guides/allocation-limits.md) - an optional hard cap on the amount of time that the instance can consume.
* [Allocations](https://quantum.cloud.ibm.com/docs/en/guides/allocation-limits.md) - the amount of time that an instance is targetted to consume during the 28 day rolling window. An instance can exceed its allocation, but it will drop in priority relative to other instances based on the "fairness" score. 
* Fairness - a measure of how much of the allocation has been used. It is the ratio of consumed time in the 28 day rolling window, over the allocation in the instance. This ensures that instances that have used the least percentage of their allocation get highest priority. Jobs can still run if fairness is above 1, as long as the instance has not hit a limit.
* [Fair share scheduler](https://quantum.cloud.ibm.com/docs/en/guides/fair-share-scheduler.md) - the way the IBM Quantum system determines which jobs should go next. When the IBM Quantum Platform is ready to run a new job, it picks the instance with the lowest fairness value.

## Problem statement

Access to our Quantum Systems is given with an allocation to clients over a 28 day rolling window. For clients with large allocation, they split this up into a bunch of instances that different people have access to. Quantum work tends to be bursty, so lots of project work happens one month, then work dies down during analysis and paper writing for a couple of months after.

For clients that have a large number of instances, the easy thing to do is allocate equally across all of those instances. However, it is likely that less than half of those instances get used in any given month. So, this strategy is wasteful. We would much rather allocate more to the instances that are used more often, up to some project limit that the administrator sets for that. This would allow us to run more jobs, and also to run them faster.

Likewise, project administrators want a way to temporarily increase the hard limit for a project during these bursty periods, then for the increase to go away after it expires.

## Scope: configured vs. unconfigured instances

`qauvern` only operates on the instances listed in the config file. Any other instance on the same account+plan is **unconfigured** and is left exactly as-is — its allocation and limit are never touched. The optimizer still subtracts unconfigured allocation from the account budget when deciding how much to redistribute, so it never overcommits the cap.

The config file is generated once with `configure` and is expected to be checked into version control. The `update` command helps catch drift between the file and the live API (instances added, archived, renamed; net grants expiring; missing `limit_seconds`).

## Core load balancing algorithm

For each managed instance:

1. **Resolve effective limit** via `LimitResolver` (see below). This is the upper bound on this instance's allocation for the upcoming run.
2. **Compute an activity score** by summing weighted contributions from the 28d, 14d, 7d, 3d, and 24h usage buckets:
   - Each bucket contributes `(bucket_usage / bucket_days) * bias^exponent`, where the exponent reflects recency `(24h=5.0, 3d=4.0, 7d=3.0, 14d=2.0, 28d=1.0)`.
   - With `bias=2.0`, 24h usage carries 16× the weight of 28d usage.
   - Instances with no usage across all buckets get a score of 0 and are classified inactive.

Then, account-wide:

3. **Pin every managed instance to its floor.** The floor is `max(minimum_allocation_seconds, consumed_seconds_28d)` — we never reduce an instance below what it has already consumed in the rolling window, and we never go below the user-configured minimum. Inactive instances stay at the floor.
4. **Build the redistribution pool** from unallocated headroom plus everything managed instances hold above their floor. If `allocation_reserve_percent` is set, withhold a fixed fraction of the total account budget (`allocation_budget_seconds × reserve_percent / 100`) from the pool so total allocation stays under `budget × (1 − reserve_percent / 100)`.
5. **Use the water-fill algorithm to distribute the pool across active instances** proportional to activity score:
   - Each round, every active instance is offered `(score / total_score) * remaining_pool`.
   - If an instance would exceed its effective limit, it takes only enough to reach the limit and drops out of the candidate set; the surplus from its proportional share flows to the remaining candidates in the next round.
   - When every active instance is capped by limits, leftover capacity stays unallocated rather than being forced onto any instance.
6. **Apply changes to IBM Cloud** wherever the projected allocation or limit differs from the live state.

### Invariants

The optimizer validates the resulting plan against these invariants and refuses to apply changes that fail validation:

1. Total projected allocation does not exceed the account budget.
2. Total projected allocation respects the `allocation_reserve_percent` buffer — i.e. it does not exceed `budget × (1 − reserve_percent / 100)`. When unavoidable floors (28-day usage / `minimum_allocation_seconds`) plus unconfigured-instance allocation already exceed that cap, the reserve cannot be honored and this invariant fails.
3. Each managed instance's new allocation is `>= consumed_seconds_28d`.
4. Each managed instance's new allocation is `>= minimum_allocation_seconds`.
5. Each managed instance's new allocation is `<= effective limit`, unless invariants 3 or 4 force it higher (a limit tightened below the floor is an unavoidable, non-actionable breach and is not flagged here).
6. No managed instance's new allocation is 0 (archiving is not allowed).

## Limit-centric configuration

Some clients manage consumption primarily via limits, rather than saturating allocations. Three optional config fields support this workflow.

### `allocation_reserve_percent` (account level)

Holds back a percentage of the **total account budget** (`allocation_budget_seconds`) as a hard buffer: total allocation across all instances will never exceed `budget × (1 − reserve_percent / 100)`. The reserved amount stays unallocated and is not distributed to any instance. Because the reserve is anchored to the budget rather than the movable pool, the cap is predictable regardless of current allocations or usage. Defaults to 0 (no reserve). Must be in `[0, 100)`. Configured at the top level of the YAML.

### `limit_seconds` (instance level)

Sets a base usage limit on the instance. When set, the optimizer applies this limit on every run via `LimitResolver`. If absent, the optimizer leaves the live IQP limit alone.

### `net_grants` (instance level)

A list of additive time-budget boosts above `limit_seconds`. Each grant has `start_date`, `net_grant_seconds`, and an optional `end_date` (defaults to `start_date + 28 days`). A grant is active when `start_date <= today < end_date`; once expired, the effective limit reverts to `limit_seconds`. Multiple active grants stack. Setting `net_grants` requires also setting `limit_seconds`.

Note that the rolloff math used to compute the grants' contribution (see `LimitResolver`) is anchored on the 28-day rolling window from the earliest active grant's `start_date`, regardless of `end_date` — for grants longer than 28 days, the contribution plateaus once all pre-grant usage has exited the window.

### LimitResolver

[`src/qauvern/limit_resolver.py`](src/qauvern/limit_resolver.py) resolves the effective config-side limit per instance before the optimizer builds recommendations. Resolution order (first match wins):

1. No `limit_seconds` and no `net_grants` configured → `None` (no override; optimizer leaves the live limit alone).
2. `limit_seconds` set, no active net grants → `limit_seconds`.
3. `limit_seconds` set, at least one active net grant → `limit_seconds + grant_total + max(0, rolloff - limit_seconds)`.

Where:
- `grant_total` is the sum of `net_grant_seconds` across grants active today.
- `boost_start` is the earliest `start_date` among active grants.
- `rolloff` is the sum of `daily_usage` on days strictly before `boost_start` that are still inside the current 28-day rolling window `[today - 28, today)`.

The `max(0, rolloff - limit_seconds)` term lets pre-grant usage that exceeded the base limit decay out of the effective limit as those days exit the rolling window. Pre-grant days that stayed at or below the base limit contribute nothing. When multiple grants are active, rolloff is anchored at the earliest active grant's start, not computed per-grant.
