"""Daily Performance facts and serialized, restart-safe asynchronous exports."""

import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from psycopg import sql
from psycopg.types.json import Jsonb

from .api import APIError
from .collector import run_job
from .config import settings
from .database import connect
from .performance_api import DeferredRequest, PerformanceAPI, RejectedRequest

MSK = ZoneInfo("Europe/Moscow")
log = logging.getLogger(__name__)


def number(value):
    if value is None or value == "" or value == "-":
        return None
    result = Decimal(
        str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    )
    if not result.is_finite():
        raise ValueError("Non-finite advertising metric")
    return result


def report_day(value):
    return (
        datetime.strptime(value, "%d.%m.%Y").date()
        if "." in value
        else date.fromisoformat(value)
    )


def days(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def save(conn, table, row, keys, condition=""):
    columns = list(row)
    query = sql.SQL(
        "INSERT INTO ozon.{} ({}) VALUES({}) ON CONFLICT({}) DO UPDATE SET {}"
        + condition
    ).format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
        sql.SQL(",").join(map(sql.Identifier, keys)),
        sql.SQL(",").join(
            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
            for c in columns
            if c not in keys
        ),
    )
    conn.execute(query, [row[c] for c in columns])


def coverage(conn, kind, start, end, scopes=("",)):
    for day in days(start, end):
        for scope in scopes:
            save(
                conn,
                "ads_coverage",
                {
                    "client_id": settings.client_id,
                    "kind": kind,
                    "day": day,
                    "scope": str(scope),
                    "fetched_at": datetime.now(UTC),
                },
                ["client_id", "kind", "day", "scope"],
            )


def catalog(api):
    campaigns, page, seen = [], 1, set()
    while True:
        data = api.request(
            "GET", "/api/client/campaign", params={"page": page, "pageSize": 100}
        )
        batch = data["list"]
        for row in batch:
            campaign_id = int(row["id"])
            if campaign_id in seen:
                raise ValueError("Duplicate/non-advancing campaign pagination")
            seen.add(campaign_id)
        campaigns.extend(batch)
        if len(campaigns) >= int(data["total"]):
            break
        if not batch:
            raise ValueError("Incomplete campaign catalog")
        page += 1
    today = datetime.now(MSK).date()
    with connect() as conn:
        for row in campaigns:
            placements = row.get("placement") or []
            if isinstance(placements, str):
                placements = [placements]
            save(
                conn,
                "ads_campaign",
                {
                    "client_id": settings.client_id,
                    "campaign_id": int(row["id"]),
                    "title": row.get("title") or "Кампания " + row["id"],
                    "state": row.get("state", "UNKNOWN"),
                    "object_type": row.get("advObjectType", "UNKNOWN"),
                    "payment_type": row.get("PaymentType", row.get("paymentType")),
                    "placements": placements,
                    "daily_budget": number(row.get("dailyBudget")) / 1000000
                    if row.get("dailyBudget") is not None
                    else None,
                    "weekly_budget": number(row.get("weeklyBudget")) / 1000000
                    if row.get("weeklyBudget") is not None
                    else None,
                    "raw": Jsonb(row),
                    "fetched_at": datetime.now(UTC),
                },
                ["client_id", "campaign_id"],
            )
            save(
                conn,
                "ads_campaign_snapshot",
                {
                    "client_id": settings.client_id,
                    "day": today,
                    "campaign_id": int(row["id"]),
                    "raw": Jsonb(row),
                },
                ["client_id", "day", "campaign_id"],
            )


