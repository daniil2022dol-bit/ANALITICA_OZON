BEGIN;
CREATE TABLE ozon.ads_campaign (
 client_id bigint NOT NULL, campaign_id bigint NOT NULL, title text NOT NULL,
 state text NOT NULL, object_type text NOT NULL, payment_type text,
 placements text[] NOT NULL DEFAULT '{}', daily_budget numeric, weekly_budget numeric,
 raw jsonb NOT NULL, fetched_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(client_id,campaign_id)
);
CREATE TABLE ozon.ads_campaign_snapshot (
 client_id bigint NOT NULL, day date NOT NULL, campaign_id bigint NOT NULL,
 raw jsonb NOT NULL, PRIMARY KEY(client_id,day,campaign_id)
);
CREATE TABLE ozon.ads_campaign_daily (
 client_id bigint NOT NULL, day date NOT NULL, campaign_id bigint NOT NULL,
 views bigint, clicks bigint, orders bigint, spend numeric, revenue numeric,
 raw jsonb NOT NULL, fetched_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(client_id,day,campaign_id)
);
CREATE TABLE ozon.ads_expense_daily (
 client_id bigint NOT NULL, day date NOT NULL, campaign_id bigint NOT NULL,
 spend numeric, bonuses numeric, prepayment numeric, raw jsonb NOT NULL,
 PRIMARY KEY(client_id,day,campaign_id)
);
CREATE TABLE ozon.ads_sku_daily (
 client_id bigint NOT NULL, day date NOT NULL, campaign_id bigint NOT NULL, sku bigint NOT NULL,
 title text, views bigint, clicks bigint, carts bigint, orders bigint,
 spend numeric, revenue numeric, model_orders bigint, model_revenue numeric,
 source text NOT NULL CHECK(source IN ('detail','sku')), raw jsonb NOT NULL,
 fetched_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(client_id,day,campaign_id,sku)
);
CREATE INDEX ads_sku_idx ON ozon.ads_sku_daily(client_id,sku,day);
CREATE TABLE ozon.ads_cpo_daily (
 client_id bigint NOT NULL, day date NOT NULL, sku bigint NOT NULL,
 title text, offer_id text, orders bigint, spend numeric, revenue numeric,
 bid_percent numeric, bid_rubles numeric, promotion_status text, raw jsonb NOT NULL,
 fetched_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(client_id,day,sku)
);
COMMENT ON TABLE ozon.ads_cpo_daily IS 'Выбранные товары CPO, один отчёт на один день. CPC-поля в raw дублируются с CPC и не суммируются. PromotionStatus — текущее состояние, не история состояния за day.';
CREATE TABLE ozon.ads_coverage (
 client_id bigint NOT NULL, kind text NOT NULL, day date NOT NULL,
 scope text NOT NULL DEFAULT '', fetched_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(client_id,kind,day,scope)
);
COMMENT ON TABLE ozon.ads_coverage IS 'День/источник/кампания полностью загружены, включая пустой ответ. Отсутствие покрытия не означает нулевые продажи.';
CREATE TABLE ozon.ads_report (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, client_id bigint NOT NULL,
 kind text NOT NULL CHECK(kind IN ('detail','cpo')), date_from date NOT NULL, date_to date NOT NULL,
 campaigns bigint[] NOT NULL DEFAULT '{}', code text UNIQUE,
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','requesting','pending','ready','failed','uncertain')),
 parameters jsonb NOT NULL, file_path text, sha256 text, metadata jsonb,
 requested_on date NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), polled_at timestamptz, finished_at timestamptz,
 UNIQUE(client_id,kind,date_from,date_to,campaigns,requested_on)
);
CREATE UNIQUE INDEX ads_one_export ON ozon.ads_report(client_id) WHERE status IN ('requesting','pending','uncertain');
CREATE TABLE ozon.ads_api_call (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, client_id bigint NOT NULL,
 called_at timestamptz NOT NULL DEFAULT now(), endpoint text NOT NULL,
 status integer, export_cost integer NOT NULL DEFAULT 0, error text
);
CREATE INDEX ads_api_daily_idx ON ozon.ads_api_call(client_id,called_at);
CREATE TABLE ozon.ads_api_cooldown (
 client_id bigint PRIMARY KEY, next_allowed_at timestamptz NOT NULL
);
COMMENT ON TABLE ozon.ads_api_call IS 'Отдельный бюджет Performance. Без токенов, credential bodies, Authorization и бизнес-ответов; исходные данные — в таблицах фактов и защищённых отчётах.';
COMMIT;
