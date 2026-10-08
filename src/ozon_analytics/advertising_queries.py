"""SQL-only advertising dashboard. Ratios use summed numerators/denominators."""

from collections import defaultdict
from decimal import Decimal

from .config import settings
from .database import connect
from .performance import days

METRICS = ("views", "clicks", "orders", "spend", "revenue")


def totals(rows, covered=True):
    result = {}
    for key in METRICS:
        values = [row.get(key) for row in rows]
        result[key] = (
            None
            if not covered or any(v is None for v in values)
            else sum(values, Decimal(0))
        )
    for name, numerator, denominator, factor in [
        ("ctr", "clicks", "views", 100),
        ("cpc", "spend", "clicks", 1),
        ("conversion", "orders", "clicks", 100),
        ("cpo", "spend", "orders", 1),
        ("drr", "spend", "revenue", 100),
        ("roas", "revenue", "spend", 1),
    ]:
        a, b = result[numerator], result[denominator]
        result[name] = (
            a / b * factor if a is not None and b is not None and b > 0 else None
        )
    return result


def groups(rows, key, metadata=None):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)
    return sorted(
        [
            {key: value, **(metadata or {}).get(value, {}), **totals(group)}
            for value, group in grouped.items()
        ],
        key=lambda r: r["spend"] or 0,
        reverse=True,
    )


