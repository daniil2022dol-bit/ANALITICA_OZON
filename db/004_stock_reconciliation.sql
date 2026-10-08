BEGIN;
-- Preserve classification as returned/derived when each stock row was fetched.
ALTER TABLE ozon.stock_daily ADD COLUMN location_kind text NOT NULL DEFAULT 'unknown'
 CHECK(location_kind IN ('warehouse','pickup','unknown'));
ALTER TABLE ozon.stock_daily ADD COLUMN warehouse_name_snapshot text;
UPDATE ozon.stock_daily s SET
 warehouse_name_snapshot=item->>'warehouse_name',
 location_kind=CASE WHEN item->>'warehouse_name' LIKE 'ПВЗ\_%' ESCAPE '\' THEN 'pickup'
                    WHEN nullif(item->>'warehouse_name','') IS NOT NULL THEN 'warehouse' ELSE 'unknown' END
FROM ozon.api_response r CROSS JOIN LATERAL jsonb_array_elements(r.response_body->'items') item
WHERE r.run_id=s.run_id AND r.endpoint='/v1/analytics/stocks'
 AND (item->>'sku')::bigint=s.sku AND (item->>'warehouse_id')::bigint=s.warehouse_id;
CREATE OR REPLACE VIEW ozon.v_stock_daily AS
SELECT p.snapshot_date,s.* FROM ozon.published_snapshot p JOIN ozon.stock_daily s USING(run_id);

CREATE TABLE ozon.product_info_snapshot (
 client_id bigint NOT NULL REFERENCES ozon.seller_account,
 day date NOT NULL, observed_at timestamptz NOT NULL,
 requested_product_ids bigint[] NOT NULL, response_body jsonb NOT NULL,
 PRIMARY KEY(client_id,day)
);
CREATE TABLE ozon.inventory_daily (
 client_id bigint NOT NULL REFERENCES ozon.seller_account,
 day date NOT NULL, sku bigint NOT NULL, source text NOT NULL,
 product_id bigint, offer_id text, name text, present integer, reserved integer,
 observed_at timestamptz NOT NULL, raw jsonb NOT NULL,
 PRIMARY KEY(client_id,day,sku,source)
);
COMMENT ON TABLE ozon.inventory_daily IS 'Independent product/info/list inventory by SKU and sale schema. present is Сейчас на складе, reserved is Зарезервировано; do not add to analytics/stocks statuses or distribute to clusters.';
ALTER TABLE ozon.api_call ADD COLUMN rate_limit_headers jsonb;
COMMIT;
