from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from ozon_analytics import performance, performance_api
from ozon_analytics.advertising_queries import advertising, totals
from ozon_analytics.api import APIError
from ozon_analytics.performance import (
    catalog,
    enqueue,
    ingest_cpo,
    ingest_daily,
    ingest_sku,
    number,
    reports,
)
from ozon_analytics.performance_api import DeferredRequest, PerformanceAPI, allowed
from ozon_analytics.web import app, create_session

DAY = date(2026, 10, 6)


def campaign_api():
    return SimpleNamespace(
        request=lambda *a, **k: {
            "total": "2",
            "list": [
                {
                    "id": "10",
                    "title": "Test CPC",
                    "state": "CAMPAIGN_STATE_RUNNING",
                    "advObjectType": "SKU",
                    "PaymentType": "CPC",
                    "placement": ["PLACEMENT_SEARCH"],
                    "weeklyBudget": "4000000000",
                },
                {
                    "id": "20",
                    "title": "Test CPO",
                    "state": "CAMPAIGN_STATE_RUNNING",
                    "advObjectType": "SEARCH_PROMO",
                },
            ],
        }
    )


def daily_row(**values):
    return {
        "id": "10",
        "date": str(DAY),
        "views": "1000",
        "clicks": "10",
        "orders": "2",
        "moneySpent": "100,00",
        "ordersMoney": "500,00",
        **values,
    }


def sku_row(**values):
    return {
        "campaignId": "10",
        "sku": "100",
        "date": str(DAY),
        "views": "1000",
        "clicks": "10",
        "orders": "2",
        "expense": "100.00",
        "sales": "500.00",
        "modelSales": "50.00",
        **values,
    }


def detail_row(**values):
    return {
        "sku": "100",
        "date": "06.10.2026",
        "views": "1000",
        "clicks": "10",
        "moneySpent": "80,00",
        "ordersMoney": "400,00",
        **values,
    }


def test_numbers_and_weighted_ratios():
    assert number("1\u00a0234,56") == Decimal("1234.56")
    assert number("-") is None
    with pytest.raises(ValueError):
        number("NaN")
    result = totals(
        [
            dict(views=100, clicks=10, orders=1, spend=10, revenue=20),
            dict(views=900, clicks=10, orders=0, spend=90, revenue=0),
        ]
    )
    assert result["ctr"] == 2
    assert result["cpc"] == 5
    assert result["drr"] == 500
    assert (
        totals([dict(views=0, clicks=0, orders=0, spend=0, revenue=0)])["roas"] is None
    )
    assert (
        totals([dict(views=1, clicks=1, orders=None, spend=1, revenue=0)])["orders"]
        is None
    )
    assert totals([], covered=False)["spend"] is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/client/campaign/10/activate",
        "/api/client/campaign/all_sku_promo/set_bid",
        "/api/client/campaign/search_promo/carrots/enable",
        "/api/client/statistics/../campaign/10/activate",
    ],
)
def test_unsafe_get_denied(path):
    assert not allowed("GET", path)
    with pytest.raises(APIError):
        PerformanceAPI().request("GET", path)


def test_catalog_units_and_source_priority(db):
    catalog(campaign_api())
    with db() as conn:
        c = conn.execute(
            "SELECT * FROM ozon.ads_campaign WHERE campaign_id=10"
        ).fetchone()
        assert c["weekly_budget"] == 4000
        assert c["payment_type"] == "CPC"
        assert c["placements"] == ["PLACEMENT_SEARCH"]
    ingest_sku({"10": {"report": {"rows": [detail_row()]}}}, DAY, DAY, [10], "detail")
    ingest_sku({"rows": [sku_row()]}, DAY, DAY, [10], "sku")
    ingest_sku({"10": {"report": {"rows": [detail_row()]}}}, DAY, DAY, [10], "detail")
    with db() as conn:
        r = conn.execute("SELECT * FROM ozon.ads_sku_daily").fetchone()
        assert r["spend"] == 100
        assert r["model_revenue"] == 50
        assert r["source"] == "sku"
        # Tomorrow's refreshed historical report replaces yesterday's direct data.
        conn.execute("UPDATE ozon.ads_sku_daily SET fetched_at=now()-interval '2 days'")
    ingest_sku(
        {"10": {"report": {"rows": [detail_row(moneySpent="105,00")]}}},
        DAY,
        DAY,
        [10],
        "detail",
    )
    with db() as conn:
        r = conn.execute("SELECT * FROM ozon.ads_sku_daily").fetchone()
        assert r["spend"] == 105
        assert r["orders"] is None  # JSON omits fields: do not invent zeros.


