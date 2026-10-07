-- Примеры чтения; не изменяют данные. Выполнить после 001_schema.sql.
-- Подставьте фактический client_id, SKU и диапазон дат.

-- 1. Остатки последнего успешного дня по всем кластерам и товарам.
SELECT s.snapshot_date, s.sku, p.offer_id, p.name,
       s.cluster_id, c.name AS cluster_name, s.available_stock_count,
       s.valid_stock_count, s.transit_stock_count, s.ads_cluster, s.idc_cluster
FROM ozon.v_cluster_stock_daily AS s
JOIN ozon.product AS p ON p.client_id = s.client_id AND p.sku = s.sku
LEFT JOIN ozon.cluster AS c ON c.cluster_id = s.cluster_id
WHERE s.client_id = 123456
  AND s.snapshot_date = (
      SELECT max(snapshot_date) FROM ozon.published_snapshot
      WHERE client_id = 123456
  )
ORDER BY s.sku, c.name;

-- 2. История SKU за год. Названия из справочников — текущие;
-- исходные названия на дату выгрузки сохранены в api_response.
SELECT snapshot_date, cluster_id, available_stock_count,
       transit_stock_count, ads_cluster, idc_cluster
FROM ozon.v_cluster_stock_daily
WHERE client_id = 123456 AND sku = 987654321
  AND snapshot_date BETWEEN DATE '2026-10-07' AND DATE '2027-10-06'
ORDER BY snapshot_date, cluster_id;

-- 3. Детализация до складов на выбранную дату.
SELECT s.sku, w.name AS warehouse_name, c.name AS cluster_name,
       s.available_stock_count, s.transit_stock_count, s.observed_at
FROM ozon.v_stock_daily AS s
JOIN ozon.warehouse AS w ON w.warehouse_id = s.warehouse_id
LEFT JOIN ozon.cluster AS c ON c.cluster_id = s.cluster_id
WHERE s.client_id = 123456 AND s.snapshot_date = DATE '2026-10-07'
ORDER BY s.sku, w.name;

-- 4. Дни без успешной выгрузки. Ноль остатков и отсутствие снимка различаются.
SELECT d.day::date AS missing_date
FROM generate_series(DATE '2026-10-01'::timestamp,
                     DATE '2026-10-07'::timestamp, INTERVAL '1 day') AS d(day)
LEFT JOIN ozon.published_snapshot AS s
    ON s.client_id = 123456 AND s.snapshot_date = d.day::date
WHERE s.run_id IS NULL
ORDER BY missing_date;

-- 5. Контроль объёма таблиц и индексов после реальной выгрузки.
SELECT relname AS table_name,
       pg_size_pretty(pg_relation_size(relid)) AS table_size,
       pg_size_pretty(pg_indexes_size(relid)) AS indexes_size,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables
WHERE schemaname = 'ozon'
ORDER BY pg_total_relation_size(relid) DESC;

-- ПОРЯДОК ЗАГРУЗКИ (для будущего загрузчика):
-- 1. Обновить справочники, включая архивные SKU.
-- 2. Создать ingest_run с фиксированным requested_skus и expected_batches.
-- 3. Запросить /v1/analytics/stocks без ограничивающих фильтров по кластерам,
--    ликвидности, тегам и складам. Сохранить исходные ответы и stock_daily.
--    Для повторного HTTP-запроса использовать тот же номер логической пачки
--    в коде загрузчика; успешные пачки считать только один раз.
--    API не гарантирует строки для всех сочетаний SKU и складов:
--    не создавайте искусственные нулевые остатки для отсутствующих строк.
-- 4. Проверить успешность и покрытие пачек, ключи (SKU, склад), данные.
--    При дублях (SKU, склад) в ответе остановить загрузку для проверки
--    фактической детализации API, не суммировать дубли автоматически.
-- 5. В одной транзакции завершить запуск и опубликовать его:
--
-- BEGIN;
-- UPDATE ozon.ingest_run
-- SET status = 'succeeded', finished_at = now(),
--     successful_batches = expected_batches, validation_passed = true
-- WHERE run_id = :run_id AND status = 'running';
-- SELECT ozon.publish_stock_snapshot(:run_id);
-- COMMIT;
--
-- validation_passed ставить только после проверок; сама БД не проверяет API.
-- При сбое: status='failed', finished_at=now(), error_message=...;
-- опубликованный дневной снимок остаётся прежним.
--
-- ХРАНЕНИЕ:
-- Дневные снимки хранить минимум 365 дней. Исходные ответы — тот же период,
-- при необходимости переносить response_body в сжатые файлы/object_uri.
-- Неактуальные успешные попытки и неудачные попытки можно удалять раньше
-- транзакционно: api_response, stock_daily, затем ingest_run.
-- FK запрещает удаление ingest_run, на который ссылается published_snapshot.
-- Удаление истории/расписание очистки эта схема автоматически не запускает.
