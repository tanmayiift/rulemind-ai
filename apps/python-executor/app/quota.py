"""Per-plan usage quotas over the metered decision volume (Track B4).

Resolution order mirrors app/rate_limit.py deliberately -- an operator who has learned one
configuration idiom should not have to learn a second:

  1. per-tenant   Tenant.config["quota"]["decisions_per_month"]  (an override for one customer)
  2. per-plan     QUOTA_DECISIONS_<PLAN>   e.g. QUOTA_DECISIONS_ENTERPRISE=50000000
  3. global       QUOTA_DECISIONS_DEFAULT
  4. built-in     DEFAULT_PLAN_DECISIONS

ENFORCEMENT IS OPT-IN (QUOTA_ENFORCE=1), METERING IS ALWAYS ON. That split is the important
part. Metering is additive -- it changes no response. Enforcement rejects traffic, so turning
it on by default would mean an existing deployment upgrades and starts refusing decisions the
moment it crosses a limit nobody chose. An operator must be able to watch real numbers
accumulate, set limits against them, and only then switch enforcement on.

A quota is a BILLING boundary, not a burst control -- that is rate_limit.py's job. The two are
kept separate because they answer different questions ("have you paid for this volume?" versus
"are you sending it too fast?") and conflating them produces the classic bug where a customer
who bought more volume is throttled anyway.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, Optional

# Monthly decision allowances by plan, used only when nothing is configured. 0 means unlimited.
DEFAULT_PLAN_DECISIONS: Dict[str, int] = {
    "enterprise": 0,        # unlimited -- enterprise volume is negotiated in the contract
    "growth": 5_000_000,
    "standard": 500_000,
    "free": 10_000,
}
DEFAULT_DECISIONS = 500_000

METRIC_DECISIONS = "decisions"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_int(name: str) -> Optional[int]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def enforcement_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get("QUOTA_ENFORCE", "0")).strip().lower() in _TRUTHY


def monthly_limit(tenant: Optional[Dict[str, Any]]) -> int:
    """Decisions allowed per calendar month for this workspace. 0 means unlimited."""
    tenant = tenant or {}
    config = tenant.get("config") or {}
    override = (config.get("quota") or {}).get("decisions_per_month")
    if override is not None:
        try:
            value = int(override)
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass  # a malformed override must not take the workspace offline -- fall through

    plan = str(tenant.get("plan") or "standard").strip().lower()
    per_plan = _env_int("QUOTA_DECISIONS_{0}".format(plan.upper().replace("-", "_")))
    if per_plan is not None:
        return per_plan
    default = _env_int("QUOTA_DECISIONS_DEFAULT")
    if default is not None:
        return default
    return DEFAULT_PLAN_DECISIONS.get(plan, DEFAULT_DECISIONS)


def period_bounds(today: Optional[date] = None) -> tuple:
    """The calendar month containing `today`, as (first_day, last_day) inclusive.

    Calendar month, not a rolling 30 days: an invoice line has to correspond to a period a
    human can name, and "1-31 August" is checkable against a bill in a way "the last 30 days"
    is not.
    """
    day = today or date.today()
    start = day.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end  # end is EXCLUSIVE -- callers filter `day < end`


def evaluate(tenant: Optional[Dict[str, Any]], used: int, today: Optional[date] = None) -> Dict[str, Any]:
    """Report this workspace's quota position. Pure -- no I/O, so it is trivially testable."""
    limit = monthly_limit(tenant)
    start, end = period_bounds(today)
    unlimited = limit == 0
    remaining = None if unlimited else max(0, limit - used)
    return {
        "metric": METRIC_DECISIONS,
        "plan": str((tenant or {}).get("plan") or "standard"),
        "limit": limit,
        "unlimited": unlimited,
        "used": used,
        "remaining": remaining,
        "exceeded": (not unlimited) and used >= limit,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),  # exclusive
        "enforced": enforcement_enabled(),
    }