def ingest_daily(kind, data, start, end):
    rows = data["rows"]
    normalized, seen = [], set()
    for row in rows:
        day, campaign = report_day(row["date"]), int(row["id"])
        if not start <= day <= end or (day, campaign) in seen:
            raise ValueError("Unexpected/duplicate campaign day")
        seen.add((day, campaign))
        result = {
            "client_id": settings.client_id,
            "day": day,
            "campaign_id": campaign,
            "raw": Jsonb(row),
            "spend": number(row.get("moneySpent")),
        }
        if kind == "daily":
            result.update(
                {
                    k: number(row.get(v))
                    for k, v in {
                        "views": "views",
                        "clicks": "clicks",
                        "orders": "orders",
                        "revenue": "ordersMoney",
                    }.items()
                }
            )
        else:
            result.update(
                bonuses=number(row.get("bonusSpent")),
                prepayment=number(row.get("prepaymentSpent")),
            )
        normalized.append(result)
    table = "ads_campaign_daily" if kind == "daily" else "ads_expense_daily"
    with connect() as conn:
        conn.execute(
            sql.SQL(
                "DELETE FROM ozon.{} WHERE client_id=%s AND day BETWEEN %s AND %s"
            ).format(sql.Identifier(table)),
            (settings.client_id, start, end),
        )
        for row in normalized:
            save(conn, table, row, ["client_id", "day", "campaign_id"])
        coverage(conn, kind, start, end)


def ingest_sku(data, start, end, campaigns, source):
    rows = (
        data["rows"]
        if source == "sku"
        else [
            dict(row, campaignId=campaign)
            for campaign, report in data.items()
            for row in report["report"]["rows"]
        ]
    )
    normalized, seen = [], set()
    for row in rows:
        campaign, sku, day = (
            int(row["campaignId"]),
            int(row["sku"]),
            report_day(row["date"]),
        )
        if (
            campaign not in campaigns
            or not start <= day <= end
            or (campaign, sku, day) in seen
        ):
            raise ValueError("Unexpected/duplicate SKU report grain")
        seen.add((campaign, sku, day))
        fields = {
            "views": "views",
            "clicks": "clicks",
            "carts": "toCart",
            "orders": "orders",
            "spend": "expense" if source == "sku" else "moneySpent",
            "revenue": "sales" if source == "sku" else "ordersMoney",
            "model_orders": "modelOrders",
            "model_revenue": "modelSales" if source == "sku" else "modelsMoney",
        }
        normalized.append(
            {
                "client_id": settings.client_id,
                "day": day,
                "campaign_id": campaign,
                "sku": sku,
                "title": row.get("title"),
                "source": source,
                "raw": Jsonb(row),
                "fetched_at": datetime.now(UTC),
                **{k: number(row.get(v)) for k, v in fields.items()},
            }
        )
    with connect() as conn:
        # Direct yesterday report wins over overlapping historical detail. Both replace, never add.
        conn.execute(
            "DELETE FROM ozon.ads_sku_daily WHERE client_id=%s AND day BETWEEN %s AND %s AND campaign_id=ANY(%s) AND (source='detail' OR %s='sku' OR fetched_at < date_trunc('day',now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow')",
            (settings.client_id, start, end, campaigns, source),
        )
        for row in normalized:
            save(
                conn,
                "ads_sku_daily",
                row,
                ["client_id", "day", "campaign_id", "sku"],
                " WHERE ozon.ads_sku_daily.source='detail' OR EXCLUDED.source='sku' OR ozon.ads_sku_daily.fetched_at < date_trunc('day',now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow'",
            )
        coverage(conn, source, start, end, campaigns)


def ingest_cpo(data, day):
    normalized, seen = [], set()
    for row in data["rows"]:
        sku = int(row["SKU"])
        if sku in seen:
            raise ValueError("Duplicate CPO SKU")
        seen.add(sku)
        normalized.append(
            {
                "client_id": settings.client_id,
                "day": day,
                "sku": sku,
                "title": row.get("Title"),
                "offer_id": row.get("OfferID"),
                "orders": number(row.get("Orders")),
                "spend": number(row.get("MoneySpent")),
                "revenue": number(row.get("OrdersMoney")),
                "bid_percent": number(row.get("Bid")),
                "bid_rubles": number(row.get("BidValue")),
                "promotion_status": row.get("PromotionStatus"),
                "raw": Jsonb(row),
                "fetched_at": datetime.now(UTC),
            }
        )
    with connect() as conn:
        conn.execute(
            "DELETE FROM ozon.ads_cpo_daily WHERE client_id=%s AND day=%s",
            (settings.client_id, day),
        )
        for row in normalized:
            save(conn, "ads_cpo_daily", row, ["client_id", "day", "sku"])
        coverage(conn, "cpo", day, day)


