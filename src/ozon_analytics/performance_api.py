"""Read/report-only Performance client; independent durable quotas, no saved tokens."""

import re
import time
from datetime import UTC, datetime, timedelta

import httpx

from .api import APIError, retry_after
from .config import settings
from .database import connect

BASE = "https://api-performance.ozon.ru"
GET_PATHS = {
    "/api/client/campaign",
    "/api/client/statistics/daily/json",
    "/api/client/statistics/expense/json",
    "/api/client/statistics/report",
}
POST_PATHS = {
    "/api/client/statistics/products/sku",
    "/api/client/statistics/json",
    "/api/client/statistic/products/generate/json",
}


def allowed(method, path):
    # Some Performance GET methods change bids or activate advertising!
    return (method == "POST" and path in POST_PATHS) or (
        method == "GET"
        and (
            path in GET_PATHS
            or re.fullmatch(r"/api/client/statistics/[0-9a-fA-F-]{36}", path)
        )
    )


class PerformanceAPI:
    def __init__(self, client=None, sleeper=time.sleep):
        self.client = client or httpx.Client(timeout=90, trust_env=False)
        self.sleep = sleeper
        self.token = None
        self.expires = 0

    def reserve(self, path, exports=0):
        while True:
            with connect() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(95141811)")
                now = datetime.now(UTC)
                cooldown = conn.execute(
                    "SELECT next_allowed_at FROM ozon.ads_api_cooldown WHERE client_id=%s",
                    (settings.client_id,),
                ).fetchone()
                wait = (
                    max(0, (cooldown["next_allowed_at"] - now).total_seconds())
                    if cooldown
                    else 0
                )
                if wait > 300:
                    raise APIError("Performance cooldown active; resume on next timer")
                if not wait:
                    counts = conn.execute(
                        "SELECT count(*) n,coalesce(sum(export_cost),0) exports FROM ozon.ads_api_call WHERE client_id=%s AND called_at>=now()-interval '24 hours'",
                        (settings.client_id,),
                    ).fetchone()
                    if (
                        counts["n"] >= settings.ads_daily_limit
                        or counts["exports"] + exports > settings.ads_export_limit
                    ):
                        raise APIError("Performance rolling 24-hour budget reached")
                    conn.execute(
                        "INSERT INTO ozon.ads_api_cooldown VALUES(%s,%s) ON CONFLICT(client_id) DO UPDATE SET next_allowed_at=EXCLUDED.next_allowed_at",
                        (
                            settings.client_id,
                            now + timedelta(seconds=settings.interval),
                        ),
                    )
                    return conn.execute(
                        "INSERT INTO ozon.ads_api_call(client_id,endpoint,export_cost) VALUES(%s,%s,%s) RETURNING id",
                        (settings.client_id, path, exports),
                    ).fetchone()["id"]
            self.sleep(wait)

    def send(self, method, path, *, body=None, params=None, exports=0, token=False):
        call_id = self.reserve(path, exports)
        try:
            response = self.client.request(
                method,
                BASE + path,
                json=body if method == "POST" else None,
                params=params,
                headers={} if token else {"Authorization": "Bearer " + self.token},
            )
        except httpx.HTTPError as exc:
            with connect() as conn:
                conn.execute(
                    "UPDATE ozon.ads_api_call SET error=%s WHERE id=%s",
                    (type(exc).__name__, call_id),
                )
            self.cooldown(120)
            raise APIError("Performance network failure; no automatic replay") from None
        with connect() as conn:
            conn.execute(
                "UPDATE ozon.ads_api_call SET status=%s WHERE id=%s",
                (response.status_code, call_id),
            )
        if response.status_code >= 400:
            self.cooldown(
                retry_after(response.headers.get("Retry-After"))
                if response.status_code == 429
                else 86400
                if response.status_code in (400, 401, 403)
                else 120
            )
            raise APIError(f"Performance HTTP {response.status_code}: {path}")
        if len(response.content) > 50 * 1024 * 1024:
            raise APIError("Performance report exceeds 50MB")
        try:
            return response.json()
        except ValueError:
            raise APIError("Performance returned invalid JSON") from None

    def cooldown(self, seconds):
        with connect() as conn:
            conn.execute(
                "INSERT INTO ozon.ads_api_cooldown VALUES(%s,%s) ON CONFLICT(client_id) DO UPDATE SET next_allowed_at=greatest(ozon.ads_api_cooldown.next_allowed_at,EXCLUDED.next_allowed_at)",
                (settings.client_id, datetime.now(UTC) + timedelta(seconds=seconds)),
            )

    def request(self, method, path, *, body=None, params=None, exports=0):
        if not allowed(method, path):
            raise APIError("Performance endpoint outside read/report allowlist")
        if not self.token or time.monotonic() >= self.expires:
            if not settings.performance_client_id or not settings.performance_secret:
                raise APIError("Performance credentials are not configured")
            data = self.send(
                "POST",
                "/api/client/token",
                body={
                    "client_id": settings.performance_client_id,
                    "client_secret": settings.performance_secret,
                    "grant_type": "client_credentials",
                },
                token=True,
            )
            self.token = data["access_token"]
            self.expires = time.monotonic() + max(1, int(data["expires_in"]) - 60)
        return self.send(method, path, body=body, params=params, exports=exports)
