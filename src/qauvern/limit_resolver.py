# (C) Copyright IBM 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Resolves the effective limit for an instance given net grants and rolloff."""

from datetime import date, timedelta

from .models import Instance, InstanceConfig


class LimitResolver:
    """Resolves the effective instance limit from instance config and runtime state.

    Net grants are additive boosts above ``InstanceConfig.limit_seconds`` valid for
    28 days from start_date. As pre-grant usage days scroll out of the rolling
    28-day window, the effective limit decreases by that day's usage (rolloff).
    This ensures headroom decays as old usage leaves the window.
    """

    def resolve(self, instance_config: InstanceConfig, instance: Instance, today: date) -> int | None:
        """Return the effective limit in seconds for the given instance today.

        Resolution order:
        1. Exhausted instance -> 1
        2. Active net grants with rolloff -> base + sum(each grant's contribution)
        3. Base limit only -> instance_config.limit_seconds
        4. No limit -> None

        Grant contribution = max(0, net_grant_seconds - rolloff)
        where rolloff = sum(usage on days that were in the 28-day window at grant
        start but have since exited, and are strictly before grant start).
        """
        if instance.exhausted(instance_config.target_usage_seconds):
            return 1

        daily_usage: dict[date, int] = instance.usage.daily_usage
        base_limit = instance_config.limit_seconds

        if not instance_config.net_grants:
            return base_limit

        total_grant_contribution = 0
        any_active = False

        for grant in instance_config.net_grants:
            grant_start = grant.start_date.date()
            grant_end = grant.end_date.date()

            if not (grant_start <= today < grant_end):
                continue

            any_active = True

            # Pre-grant exit set: days that were in the 28-day window at grant_start
            # and have since exited the current 28-day window.
            # Was in window at grant_start: d >= grant_start - 27
            # Has since exited: d < today - 28
            # Is pre-grant: d < grant_start
            window_floor_at_grant = grant_start - timedelta(days=27)
            current_window_floor = today - timedelta(days=28)

            rolloff = sum(
                seconds
                for day, seconds in daily_usage.items()
                if day < grant_start and day >= window_floor_at_grant and day < current_window_floor
            )

            contribution = max(0, grant.net_grant_seconds - rolloff)
            total_grant_contribution += contribution

        if not any_active:
            return base_limit

        effective_base = base_limit if base_limit is not None else 0
        return effective_base + total_grant_contribution
