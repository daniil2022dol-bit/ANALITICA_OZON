from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from ozon_analytics import collector, web
from ozon_analytics.api import APIError
from ozon_analytics.database import migrate
from ozon_analytics.queries import dashboard
from ozon_analytics.stock_queries import stock_source


class StockAPI:
    def __init__(self):
        self.calls = []
        self.info = {
            "items": [
                {
                    "id": 100 + sku,
                    "sku": sku,
                    "offer_id": f"ITEM-{sku}",
                    "name": f"Product {sku}",
                    "is_archived": sku in (2, 4),
                    "stocks": {
                        "stocks": [
                            {
                                "sku": sku,
                                "source": "fbo",
                                "present": {1: 137, 4: 5}.get(sku, 0),
                                "reserved": 2 if sku == 1 else 0,
                            }
                        ]
                    },
                }
                for sku in range(1, 5)
            ],
        }
        self.info["items"][0]["stocks"]["stocks"].append(
            {"sku": 9, "source": "fbs", "present": 0, "reserved": 0}
        )
        self.rows = [
            self.row(1, 10, "ТВЕРЬ", 100, 30),
            self.row(1, -11, "ПВЗ_11", 7, 0),
            self.row(4, 10, "ТВЕРЬ", 5, 0),
        ]

    @staticmethod
    def row(sku, warehouse, name, available, withdrawn):
        return {
            "sku": sku,
            "offer_id": f"ITEM-{sku}",
            "warehouse_id": warehouse,
            "warehouse_name": name,
            "cluster_id": 20,
            "cluster_name": "Центр",
            "available_stock_count": available,
            "return_to_seller_stock_count": withdrawn,
            "outbound_returns_picking": withdrawn,
            "item_tags": ["FBS_RETURN"] if warehouse < 0 else [],
            "ads_cluster": 2,
        }

    def post(self, endpoint, body):
        self.calls.append((endpoint, body))
        if endpoint == "/v2/cluster/list":
            return {"result": []}
        if endpoint == "/v3/product/list":
            skus = (1, 3) if body["filter"]["visibility"] == "ALL" else (2, 4)
            return {
                "result": {
                    "items": [
                        {"sku": sku, "product_id": 100 + sku, "offer_id": f"ITEM-{sku}"}
                        for sku in skus
                    ]
                }
            }
        if endpoint == "/v3/product/info/list":
            assert body == {"product_id": [101, 102, 103, 104]}
            return self.info
        if endpoint == "/v1/analytics/stocks":
            assert body == {"skus": ["1", "2", "3", "4"]}
            return {"items": self.rows, "marker": "preserved"}
        raise AssertionError(endpoint)


def test_fbo_statuses_pickup_and_archive_are_separate(db):
    api = StockAPI()
    collector.collect_stocks(api)
    collector.collect_stocks(api)
    assert len(api.calls) == 5  # Includes exactly one metadata call, no repeat.
    day = collector.today()
    with db() as conn:
        conn.execute("INSERT INTO ozon.warehouse VALUES(777,'Чужой склад',now())")
        assert (
            conn.execute("SELECT count(*) n FROM ozon.stock_daily").fetchone()["n"] == 3
        )
        assert (
            conn.execute(
                "SELECT count(*) n FROM ozon.product WHERE name IS NULL"
            ).fetchone()["n"]
            == 0
        )
    data = dashboard(day, day)
    assert sum(r["available_stock_count"] for r in data["stocks"]) == 105
    assert sum(r["return_to_seller_stock_count"] for r in data["stocks"]) == 30
    assert sum(r["outbound_returns_picking"] for r in data["stocks"]) == 30
    assert data["stock_summary"]["inventory"]["present"] == 142
    assert data["stock_summary"]["inventory"]["reserved"] == 2
    assert data["stock_history"][0]["units"] == 105
    assert data["stock_summary"]["observed_at"] is not None
    assert {p["sku"] for p in data["options"]["products"]} == {1, 3, 4}
    assert {w["id"] for w in data["options"]["warehouses"]} == {10}
    # Unknown statuses remain unknown; the repeated withdrawal stage is not added.
    assert all(r["other_stock_count"] is None for r in data["stocks"])
    included = dashboard(day, day, include_pickup=True, include_archived=True)
    assert sum(r["available_stock_count"] for r in included["stocks"]) == 112
    assert included["stock_history"][0]["units"] == 112
    assert {p["sku"] for p in included["options"]["products"]} == {1, 2, 3, 4}
    assert {w["id"] for w in included["options"]["warehouses"]} == {10, -11}
    scoped = dashboard(day, day, sku=1, warehouse=10)
    assert scoped["stocks"][0]["available_stock_count"] == 100
    assert (
        scoped["stock_summary"]["inventory"] is None
    )  # No guessed cluster allocation.
    assert not dashboard(day, day, warehouse=-11)["stocks"]
    assert (
        dashboard(day, day, warehouse=-11, include_pickup=True)["stocks"][0][
            "available_stock_count"
        ]
        == 7
    )
    assert dashboard(day, day, sku=3)["stock_summary"]["inventory"]["present"] == 0


