from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings
from .database import connect
from .forecast import forecast
from .stock_queries import stock_rows


def dashboard(
    date_from,
    date_to,
    sku=None,
    cluster=None,
    warehouse=None,
    include_pickup=False,
    include_archived=False,
):
    client = settings.client_id
    day = datetime.now(ZoneInfo("Europe/Moscow")).date()
    try:
        expiry = datetime.fromisoformat(settings.api_key_expires_at).astimezone(
            ZoneInfo("Europe/Moscow")
        )
    except ValueError:
        expiry = None
    with connect(web=True) as conn:
        params = [client, date_from, date_to]
        conditions = ["s.client_id=%s", "s.day BETWEEN %s AND %s"]
        for column, value in [
            ("sku", sku),
            ("cluster_id", cluster),
            ("warehouse_id", warehouse),
        ]:
            if value is not None:
                conditions.append(f"s.{column}=%s")
                params.append(value)
        where = " AND ".join(conditions)
        sales = list(
            conn.execute(
                f"""SELECT s.day,s.currency,sum(s.quantity) AS units,
            sum(s.ordered_amount) AS amount,count(DISTINCT s.order_number) AS orders,
            sum(s.quantity) FILTER(WHERE s.status='cancelled') AS cancelled_units
            FROM ozon.v_sales_fbo s WHERE {where} GROUP BY s.day,s.currency ORDER BY s.day""",
                params,
            )
        )
        geography = list(
            conn.execute(
                f"""SELECT coalesce(s.cluster_from,'Не указан') AS origin,
            coalesce(s.cluster_to,'Не указан') AS destination,s.currency,
            sum(s.quantity) AS units,sum(s.ordered_amount) AS amount
            FROM ozon.v_sales_fbo s WHERE {where} GROUP BY 1,2,3 ORDER BY units DESC LIMIT 100""",
                params,
            )
        )
        tops = list(
            conn.execute(
                f"""SELECT s.sku,p.offer_id,p.name,s.currency,
            sum(s.quantity) AS units,sum(s.ordered_amount) AS amount
            FROM ozon.v_sales_fbo s JOIN ozon.product p ON p.client_id=s.client_id AND p.sku=s.sku
            WHERE {where} GROUP BY s.sku,p.offer_id,p.name,s.currency ORDER BY units DESC LIMIT 100""",
                params,
            )
        )
        coverage = [
            r["day"]
            for r in conn.execute(
                "SELECT day FROM ozon.sales_coverage WHERE client_id=%s AND day BETWEEN %s AND %s ORDER BY day",
                (client, date_from, date_to),
            )
        ]
        snapshot = conn.execute(
            "SELECT max(snapshot_date) AS day FROM ozon.published_snapshot WHERE client_id=%s AND snapshot_date<=%s",
            (client, date_to),
        ).fetchone()["day"]
        stocks, stock_summary = stock_rows(
            conn, snapshot, sku, cluster, warehouse, include_pickup
        )
        lookback = (snapshot or day) - timedelta(days=14)
        forecast_coverage = conn.execute(
            "SELECT count(*) AS days FROM ozon.sales_coverage WHERE client_id=%s AND day>=%s AND day<%s",
            (client, lookback, snapshot or day),
        ).fetchone()["days"]
        # Demand keyed by physical warehouse; cluster classification for a
        # forecast follows the stock snapshot's warehouse membership, not a
        # guessed historical numeric cluster identifier.
        demand = list(
            conn.execute(
                """SELECT s.sku,s.warehouse_id,sum(s.quantity) AS units FROM ozon.v_sales_fbo s
            WHERE s.client_id=%s AND s.day>=%s AND s.day<%s AND s.status<>'cancelled' GROUP BY s.sku,s.warehouse_id""",
                (client, lookback, snapshot or day),
            )
        )
        mapping = (
            {
                r["warehouse_id"]: r["cluster_id"]
                for r in conn.execute(
                    """SELECT DISTINCT warehouse_id,cluster_id
            FROM ozon.v_stock_daily WHERE client_id=%s AND snapshot_date=%s""",
                    (client, snapshot),
                )
            }
            if snapshot
            else {}
        )
        totals = {}
        for row in demand:
            key = (
                row["sku"],
                row["warehouse_id"] if warehouse else mapping.get(row["warehouse_id"]),
            )
            totals[key] = totals.get(key, 0) + row["units"]
        for row in stocks:
            key = (row["sku"], row["warehouse_id"] if warehouse else row["cluster_id"])
            if forecast_coverage == 14:
                rate = totals.get(key, 0) / 14
                source = "Заказы FBO за 14 полных дней"
            else:
                rate = row.get("ads_cluster")
                source = (
                    "Средние продажи Ozon за 28 дней"
                    if rate is not None
                    else "Недостаточно истории склада"
                )
            row["forecast"] = forecast(
                row["available_stock_count"],
                rate,
                snapshot,
                settings.lead_days,
                settings.safety_days,
                source,
            )
        storage_params = [client, date_to]
        storage_where = [
            "r.client_id=%s",
            "r.day=(SELECT max(day) FROM ozon.storage_report WHERE client_id=r.client_id AND day<=%s AND kind='supplies' AND status='success')",
            "r.kind='supplies'",
            "r.status='success'",
            "s.supply_id IS NOT NULL",
        ]
        for col, val in [
            ("sku", sku),
            ("cluster_id", cluster),
            ("warehouse_id", warehouse),
        ]:
            if val is not None:
                storage_where.append(f"s.{col}=%s")
                storage_params.append(val)
        storage = list(
            conn.execute(
                """SELECT s.*,r.day AS report_day,p.offer_id AS product_offer_id,p.name,c.name AS cluster_name
            FROM ozon.storage_row s JOIN ozon.storage_report r ON r.id=s.report_id
            LEFT JOIN ozon.product p ON p.client_id=r.client_id AND p.sku=s.sku
            LEFT JOIN ozon.cluster c ON c.cluster_id=s.cluster_id WHERE """
                + " AND ".join(storage_where)
                + " ORDER BY s.free_days_left NULLS LAST,s.sku",
                storage_params,
            )
        )
        for row in storage:
            row.pop("raw", None)
            days = row["free_days_left"]
            row["days_remaining"] = (
                days - (date_to - row["report_day"]).days if days is not None else None
            )
        jobs = list(
            conn.execute("SELECT * FROM ozon.job_run ORDER BY started_at DESC LIMIT 12")
        )
        fee_params = [client, date_from, date_to]
        fee_where = ["r.client_id=%s", "s.row_day BETWEEN %s AND %s"]
        for col, val in [
            ("sku", sku),
            ("cluster_id", cluster),
            ("warehouse_id", warehouse),
        ]:
            if val is not None:
                fee_where.append(f"s.{col}=%s")
                fee_params.append(val)
        storage_fees = list(
            conn.execute(
                """WITH chosen AS (
            SELECT DISTINCT ON(s.row_day) s.row_day,r.id FROM ozon.storage_report r
            JOIN ozon.storage_row s ON s.report_id=r.id
            WHERE r.client_id=%s AND r.kind='products' AND r.status='success'
            AND s.row_day BETWEEN %s AND %s
            ORDER BY s.row_day,r.completed_at DESC,r.id DESC
            ) SELECT s.row_day AS day,
            CASE WHEN count(s.fee)=count(*) THEN sum(s.fee) END AS fee,
            CASE WHEN count(s.paid_quantity)=count(*) THEN sum(s.paid_quantity) END AS paid_units
            FROM chosen ch JOIN ozon.storage_row s ON s.report_id=ch.id AND s.row_day=ch.row_day
            JOIN ozon.storage_report r ON r.id=s.report_id WHERE """
                + " AND ".join(fee_where)
                + " GROUP BY s.row_day ORDER BY s.row_day",
                [client, date_from, date_to] + fee_params,
            )
        )
        reports = list(
            conn.execute(
                "SELECT day,kind,status,error,completed_at FROM ozon.storage_report WHERE client_id=%s ORDER BY day DESC,kind LIMIT 6",
                (client,),
            )
        )
        host = conn.execute(
            "SELECT * FROM ozon.host_metric ORDER BY measured_at DESC LIMIT 1"
        ).fetchone()
        host_history = list(
            conn.execute(
                "SELECT measured_at,memory_percent,disk_percent,cpu_percent FROM ozon.host_metric WHERE measured_at>now()-interval '24 hours' ORDER BY measured_at"
            )
        )
        backup = conn.execute(
            "SELECT * FROM ozon.backup_run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if backup:
            backup.pop("local_path", None)
        options = {
            "products": list(
                conn.execute(
                    "SELECT p.sku,p.offer_id,p.name,p.archived FROM ozon.product p WHERE p.client_id=%s AND (%s OR coalesce(p.archived,false)=false OR EXISTS(SELECT 1 FROM ozon.v_stock_daily s WHERE s.client_id=p.client_id AND s.sku=p.sku AND s.snapshot_date=%s AND (s.available_stock_count>0 OR s.return_to_seller_stock_count>0))) ORDER BY p.offer_id",
                    (client, include_archived, snapshot),
                )
            ),
            "clusters": list(
                conn.execute(
                    "SELECT c.cluster_id AS id,c.name FROM ozon.cluster c WHERE EXISTS(SELECT 1 FROM ozon.v_stock_daily s WHERE s.client_id=%s AND s.cluster_id=c.cluster_id AND s.snapshot_date=%s AND (%s OR s.location_kind<>'pickup')) ORDER BY c.name",
                    (client, snapshot, include_pickup),
                )
            ),
            "warehouses": list(
                conn.execute(
                    "SELECT w.warehouse_id AS id,w.name FROM ozon.warehouse w WHERE (%s OR left(upper(w.name),4)<>'ПВЗ_') AND (EXISTS(SELECT 1 FROM ozon.v_stock_daily s WHERE s.client_id=%s AND s.warehouse_id=w.warehouse_id AND s.snapshot_date=%s) OR EXISTS(SELECT 1 FROM ozon.posting p WHERE p.client_id=%s AND p.warehouse_id=w.warehouse_id AND (p.created_at AT TIME ZONE 'Europe/Moscow')::date BETWEEN %s AND %s)) ORDER BY w.name",
                    (include_pickup, client, snapshot, client, date_from, date_to),
                )
            ),
        }
        calls = conn.execute(
            "SELECT count(*) AS today,count(*) FILTER(WHERE status=429) AS limited FROM ozon.api_call WHERE called_at>=date_trunc('day',now())"
        ).fetchone()
        # Historical daily stock chart, never forward-fill missing snapshots.
        history_params = [client, date_from, date_to]
        history_where = ["s.client_id=%s", "s.snapshot_date BETWEEN %s AND %s"]
        if not include_pickup:
            history_where.append("s.location_kind<>'pickup'")
        for col, val in [
            ("sku", sku),
            ("cluster_id", cluster),
            ("warehouse_id", warehouse),
        ]:
            if val is not None:
                history_where.append(f"s.{col}=%s")
                history_params.append(val)
        history = list(
            conn.execute(
                """SELECT s.snapshot_date AS day,CASE WHEN count(s.available_stock_count)=count(*) THEN sum(s.available_stock_count) END AS units
            FROM ozon.v_stock_daily s WHERE """
                + " AND ".join(history_where)
                + " GROUP BY s.snapshot_date ORDER BY s.snapshot_date",
                history_params,
            )
        )
        return {
            "sales": sales,
            "geography": geography,
            "top_products": tops,
            "stocks": stocks,
            "stock_summary": stock_summary,
            "storage": storage,
            "storage_fees": storage_fees,
            "coverage": coverage,
            "snapshot_date": snapshot,
            "stock_history": history,
            "jobs": jobs,
            "reports": reports,
            "host": host,
            "host_history": host_history,
            "backup": backup,
            "options": options,
            "api_calls": calls,
            "today": day,
            "forecast_days": forecast_coverage,
            "currency": "RUB",
            "api_key_expires_at": expiry,
            "api_key_days_remaining": (expiry.date() - day).days if expiry else None,
        }
