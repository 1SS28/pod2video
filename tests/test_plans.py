import pytest

from core.plans import PLANS, detect_plan, monthly_cost, packs_cost, rank_plans


def sub(amount, interval="month", discount=None):
    return {"price": {"amount": amount, "currency": "usd"}, "billing_interval": interval, "discount": discount}


def test_detect_plan_from_account():
    assert detect_plan({"tier": "creator", "subscription": sub(1900)}).credits_per_usd == pytest.approx(12_000 / 19)
    annual = detect_plan({"tier": "pro", "subscription": sub(30000, "year")})
    assert annual.key == "pro-annual" and annual.credits_per_usd == pytest.approx(1000)
    discounted = detect_plan({"tier": "creator", "subscription": sub(1900, discount={"percent_off": 20, "amount_off": None})})
    assert discounted.credits_per_usd == pytest.approx(12_000 / 15.2)
    assert detect_plan({"tier": "business", "subscription": None}).key == "usage-business"
    assert detect_plan({"tier": "free", "subscription": None}).key == "pack"


def test_packs_cost_picks_the_cheapest_combination():
    assert packs_cost(0) == 0
    assert packs_cost(4_000) == 10
    assert packs_cost(10_000) == 27  # one 12k pack beats three 4k packs ($30)
    assert packs_cost(64_000 * 3 + 1) == 3 * 120 + 10


def test_monthly_cost_tops_up_subscriptions_with_packs():
    creator = PLANS["creator-monthly"]
    assert monthly_cost(creator, 10_000) == 19
    assert monthly_cost(creator, 16_000) == 19 + 10
    assert monthly_cost(PLANS["usage-creator"], 150_000) == pytest.approx(174.0)  # the docs' own example


def test_weekly_show_fits_creator():
    best, cost = rank_plans(10_712, include_usage=False)[0]
    assert best.key == "creator-annual" and cost == 12
