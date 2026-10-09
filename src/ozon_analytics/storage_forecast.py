"""Placement forecasts from actual SKU/warehouse charges and free allowances.

The supplies report describes allowances, not physical lot inventory. Keep the
allowance pool global to a SKU, then estimate warehouse allocation from the
observed free stock. Never price a physical unit twice or invent an unknown fee.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from .config import settings
from .database import connect
from .storage import normalize, number

ZERO = Decimal(0)
CENT = Decimal("0.01")


def amount(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def cohort(row):
    return tuple(
        normalize(row.get(k)) for k in ("category", "product_type", "item_feature")
    )


def rate_from(evidence):
    """Recover a cent-denominated rate only when it reproduces every charge."""
    evidence = [
        r
        for r in evidence
        if number(r.get("paid_volume_ml")) is not None
        and number(r["paid_volume_ml"]) > 0
        and number(r.get("fee")) is not None
    ]
    volume = sum(number(r.get("paid_volume_ml")) or ZERO for r in evidence)
    fee = sum(number(r.get("fee")) or ZERO for r in evidence)
    if volume <= 0:
        return None
    observed = fee * 1000 / volume
    rounded = amount(observed)
    if all(
        amount(number(r["paid_volume_ml"]) * rounded / 1000) == number(r["fee"])
        for r in evidence
    ):
        return rounded
    # Different prices/discounts cannot be averaged into an invented tariff.
    half_cent = CENT / 2
    lower = max(
        max(ZERO, (number(r["fee"]) - half_cent) * 1000 / number(r["paid_volume_ml"]))
        for r in evidence
    )
    upper = min(
        (number(r["fee"]) + half_cent) * 1000 / number(r["paid_volume_ml"])
        for r in evidence
    )
    return min(upper, max(lower, observed)) if lower < upper else None


def unit_price(row, observations):
    paid, fee = number(row.get("paid_quantity")), number(row.get("fee"))
    if paid is not None and paid > 0 and fee is not None and fee >= 0:
        return fee / paid, "observed", row["row_day"]
    volume, quantity = number(row.get("volume_ml")), number(row.get("quantity"))
    matching = [
        r
        for r in observations
        if r["sku"] == row["sku"]
        and normalize(r["warehouse_name"]) == normalize(row["warehouse_name"])
        and cohort(r) == cohort(row)
    ]
    if matching:
        latest = max(r["row_day"] for r in matching)
        matching = [r for r in matching if r["row_day"] == latest]
        if volume is not None and quantity and quantity > 0:
            rate = rate_from(matching)
            if rate is not None:
                return volume / quantity / 1000 * rate, "sku_warehouse_history", latest
        paid = sum(number(r.get("paid_quantity")) or ZERO for r in matching)
        if paid > 0:
            return (
                sum(number(r.get("fee")) or ZERO for r in matching) / paid,
                "sku_warehouse_history",
                latest,
            )
    if volume is None or not quantity or quantity <= 0 or not row.get("product_type"):
        return None, "unknown", None
    same_type = [r for r in observations if cohort(r) == cohort(row)]
    same_warehouse = [
        r
        for r in same_type
        if normalize(r["warehouse_name"]) == normalize(row["warehouse_name"])
    ]
    candidates = same_warehouse or same_type
    if not candidates:
        return None, "unknown", None
    latest = max(r["row_day"] for r in candidates)
    candidates = [r for r in candidates if r["row_day"] == latest]
    rate = rate_from(candidates)
    if rate is None:
        return None, "unknown", None
    return (
        volume / quantity / 1000 * rate,
        "warehouse_type" if same_warehouse else "type_estimate",
        latest,
    )


def allowances(rows, free_units, baseline):
    """Consume spare allowance before increasing paid physical inventory."""
    seen, dated, unknown, incomplete = {}, [], ZERO, False
    for row in rows:
        key = (
            row["supply_id"],
            normalize(row.get("warehouse_name")),
            row.get("free_until"),
            cohort(row),
        )
        signature = (row.get("quantity"), row.get("free_until"))
        if key in seen:
            incomplete |= seen[key] != signature
            continue
        seen[key] = signature
        qty = number(row.get("quantity"))
        if qty is None or qty < 0:
            incomplete = True
            continue
        if row.get("free_until") is None:
            unknown += qty
        elif row["free_until"] >= baseline:
            dated.append((row["free_until"] + timedelta(days=1), qty))
    if incomplete:
        return [], free_units
    capacity = sum(q for _, q in dated) + unknown
    spare = max(ZERO, capacity - free_units)
    remaining, events = free_units, []
    for start, qty in sorted(dated):
        unused = min(spare, qty)
        spare -= unused
        covered = min(remaining, qty - unused)
        remaining -= covered
        if covered:
            events.append((start, covered))
    return events, remaining


def build_projection(
    products, supplies, observations, as_of, *, sku=None, cluster=None, warehouse=None
):
    if not products:
        return {
            "as_of": as_of,
            "actual": None,
            "timeline": [],
            "rows": [],
            "batches": [],
            "quality": {},
        }
    baseline = max(r["row_day"] for r in products)
    products = [r for r in products if r["row_day"] == baseline]
    observations = [
        r
        for r in observations
        if r["row_day"] <= baseline
        and (number(r.get("paid_quantity")) or ZERO) > 0
        and number(r.get("fee")) is not None
        and number(r["fee"]) >= 0
    ]
    by_sku, quota_by_sku = defaultdict(list), defaultdict(list)
    for r in supplies:
        if r.get("supply_id"):
            quota_by_sku[r["sku"]].append(r)
    for original in products:
        row = dict(original)
        row.pop("raw", None)
        row["quantity"] = number(row.get("quantity"))
        row["paid_quantity"] = number(row.get("paid_quantity"))
        row["fee"] = number(row.get("fee"))
        q, p = row["quantity"], row["paid_quantity"]
        row["free_quantity"] = (
            q - p if q is not None and p is not None and ZERO <= p <= q else None
        )
        row["unit_price"], row["rate_source"], row["rate_day"] = unit_price(
            row, observations
        )
        volume = number(row.get("volume_ml"))
        row["unit_liters"] = (
            volume / q / 1000 if q and q > 0 and volume is not None else None
        )
        by_sku[row["sku"]].append(row)
    chosen, pools = [], {}
    for product_sku, rows in by_sku.items():
        free = sum(r["free_quantity"] or ZERO for r in rows)
        events, unknown = allowances(quota_by_sku[product_sku], free, baseline)
        pools[product_sku] = (free, events, unknown)
        for row in rows:
            if sku is not None and row["sku"] != sku:
                continue
            if cluster is not None and row.get("cluster_id") != cluster:
                continue
            if warehouse is not None and row.get("warehouse_id") != warehouse:
                continue
            chosen.append(row)
    quality = {
        "estimated_rate_units": sum(
            r["quantity"] or ZERO
            for r in chosen
            if r["rate_source"]
            in ("warehouse_type", "type_estimate", "sku_warehouse_history")
        ),
        "other_warehouse_rate_units": sum(
            r["quantity"] or ZERO for r in chosen if r["rate_source"] == "type_estimate"
        ),
        "missing_rate_units": sum(
            r["quantity"] or ZERO for r in chosen if r["unit_price"] is None
        ),
        "unknown_deadline_units": ZERO,
        "missing_quantity_rows": sum(
            r["quantity"] is None or r["free_quantity"] is None for r in chosen
        ),
    }
    for row in chosen:
        free, events, unknown = pools[row["sku"]]
        row["unknown_deadline_units"] = (
            unknown * (row["free_quantity"] or ZERO) / free if free else ZERO
        )
        quality["unknown_deadline_units"] += row["unknown_deadline_units"]
        row["first_cost_increase"] = next(
            (
                start
                for start, q in events
                if start > as_of and q > 0 and row["free_quantity"]
            ),
            None,
        )
        row["_events"] = events
        row["_pool_free"] = free

    def predict_row(row, target):
        free = row["_pool_free"]
        share = (row["free_quantity"] or ZERO) / free if free else ZERO
        extra = sum(q for start, q in row["_events"] if start <= target) * share
        unknown = row["unknown_deadline_units"] if target > baseline else ZERO
        low = row["fee"] or ZERO
        high = low
        unpriced = row["fee"] is None or row["free_quantity"] is None
        if row["unit_price"] is not None:
            low += extra * row["unit_price"]
            high = low + unknown * row["unit_price"]
        elif extra > 0 or unknown > 0:
            unpriced = True
        return {
            "day": target,
            "low": amount(low),
            "high": None if unpriced else amount(high),
            "paid_units": (row["paid_quantity"] or ZERO) + extra,
        }

    timeline = []
    for offset in range(31):
        target = as_of + timedelta(days=offset)
        points = [predict_row(r, target) for r in chosen]
        low = sum((p["low"] for p in points), ZERO)
        high = (
            sum((p["high"] for p in points), ZERO)
            if all(p["high"] is not None for p in points)
            else None
        )
        timeline.append(
            {
                "day": target,
                "low": low,
                "high": high,
                "paid_units": sum((p["paid_units"] for p in points), ZERO),
            }
        )
    for row in chosen:
        row["tomorrow"] = predict_row(row, as_of + timedelta(days=1))
        row["week"] = predict_row(row, as_of + timedelta(days=7))
        row["month"] = predict_row(row, as_of + timedelta(days=30))
        row.pop("_events")
        row.pop("_pool_free")
    selected_skus = {r["sku"] for r in chosen}
    batches = []
    seen = set()
    for original in supplies:
        if original["sku"] not in selected_skus or not original.get("supply_id"):
            continue
        row = dict(original)
        row.pop("raw", None)
        key = (
            row["sku"],
            row["supply_id"],
            normalize(row.get("warehouse_name")),
            row.get("free_until"),
            cohort(row),
        )
        if key in seen:
            continue
        seen.add(key)
        row["paid_start"] = (
            row["free_until"] + timedelta(days=1) if row.get("free_until") else None
        )
        row["days_remaining"] = (
            (row["free_until"] - as_of).days + 1 if row.get("free_until") else None
        )
        batches.append(row)
    batches.sort(
        key=lambda r: (
            r["paid_start"] is None,
            str(r["paid_start"]),
            str(r.get("product_offer_id") or r["sku"]),
        )
    )
    actual_fees = [r["fee"] for r in chosen]
    actual = {
        "day": baseline,
        "fee": sum(actual_fees, ZERO)
        if all(f is not None for f in actual_fees)
        else None,
        "known_fee": sum((f or ZERO for f in actual_fees), ZERO),
        "units": sum((r["quantity"] or ZERO for r in chosen), ZERO),
        "paid_units": sum((r["paid_quantity"] or ZERO for r in chosen), ZERO),
    }

    def cumulative(days):
        points = timeline[1 : days + 1]
        return {
            "low": sum((p["low"] for p in points), ZERO),
            "high": sum((p["high"] for p in points), ZERO)
            if all(p["high"] is not None for p in points)
            else None,
        }

    return {
        "as_of": as_of,
        "actual": actual,
        "timeline": timeline,
        "rows": sorted(
            chosen,
            key=lambda r: (
                str(r.get("product_offer_id") or r.get("offer_id") or r["sku"]),
                r["warehouse_name"],
            ),
        ),
        "batches": batches,
        "quality": quality,
        "totals": {"week": cumulative(7), "month": cumulative(30)},
    }


def storage_projection(as_of, sku=None, cluster=None, warehouse=None):
    # Restrict report versions BEFORE choosing their latest rows. A newer
    # report that repeats yesterday must not erase yesterday's historical view.
    with connect(web=True) as conn:
        observed = list(
            conn.execute(
                """
            WITH eligible AS (
              SELECT DISTINCT ON(s.row_day,s.sku,s.warehouse_name,coalesce(s.item_feature,''))
                s.*,r.client_id,r.completed_at
              FROM ozon.storage_row s JOIN ozon.storage_report r ON r.id=s.report_id
              WHERE r.client_id=%s AND r.kind='products' AND r.status='success'
                AND r.day<=%s AND s.row_day<=%s AND s.row_day>=%s
              ORDER BY s.row_day,s.sku,s.warehouse_name,coalesce(s.item_feature,''),
                r.completed_at DESC,r.id DESC,s.row_number
            )
            SELECT s.*,p.offer_id AS product_offer_id,p.name,c.name AS cluster_name
            FROM eligible s LEFT JOIN ozon.product p USING(client_id,sku)
            LEFT JOIN ozon.cluster c ON c.cluster_id=s.cluster_id
            ORDER BY s.row_day,s.sku,s.warehouse_name
        """,
                (settings.client_id, as_of, as_of, as_of - timedelta(days=90)),
            )
        )
        baseline = max((r["row_day"] for r in observed), default=None)
        products = [r for r in observed if r["row_day"] == baseline]
        observations = [
            r
            for r in observed
            if baseline and r["row_day"] >= baseline - timedelta(days=30)
        ]
        quota_report = conn.execute(
            """
            SELECT id,day,completed_at FROM ozon.storage_report
            WHERE client_id=%s AND kind='supplies' AND status='success' AND day<=%s
            ORDER BY day DESC,completed_at DESC,id DESC LIMIT 1
        """,
            (settings.client_id, baseline or as_of),
        ).fetchone()
        supplies = (
            list(
                conn.execute(
                    """
            SELECT s.*,p.offer_id AS product_offer_id,p.name
            FROM ozon.storage_row s JOIN ozon.storage_report r ON r.id=s.report_id
            LEFT JOIN ozon.product p ON p.client_id=r.client_id AND p.sku=s.sku
            WHERE s.report_id=%s
        """,
                    (quota_report["id"],),
                )
            )
            if quota_report
            else []
        )
    result = build_projection(
        products,
        supplies,
        observations,
        as_of,
        sku=sku,
        cluster=cluster,
        warehouse=warehouse,
    )
    result["supplies_report"] = quota_report
    result["assumptions"] = {
        "stock": "constant",
        "tariff": "unchanged",
        "sales": False,
        "free_until_inclusive": True,
        "warehouse_allocation": "proportional_current_free_stock",
    }
    return result
