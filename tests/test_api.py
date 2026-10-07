from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from ozon_analytics import api


def test_retry_after_and_download_hosts():
    assert api.retry_after("1") == 60
    assert api.retry_after("invalid") == 120
    assert (
        api.retry_after(
            "Wed, 07 Oct 2026 18:00:00 GMT", datetime(2026, 10, 7, 17, 0, tzinfo=UTC)
        )
        == 3600
    )
    assert api.allowed_download("https://reports.ozon.ru/file")
    assert not api.allowed_download("https://ozon.ru.evil.example/file")
    assert not api.allowed_download("http://reports.ozon.ru/file")
    assert not api.allowed_download("https://user:pass@ozon.ru/file")


def test_write_endpoint_never_called():
    client = api.SellerAPI(
        httpx.Client(
            transport=httpx.MockTransport(lambda r: pytest.fail("Network called"))
        )
    )
    with pytest.raises(api.APIError, match="allowlist"):
        client.post("/v3/product/import", {})


def test_reservation_enforces_global_analytics_and_report_limits(db, monkeypatch):
    monkeypatch.setattr(api, "settings", replace(api.settings, daily_limit=10))
    client = api.SellerAPI()
    client.reserve("/v1/analytics/data", {})
    with db() as conn:
        cooldowns = {
            r["endpoint"]: r["next_allowed_at"]
            for r in conn.execute("SELECT * FROM ozon.api_cooldown")
        }
        assert (cooldowns["/v1/analytics/data"] - cooldowns["*"]).total_seconds() >= 59
        conn.execute("DELETE FROM ozon.api_cooldown")
    endpoint = "/v1/report/placement/by-products/create"
    for _ in range(2):
        client.reserve(endpoint, {})
        with db() as conn:
            conn.execute("DELETE FROM ozon.api_cooldown")
    with pytest.raises(api.APIError, match="Daily request budget"):
        client.reserve(endpoint, {})
    # Cooldown survives construction of a new client/process.
    with db() as conn:
        conn.execute(
            "INSERT INTO ozon.api_cooldown VALUES(123,'*',now()+interval '1 hour')"
        )
    with pytest.raises(api.APIError, match="Cooldown active"):
        api.SellerAPI().reserve("/v1/roles", {})


def test_4xx_activates_persistent_cooldown_without_retry(db):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(403, json={"message": "Denied"})

    client = api.SellerAPI(httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(api.APIError, match="Ozon 403"):
        client.post("/v1/roles", {})
    with pytest.raises(api.APIError, match="Cooldown active"):
        api.SellerAPI().post("/v1/roles", {})
    assert len(calls) == 1


def test_report_creation_network_failure_never_retried(db):
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("Test")

    client = api.SellerAPI(httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(api.APIError, match="Network failure"):
        client.post("/v1/report/placement/by-products/create", {})
    assert len(calls) == 1


def test_429_waits_then_retries_without_spamming(db):
    statuses = iter([429, 200])
    waits = []

    def respond(request):
        return httpx.Response(next(statuses), json={}, headers={"Retry-After": "2"})

    def advance(seconds):
        waits.append(seconds)
        # Simulate time passing while keeping this test fast.
        with db() as conn:
            conn.execute("DELETE FROM ozon.api_cooldown")

    client = api.SellerAPI(
        httpx.Client(transport=httpx.MockTransport(respond)), sleeper=advance
    )
    assert client.post("/v1/roles", {}) == {}
    assert len(waits) == 1 and waits[0] >= 59
    with db() as conn:
        assert [
            r["status"]
            for r in conn.execute("SELECT status FROM ozon.api_call ORDER BY id")
        ] == [429, 200]