def enqueue(conn, kind, start, end, campaigns, requested_on):
    parameters = (
        {
            "campaigns": [str(c) for c in campaigns],
            "dateFrom": str(start),
            "dateTo": str(end),
            "groupBy": "DATE",
        }
        if kind == "detail"
        else {"from": f"{start}T00:00:00+03:00", "to": f"{end}T23:59:59+03:00"}
    )
    conn.execute(
        "INSERT INTO ozon.ads_report(client_id,kind,date_from,date_to,campaigns,parameters,requested_on) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (
            settings.client_id,
            kind,
            start,
            end,
            campaigns,
            Jsonb(parameters),
            requested_on,
        ),
    )


def facts(api):
    today = datetime.now(MSK).date()
    end = today - timedelta(days=1)
    with connect() as conn:
        campaigns = [
            r["campaign_id"]
            for r in conn.execute(
                "SELECT campaign_id FROM ozon.ads_campaign WHERE client_id=%s AND object_type='SKU' ORDER BY campaign_id",
                (settings.client_id,),
            )
        ]
        cpo = conn.execute(
            "SELECT 1 FROM ozon.ads_campaign WHERE client_id=%s AND object_type='SEARCH_PROMO' LIMIT 1",
            (settings.client_id,),
        ).fetchone()
    start = end - timedelta(days=13)
    for kind in ("daily", "expense"):
        data = api.request(
            "GET",
            f"/api/client/statistics/{kind}/json",
            params={"dateFrom": str(start), "dateTo": str(end)},
        )
        ingest_daily(kind, data, start, end)
    for i in range(0, len(campaigns), 10):
        batch = campaigns[i : i + 10]
        with connect() as conn:
            fresh = {
                int(r["scope"])
                for r in conn.execute(
                    "SELECT scope FROM ozon.ads_coverage WHERE client_id=%s AND kind='sku' AND day=%s AND fetched_at>=%s",
                    (
                        settings.client_id,
                        end,
                        datetime.combine(today, datetime.min.time(), MSK),
                    ),
                )
            }
        direct_batch = [c for c in batch if c not in fresh]
        if direct_batch:
            data = api.request(
                "POST",
                "/api/client/statistics/products/sku",
                body={
                    "campaignIds": [str(c) for c in direct_batch],
                    "dateFrom": str(end),
                    "dateTo": str(end),
                },
            )
            ingest_sku(data, end, end, direct_batch, "sku")
    with connect() as conn:
        covered = {
            int(r["scope"])
            for r in conn.execute(
                "SELECT scope FROM ozon.ads_coverage WHERE client_id=%s AND kind='detail' AND day BETWEEN %s AND %s AND fetched_at>=%s GROUP BY scope HAVING count(*)=%s",
                (
                    settings.client_id,
                    start,
                    end,
                    datetime.combine(today, datetime.min.time(), MSK),
                    (end - start).days + 1,
                ),
            )
        }
    pending_campaigns = [c for c in campaigns if c not in covered]
    for i in range(0, len(pending_campaigns), 10):
        with connect() as conn:
            enqueue(conn, "detail", start, end, pending_campaigns[i : i + 10], today)
    if cpo:
        with connect() as conn:
            for offset in range(7):
                day = end - timedelta(days=offset)
                if conn.execute(
                    "SELECT 1 FROM ozon.ads_coverage WHERE client_id=%s AND kind='cpo' AND day=%s AND fetched_at>=%s",
                    (
                        settings.client_id,
                        day,
                        datetime.combine(today, datetime.min.time(), MSK),
                    ),
                ).fetchone():
                    continue
                enqueue(conn, "cpo", day, day, [], today)


def finish_report(report, data):
    if report["kind"] == "detail":
        ingest_sku(
            data, report["date_from"], report["date_to"], report["campaigns"], "detail"
        )
    else:
        if report["date_from"] != report["date_to"]:
            raise ValueError("CPO facts require one calendar day")
        ingest_cpo(data, report["date_from"])
    folder = settings.data_dir / "reports" / "performance"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(data, ensure_ascii=False).encode()
    path = folder / f"{report['id']}.json"
    path.write_bytes(payload)
    path.chmod(0o600)
    metadata = (
        {key: value["report"].get("totals", {}) for key, value in data.items()}
        if report["kind"] == "detail"
        else {}
    )
    with connect() as conn:
        conn.execute(
            "UPDATE ozon.ads_report SET status='ready',finished_at=now(),file_path=%s,sha256=%s,metadata=%s WHERE id=%s",
            (
                str(path),
                hashlib.sha256(payload).hexdigest(),
                Jsonb(metadata),
                report["id"],
            ),
        )


