from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings
from .database import connect
from .forecast import forecast


def dashboard(date_from, date_to, sku=None, cluster=None, warehouse=None):
    client = settings.client_id
    day = datetime.now(ZoneInfo("Europe/Moscow")).date()
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
        if warehouse:
            stock_sql = """SELECT s.sku,s.warehouse_id,s.cluster_id,s.macrolocal_cluster_id,s.available_stock_count,
               s.transit_stock_count,NULL::double precision AS ads_cluster,w.name AS warehouse_name,
               c.name AS cluster_name,p.offer_id,p.name
               FROM ozon.v_stock_daily s JOIN ozon.warehouse w USING(warehouse_id)
               JOIN ozon.product p ON p.sku=s.sku AND p.client_id=s.client_id
               LEFT JOIN ozon.cluster c USING(cluster_id)"""
        else:
            stock_sql = """SELECT s.sku,NULL::bigint AS warehouse_id,s.cluster_id,
               s.available_stock_count,s.transit_stock_count,s.ads_cluster,
               c.name AS cluster_name,p.offer_id,p.name
               FROM ozon.v_cluster_stock_daily s JOIN ozon.product p ON p.sku=s.sku AND p.client_id=s.client_id
               LEFT JOIN ozon.cluster c USING(cluster_id)"""
        stock_params = [client, snapshot]
        stock_where = ["s.client_id=%s", "s.snapshot_date=%s"]
        for col, val in [
            ("sku", sku),
            ("cluster_id", cluster),
            ("warehouse_id", warehouse),
        ]:
            if val is not None:
                stock_where.append(f"s.{col}=%s")
                stock_params.append(val)
        stocks = (
            list(
                conn.execute(
                    stock_sql
                    + " WHERE "
                    + " AND ".join(stock_where)
                    + " ORDER BY p.offer_id,s.cluster_id",
                    stock_params,
                )
            )
            if snapshot
            else []
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
                    "SELECT sku,offer_id,name FROM ozon.product WHERE client_id=%s ORDER BY offer_id",
                    (client,),
                )
            ),
            "clusters": list(
                conn.execute(
                    "SELECT cluster_id AS id,name FROM ozon.cluster ORDER BY name"
                )
            ),
            "warehouses": list(
                conn.execute(
                    "SELECT warehouse_id AS id,name FROM ozon.warehouse ORDER BY name"
                )
            ),
        }
        calls = conn.execute(
            "SELECT count(*) AS today,count(*) FILTER(WHERE status=429) AS limited FROM ozon.api_call WHERE called_at>=date_trunc('day',now())"
        ).fetchone()
        # Historical daily stock chart, never forward-fill missing snapshots.
        history_params = [client, date_from, date_to]
        history_where = ["s.client_id=%s", "s.snapshot_date BETWEEN %s AND %s"]
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
        }
