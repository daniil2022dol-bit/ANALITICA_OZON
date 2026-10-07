-- Интеграционные проверки схемы. Данные теста откатываются целиком.
-- Запуск: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tests.sql
BEGIN;
DO $$
DECLARE
    first_run bigint;
    failed_run bigint;
    retry_run bigint;
    next_day_run bigint;
    other_seller_run bigint;
BEGIN
    INSERT INTO ozon.seller_account (client_id, name)
    VALUES (-1, 'Тестовый кабинет'), (-2, 'Второй кабинет');
    INSERT INTO ozon.product (client_id, sku, offer_id, name)
    VALUES (-1, 101, 'ARTICLE-1', 'Товар'),
           (-1, 102, 'ARTICLE-1', 'Другой SKU того же артикула'),
           (-2, 101, 'ARTICLE-1', 'Товар второго кабинета');
    INSERT INTO ozon.warehouse (warehouse_id, name)
    VALUES (301, 'Склад 1'), (302, 'Склад 2'), (303, 'Склад для проверки FK');
    INSERT INTO ozon.cluster (cluster_id, name)
    VALUES (201, 'Кластер 1'), (202, 'Кластер 2');
    INSERT INTO ozon.macrolocal_cluster (macrolocal_cluster_id, name)
    VALUES (201, 'Макрокластер с совпадающим ID');

    INSERT INTO ozon.ingest_run
        (client_id, snapshot_date, started_at, requested_skus, expected_batches)
    VALUES (-1, '2026-10-06', '2026-10-06 03:00Z', ARRAY[101]::bigint[], 1)
    RETURNING run_id INTO first_run;

    INSERT INTO ozon.stock_daily
        (run_id, client_id, sku, warehouse_id, cluster_id, macrolocal_cluster_id,
         observed_at, available_stock_count, valid_stock_count,
         expiring_stock_count, ads_cluster, idc_cluster)
    VALUES (first_run, -1, 101, 301, 201, 201, '2026-10-06 03:01Z', 10, 3, 2, 2.5, 12),
           (first_run, -1, 101, 302, 201, 201, '2026-10-06 03:02Z', 20, 4, NULL, 2.5, 12);

    IF EXISTS (SELECT 1 FROM ozon.v_stock_daily WHERE client_id = -1) THEN
        RAISE EXCEPTION 'Partial run leaked into reports';
    END IF;
    BEGIN
        PERFORM ozon.publish_stock_snapshot(first_run);
        RAISE EXCEPTION 'Running run was published';
    EXCEPTION WHEN raise_exception THEN
        IF SQLERRM NOT LIKE 'Run % is not succeeded' THEN RAISE; END IF;
    END;
    BEGIN
        UPDATE ozon.ingest_run
        SET status = 'succeeded', finished_at = '2026-10-06 03:03Z'
        WHERE run_id = first_run;
        RAISE EXCEPTION 'Unvalidated run was accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO ozon.stock_daily (run_id, client_id, sku, warehouse_id, observed_at)
        VALUES (first_run, -1, 101, 301, now());
        RAISE EXCEPTION 'Duplicate warehouse row was accepted';
    EXCEPTION WHEN unique_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO ozon.stock_daily (run_id, client_id, sku, warehouse_id, observed_at)
        VALUES (first_run, -2, 101, 303, now());
        RAISE EXCEPTION 'Cross-seller row was accepted';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;

    UPDATE ozon.ingest_run
    SET status = 'succeeded', finished_at = '2026-10-06 03:03Z',
        validation_passed = true, successful_batches = 1
    WHERE run_id = first_run;
    PERFORM ozon.publish_stock_snapshot(first_run);

    IF (SELECT available_stock_count FROM ozon.v_cluster_stock_daily
        WHERE client_id = -1) IS DISTINCT FROM 30::bigint THEN
        RAISE EXCEPTION 'Warehouse quantities did not sum to cluster quantity';
    END IF;
    IF (SELECT ads_cluster FROM ozon.v_cluster_stock_daily
        WHERE client_id = -1) IS DISTINCT FROM 2.5::double precision THEN
        RAISE EXCEPTION 'Cluster metric was double counted';
    END IF;
    IF (SELECT expiring_stock_count FROM ozon.v_cluster_stock_daily
        WHERE client_id = -1) IS NOT NULL THEN
        RAISE EXCEPTION 'Unknown quantity was converted to an incomplete sum';
    END IF;
    IF (SELECT transit_stock_count FROM ozon.v_stock_daily
        WHERE run_id = first_run AND warehouse_id = 301) IS NOT NULL THEN
        RAISE EXCEPTION 'Missing quantity was converted to zero';
    END IF;

    INSERT INTO ozon.ingest_run
        (client_id, snapshot_date, started_at, requested_skus, expected_batches)
    VALUES (-1, '2026-10-06', '2026-10-06 04:00Z', ARRAY[101]::bigint[], 1)
    RETURNING run_id INTO failed_run;
    INSERT INTO ozon.stock_daily
        (run_id, client_id, sku, warehouse_id, observed_at, available_stock_count)
    VALUES (failed_run, -1, 101, 301, '2026-10-06 04:01Z', 1000);
    UPDATE ozon.ingest_run
    SET status = 'failed', finished_at = '2026-10-06 04:02Z', error_message = 'HTTP failure'
    WHERE run_id = failed_run;
    BEGIN
        INSERT INTO ozon.published_snapshot (client_id, snapshot_date, run_id)
        VALUES (-1, '2026-10-08', failed_run);
        RAISE EXCEPTION 'Failed run or mismatched date was accepted';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
    IF (SELECT available_stock_count FROM ozon.v_cluster_stock_daily
        WHERE client_id = -1) IS DISTINCT FROM 30::bigint THEN
        RAISE EXCEPTION 'Failed retry replaced previous snapshot';
    END IF;

    INSERT INTO ozon.ingest_run
        (client_id, snapshot_date, started_at, finished_at, status,
         requested_skus, expected_batches, successful_batches, validation_passed)
    VALUES (-1, '2026-10-06', '2026-10-06 05:00Z', '2026-10-06 05:02Z',
            'succeeded', ARRAY[101]::bigint[], 1, 1, true)
    RETURNING run_id INTO retry_run;
    INSERT INTO ozon.stock_daily
        (run_id, client_id, sku, warehouse_id, cluster_id, observed_at, available_stock_count)
    VALUES (retry_run, -1, 101, 301, 201, '2026-10-06 05:01Z', 7);
    PERFORM ozon.publish_stock_snapshot(retry_run);
    PERFORM ozon.publish_stock_snapshot(retry_run);
    PERFORM ozon.publish_stock_snapshot(first_run);
    IF (SELECT count(*) FROM ozon.v_stock_daily WHERE client_id = -1) <> 1
       OR (SELECT available_stock_count FROM ozon.v_stock_daily
           WHERE client_id = -1) IS DISTINCT FROM 7 THEN
        RAISE EXCEPTION 'Retry retained stale rows or older run replaced newer run';
    END IF;
    BEGIN
        UPDATE ozon.ingest_run SET status = 'failed' WHERE run_id = retry_run;
        RAISE EXCEPTION 'Published run status was changed';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO ozon.published_snapshot (client_id, snapshot_date, run_id)
        VALUES (-1, '2026-10-09', first_run);
        RAISE EXCEPTION 'Successful run was published with the wrong date';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;

    INSERT INTO ozon.ingest_run
        (client_id, snapshot_date, started_at, finished_at, status,
         requested_skus, expected_batches, successful_batches, validation_passed)
    VALUES (-1, '2026-10-07', '2026-10-07 03:00Z', '2026-10-07 03:03Z',
            'succeeded', ARRAY[101]::bigint[], 1, 1, true)
    RETURNING run_id INTO next_day_run;
    INSERT INTO ozon.stock_daily
        (run_id, client_id, sku, warehouse_id, cluster_id, observed_at,
         available_stock_count, ads_cluster)
    VALUES (next_day_run, -1, 101, 301, 202, '2026-10-07 03:01Z', 5, 2),
           (next_day_run, -1, 101, 302, 202, '2026-10-07 03:02Z', 0, 3);
    PERFORM ozon.publish_stock_snapshot(next_day_run);
    IF (SELECT cluster_id FROM ozon.v_stock_daily
        WHERE client_id = -1 AND snapshot_date = '2026-10-06') IS DISTINCT FROM 201::bigint THEN
        RAISE EXCEPTION 'Warehouse cluster history was overwritten';
    END IF;
    IF (SELECT ads_cluster FROM ozon.v_cluster_stock_daily
        WHERE client_id = -1 AND snapshot_date = '2026-10-07') IS NOT NULL THEN
        RAISE EXCEPTION 'Inconsistent cluster metrics were hidden';
    END IF;

    INSERT INTO ozon.ingest_run
        (client_id, snapshot_date, started_at, finished_at, status,
         requested_skus, expected_batches, successful_batches, validation_passed)
    VALUES (-2, '2026-10-07', '2026-10-07 03:00Z', '2026-10-07 03:03Z',
            'succeeded', ARRAY[101]::bigint[], 1, 1, true)
    RETURNING run_id INTO other_seller_run;
    INSERT INTO ozon.stock_daily
        (run_id, client_id, sku, warehouse_id, cluster_id, observed_at, available_stock_count)
    VALUES (other_seller_run, -2, 101, 301, 202, '2026-10-07 03:01Z', 900);
    PERFORM ozon.publish_stock_snapshot(other_seller_run);
    IF (SELECT available_stock_count FROM ozon.v_cluster_stock_daily
        WHERE client_id = -1 AND snapshot_date = '2026-10-07') IS DISTINCT FROM 5::bigint THEN
        RAISE EXCEPTION 'Seller quantities were mixed';
    END IF;
    INSERT INTO ozon.api_response
        (run_id, request_number, endpoint, request_body, http_status, response_body)
    VALUES (other_seller_run, 1, '/v1/analytics/stocks', '{"skus":["101"]}', 200, '{"items":[]}');
    BEGIN
        INSERT INTO ozon.api_response
            (run_id, request_number, endpoint, request_body, http_status)
        VALUES (other_seller_run, 2, '/v1/analytics/stocks', '{}', 200);
        RAISE EXCEPTION 'Response without content or object URI was accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;
    RAISE NOTICE 'All Ozon schema integration checks passed';
END;
$$;
ROLLBACK;
