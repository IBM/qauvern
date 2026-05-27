# qauvern — IBM Quantum Load Balancer

## Terminology

* QAU - Quantum Allocation Unit. 1 QAU represents 1600 minutes allocated to use on the IBM Quantum fleet during our 28 day rolling window
* Rolling window - a backward looking 28 days of usage. The window rolls forward continuously. As clients use minutes in the quantum system, their available minutes for consumption decrease.
* Account - all quantum access is based on an IBM Cloud account. IBM admins set the account level allocation based on the contracted QAUs that the client has provided
* [Instance](https://quantum.cloud.ibm.com/docs/en/guides/instances) - A client will create one or more quantum service instances and set an allocation in seconds to configure the time that the instance should consume during the 28 day rolling window.
* [Limits](https://quantum.cloud.ibm.com/docs/en/guides/allocation-limits) - in addition to allocations on instances, admins can optionally set limits on the instance, which provide a hard cap on the amount of time that the instance can consume.
* [Allocations](https://quantum.cloud.ibm.com/docs/en/guides/allocation-limits) - the amount of time that an instance is targetted to consume during the 28 day rolling window. When setting allocations on instances, sum of all the allocations must be less than or equal to the account level allocation.
* [Fair share scheduler](https://quantum.cloud.ibm.com/docs/en/guides/fair-share-scheduler) - the way the IBM Quantum system determines which jobs should go next. This is optimized to meet contractual constraints. When we are ready to run a new job, we pick the instance with the lowest fairness value.
* Fairness - a measure of how much of the allocation has been used. It is the ratio of consumed time in the 28 day rolling window, over the allocation in the instance. This ensures that instances that have used the least percentage of their allocation get highest priority. Jobs can still run if fairness is above 1, as long as the instance has not hit a limit.

## Problem statement

Access to our Quantum Systems is given with an allocation to clients over a 28 day rolling window. For clients with large allocation, they split this up into a bunch of instances that different people have access to. Quantum work tends to be bursty, so lots of project work happens one month, then work dies down during analysis and paper writing for a couple of months after.

For clients that have a large number of instances, the easy thing to do is allocate equally across all of those instances. However, it is likely that less than half of those instances get used in any given month. So, this strategy is wasteful. We would much rather allocate more to the instances that are used more often, up to some project limit that the administrator sets for that. This would allow us to run more jobs, and also to run them faster.

## Core Load Balancing Algorithm

The following is a high level description of the load balancing algorithm:

1. Get detailed usage for all instances.
2. Enforce the constraint that allocation for any non-exhausted instance is never reduced below its own usage in the 28d rolling window.
3. Once an instance has used all the time that was allotted for it in the accounting period, the instance should have its usage set to 0 and its limit set to 1.
4. For each instance, compute a single composite activity score by summing weighted contributions from the 28d, 14d, 7d, 3d, and 24h usage buckets:
   - Each bucket contributes `(bucket_usage / bucket_days) * bias^exponent`, where exponent reflects recency `(24h=5.0, 3d=4.0, 7d=3.0, 14d=2.0, 28d=1.0)`
   - The instance's activity score is the sum of these per-bucket contributions
   - With `bias=2.0`, this creates strong differentiation: 24h usage has 16x more weight than 28d usage
5. Based on this score, any instances with a score of 0 should have their allocation set to a minimal number.
6. Temporarily reduce all active instances to their 28-day usage floor to free up maximum allocation.
7. Redistribute all available allocation to active instances proportionally based on activity scores.

## Limit-Centric Configuration

Some clients manage consumption primarily via limits, rather than saturating allocations. Three optional config fields support this workflow.

### `allocation_reserve_percent` (account level)

Holds back a percentage of account allocation from rebalancing. The reserved amount stays unallocated and is not distributed to any instance. Defaults to 0 (existing behavior). Must be in `[0, 100)`. Configured in the YAML at the top level; injected into `Account.allocation_reserve_percent` before the optimizer runs.

### `limit_seconds` (instance level)

Sets a base usage limit on the instance. Must be `>= target_usage_seconds`. When set, the optimizer applies this limit on every run via `LimitResolver`. If absent, the optimizer falls back to the existing target-based limit logic.

### `net_grants` (instance level)

A list of additive time-budget boosts above `limit_seconds`. Each grant has `start_date`, `net_grant_seconds`, and an optional `end_date` (defaults to `start_date + 28 days`). A grant is active when `start_date <= today < end_date`; once expired, the effective limit reverts to `limit_seconds`. Multiple active grants stack.

Note that the rolloff math used to compute each grant's contribution (see LimitResolver) is anchored on the 28-day rolling window from `start_date` regardless of `end_date` — for grants longer than 28 days, the contribution plateaus once all pre-grant usage has exited the window.

### LimitResolver

`src/qauvern/limit_resolver.py` resolves the effective limit per instance before the optimizer builds recommendations. Resolution order (first match wins):

1. Exhausted instance → `1`
2. Active net grants → `limit_seconds + sum(grant contributions)`
3. Base limit only → `limit_seconds`
4. No limit configured → `None`

**Grant contribution** = `max(0, net_grant_seconds - rolloff)`, where rolloff is the sum of per-day usage that was inside the rolling window at grant start but has since exited. Multiple active grants are summed.