def test_daily_atomic_replacement_and_missing_day(db):
    ingest_daily("daily", {"rows": [daily_row()]}, DAY, DAY)
    with pytest.raises(ValueError):
        ingest_daily("daily", {"rows": [daily_row(), daily_row()]}, DAY, DAY)
    with db() as conn:
        assert (
            conn.execute("SELECT spend FROM ozon.ads_campaign_daily").fetchone()[
                "spend"
            ]
            == 100
        )
        assert (
            conn.execute("SELECT count(*) n FROM ozon.ads_coverage").fetchone()["n"]
            == 1
        )
    ingest_daily("daily", {"rows": []}, DAY, DAY)
    with db() as conn:
        assert (
            conn.execute("SELECT count(*) n FROM ozon.ads_campaign_daily").fetchone()[
                "n"
            ]
            == 0
        )


def test_cpo_and_expense_never_double_count_cpc(db):
    catalog(campaign_api())
    ingest_daily(
        "daily",
        {"rows": [daily_row(), daily_row(id="20", moneySpent="999,00")]},
        DAY,
        DAY,
    )
    ingest_daily(
        "expense",
        {"rows": [daily_row(bonusSpent="30,00", prepaymentSpent="40,00")]},
        DAY,
        DAY,
    )
    ingest_sku({"rows": [sku_row(expense="99.00")]}, DAY, DAY, [10], "sku")
    ingest_cpo(
        {
            "rows": [
                {
                    "SKU": "100",
                    "Orders": "1",
                    "OrdersMoney": "1000,00",
                    "MoneySpent": "200,00",
                    "MoneySpentFromCPC": "9999,00",
                    "ordersFromCPC": "999",
                }
            ]
        },
        DAY,
    )
    result = advertising(DAY, DAY)
    assert result["summary"]["spend"] == 100
    assert result["products"][0]["spend"] == 99
    assert result["expense"]["bonuses"] == 30
    assert result["cpo"]["summary"]["spend"] == 200
    assert result["cpo"]["summary"]["orders"] == 1
    assert advertising(DAY, DAY, sku=100)["summary"]["spend"] == 99
    assert advertising(DAY, DAY, campaign=20)["cpo"]["products"] == []
    history = advertising(DAY - timedelta(days=1), DAY)
    assert history["daily"][0]["spend"] is None


def test_api_token_ram_only_and_quota(db, monkeypatch):
    monkeypatch.setattr(
        performance_api,
        "settings",
        SimpleNamespace(
            client_id=123,
            interval=0,
            ads_daily_limit=3,
            ads_export_limit=10,
            performance_client_id="fake-id",
            performance_secret="fake-secret",
        ),
    )
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "fake-access-token", "expires_in": 1800}
            if request.url.path.endswith("/token")
            else {"rows": []},
        )

    api = PerformanceAPI(httpx.Client(transport=httpx.MockTransport(transport)))
    api.request("GET", "/api/client/statistics/daily/json")
    api.request("GET", "/api/client/statistics/daily/json")
    with pytest.raises(DeferredRequest):
        api.request("GET", "/api/client/statistics/daily/json")
    assert len(requests) == 3
    assert requests[1].headers["Authorization"] == "Bearer fake-access-token"
    with db() as conn:
        audit = list(conn.execute("SELECT * FROM ozon.ads_api_call"))
        assert "fake-secret" not in str(audit) and "fake-access-token" not in str(audit)
        assert conn.execute("SELECT count(*) n FROM ozon.api_call").fetchone()["n"] == 0


def test_429_persists_cooldown_without_replay(db, monkeypatch):
    monkeypatch.setattr(
        performance_api,
        "settings",
        SimpleNamespace(
            client_id=123, interval=0, ads_daily_limit=100, ads_export_limit=10
        ),
    )
    api = PerformanceAPI(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(429, headers={"Retry-After": "600"}, json={})
            )
        )
    )
    api.token = "fake"
    api.expires = 99999999999
    with pytest.raises(APIError):
        api.request("GET", "/api/client/campaign")
    with pytest.raises(DeferredRequest):
        api.request("GET", "/api/client/campaign")
    with db() as conn:
        assert (
            conn.execute("SELECT count(*) n FROM ozon.ads_api_call").fetchone()["n"]
            == 1
        )
        assert conn.execute(
            "SELECT next_allowed_at FROM ozon.ads_api_cooldown"
        ).fetchone()["next_allowed_at"] > datetime.now(UTC) + timedelta(seconds=590)


