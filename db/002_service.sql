BEGIN;
ALTER TABLE ozon.seller_account ADD COLUMN currency text NOT NULL DEFAULT 'RUB';
CREATE TABLE ozon.job_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','success','failed')),
    details jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE ozon.api_call (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id bigint NOT NULL,
    endpoint text NOT NULL,
    called_at timestamptz NOT NULL DEFAULT now(),
    status integer,
    request_body jsonb NOT NULL,
    response_body jsonb,
    error text
);
CREATE INDEX api_call_daily_idx ON ozon.api_call (client_id, called_at, endpoint);
CREATE TABLE ozon.api_cooldown (
    client_id bigint NOT NULL,
    endpoint text NOT NULL,
    next_allowed_at timestamptz NOT NULL,
    PRIMARY KEY(client_id, endpoint)
);
CREATE TABLE ozon.posting (
    client_id bigint NOT NULL REFERENCES ozon.seller_account,
    posting_number text NOT NULL,
    order_number text,
    created_at timestamptz NOT NULL,
    status text NOT NULL,
    warehouse_id bigint REFERENCES ozon.warehouse,
    cluster_id bigint REFERENCES ozon.cluster,
    cluster_from text,
    cluster_to text,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(client_id, posting_number)
);
CREATE INDEX posting_date_idx ON ozon.posting (client_id, created_at);
CREATE INDEX posting_pending_idx ON ozon.posting (client_id, status, created_at);
COMMENT ON COLUMN ozon.posting.cluster_id IS 'Кластер отправки, сопоставленный по справочнику на момент заказа; при отсутствии подтверждённого соответствия NULL.';
COMMENT ON COLUMN ozon.posting.cluster_to IS 'Исходный код/название региона назначения из financial_data. Не числовой cluster_id.';
CREATE TABLE ozon.posting_item (
    client_id bigint NOT NULL,
    posting_number text NOT NULL,
    sku bigint NOT NULL,
    quantity integer NOT NULL CHECK(quantity > 0),
    unit_price numeric(20,4) NOT NULL,
    currency text NOT NULL,
    PRIMARY KEY(client_id, posting_number, sku),
    FOREIGN KEY(client_id, posting_number) REFERENCES ozon.posting ON DELETE CASCADE,
    FOREIGN KEY(client_id, sku) REFERENCES ozon.product
);
CREATE TABLE ozon.sales_coverage (
    client_id bigint NOT NULL REFERENCES ozon.seller_account,
    day date NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(client_id, day)
);
COMMENT ON TABLE ozon.sales_coverage IS 'Полностью выгруженные календарные дни FBO по Europe/Moscow. Позволяет отличить ноль заказов от отсутствия данных.';
CREATE TABLE ozon.sales_daily (
    client_id bigint NOT NULL REFERENCES ozon.seller_account,
    day date NOT NULL,
    sku bigint NOT NULL,
    ordered_units integer NOT NULL,
    ordered_amount numeric(20,4) NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(client_id, day, sku),
    FOREIGN KEY(client_id, sku) REFERENCES ozon.product
);
COMMENT ON TABLE ozon.sales_daily IS 'Официальная аналитика sku/day: revenue означает сумму заказов, не прибыль и не выплаты. Даты — календарь отчёта Ozon (UTC); не объединять с локальными датами FBO без явного указания.';
CREATE TABLE ozon.analytics_coverage (
    client_id bigint NOT NULL REFERENCES ozon.seller_account,
    day date NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(client_id, day)
);
CREATE TABLE ozon.storage_report (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id bigint NOT NULL REFERENCES ozon.seller_account,
    day date NOT NULL,
    kind text NOT NULL CHECK(kind IN ('products','supplies')),
    code text,
    status text NOT NULL DEFAULT 'requested',
    file_path text,
    file_sha256 text,
    columns jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE(client_id,day,kind)
);
CREATE TABLE ozon.storage_row (
    report_id bigint NOT NULL REFERENCES ozon.storage_report ON DELETE CASCADE,
    row_number integer NOT NULL,
    sku bigint,
    offer_id text,
    warehouse_id bigint REFERENCES ozon.warehouse,
    warehouse_name text,
    cluster_id bigint REFERENCES ozon.cluster,
    supply_id text,
    free_until date,
    free_days_left integer,
    quantity numeric,
    fee numeric,
    raw jsonb NOT NULL,
    PRIMARY KEY(report_id,row_number)
);
COMMENT ON TABLE ozon.storage_row IS 'Исходная строка отчёта размещения. Срок бесплатного хранения читается только из подтверждённой колонки API; отсутствие означает NULL, не ноль. Партии не усредняются.';
CREATE TABLE ozon.host_metric (
    measured_at timestamptz PRIMARY KEY DEFAULT now(),
    cpu_percent real NOT NULL,
    memory_percent real NOT NULL,
    memory_available_bytes bigint NOT NULL,
    disk_percent real NOT NULL,
    disk_free_bytes bigint NOT NULL,
    swap_percent real NOT NULL,
    load_1 real NOT NULL
);
CREATE TABLE ozon.backup_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running',
    local_path text,
    sha256 text,
    size_bytes bigint,
    offsite boolean NOT NULL DEFAULT false,
    error text
);
CREATE VIEW ozon.v_sales_fbo AS
SELECT p.client_id, (p.created_at AT TIME ZONE 'Europe/Moscow')::date AS day,
       p.posting_number, p.order_number, p.status, p.warehouse_id, p.cluster_id,
       p.cluster_from, p.cluster_to, i.sku, i.quantity,
       i.unit_price*i.quantity AS ordered_amount, i.currency
FROM ozon.posting p JOIN ozon.posting_item i USING(client_id,posting_number);
COMMIT;
