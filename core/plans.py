"""Magic Hour plans: what a credit costs on each, and which plan fits a monthly volume.

Source: docs.magichour.ai/billing (subscription, credit packs, usage-based), checked 2026-10-03.
Prices change; `detect_plan()` prefers the price on the user's own account when it can.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    credits_per_usd: float | None  # None: credits are not bought on this plan (Free)
    monthly_credits: int | None = None  # credits included per month (subscriptions)
    monthly_usd: float | None = None  # price per month (annual plans: per-month equivalent)
    concurrency: int | None = None  # concurrent generations; None = unlimited
    self_serve: bool = True
    note: str = ""


PLANS: dict[str, Plan] = {
    p.key: p
    for p in [
        Plan("free", "Free", None, note="watermarked; for trying the API"),
        Plan("pack", "Credit packs", 400.0, note="no plan: lowest resolution"),
        Plan("creator-monthly", "Creator, monthly", 12_000 / 19, 12_000, 19, 3),
        Plan("creator-annual", "Creator, annual", 12_000 / 12, 12_000, 12, 3, note="$144 up front"),
        Plan("pro-monthly", "Pro, monthly", 25_000 / 39, 25_000, 39, 5),
        Plan("pro-annual", "Pro, annual", 25_000 / 25, 25_000, 25, 5, note="$300 up front"),
        Plan("business-monthly", "Business, monthly", 70_000 / 99, 70_000, 99, None),
        Plan("business-annual", "Business, annual", 70_000 / 66, 70_000, 66, None, note="$792 up front"),
        Plan("usage-creator", "Usage-based (Creator)", 1 / 0.0012, self_serve=False, note="ask Magic Hour to set up"),
        Plan("usage-pro", "Usage-based (Pro)", 1 / 0.00195, self_serve=False, note="ask Magic Hour to set up"),
        Plan("usage-business", "Usage-based (Business)", 1 / 0.0025, self_serve=False, note="ask Magic Hour to set up"),
    ]
}
DEFAULT_PLAN = "creator-monthly"

PACKS = [(4_000, 10.0), (12_000, 27.0), (32_000, 64.0), (64_000, 120.0)]  # (credits, $)
_USAGE_RATES = {"usage-creator": 0.0012, "usage-pro": 0.00195, "usage-business": 0.0025}
# Graduated monthly discounts for usage-based billing: (band upper bound, discount).
_USAGE_BANDS = [(100_000, 0.0), (1_000_000, 0.10), (5_000_000, 0.20), (10_000_000, 0.30), (50_000_000, 0.40), (math.inf, 0.50)]


def custom_plan(credits_per_usd: float) -> Plan:
    return Plan("custom", f"Custom ({credits_per_usd:g} credits/$)", credits_per_usd)


def detect_plan(account: dict[str, Any]) -> Plan | None:
    """Work out the plan (and the real price per credit) from GET /v1/account."""
    tier = account.get("tier")
    sub = account.get("subscription")
    if sub:
        annual = sub.get("billing_interval") == "year"
        plan = PLANS.get(f"{tier}-{'annual' if annual else 'monthly'}")
        price = sub.get("price") or {}
        if not plan or price.get("amount") is None or str(price.get("currency", "usd")).lower() != "usd":
            return plan
        usd = price["amount"] / 100
        discount = sub.get("discount") or {}
        if discount.get("percent_off"):
            usd *= 1 - discount["percent_off"] / 100
        elif discount.get("amount_off"):
            usd -= discount["amount_off"] / 100
        if usd <= 0:
            return plan
        credits = plan.monthly_credits * (12 if annual else 1)
        return replace(plan, credits_per_usd=credits / usd, monthly_usd=usd / (12 if annual else 1), note="price from your account")
    if tier and tier != "free":
        return PLANS.get(f"usage-{tier}")  # a paid tier without a subscription is usage-based billing
    return PLANS["pack"]  # free tier: value its credits at the pack price


def packs_cost(credits: int) -> float:
    """Cheapest combination of credit packs that covers `credits`."""
    if credits <= 0:
        return 0.0
    size, price = PACKS[-1]  # the largest pack is also the cheapest per credit
    full, rest = divmod(credits, size)
    return full * price + _small_packs(rest)


@functools.lru_cache(maxsize=None)
def _small_packs(credits: int) -> float:
    if credits <= 0:
        return 0.0
    return min(price + _small_packs(credits - size) for size, price in PACKS)


def _usage_cost(credits: int, rate: float) -> float:
    total, previous = 0.0, 0
    for upper, discount in _USAGE_BANDS:
        in_band = max(0, min(credits, upper) - previous)
        total += in_band * rate * (1 - discount)
        previous = upper
        if credits <= upper:
            break
    return total


def monthly_cost(plan: Plan, credits_per_month: int) -> float | None:
    """What `credits_per_month` costs per month on `plan` (subscriptions top up with packs)."""
    credits_per_month = max(0, math.ceil(credits_per_month))
    if plan.key == "free":
        return None
    if plan.key == "pack":
        return packs_cost(credits_per_month)
    if plan.key in _USAGE_RATES:
        return _usage_cost(credits_per_month, _USAGE_RATES[plan.key])
    if plan.monthly_credits is None:  # custom
        return credits_per_month / plan.credits_per_usd
    return plan.monthly_usd + packs_cost(max(0, credits_per_month - plan.monthly_credits))


def rank_plans(credits_per_month: int, *, include_usage: bool = True) -> list[tuple[Plan, float]]:
    """Paid plans ordered by monthly cost for this volume (cheapest first)."""
    ranked = []
    for plan in PLANS.values():
        if plan.key == "free" or (not plan.self_serve and not include_usage):
            continue
        cost = monthly_cost(plan, credits_per_month)
        if cost is not None:
            ranked.append((plan, cost))
    return sorted(ranked, key=lambda pc: (pc[1], not pc[0].self_serve))