def test_report_resume_and_no_recreation(db, monkeypatch, tmp_path):
    monkeypatch.setattr(
        performance, "settings", SimpleNamespace(client_id=123, data_dir=tmp_path)
    )
    with db() as conn:
        enqueue(conn, "detail", DAY, DAY, [10], DAY)
    calls = []
    code = "00000000-0000-0000-0000-000000000001"

    def request(method, path, **kwargs):
        calls.append((method, path))
        if method == "POST":
            return {"UUID": code}
        if path.endswith(code):
            return {"state": "IN_PROGRESS"}
        return {"10": {"report": {"rows": [detail_row()]}}}

    api = SimpleNamespace(request=request, sleep=lambda s: None)
    reports(api)
    reports(api)  # Fresh poll is skipped.
    assert sum(m == "POST" for m, p in calls) == 1
    with db() as conn:
        conn.execute("UPDATE ozon.ads_report SET polled_at=now()-interval '2 minutes'")
    api.request = lambda method, path, **k: (
        {"state": "OK"}
        if path.endswith(code)
        else {"10": {"report": {"rows": [detail_row()]}}}
    )
    reports(api)
    with db() as conn:
        r = conn.execute("SELECT * FROM ozon.ads_report").fetchone()
        assert r["status"] == "ready" and r["sha256"]
        assert (
            conn.execute("SELECT count(*) n FROM ozon.ads_sku_daily").fetchone()["n"]
            == 1
        )


@pytest.mark.parametrize("deferred", [False, True])
def test_ambiguous_creation_blocks_duplicates_but_unsent_stays_queued(db, deferred):
    with db() as conn:
        enqueue(conn, "cpo", DAY, DAY, [], DAY)
    calls = []

    def request(*a, **k):
        calls.append(a)
        raise DeferredRequest("quota") if deferred else APIError("network timeout")

    api = SimpleNamespace(request=request)
    with pytest.raises(APIError):
        reports(api)
    with db() as conn:
        assert conn.execute("SELECT status FROM ozon.ads_report").fetchone()[
            "status"
        ] == ("queued" if deferred else "uncertain")
    if not deferred:
        with pytest.raises(APIError):
            reports(api)
        assert len(calls) == 1


def test_advertising_endpoint_authenticated_and_sql_only(db, monkeypatch):
    catalog(campaign_api())
    monkeypatch.setattr(
        PerformanceAPI,
        "request",
        lambda *a, **k: pytest.fail("Dashboard must not call Ozon"),
    )
    with TestClient(app) as client:
        url = "/api/advertising?date_from=2026-10-06&date_to=2026-10-06"
        assert client.get(url).status_code == 401
        client.cookies.set("ozon_session", create_session())
        assert client.get(url).status_code == 200
        assert (
            client.get(
                "/api/advertising?date_from=2026-10-07&date_to=2026-10-01"
            ).status_code
            == 422
        )


def test_incomplete_detail_does_not_publish_coverage(db):
    with pytest.raises(ValueError):
        ingest_sku(
            {"10": {"report": {"rows": [detail_row()]}}}, DAY, DAY, [10, 11], "detail"
        )
    with db() as conn:
        assert (
            conn.execute("SELECT count(*) n FROM ozon.ads_coverage").fetchone()["n"]
            == 0
        )


def test_report_without_uuid_is_uncertain_not_replayed(db):
    with db() as conn:
        enqueue(conn, "cpo", DAY, DAY, [], DAY)
    api = SimpleNamespace(request=lambda *a, **k: {"UUID": None})
    with pytest.raises((ValueError, TypeError, AttributeError)):
        reports(api)
    with db() as conn:
        assert (
            conn.execute("SELECT status FROM ozon.ads_report").fetchone()["status"]
            == "uncertain"
        )


def test_catalog_detects_stuck_pagination(db):
    api = campaign_api()
    original = api.request
    api.request = lambda *a, **k: {**original(), "total": "200"}
    with pytest.raises(ValueError):
        catalog(api)
    with db() as conn:
        assert (
            conn.execute("SELECT count(*) n FROM ozon.ads_campaign").fetchone()["n"]
            == 0
        )
