BEGIN;
ALTER TABLE ozon.storage_row
 ADD COLUMN volume_ml numeric,
 ADD COLUMN paid_volume_ml numeric,
 ADD COLUMN category text,
 ADD COLUMN product_type text,
 ADD COLUMN item_feature text,
 ADD COLUMN quantity_day date,
 ADD COLUMN stock_quantity numeric;
-- Unknown strings remain NULL; CSV numbers may use spaces and a comma.
CREATE FUNCTION pg_temp.storage_number(value text) RETURNS numeric LANGUAGE SQL AS $$
 SELECT CASE WHEN cleaned ~ '^[+-]?[0-9]+([.][0-9]+)?$' THEN cleaned::numeric END
 FROM (SELECT replace(replace(replace(value,' ',''),chr(160),''),',','.') AS cleaned) x;
$$;
UPDATE ozon.storage_row s SET
 volume_ml=pg_temp.storage_number(s.raw->>'Суммарный объем в миллилитрах'),
 paid_volume_ml=pg_temp.storage_number(s.raw->>'Платный объем в миллилитрах'),
 category=s.raw->>'Категория товара',product_type=s.raw->>'Описательный тип',
 item_feature=s.raw->>'Признак товара',
 quantity_day=CASE WHEN r.kind='products' THEN s.row_day ELSE r.day END,
 stock_quantity=(SELECT pg_temp.storage_number(value) FROM jsonb_each_text(s.raw)
                 WHERE key LIKE 'Остаток на складах на (%)' LIMIT 1)
FROM ozon.storage_report r WHERE r.id=s.report_id;
COMMENT ON COLUMN ozon.storage_row.quantity IS 'Products: physical units in the billing report. Supplies: daily free placement allowance from a supply, not physical remaining stock of that supply.';
COMMENT ON COLUMN ozon.storage_row.quantity_day IS 'Day of the quantity/free allowance observation; do not add observations from different days.';
CREATE VIEW ozon.v_storage_unit_cost AS
SELECT DISTINCT ON(r.client_id,s.row_day,s.sku,s.warehouse_name,coalesce(s.item_feature,''))
 r.client_id,s.report_id,s.row_number,s.row_day,s.sku,s.warehouse_id,s.warehouse_name,s.cluster_id,
 s.category,s.product_type,s.item_feature,s.quantity,s.paid_quantity,
 s.volume_ml,s.paid_volume_ml,s.fee,r.completed_at,
 s.fee/nullif(s.paid_quantity,0) AS observed_rubles_per_unit_day,
 1000*s.fee/nullif(s.paid_volume_ml,0) AS observed_rubles_per_liter_day
FROM ozon.storage_row s JOIN ozon.storage_report r ON r.id=s.report_id
WHERE r.kind='products' AND r.status='success' AND s.row_day IS NOT NULL
ORDER BY r.client_id,s.row_day,s.sku,s.warehouse_name,coalesce(s.item_feature,''),r.completed_at DESC,r.id DESC,s.row_number;
COMMENT ON VIEW ozon.v_storage_unit_cost IS 'Observed unit/liter cost from actual Ozon charges by SKU and warehouse. This is not a future official tariff. Overlapping report versions are not double counted.';
COMMIT;
