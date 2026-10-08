"""Stock status totals, independent inventory, and faithful API source downloads."""

from .config import settings
from .database import connect

QUANTITIES = (
    "available_stock_count",
    "valid_stock_count",
    "transit_stock_count",
    "requested_stock_count",
    "inbound_replenishment",
    "other_stock_count",
    "outbound_pending_delivery",
    "return_to_seller_stock_count",
    "outbound_returns_picking",
    "return_from_customer_stock_count",
    "stock_not_being_sold",
    "outbound_returns_ready_to_ship",
    "outbound_returns_return_to_seller",
    "stock_defect_stock_count",
    "transit_defect_stock_count",
    "waiting_docs_stock_count",
    "waiting_docs_to_export_stock_count",
    "excess_stock_count",
    "expiring_stock_count",
)


def sums(alias="s"):
    return ",".join(
        f"CASE WHEN count({alias}.{key})=count(*) THEN sum({alias}.{key}) END AS {key}"
        for key in QUANTITIES
    )


def stock_rows(conn, snapshot, sku, cluster, warehouse, include_pickup):
    where = ["s.client_id=%s", "s.snapshot_date=%s"]
    params = [settings.client_id, snapshot]
    for key, value in [
        ("sku", sku),
        ("cluster_id", cluster),
        ("warehouse_id", warehouse),
    ]:
        if value is not None:
            where.append("s." + key + "=%s")
            params.append(value)
    if not include_pickup:
        where.append("s.location_kind<>'pickup'")
    predicate = " AND ".join(where)
    joins = "FROM ozon.v_stock_daily s JOIN ozon.product p ON p.client_id=s.client_id AND p.sku=s.sku LEFT JOIN ozon.cluster c USING(cluster_id) JOIN ozon.warehouse w USING(warehouse_id)"
    if warehouse is not None:
        q = f"SELECT s.sku,s.warehouse_id,s.cluster_id,s.macrolocal_cluster_id,{','.join('s.' + x for x in QUANTITIES)},NULL::double precision AS ads_cluster,coalesce(s.warehouse_name_snapshot,w.name) AS warehouse_name,c.name AS cluster_name,p.offer_id,p.name {joins} WHERE {predicate} ORDER BY p.offer_id,s.cluster_id"
    else:
        q = f"SELECT s.sku,NULL::bigint AS warehouse_id,s.cluster_id,{sums()},CASE WHEN count(s.ads_cluster)=count(*) AND count(DISTINCT s.ads_cluster)=1 THEN max(s.ads_cluster) END AS ads_cluster,c.name AS cluster_name,p.offer_id,p.name {joins} WHERE {predicate} GROUP BY s.sku,s.cluster_id,p.offer_id,p.name,c.name ORDER BY p.offer_id,s.cluster_id"
    rows = list(conn.execute(q, params)) if snapshot else []
    # Reconciliation always retains pickup rows, even when hidden in normal cards.
    base_where = [v for v in where if v != "s.location_kind<>'pickup'"]
    breakdown = (
        list(
            conn.execute(
                f"SELECT s.location_kind,count(*) AS rows,count(DISTINCT s.warehouse_id) AS locations,{sums()} FROM ozon.v_stock_daily s WHERE {' AND '.join(base_where)} GROUP BY s.location_kind ORDER BY s.location_kind",
                params,
            )
        )
        if snapshot
        else []
    )
    inventory = None
    if cluster is None and warehouse is None:
        info_day = (
            conn.execute(
                "SELECT max(day) AS day FROM ozon.product_info_snapshot WHERE client_id=%s AND day<=%s",
                (settings.client_id, snapshot),
            ).fetchone()["day"]
            if snapshot
            else None
        )
        inv_params = [settings.client_id, info_day]
        inv_where = "client_id=%s AND day=%s AND source='fbo'"
        if sku is not None:
            inv_where += " AND sku=%s"
            inv_params.append(sku)
        inventory = conn.execute(
            f"SELECT count(*) AS rows,CASE WHEN count(present)=count(*) AND count(*)>0 THEN sum(present) END AS present,CASE WHEN count(reserved)=count(*) AND count(*)>0 THEN sum(reserved) END AS reserved,max(observed_at) AS observed_at FROM ozon.inventory_daily WHERE {inv_where}",
            inv_params,
        ).fetchone()
        inventory["day"] = info_day
    return rows, {"locations": breakdown, "inventory": inventory}


def stock_source(day, sku=None, warehouse=None, kind="analytics"):
    with connect(web=True) as conn:
        if kind == "catalog":
            record = conn.execute(
                "SELECT day,observed_at,requested_product_ids,response_body FROM ozon.product_info_snapshot WHERE client_id=%s AND day=%s",
                (settings.client_id, day),
            ).fetchone()
            if not record:
                return None
            data = record.pop("response_body")
            if sku is not None:
                data = dict(
                    data,
                    items=[
                        p
                        for p in data["items"]
                        if p.get("sku") == sku
                        or any(
                            s.get("sku") == sku
                            for s in p.get("stocks", {}).get("stocks", [])
                        )
                    ],
                )
            return {
                "endpoint": "/v3/product/info/list",
                "snapshot": record,
                "response": data,
            }
        run = conn.execute(
            "SELECT r.run_id,r.snapshot_date,r.started_at,r.finished_at,r.requested_skus FROM ozon.published_snapshot p JOIN ozon.ingest_run r USING(run_id) WHERE p.client_id=%s AND p.snapshot_date=%s",
            (settings.client_id, day),
        ).fetchone()
        if not run:
            return None
        responses = []
        for record in conn.execute(
            "SELECT request_body,response_body,received_at FROM ozon.api_response WHERE run_id=%s ORDER BY request_number",
            (run["run_id"],),
        ):
            items = record["response_body"]["items"]
            selected = [
                p
                for p in items
                if (sku is None or p.get("sku") == sku)
                and (warehouse is None or p.get("warehouse_id") == warehouse)
            ]
            responses.append(
                {
                    "request": record["request_body"],
                    "received_at": record["received_at"],
                    "original_items": len(items),
                    "response": dict(record["response_body"], items=selected),
                    "filtered": sku is not None or warehouse is not None,
                }
            )
        return {
            "endpoint": "/v1/analytics/stocks",
            "snapshot": run,
            "responses": responses,
        }