def advertising(date_from, date_to, sku=None, campaign=None):
    client = settings.client_id
    with connect(web=True) as conn:
        campaigns = list(
            conn.execute(
                "SELECT campaign_id,title,state,object_type,payment_type,placements,weekly_budget,fetched_at FROM ozon.ads_campaign WHERE client_id=%s ORDER BY title",
                (client,),
            )
        )
        metadata = {row["campaign_id"]: row for row in campaigns}
        params = [client, date_from, date_to]
        where = "client_id=%s AND day BETWEEN %s AND %s"
        if campaign is not None:
            where += " AND campaign_id=%s"
            params.append(campaign)
        if sku is not None:
            where += " AND sku=%s"
            params.append(sku)
        where += " AND campaign_id IN (SELECT c.campaign_id FROM ozon.ads_campaign c WHERE c.client_id=%s AND c.object_type='SKU')"
        params.append(client)
        table = "ads_sku_daily" if sku is not None else "ads_campaign_daily"
        fact_columns = "day,campaign_id,views,clicks,orders,spend,revenue"
        facts = list(
            conn.execute(
                f"SELECT {fact_columns} FROM ozon.{table} WHERE {where} ORDER BY day",
                params,
            )
        )
        # SKU ranking always uses SKU facts; headline uses canonical campaign/day unless SKU filtered.
        products = list(
            conn.execute(
                f"SELECT {fact_columns},sku,carts,model_orders,model_revenue FROM ozon.ads_sku_daily WHERE {where} ORDER BY day",
                params,
            )
        )
        coverage = list(
            conn.execute(
                "SELECT * FROM ozon.ads_coverage WHERE client_id=%s AND day BETWEEN %s AND %s ORDER BY day",
                (client, date_from, date_to),
            )
        )
        expense_params = [client, date_from, date_to]
        expense_where = "client_id=%s AND day BETWEEN %s AND %s"
        if campaign is not None:
            expense_where += " AND campaign_id=%s"
            expense_params.append(campaign)
        expenses = (
            list(
                conn.execute(
                    f"SELECT spend,bonuses,prepayment FROM ozon.ads_expense_daily WHERE {expense_where}",
                    expense_params,
                )
            )
            if sku is None
            else []
        )
        cpo_where = "client_id=%s AND day BETWEEN %s AND %s"
        cpo_params = [client, date_from, date_to]
        if sku is not None:
            cpo_where += " AND sku=%s"
            cpo_params.append(sku)
        cpo = (
            list(
                conn.execute(
                    f"SELECT day,sku,title,offer_id,orders,spend,revenue,bid_percent,bid_rubles,promotion_status FROM ozon.ads_cpo_daily WHERE {cpo_where} ORDER BY day",
                    cpo_params,
                )
            )
            if campaign is None
            else []
        )
        product_options = list(
            conn.execute(
                "SELECT DISTINCT ON (d.sku) d.sku,coalesce(p.offer_id,d.sku::text) offer_id,coalesce(p.name,d.title) title FROM ozon.ads_sku_daily d LEFT JOIN ozon.product p USING(client_id,sku) WHERE d.client_id=%s ORDER BY d.sku,d.fetched_at DESC",
                (client,),
            )
        )
        snapshot = conn.execute(
            "SELECT max(snapshot_date) AS day FROM ozon.published_snapshot WHERE client_id=%s",
            (client,),
        ).fetchone()["day"]
        stocks = list(
            conn.execute(
                "SELECT sku,CASE WHEN bool_and(available_stock_count IS NOT NULL) THEN sum(available_stock_count) END available FROM ozon.v_stock_daily WHERE client_id=%s AND snapshot_date=%s GROUP BY sku",
                (client, snapshot),
            )
        )
        reports = list(
            conn.execute(
                "SELECT id,kind,date_from,date_to,status,created_at,finished_at FROM ozon.ads_report WHERE client_id=%s AND (status IN ('queued','requesting','pending','uncertain','failed') OR created_at>now()-interval '1 day') ORDER BY id DESC LIMIT 30",
                (client,),
            )
        )
        jobs = list(
            conn.execute(
                "SELECT job,status,started_at,finished_at,details FROM ozon.job_run WHERE job LIKE 'ads_%' ORDER BY id DESC LIMIT 15"
            )
        )
        calls = conn.execute(
            "SELECT count(*) calls,count(*) FILTER(WHERE status=429) limited,coalesce(sum(export_cost),0) exports FROM ozon.ads_api_call WHERE client_id=%s AND called_at>=now()-interval '24 hours'",
            (client,),
        ).fetchone()
    covered_days = {r["day"] for r in coverage if r["kind"] == "daily"}
    relevant = (
        {campaign}
        if campaign is not None
        else {r["campaign_id"] for r in campaigns if r["object_type"] == "SKU"}
    )
    detail_scopes = defaultdict(set)
    for row in coverage:
        if row["kind"] in ("sku", "detail"):
            detail_scopes[row["day"]].add(int(row["scope"]))
    detail_days = {
        day for day, scope in detail_scopes.items() if relevant and relevant <= scope
    }
    headline_days = detail_days if sku is not None else covered_days
    byday = defaultdict(list)
    for row in facts:
        byday[row["day"]].append(row)
    daily = [
        {"day": day, **totals(byday[day], day in headline_days)}
        for day in days(date_from, date_to)
    ]
    product_meta = {row["sku"]: row for row in product_options}
    stock_meta = {row["sku"]: row["available"] for row in stocks}
    product_groups = groups(products, "sku", product_meta)
    for row in product_groups:
        row["available"] = stock_meta.get(row["sku"])
        for key in ("carts", "model_orders", "model_revenue"):
            values = [p[key] for p in products if p["sku"] == row["sku"]]
            row[key] = (
                sum(values, Decimal(0)) if all(v is not None for v in values) else None
            )
    cpo_days = {r["day"] for r in coverage if r["kind"] == "cpo"}
    cpo_meta = {
        row["sku"]: {
            "title": row["title"],
            "offer_id": row["offer_id"],
            "bid_percent": row["bid_percent"],
            "bid_rubles": row["bid_rubles"],
            "promotion_status": row["promotion_status"],
        }
        for row in cpo
    }
    return {
        "summary": totals(facts, bool(headline_days)),
        "daily": daily,
        "campaigns": groups(facts, "campaign_id", metadata),
        "products": product_groups,
        "options": {"campaigns": campaigns, "products": product_options},
        "coverage": sorted(headline_days),
        "detail_coverage": sorted(detail_days),
        "expense": {
            key: sum((r[key] for r in expenses), Decimal(0))
            if expenses and all(r[key] is not None for r in expenses)
            else None
            for key in ("spend", "bonuses", "prepayment")
        },
        "cpo": {
            "summary": totals(
                [{**r, "views": 0, "clicks": 0} for r in cpo], bool(cpo_days)
            ),
            "products": groups(
                [{**r, "views": 0, "clicks": 0} for r in cpo], "sku", cpo_meta
            ),
            "coverage": sorted(cpo_days),
        },
        "jobs": jobs,
        "reports": reports,
        "api": calls,
        "stock_day": snapshot,
        "sku_filtered": sku is not None,
    }
