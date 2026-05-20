# qauvern — IBM Quantum Load Balancer

## Terminology

* QAU - Quantum Allocation Unit. 1 QAU represents 1600 minutes allocated to use on the IBM Quantum fleet during our 28 day rolling window
* Rolling window - a backward looking 28 days of usage. The window rolls forward continuously. As clients use minutes in the quantum system, their available minutes for consumption decrease.
* Account - all quantum access is based on an IBM Cloud account. IBM admins set the account level allocation based on the contracted QAUs that the client has provided
* [Project/Instance](https://quantum.cloud.ibm.com/docs/en/guides/instances) - **Projects and instances are conceptually the same thing.** Each project corresponds to exactly one service instance (identified by a unique CRN). A client will create one or more quantum service instances and set an allocation in seconds to configure the time that the instance should consume during the 28 day rolling window.
* [Limits](https://quantum.cloud.ibm.com/docs/en/guides/allocation-limits) - in addition to allocations on instances, admins can optionally set limits on the instance, which provide a hard cap on the amount of time that the instance can consume.
* [Allocations](https://quantum.cloud.ibm.com/docs/en/guides/allocation-limits) - the amount of time that an instance is targetted to consume during the 28 day rolling window. When setting allocations on instances, sum of all the allocations must be less than or equal to the account level allocation.
* [Fair share scheduler](https://quantum.cloud.ibm.com/docs/en/guides/fair-share-scheduler) - the way the IBM Quantum system determines which jobs should go next. This is optimized to meet contractual constraints. When we are ready to run a new job, we pick the instance with the lowest fairness value.
* Fairness - a measure of how much of the allocation has been used. It is the ratio of consumed time in the 28 day rolling window, over the allocation in the instance. This ensures that instances that have used the least percentage of their allocation get highest priority. Jobs can still run if fairness is above 1, as long as the instance has not hit a limit.

## Problem statement

Access to our Quantum Systems is given with an allocation to clients over a 28 day rolling window. For clients with large allocation, they split this up into a bunch of instances that different people have access to. Quantum work tends to be bursty, so lots of project work happens one month, then work dies down during analysis and paper writing for a couple of months after.

For clients that have a large number of instances, the easy thing to do is allocate equally across all of those instances. However, it is likely that less than half of those instances get used in any given month. So, this strategy is wasteful. We would much rather allocate more to the instances that are used more often, up to some project limit that the administrator sets for that. This would allow us to run more jobs, and also to run them faster.

## Solution

### Declarative Input File

The admin of an account should have a way to declare intent of projects over the lifespan of a year. That file should specify the time period that it is attempting to balance across, as well as list of projects. **Each project has exactly one CRN (service instance)** and a total allocation for the duration of the project. This should be in yaml.

### Load balancing

Given the input file, qauvern should be able to compute the optimal allocation of instances and then update them via the API.

### Querying the API

qauvern should be able to query the API to get the current allocation of instances, and the historical usage of instances. It should be able to update the allocation of instances to the API. For instances that are not being actively used, it can set minimal allocation to them, and give more allocation to instances being heavily used, so those instances can progress further.

Each project should have a maximum total consumption. The limit on the instance should be set as the maximum total consumption minus any consumption that has already been used.

### API calls

Before any API calls are made, an IBM Cloud IAM token must be obtained. This can be done by making a call to the IAM token service: https://cloud.ibm.com/apidocs/iam-identity-token-api#token-service

The full OpenAPI spec for the quantum API is available here: https://quantum.cloud.ibm.com/api/openapi.json

#### IBM Quantum API Endpoints

**IMPORTANT**: All IBM Quantum API endpoints use the `Service-CRN` header to identify the instance, NOT the CRN in the URL path.

**Regional Endpoints**: The API uses region-specific endpoints based on the instance's region (extracted from the CRN):
- `us-east` (default): `https://quantum.cloud.ibm.com/api`
- Other regions (e.g., `eu-de`): `https://{region}.quantum.cloud.ibm.com/api`

The client automatically extracts the region from the CRN and uses the appropriate regional endpoint.

1. **Get Instance Details** - `GET /v1/instance`
   - Full URL: `https://quantum.cloud.ibm.com/api/v1/instance`
   - Headers: `Service-CRN: <instance_crn>`, `Authorization: Bearer <iam_token>`
   - Returns: Instance details, including `usage_allocation_seconds`, `backends`, `plan_id`
   - Response schema: `Instance`
   - **Note**: This endpoint does NOT return `instance_limit_seconds` or consumed usage data

2. **Get Instance Configuration** - `GET /v1/instances/configuration`
   - Full URL: `https://quantum.cloud.ibm.com/api/v1/instances/configuration`
   - Headers: `Service-CRN: <instance_crn>`, `Authorization: Bearer <iam_token>`
   - Returns: Instance configuration, including limits
   - Response schema: `InstanceConfiguration`

3. **Update Instance Configuration** - `PUT /v1/instances/configuration`
   - Full URL: `https://quantum.cloud.ibm.com/api/v1/instances/configuration`
   - Headers: `Service-CRN: <instance_crn>`, `Authorization: Bearer <iam_token>`
   - Body: `{"instance_limit": <seconds_or_null>}`
   - Use `null` to remove the instance limit
   - Response: 204 No Content on success
   - Request schema: `InstanceConfigurationUpdate`

4. **Get Instance Usage (Rolling Window)** - `GET /v1/instances/usage`
   - Full URL: `https://quantum.cloud.ibm.com/api/v1/instances/usage`
   - Headers: `Service-CRN: <instance_crn>`, `Authorization: Bearer <iam_token>`
   - Returns: Usage data, including `usage_consumed_seconds`, `usage_allocation_seconds`, `usage_period`
   - Response schema: `GetUsageResponse`
   - **Note**: This endpoint only returns the rolling 28-day window usage. It does NOT support custom date ranges.

5. **Get Analytics Usage (Custom Date Range)** - `GET /v1/analytics/usage`
   - Full URL: `https://quantum.cloud.ibm.com/api/v1/analytics/usage`
   - Headers: `Authorization: Bearer <iam_token>`
   - Query Parameters:
     - `instance`: Array of instance CRNs (e.g., ["crn:v1:..."])
     - `interval_start`: ISO 8601 datetime string (e.g., "2026-01-01T00:00:00Z")
     - `interval_end`: ISO 8601 datetime string (e.g., "2026-04-01T23:59:59Z")
   - Returns: Detailed usage analytics for the specified date range
   - Response schema: `GetAnalyticsUsageResponse`
   - Response includes: `usage` (total consumed time in **milliseconds**), `jobs`, `sessions`, etc.
   - **Note**: This endpoint supports custom date ranges for detailed usage analysis
   - **Important**: Usage is returned in milliseconds, must be converted to seconds

6. **Get Account Information** - `GET /v1/accounts/{account_id}`
   - Full URL: `https://quantum.cloud.ibm.com/api/v1/accounts/{account_id}`
   - Headers: `Authorization: Bearer <iam_token>`
   - Returns: Account details, including total allocation
   - Response schema: `Account`
   - **Note**: This endpoint requires admin privileges on the account

#### IBM Cloud Resource Controller API

The Resource Controller API is used for listing instances and updating allocations.

**List Resource Instances** - `GET /v2/resource_instances`
- Base URL: `https://resource-controller.cloud.ibm.com`
- Full URL: `https://resource-controller.cloud.ibm.com/v2/resource_instances`
- Headers: `Authorization: Bearer <iam_token>`
- Query Parameters: `resource_id=b6049020-80f4-11eb-a0f7-e35ec9b4054f` (quantum-computing service ID)
- Returns: List of all quantum computing service instances
- Response includes: `id` (CRN), `name`, `account_id`, `parameters.usage_allocation_seconds`, `extensions.instance_limit_seconds`
- **Note**: The `sub_type` parameter doesn't work for quantum-computing, so we filter by `resource_id` instead
- Documentation: https://cloud.ibm.com/apidocs/resource-controller/resource-controller#list-resource-instances

**Update Instance Allocation** - `PATCH /v2/resource_instances/{crn}`
- Base URL: `https://resource-controller.cloud.ibm.com`
- Full URL: `https://resource-controller.cloud.ibm.com/v2/resource_instances/{url_encoded_crn}`
- Headers: `Authorization: Bearer <iam_token>`
- Body: `{"parameters": {"usage_allocation_seconds": <seconds>}}`
- **NOTE**: For this endpoint only, the CRN must be URL-encoded in the path
- Documentation: https://cloud.ibm.com/apidocs/resource-controller/resource-controller#update-resource-instance

#### Authentication Details

All IBM Quantum API calls require:
1. An IBM Cloud IAM token obtained from the IAM token service
2. The `Service-CRN` header set to the instance CRN (except for account-level calls)
3. The `Authorization: Bearer <token>` header

The OpenAPI spec defines three security schemes:
- `IBMCloudAPIKey`: API key authentication
- `ServiceCRN`: Service CRN header (required for instance-specific operations)
- `IBMCloudAuth`: IAM token authentication


### Implementation Language

The tool should be written in Python.

It should have a CLI with these subcommands:

- `show` - show a summary of account allocation, and instance allocation and limits.
- `instances` - show the summed usage of all instances in the account, but don't try to do account allocation because that requires admin privileges.
- `optimize` - optimize the allocation of instances to maximize the usage of the account allocation.
- `analyze` - analyze the usage of instances and account allocation, show suggestions for optimization, but do not make any changes.
- `configure` - given a specific account id, list all the instances from the account and create a base yaml file for the tool to use.
- `create` — create a new IBM Quantum service instance, specifying name, target region, resource group, plan, initial allocation, limit, and tags.

ASCII color should be used on the CLI output.

### Staging environment

The CLI accepts a `--staging` flag (or `IBMCLOUD_STAGING` environment variable). When set, all API calls switch to the IBM Cloud staging environment:
- IAM token endpoint: `https://iam.test.cloud.ibm.com/identity/token`
- Quantum API: `https://quantum.test.cloud.ibm.com/api`
- Resource Controller: `https://resource-controller.test.cloud.ibm.com`

### Testing Framework

In order to test the tool, we need a testing framework that can respond to the API calls that the tool would make. The testing framework should make it easy to setup a scenario with specific account allocation, and specific instance allocation and limits, and historical usage over some time period.

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

### `project_limit_seconds` (project level)

Sets a base usage limit on the instance. Must be `>= target_usage_seconds`. When set, the optimizer applies this limit on every run via `LimitResolver`. If absent, the optimizer falls back to the existing target-based limit logic.

### `net_grants` (project level)

A list of additive time-budget boosts above `project_limit_seconds`. Each grant has `start_date`, `net_grant_seconds`, and an optional `end_date` (defaults to `start_date + 28 days`). A grant is active when `start_date <= today < end_date`; once expired, the effective limit reverts to `project_limit_seconds`. Multiple active grants stack.

Note that the rolloff math used to compute each grant's contribution (see LimitResolver) is anchored on the 28-day rolling window from `start_date` regardless of `end_date` — for grants longer than 28 days, the contribution plateaus once all pre-grant usage has exited the window.

### LimitResolver

`src/qauvern/limit_resolver.py` resolves the effective limit per instance before the optimizer builds recommendations. Resolution order (first match wins):

1. Exhausted instance → `1`
2. Active net grants → `project_limit_seconds + sum(grant contributions)`
3. Base limit only → `project_limit_seconds`
4. No limit configured → `None`

**Grant contribution** = `max(0, net_grant_seconds - rolloff)`, where rolloff is the sum of per-day usage that was inside the rolling window at grant start but has since exited. Multiple active grants are summed.
