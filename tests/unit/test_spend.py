from decimal import Decimal

import pytest

from anycompute.contract import ProviderCaps
from tests.conformance.runner import Reservation, SpendLimitError, load_prices, reserve_launch

pytestmark = pytest.mark.timeout(30)


@pytest.mark.parametrize(
    ("provider", "creates", "sandboxes", "usd"),
    [
        ("modal", 7, 28, "0.5027988"),
        ("e2b", 15, 15, "0.1656"),
        ("daytona", 15, 15, "0.066924"),
        ("boat", 15, 15, "0.036"),
        ("docker", 15, 15, "0"),
    ],
)
def test_maximum_sized_run_at_recorded_prices(provider, creates, sandboxes, usd):
    result = reserve_launch(load_prices(), provider, Reservation(), [120] * creates)
    assert (result.sandboxes, result.usd) == (sandboxes, Decimal(usd))


def synthetic_prices(provider, **overrides):
    values = dict(
        compute_usd_per_hour=Decimal("0.36"),
        start_fee_usd=Decimal("0.02"),
        stopped_storage_usd_per_week=Decimal("0.03"),
        granularity_s=60,
        min_charge_s=200,
    ) | overrides
    return {provider: {key: {"value": value} for key, value in values.items()}}


@pytest.mark.parametrize(
    ("provider", "sandboxes", "usd"),
    [("modal", 8, "0.336"), ("boat", 2, "0.144"), ("daytona", 2, "0.144"), ("e2b", 2, "0.084")],
)
def test_billing_applies_per_create_including_lifecycle_child(provider, sandboxes, usd):
    result = reserve_launch(synthetic_prices(provider), provider, Reservation(), [1, 61])
    # With grace, 121 s rounds to 180 s (200 s minimum); 181 s rounds to 240 s.
    assert (result.sandboxes, result.usd) == (sandboxes, Decimal(usd))


def test_budget_allows_exactly_two_dollars_and_refuses_the_next_launch():
    prices = synthetic_prices("e2b", compute_usd_per_hour=18, start_fee_usd=0)
    first = reserve_launch(prices, "e2b", Reservation(), [0])
    exactly_two = reserve_launch(prices, "e2b", first, [0])
    assert exactly_two.usd == Decimal("2")
    with pytest.raises(SpendLimitError, match=r"e2b.*\$3.*\$2"):
        reserve_launch(prices, "e2b", exactly_two, [0])
    assert exactly_two.sandboxes == 2


def test_division_does_not_change_the_budget_boundary():
    prices = synthetic_prices("e2b", compute_usd_per_hour=12, start_fee_usd=0)
    result = Reservation()
    for _ in range(3):
        result = reserve_launch(prices, "e2b", result, [0])
    assert result.usd == Decimal("2")


@pytest.mark.parametrize(("provider", "creates", "allocations", "caps"), [
    ("docker", 15, 15, ProviderCaps()),
    ("modal", 7, 28, ProviderCaps(create_retry="partial")),
    ("daytona", 15, 15, ProviderCaps(create_retry="partial", lifetime="partial")),
])
def test_project_allocation_bound_caps_logical_creates(provider, creates, allocations, caps):
    prices = load_prices()
    state = reserve_launch(prices, provider, Reservation(), [1] * (creates - 1), caps=caps)
    assert reserve_launch(prices, provider, state, [1], caps=caps).sandboxes == allocations
    with pytest.raises(SpendLimitError, match=rf"{provider}.*{creates}"):
        reserve_launch(prices, provider, state, [1, 1], caps=caps)


@pytest.mark.parametrize("ttl", [-1, 121, 1.5, 1.0, True, "60", None])
def test_rejects_invalid_effective_ttl(ttl):
    with pytest.raises(ValueError, match="effective TTL"):
        reserve_launch(load_prices(), "e2b", Reservation(), [ttl])


@pytest.mark.parametrize("sandboxes", [-1, False, 1.0, "1"])
def test_rejects_nonintegral_or_negative_prior_sandbox_count(sandboxes):
    with pytest.raises(ValueError, match="reserved sandbox count"):
        reserve_launch(load_prices(), "e2b", Reservation(sandboxes=sandboxes), [1])


@pytest.mark.parametrize("usd_seconds", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), True, 0.5])
def test_rejects_invalid_prior_spend(usd_seconds):
    with pytest.raises(ValueError, match="reserved cost"):
        reserve_launch(load_prices(), "e2b", Reservation(usd_seconds=usd_seconds), [1])


@pytest.mark.parametrize(
    ("field", "value"),
    [("granularity_s", 0), ("granularity_s", True), ("min_charge_s", -1),
     ("min_charge_s", 0.5), ("compute_usd_per_hour", Decimal("sNaN")),
     ("start_fee_usd", -1), ("stopped_storage_usd_per_week", "0")],
)
def test_rejects_invalid_price_terms(field, value):
    with pytest.raises(ValueError, match=field):
        reserve_launch(synthetic_prices("boat", **{field: value}), "boat", Reservation(), [1])


def test_refuses_missing_price_table():
    with pytest.raises(ValueError, match="e2b"):
        reserve_launch({}, "e2b", Reservation(), [1])
