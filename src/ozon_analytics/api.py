"""Serial read-only API client with persistent budgets and cooldowns.

Official limits checked 2026-10-07: general 50 requests/s, analytics/data
1 request/min and 50/day without Premium, placement reports 5/day per method.
We use >=5s globally, >=65s for analytics, <=2 placement creations/day.
"""
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
from psycopg.types.json import Jsonb

from .config import settings
from .database import connect

READ_METHODS = frozenset({
    '/v1/roles', '/v1/seller/info', '/v3/product/list', '/v1/cluster/list',
    '/v2/cluster/list', '/v1/analytics/stocks', '/v1/analytics/data',
    '/v3/posting/fbo/list', '/v1/report/info',
    '/v1/report/placement/by-products/create', '/v1/report/placement/by-supplies/create',
})


class APIError(RuntimeError):
    pass


def retry_after(value, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        return max(60, float(value))
    except (TypeError, ValueError):
        try:
            return max(60, (parsedate_to_datetime(value) - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 120


def allowed_download(url):
    p = urlparse(url)
    return p.scheme == 'https' and not p.username and p.port in (None, 443) and any(
        p.hostname == d or (p.hostname or '').endswith('.' + d)
        for d in ('ozon.ru', 'ozon.com', 'ozone.ru'))


class SellerAPI:
    def __init__(self, client=None, sleeper=time.sleep):
        self.client = client or httpx.Client(timeout=60, trust_env=False)
        self.sleep = sleeper

    def reserve(self, endpoint, body):
        while True:
            now = datetime.now(timezone.utc)
            with connect() as conn:
                conn.execute('SELECT pg_advisory_xact_lock(%s)', (settings.client_id,))
                rows = list(conn.execute('SELECT next_allowed_at FROM ozon.api_cooldown WHERE client_id=%s AND endpoint IN (%s,%s)',
                                         (settings.client_id, '*', endpoint)))
                wait = max([0] + [(r['next_allowed_at']-now).total_seconds() for r in rows])
                if wait > 0:
                    if wait > 300:
                        raise APIError(f'Cooldown active for {endpoint}; next scheduled run will resume')
                else:
                    day = now.date()
                    counts = conn.execute('SELECT count(*) AS total, count(*) FILTER(WHERE endpoint=%s) AS endpoint_count FROM ozon.api_call WHERE client_id=%s AND called_at >= %s',
                                          (endpoint, settings.client_id, day)).fetchone()
                    limit = 2 if 'placement/' in endpoint else 45 if endpoint == '/v1/analytics/data' else settings.daily_limit
                    if counts['total'] >= settings.daily_limit or counts['endpoint_count'] >= limit:
                        raise APIError(f'Daily request budget reached for {endpoint}')
                    for key, seconds in [('*', settings.interval), (endpoint, 65 if endpoint == '/v1/analytics/data' else settings.interval)]:
                        conn.execute('INSERT INTO ozon.api_cooldown VALUES(%s,%s,%s) ON CONFLICT(client_id,endpoint) DO UPDATE SET next_allowed_at=EXCLUDED.next_allowed_at',
                                     (settings.client_id, key, now+timedelta(seconds=seconds)))
                    call_id = conn.execute('INSERT INTO ozon.api_call(client_id,endpoint,request_body) VALUES(%s,%s,%s) RETURNING id',
                                           (settings.client_id, endpoint, Jsonb(body))).fetchone()['id']
                    return call_id
            self.sleep(wait)

    def post(self, endpoint, body):
        if endpoint not in READ_METHODS:
            raise APIError('Endpoint is outside the read-only allowlist')
        # Ambiguous report creation failures never cause automatic recreation.
        attempts = 1 if 'placement/' in endpoint else 3
        for attempt in range(attempts):
            call_id = self.reserve(endpoint, body)
            try:
                response = self.client.post('https://api-seller.ozon.ru'+endpoint,
                    json=body, headers={'Client-Id':str(settings.client_id), 'Api-Key':settings.api_key})
                try:
                    data = response.json()
                except ValueError:
                    data = {'error':'Non-JSON response'}
                with connect() as conn:
                    conn.execute('UPDATE ozon.api_call SET status=%s,response_body=%s WHERE id=%s',
                                 (response.status_code, Jsonb(data), call_id))
                    if response.status_code == 429:
                        cooldown = datetime.now(timezone.utc)+timedelta(seconds=retry_after(response.headers.get('Retry-After')))
                        conn.execute("INSERT INTO ozon.api_cooldown VALUES(%s,'*',%s) ON CONFLICT(client_id,endpoint) DO UPDATE SET next_allowed_at=greatest(ozon.api_cooldown.next_allowed_at,EXCLUDED.next_allowed_at)",
                                     (settings.client_id, cooldown))
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt+1 < attempts:
                        if response.status_code >= 500:
                            self.sleep(60*(attempt+1))
                        continue
                if response.status_code >= 400:
                    # Do not put response bodies/credentials in operational logs.
                    raise APIError(f'Ozon {response.status_code}: {endpoint}; see protected api_call record')
                return data
            except httpx.HTTPError as exc:
                with connect() as conn:
                    conn.execute('UPDATE ozon.api_call SET error=%s WHERE id=%s',
                                 (type(exc).__name__, call_id))
                if attempt+1 == attempts:
                    raise APIError(f'Network failure: {endpoint}') from None
                self.sleep(60*(attempt+1))
        raise APIError(f'Retry budget exhausted: {endpoint}')

    def download(self, url):
        for _ in range(5):
            if not allowed_download(url):
                raise APIError('Report download host is outside the Ozon allowlist')
            response = self.client.get(url)
            if response.is_redirect:
                url = str(response.url.join(response.headers['location']))
                continue
            response.raise_for_status()
            if len(response.content) > 50*1024*1024:
                raise APIError('Report exceeds 50 MB limit')
            return response.content
        raise APIError('Too many report redirects')
