-- Ozon FBO: ежедневные снимки остатков. PostgreSQL 14+.
-- Применение: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/001_schema.sql
-- Это первая миграция: применяется один раз к новой базе, без удаления данных.
-- Контракт полей: https://docs.ozon.com/api/seller/swagger.json
-- Операция AnalyticsAPI_AnalyticsStocks, проверено 2026-10-07.
--
-- Единица хранения: один SKU на одном складе в одной попытке загрузки.
-- Снимок за день выбирается через published_snapshot; читайте представления v_*.
-- Классификация склада сохраняется в самом снимке, а не берётся из текущего
-- справочника: перемещение склада в другой кластер не переписывает историю.
-- Дата снимка определяется загрузчиком в Europe/Moscow. Время — timestamptz.
-- NULL означает отсутствие значения в API; не заменяйте его автоматически на 0.
-- Статусы остатков могут пересекаться. Суммы всех статусов как «итого» здесь нет.
-- Успешные запуски после публикации считаются неизменяемыми; повторная выгрузка
-- создаёт новый запуск и атомарно заменяет ссылку на дневной снимок.

BEGIN;
CREATE SCHEMA ozon;

CREATE TABLE ozon.seller_account (
    client_id bigint PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE ozon.seller_account IS 'Кабинеты продавца. API-ключ хранится в конфигурации загрузчика.';

CREATE TABLE ozon.product (
    client_id bigint NOT NULL REFERENCES ozon.seller_account(client_id),
    sku bigint NOT NULL,
    product_id bigint,
    offer_id text NOT NULL,
    name text,
    archived boolean,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, sku)
);
CREATE INDEX product_offer_idx ON ozon.product (client_id, offer_id);
COMMENT ON TABLE ozon.product IS 'Текущий справочник. Один артикул может соответствовать нескольким SKU; offer_id не уникален.';
COMMENT ON COLUMN ozon.product.archived IS 'Получать архивные товары отдельно: visibility=ALL в /v3/product/list исключает архивные.';

