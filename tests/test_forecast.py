from datetime import date

import pytest

from ozon_analytics.forecast import forecast


@pytest.mark.parametrize(
    "stock,rate", [(None, 3), (2, None), (2, float("nan")), (2, -1)]
)
def test_missing_or_invalid_demand_does_not_predict(stock, rate):
    assert forecast(stock, rate, date(2026, 10, 7))["level"] == "unknown"


@pytest.mark.parametrize("stock", [0, 20])
def test_zero_demand_is_not_invented_stockout(stock):
    result = forecast(stock, 0, date(2026, 10, 7))
    assert result["level"] == "no_demand"
    assert result["stockout_date"] is None


def test_empty_stock_and_replenishment():
    result = forecast(0, 5, date(2026, 10, 7), lead_days=14, safety_days=7)
    assert result["stockout_date"] == "2026-10-07"
    assert result["recommended_units"] == 105
    assert result["level"] == "critical"


def test_fractional_days_round_up_for_date():
    result = forecast(31, 2, date(2026, 10, 7))
    assert result["days_left"] == 15.5
    assert result["stockout_date"] == "2026-10-23"
    assert result["reorder_date"] == "2026-10-02"