@pytest.mark.parametrize(
    "invalid", ["incomplete", "duplicate", "nameless", "duplicate_inventory"]
)
def test_invalid_metadata_does_not_replace_valid_inventory(db, invalid):
    api = StockAPI()
    observed = datetime.now(UTC)
    collector.save_product_info(api.info, [101, 102, 103, 104], observed)
    broken = deepcopy(api.info)
    if invalid == "incomplete":
        broken["items"].pop()
    elif invalid == "duplicate":
        broken["items"][-1] = deepcopy(broken["items"][0])
    elif invalid == "nameless":
        broken["items"][0]["name"] = None
    else:
        broken["items"][0]["stocks"]["stocks"].append(
            deepcopy(broken["items"][0]["stocks"]["stocks"][0])
        )
    with pytest.raises(APIError):
        collector.save_product_info(broken, [101, 102, 103, 104], observed)
    with db() as conn:
        assert (
            conn.execute("SELECT count(*) n FROM ozon.inventory_daily").fetchone()["n"]
            == 5
        )
        assert (
            conn.execute(
                "SELECT response_body FROM ozon.product_info_snapshot"
            ).fetchone()["response_body"]
            == api.info
        )


def test_inventory_null_is_not_zero_and_source_has_exact_date(db):
    api = StockAPI()
    del api.info["items"][0]["stocks"]["stocks"][0]["present"]
    collector.collect_stocks(api)
    day = collector.today()
    assert dashboard(day, day)["stock_summary"]["inventory"]["present"] is None
    assert stock_source(day - timedelta(days=1)) is None
    source = stock_source(day)
    assert source["responses"][0]["response"] == {
        "items": api.rows,
        "marker": "preserved",
    }
    selected = stock_source(day, sku=1, warehouse=-11)
    assert selected["responses"][0]["response"]["items"] == [api.rows[1]]
    assert selected["responses"][0]["filtered"] is True
    assert stock_source(day, sku=1, kind="catalog")["response"]["items"] == [
        api.info["items"][0]
    ]


def test_saved_sources_require_login_and_download_without_network(db):
    collector.collect_stocks(StockAPI())
    day = collector.today()
    with TestClient(web.app, base_url="https://testserver") as client:
        path = f"/api/stocks/source?day={day}"
        assert client.get(path).status_code == 401
        client.cookies.set("ozon_session", web.create_session())
        response = client.get(path)
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert len(response.json()["responses"][0]["response"]["items"]) == 3
        assert client.get(path + "&kind=catalog").status_code == 200
        assert client.get(path + "&kind=bad").status_code == 422
        assert (
            client.get(f"/api/stocks/source?day={day - timedelta(days=1)}").status_code
            == 404
        )


def test_migration_classifies_old_snapshots_without_changing_quantities(db):
    # Recreate the deployed three-migration schema and its published rows.
    with db() as conn:
        conn.execute("DROP SCHEMA ozon CASCADE")
        conn.execute(
            "DELETE FROM public.schema_migration WHERE name >= '004_'"
        )
        for path in sorted(Path("db").glob("00[123]_*.sql")):
            conn.execute(
                "\n".join(
                    line
                    for line in path.read_text().splitlines()
                    if line.strip().upper() not in ("BEGIN;", "COMMIT;")
                )
            )
        conn.execute(
            "INSERT INTO ozon.seller_account(client_id,name) VALUES(123,'Test')"
        )
        run = conn.execute(
            "INSERT INTO ozon.ingest_run(client_id,snapshot_date,requested_skus,expected_batches,successful_batches,status,validation_passed,finished_at) VALUES(123,%s,ARRAY[1],1,1,'succeeded',true,now()) RETURNING run_id",
            (collector.today(),),
        ).fetchone()["run_id"]
        conn.execute(
            "INSERT INTO ozon.product(client_id,sku,offer_id) VALUES(123,1,'ITEM-1')"
        )
        rows = StockAPI().rows[:2]
        conn.execute(
            "INSERT INTO ozon.api_response(run_id,request_number,endpoint,request_body,http_status,response_body) VALUES(%s,1,'/v1/analytics/stocks','{}',200,%s)",
            (run, Jsonb({"items": rows})),
        )
        for row in rows:
            conn.execute(
                "INSERT INTO ozon.warehouse(warehouse_id,name) VALUES(%s,'Renamed now')",
                (row["warehouse_id"],),
            )
            conn.execute(
                "INSERT INTO ozon.stock_daily(run_id,client_id,sku,warehouse_id,observed_at,available_stock_count) VALUES(%s,123,1,%s,now(),%s)",
                (run, row["warehouse_id"], row["available_stock_count"]),
            )
        conn.execute("SELECT ozon.publish_stock_snapshot(%s)", (run,))
    migrate()
    with db() as conn:
        saved = list(
            conn.execute(
                "SELECT location_kind,warehouse_name_snapshot,available_stock_count FROM ozon.v_stock_daily ORDER BY warehouse_id"
            )
        )
        assert saved == [
            {
                "location_kind": "pickup",
                "warehouse_name_snapshot": "ПВЗ_11",
                "available_stock_count": 7,
            },
            {
                "location_kind": "warehouse",
                "warehouse_name_snapshot": "ТВЕРЬ",
                "available_stock_count": 100,
            },
        ]
        assert (
            conn.execute("SELECT run_id FROM ozon.published_snapshot").fetchone()[
                "run_id"
            ]
            == run
        )