def reports(api):
    # One export at a time; a pending/ambiguous export blocks further creation.
    # Limit work per invocation; next timer resumes persisted UUIDs.
    for _ in range(8):
        with connect() as conn:
            report = conn.execute(
                "SELECT * FROM ozon.ads_report WHERE client_id=%s AND status IN ('requesting','pending','uncertain') ORDER BY id LIMIT 1",
                (settings.client_id,),
            ).fetchone()
            if report and report["status"] != "pending":
                raise APIError(
                    "Ambiguous Performance report creation; manual reconciliation required"
                )
            if not report:
                report = conn.execute(
                    "SELECT * FROM ozon.ads_report WHERE client_id=%s AND status='queued' ORDER BY id LIMIT 1 FOR UPDATE",
                    (settings.client_id,),
                ).fetchone()
                if not report:
                    return
                conn.execute(
                    "UPDATE ozon.ads_report SET status='requesting' WHERE id=%s",
                    (report["id"],),
                )
        if not report["code"]:
            path = (
                "/api/client/statistics/json"
                if report["kind"] == "detail"
                else "/api/client/statistic/products/generate/json"
            )
            try:
                data = api.request(
                    "POST",
                    path,
                    body=report["parameters"],
                    exports=max(1, len(report["campaigns"])),
                )
                code = data["UUID"]
            except (DeferredRequest, RejectedRequest) as exc:
                with connect() as conn:
                    conn.execute(
                        "UPDATE ozon.ads_report SET status=%s WHERE id=%s",
                        (
                            "queued"
                            if isinstance(exc, DeferredRequest)
                            or "HTTP 429" in str(exc)
                            else "failed",
                            report["id"],
                        ),
                    )
                raise
            except Exception:
                with connect() as conn:
                    conn.execute(
                        "UPDATE ozon.ads_report SET status='uncertain' WHERE id=%s",
                        (report["id"],),
                    )
                raise
            with connect() as conn:
                conn.execute(
                    "UPDATE ozon.ads_report SET code=%s,status='pending' WHERE id=%s",
                    (code, report["id"]),
                )
            report["code"] = code
            api.sleep(30)
        elif (
            report["polled_at"]
            and (datetime.now(UTC) - report["polled_at"]).total_seconds() < 60
        ):
            return
        data = api.request("GET", "/api/client/statistics/" + report["code"])
        with connect() as conn:
            conn.execute(
                "UPDATE ozon.ads_report SET polled_at=now() WHERE id=%s",
                (report["id"],),
            )
        if data["state"] == "ERROR":
            with connect() as conn:
                conn.execute(
                    "UPDATE ozon.ads_report SET status='failed',finished_at=now() WHERE id=%s",
                    (report["id"],),
                )
            raise APIError("Performance export failed; see protected report record")
        if data["state"] != "OK":
            return
        payload = api.request(
            "GET", "/api/client/statistics/report", params={"UUID": report["code"]}
        )
        finish_report(report, payload)


def collect_performance():
    if datetime.now(MSK).strftime("%H:%M") < "10:10":
        return
    with connect() as lock:
        if not lock.execute(
            "SELECT pg_try_advisory_lock(95141812) acquired"
        ).fetchone()["acquired"]:
            return
        api = PerformanceAPI()
        results = []
        for name, function in [("ads_catalog", catalog), ("ads_facts", facts)]:
            with connect() as conn:
                done = conn.execute(
                    "SELECT 1 FROM ozon.job_run WHERE job=%s AND status='success' AND started_at>=%s",
                    (
                        name,
                        datetime.combine(
                            datetime.now(MSK).date(), datetime.min.time(), MSK
                        ),
                    ),
                ).fetchone()
            if not done:
                success = run_job(name, function, api)
                results.append(success)
                if not success:
                    raise SystemExit(1)
        results.append(run_job("ads_reports", reports, api))
        if not all(results):
            raise SystemExit(1)