CREATE TABLE ozon.warehouse (
    warehouse_id bigint PRIMARY KEY,
    name text NOT NULL,
    warehouse_type text,
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE ozon.warehouse IS 'Физические склады Ozon; общие для кабинетов. Названия текущие.';

CREATE TABLE ozon.cluster (
    cluster_id bigint PRIMARY KEY,
    name text NOT NULL,
    cluster_type text,
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE ozon.cluster IS 'Кластеры из /v1/cluster/list. Отдельное пространство идентификаторов.';

CREATE TABLE ozon.macrolocal_cluster (
    macrolocal_cluster_id bigint PRIMARY KEY,
    name text NOT NULL,
    country_code text,
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE ozon.macrolocal_cluster IS 'Макролокальные кластеры из /v2/cluster/list; не смешивать с cluster_id.';

CREATE TABLE ozon.ingest_run (
    run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id bigint NOT NULL REFERENCES ozon.seller_account(client_id),
    snapshot_date date NOT NULL,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    requested_skus bigint[] NOT NULL CHECK (cardinality(requested_skus) > 0),
    expected_batches integer NOT NULL CHECK (expected_batches > 0),
    successful_batches integer NOT NULL DEFAULT 0 CHECK (successful_batches >= 0),
    validation_passed boolean NOT NULL DEFAULT false,
    error_message text,
    -- Ошибки перечисляются текстом; контракт API не требует отдельного ENUM.
    CHECK (successful_batches <= expected_batches),
    CHECK ((status = 'running' AND finished_at IS NULL)
        OR (status IN ('succeeded', 'failed') AND finished_at IS NOT NULL)),
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK (status <> 'succeeded'
        OR (validation_passed AND successful_batches = expected_batches)),
    UNIQUE (run_id, client_id),
    UNIQUE (run_id, client_id, snapshot_date, status)
);
CREATE INDEX ingest_run_day_idx ON ozon.ingest_run (client_id, snapshot_date, started_at DESC);
COMMENT ON TABLE ozon.ingest_run IS 'Одна попытка полного получения остатков. Отдельный run_id на каждый повторный запуск.';
COMMENT ON COLUMN ozon.ingest_run.requested_skus IS 'Зафиксированный набор SKU. Делить запросы /v1/analytics/stocks на пачки до 100.';
COMMENT ON COLUMN ozon.ingest_run.validation_passed IS 'Загрузчик проверил ответы всех пачек, покрытие запрошенных SKU, отсутствие дублей, справочники и завершение загрузки строк. Отсутствующая строка не считается нулём.';
COMMENT ON COLUMN ozon.ingest_run.successful_batches IS 'Число разных успешно обработанных пачек SKU; повторные HTTP-попытки не увеличивают счётчик.';

CREATE TABLE ozon.api_response (
    response_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id bigint NOT NULL REFERENCES ozon.ingest_run(run_id),
    request_number integer NOT NULL CHECK (request_number > 0),
    endpoint text NOT NULL,
    request_body jsonb NOT NULL,
    http_status smallint NOT NULL CHECK (http_status BETWEEN 100 AND 599),
    received_at timestamptz NOT NULL DEFAULT now(),
    response_body jsonb,
    object_uri text,
    CHECK ((response_body IS NOT NULL) <> (object_uri IS NOT NULL)),
    UNIQUE (run_id, request_number)
);
COMMENT ON TABLE ozon.api_response IS 'Исходный ответ в JSONB либо URI файла в объектном хранилище. Сохраняет исходные названия, новые поля API и данные для повторной обработки.';
COMMENT ON COLUMN ozon.api_response.request_body IS 'Только JSON запроса, без заголовков авторизации.';

CREATE TABLE ozon.stock_daily (
    run_id bigint NOT NULL,
    client_id bigint NOT NULL,
    sku bigint NOT NULL,
    warehouse_id bigint NOT NULL REFERENCES ozon.warehouse(warehouse_id),
    cluster_id bigint REFERENCES ozon.cluster(cluster_id),
    macrolocal_cluster_id bigint REFERENCES ozon.macrolocal_cluster(macrolocal_cluster_id),
    observed_at timestamptz NOT NULL,

    -- Все количественные поля /v1/analytics/stocks, названия совпадают с API.
    available_stock_count integer,              -- Доступно к продаже.
    valid_stock_count integer,                  -- Готовится к продаже.
    transit_stock_count integer,                -- Поставки в пути.
    requested_stock_count integer,              -- В заявках на поставку.
    inbound_replenishment integer,              -- В перемещении.
    excess_stock_count integer,                 -- Излишки поставки для вывоза.
    expiring_stock_count integer,               -- Истекающий срок годности.
    other_stock_count integer,                  -- Проходит проверку.
    outbound_pending_delivery integer,          -- Доставляется покупателям.
    outbound_returns_picking integer,           -- Готовится к вывозу.
    outbound_returns_ready_to_ship integer,     -- Готово к вывозу.
    outbound_returns_return_to_seller integer,  -- Возвращается продавцу.
    return_from_customer_stock_count integer,   -- В возврате от покупателей.
    return_to_seller_stock_count integer,       -- Готовится к вывозу по заявке.
    stock_defect_stock_count integer,           -- Брак со стока для вывоза.
    transit_defect_stock_count integer,         -- Брак с поставки для вывоза.
    stock_not_being_sold integer,               -- Снято с продажи.
    waiting_docs_stock_count integer,           -- Маркировка: ждёт действий.
    waiting_docs_to_export_stock_count integer, -- Маркировка: ждёт вывоза.

    -- Метрики по всем кластерам повторяются в складских строках. Не суммировать.
    ads double precision,
    idc double precision,
    days_without_sales integer,
    turnover_grade text,
    -- Метрики по кластеру также не суммировать по складам.
    ads_cluster double precision,
    idc_cluster double precision,
    days_without_sales_cluster integer,
    turnover_grade_cluster text,
    item_tags text[],
    placement_zone text[],

    PRIMARY KEY (run_id, sku, warehouse_id),
    FOREIGN KEY (run_id, client_id) REFERENCES ozon.ingest_run(run_id, client_id),
    FOREIGN KEY (client_id, sku) REFERENCES ozon.product(client_id, sku)
);
CREATE INDEX stock_daily_product_idx ON ozon.stock_daily (client_id, sku, run_id);
COMMENT ON TABLE ozon.stock_daily IS 'Складские строки всех попыток. Для отчётов использовать v_stock_daily: она выбирает опубликованный снимок за день.';
COMMENT ON COLUMN ozon.stock_daily.observed_at IS 'Время получения конкретного ответа API: все пачки не являются атомарным снимком Ozon.';
COMMENT ON COLUMN ozon.stock_daily.cluster_id IS 'Принадлежность к кластеру на момент выгрузки, взятая из ответа остатков.';
COMMENT ON COLUMN ozon.stock_daily.macrolocal_cluster_id IS 'Принадлежность к макролокальному кластеру на момент выгрузки.';

CREATE TABLE ozon.published_snapshot (
    client_id bigint NOT NULL REFERENCES ozon.seller_account(client_id),
    snapshot_date date NOT NULL,
    run_id bigint NOT NULL UNIQUE,
    run_status text NOT NULL DEFAULT 'succeeded' CHECK (run_status = 'succeeded'),
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, snapshot_date),
    -- FK запрещает публикацию незавершённого запуска, другой даты/кабинета.
    FOREIGN KEY (run_id, client_id, snapshot_date, run_status)
        REFERENCES ozon.ingest_run(run_id, client_id, snapshot_date, status)
);
COMMENT ON TABLE ozon.published_snapshot IS 'Ровно один выбранный успешный запуск на кабинет и дату. Публиковать через publish_stock_snapshot().';

CREATE FUNCTION ozon.publish_stock_snapshot(p_run_id bigint)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, ozon AS $$
DECLARE
    selected_run ozon.ingest_run%ROWTYPE;
BEGIN
    SELECT * INTO STRICT selected_run FROM ozon.ingest_run
    WHERE run_id = p_run_id FOR UPDATE;
    IF selected_run.status <> 'succeeded' THEN
        RAISE EXCEPTION 'Run % is not succeeded', p_run_id;
    END IF;
    -- Сериализует публикации даже когда дневного снимка ещё нет.
    PERFORM 1 FROM ozon.seller_account
    WHERE client_id = selected_run.client_id FOR UPDATE;
    INSERT INTO ozon.published_snapshot (client_id, snapshot_date, run_id)
    VALUES (selected_run.client_id, selected_run.snapshot_date, selected_run.run_id)
    ON CONFLICT (client_id, snapshot_date) DO UPDATE
    SET run_id = EXCLUDED.run_id, published_at = now()
    -- Запоздавшая старая попытка не заменяет более свежую.
    WHERE (SELECT old_run.started_at FROM ozon.ingest_run AS old_run
           WHERE old_run.run_id = ozon.published_snapshot.run_id) < selected_run.started_at
       OR ((SELECT old_run.started_at FROM ozon.ingest_run AS old_run
            WHERE old_run.run_id = ozon.published_snapshot.run_id) = selected_run.started_at
           AND ozon.published_snapshot.run_id < selected_run.run_id);
END;
$$;
COMMENT ON FUNCTION ozon.publish_stock_snapshot(bigint) IS 'Атомарно выбирает успешный снимок. Идемпотентно; более старый запуск не перезаписывает более новый.';

CREATE VIEW ozon.v_stock_daily AS
SELECT p.snapshot_date, s.*
FROM ozon.published_snapshot AS p
JOIN ozon.stock_daily AS s ON s.run_id = p.run_id;

-- Строгие суммы: если поле отсутствует хотя бы на одном складе, результат NULL.
-- warehouse_count — число полученных складских строк, не все склады Ozon.
CREATE VIEW ozon.v_cluster_stock_daily AS
SELECT client_id, snapshot_date, sku, cluster_id,
       count(*) AS warehouse_count,
       min(observed_at) AS observed_from,
       max(observed_at) AS observed_to,
       CASE WHEN count(available_stock_count) = count(*) THEN sum(available_stock_count) END AS available_stock_count,
       CASE WHEN count(valid_stock_count) = count(*) THEN sum(valid_stock_count) END AS valid_stock_count,
       CASE WHEN count(transit_stock_count) = count(*) THEN sum(transit_stock_count) END AS transit_stock_count,
       CASE WHEN count(requested_stock_count) = count(*) THEN sum(requested_stock_count) END AS requested_stock_count,
       CASE WHEN count(inbound_replenishment) = count(*) THEN sum(inbound_replenishment) END AS inbound_replenishment,
       CASE WHEN count(excess_stock_count) = count(*) THEN sum(excess_stock_count) END AS excess_stock_count,
       CASE WHEN count(expiring_stock_count) = count(*) THEN sum(expiring_stock_count) END AS expiring_stock_count,
       CASE WHEN count(other_stock_count) = count(*) THEN sum(other_stock_count) END AS other_stock_count,
       CASE WHEN count(outbound_pending_delivery) = count(*) THEN sum(outbound_pending_delivery) END AS outbound_pending_delivery,
       CASE WHEN count(outbound_returns_picking) = count(*) THEN sum(outbound_returns_picking) END AS outbound_returns_picking,
       CASE WHEN count(outbound_returns_ready_to_ship) = count(*) THEN sum(outbound_returns_ready_to_ship) END AS outbound_returns_ready_to_ship,
       CASE WHEN count(outbound_returns_return_to_seller) = count(*) THEN sum(outbound_returns_return_to_seller) END AS outbound_returns_return_to_seller,
       CASE WHEN count(return_from_customer_stock_count) = count(*) THEN sum(return_from_customer_stock_count) END AS return_from_customer_stock_count,
       CASE WHEN count(return_to_seller_stock_count) = count(*) THEN sum(return_to_seller_stock_count) END AS return_to_seller_stock_count,
       CASE WHEN count(stock_defect_stock_count) = count(*) THEN sum(stock_defect_stock_count) END AS stock_defect_stock_count,
       CASE WHEN count(transit_defect_stock_count) = count(*) THEN sum(transit_defect_stock_count) END AS transit_defect_stock_count,
       CASE WHEN count(stock_not_being_sold) = count(*) THEN sum(stock_not_being_sold) END AS stock_not_being_sold,
       CASE WHEN count(waiting_docs_stock_count) = count(*) THEN sum(waiting_docs_stock_count) END AS waiting_docs_stock_count,
       CASE WHEN count(waiting_docs_to_export_stock_count) = count(*) THEN sum(waiting_docs_to_export_stock_count) END AS waiting_docs_to_export_stock_count,
       CASE WHEN count(ads_cluster) = count(*) AND count(DISTINCT ads_cluster) = 1
            THEN max(ads_cluster) END AS ads_cluster,
       CASE WHEN count(idc_cluster) = count(*) AND count(DISTINCT idc_cluster) = 1
            THEN max(idc_cluster) END AS idc_cluster,
       CASE WHEN count(days_without_sales_cluster) = count(*) AND count(DISTINCT days_without_sales_cluster) = 1
            THEN max(days_without_sales_cluster) END AS days_without_sales_cluster,
       CASE WHEN count(turnover_grade_cluster) = count(*) AND count(DISTINCT turnover_grade_cluster) = 1
            THEN max(turnover_grade_cluster) END AS turnover_grade_cluster
FROM ozon.v_stock_daily
GROUP BY client_id, snapshot_date, sku, cluster_id;
COMMENT ON VIEW ozon.v_cluster_stock_daily IS 'Остатки по кластерам. Кластерные метрики выбираются один раз при согласованности складских строк, иначе NULL.';

CREATE VIEW ozon.v_macrolocal_stock_daily AS
SELECT client_id, snapshot_date, sku, macrolocal_cluster_id,
       count(*) AS warehouse_count,
       CASE WHEN count(available_stock_count) = count(*) THEN sum(available_stock_count) END AS available_stock_count,
       CASE WHEN count(valid_stock_count) = count(*) THEN sum(valid_stock_count) END AS valid_stock_count,
       CASE WHEN count(transit_stock_count) = count(*) THEN sum(transit_stock_count) END AS transit_stock_count
FROM ozon.v_stock_daily
GROUP BY client_id, snapshot_date, sku, macrolocal_cluster_id;
COMMENT ON VIEW ozon.v_macrolocal_stock_daily IS 'Основные остатки по макролокальным кластерам. ads_cluster/idc_cluster здесь не агрегируются.';

COMMIT;
