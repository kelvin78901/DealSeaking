-- DealSeaking — Postgres schema draft v0.3 (2026-11-17)
-- Design reference only; real migrations will live in the app (Alembic).
-- Target: PostgreSQL 16+. Extensions: pg_trgm (fuzzy title search), citext (emails).

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;

-- ───────────────────────────── accounts & billing ─────────────────────────────

CREATE TABLE plans (
    id               text PRIMARY KEY,               -- 'free', 'pro'
    name             text NOT NULL,
    max_rules        int  NOT NULL,
    instant_alerts   boolean NOT NULL DEFAULT false,
    max_emails_day   int  NOT NULL,
    history_days     int,                            -- NULL = unlimited
    stripe_price_ids text[] NOT NULL DEFAULT '{}'    -- monthly / yearly prices
);

CREATE TABLE users (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email           citext NOT NULL UNIQUE,
    email_verified  timestamptz,                     -- NULL until first OTP/link login (double opt-in)
    plan_id         text NOT NULL DEFAULT 'free' REFERENCES plans(id),
    timezone        text NOT NULL DEFAULT 'America/New_York',
    home_zip        text,
    radius_miles    int  NOT NULL DEFAULT 30,
    quiet_start     time,
    quiet_end       time,
    digest_hour     smallint NOT NULL DEFAULT 8 CHECK (digest_hour BETWEEN 0 AND 23),
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz                      -- soft delete; purge job removes PII
);

CREATE TABLE auth_tokens (                           -- OTP codes and magic links
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  bytea  NOT NULL,                     -- never store the token itself
    kind        text   NOT NULL CHECK (kind IN ('otp', 'magic_link')),
    expires_at  timestamptz NOT NULL,                -- 15–30 min
    used_at     timestamptz,
    attempts    smallint NOT NULL DEFAULT 0,
    created_ip  inet
);
CREATE INDEX ON auth_tokens (user_id, expires_at);

CREATE TABLE subscriptions (                         -- mirrors Stripe; webhooks are the source of truth
    user_id                bigint PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    stripe_customer_id     text NOT NULL UNIQUE,
    stripe_subscription_id text UNIQUE,
    status                 text NOT NULL,            -- active, trialing, past_due, canceled …
    current_period_end     timestamptz,
    updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notification_channels (                 -- per-user push targets
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('web_push', 'telegram', 'discord', 'ntfy')),
    secret_enc  bytea NOT NULL,                      -- encrypted endpoint / token
    verified_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE email_suppressions (                    -- kept even after account deletion
    email_hash  bytea PRIMARY KEY,                   -- sha256(lower(email))
    reason      text NOT NULL CHECK (reason IN ('unsubscribe', 'bounce', 'complaint', 'manual')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ───────────────────────────── catalog ─────────────────────────────

CREATE TABLE sources (
    id             text PRIMARY KEY,                 -- 'bestbuy', 'microcenter', 'woot', …
    kind           text NOT NULL CHECK (kind IN ('api', 'feed', 'scrape')),
    enabled        boolean NOT NULL DEFAULT true,
    public_display boolean NOT NULL DEFAULT false,   -- terms allow showing to users?
    cache_ttl      interval,                         -- e.g. Best Buy API: 72 hours
    attribution    text,                             -- required credit line, if any
    terms_notes    text,                             -- what the ToS allow, reviewed when
    last_ok_at     timestamptz,
    last_error     text
);

CREATE TABLE stores (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id  text NOT NULL REFERENCES sources(id),
    ext_id     text NOT NULL,                        -- e.g. Micro Center storeid '085'
    name       text NOT NULL,
    address    text,
    zip        text,
    lat        double precision,
    lon        double precision,
    UNIQUE (source_id, ext_id)
);

CREATE TABLE categories (
    path       text PRIMARY KEY,                     -- 'storage/ssd/nvme'
    label      text NOT NULL,
    is_storage boolean NOT NULL DEFAULT false        -- enables capacity and price-per-TB
);

CREATE TABLE category_map (                          -- source category name → unified path
    source_id     text NOT NULL REFERENCES sources(id),
    source_label  text NOT NULL,                     -- e.g. 'SSD (Solid State Drives)'
    category_path text NOT NULL REFERENCES categories(path),
    PRIMARY KEY (source_id, source_label)
);

CREATE TABLE products (                              -- canonical product, shared across sources
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    brand      text,
    model      text,
    mpn        text,
    gtin       text,
    title      text NOT NULL,
    category   text REFERENCES categories(path),
    attrs      jsonb NOT NULL DEFAULT '{}',          -- capacity_gb, interface, …
    low_price  numeric(10,2),                        -- all-time low across offers
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON products (gtin) WHERE gtin IS NOT NULL;
CREATE UNIQUE INDEX ON products (lower(brand), lower(mpn)) WHERE mpn IS NOT NULL;
CREATE INDEX ON products USING gin (title gin_trgm_ops);

CREATE TABLE offers (                                -- one source's listing (was `listings`)
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id    text NOT NULL REFERENCES sources(id),
    source_sku   text NOT NULL,
    product_id   bigint REFERENCES products(id),     -- NULL until matched
    match_method text,                               -- gtin, mpn, model_regex, fuzzy, manual
    store_id     bigint REFERENCES stores(id),       -- NULL = online
    condition    text NOT NULL CHECK (condition IN ('new', 'open_box', 'refurbished', 'used')),
    condition_detail text,                           -- e.g. Best Buy 'excellent'
    title        text NOT NULL,
    url          text NOT NULL,                      -- canonical, no tracking / affiliate params
    price        numeric(10,2) NOT NULL,
    list_price   numeric(10,2),
    stock        int,
    active       boolean NOT NULL DEFAULT true,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_sku, store_id, condition)
);
CREATE INDEX ON offers (product_id) WHERE active;

CREATE TABLE price_points (                          -- time series; partition monthly
    offer_id  bigint NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    ts        timestamptz NOT NULL,
    price     numeric(10,2) NOT NULL,
    stock     int,
    PRIMARY KEY (offer_id, ts)
) PARTITION BY RANGE (ts);
-- CREATE TABLE price_points_2026_11 PARTITION OF price_points FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
-- Sources with cache_ttl (Best Buy API: 72h) are pruned by a periodic job.

CREATE TABLE deal_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offer_id    bigint NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('new', 'price_drop', 'price_up', 'restock', 'gone', 'historical_low', 'hot')),
    old_price   numeric(10,2),
    new_price   numeric(10,2),
    hot_score   real,
    baseline    boolean NOT NULL DEFAULT false,      -- first look at a scope: stored, never alerted
    needs_confirmation boolean NOT NULL DEFAULT false, -- anomalous crowd-sourced price: wait for a second crawl
    confirmed_at timestamptz,
    detected_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON deal_events (detected_at DESC);

-- ───────────────────────────── user collectors (docs/collector-protocol.md) ─────────────────────────────

CREATE TABLE collectors (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    bytea  NOT NULL UNIQUE,
    kind          text   NOT NULL CHECK (kind IN ('extension', 'docker')),
    client        text,                              -- e.g. 'extension/0.1.0'
    status        text   NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'revoked')),
    trust_score   real   NOT NULL DEFAULT 1.0,
    blocked_until timestamptz,                       -- set after a 403 / challenge: 24h back-off
    consented_at  timestamptz NOT NULL,              -- opt-in to fetching microcenter.com on their network
    last_seen_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE collector_stores (                      -- stores the user chose to cover
    collector_id bigint NOT NULL REFERENCES collectors(id) ON DELETE CASCADE,
    store_id     bigint NOT NULL REFERENCES stores(id),
    PRIMARY KEY (collector_id, store_id)
);

CREATE TABLE crawls (                                -- one leased pass over one store
    id             text PRIMARY KEY,                 -- crawl_id handed to the collector
    collector_id   bigint NOT NULL REFERENCES collectors(id) ON DELETE CASCADE,
    store_id       bigint NOT NULL REFERENCES stores(id),
    status         text NOT NULL DEFAULT 'leased' CHECK (status IN ('leased', 'complete', 'expired', 'blocked', 'rejected')),
    pages_expected int,                              -- known after page 1
    pages_received int NOT NULL DEFAULT 0,
    parser_version text,
    leased_at      timestamptz NOT NULL DEFAULT now(),
    lease_expires  timestamptz NOT NULL,
    finished_at    timestamptz
);
-- one active lease per store at a time
CREATE UNIQUE INDEX ON crawls (store_id) WHERE status = 'leased';

CREATE TABLE raw_pages (                             -- original HTML, kept 7 days for re-parsing / audits
    crawl_id     text NOT NULL REFERENCES crawls(id) ON DELETE CASCADE,
    page         int  NOT NULL,
    fetched_at   timestamptz NOT NULL,
    http_status  int  NOT NULL,
    html_gz      bytea NOT NULL,
    parsed_items int,
    PRIMARY KEY (crawl_id, page)
);

-- ───────────────────────────── rules & notifications ─────────────────────────────

CREATE TABLE rules (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    spec        jsonb  NOT NULL,                     -- validated against docs/rule.schema.json
    enabled     boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON rules (user_id);

CREATE TABLE rule_index (                            -- inverted index: token → candidate rules
    token   text   NOT NULL,                         -- 'cat:storage/ssd', 'brand:samsung', 'kw:990', 'gtin:…'
    rule_id bigint NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    PRIMARY KEY (token, rule_id)
);

CREATE TABLE rule_matches (
    rule_id    bigint NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    event_id   bigint NOT NULL REFERENCES deal_events(id) ON DELETE CASCADE,
    matched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_id, event_id)
);

CREATE TABLE notifications (                         -- outbox
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel     text NOT NULL,                       -- email, web_push, …
    mode        text NOT NULL CHECK (mode IN ('instant', 'daily_digest', 'weekly_digest')),
    dedupe_key  text NOT NULL,                       -- user:product:event-kind
    payload     jsonb NOT NULL,
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'held', 'sent', 'skipped', 'failed')),
    skip_reason text,                                -- cooldown, quota, quiet_hours, link_dead, suppressed
    send_after  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz,
    provider_id text,                                -- e.g. Resend message id, for bounce webhooks
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON notifications (status, send_after);
CREATE INDEX ON notifications (user_id, dedupe_key, sent_at DESC);

CREATE TABLE link_checks (
    url          text PRIMARY KEY,                   -- canonical URL
    http_status  int,
    availability text,                               -- schema.org ItemAvailability
    price        numeric(10,2),
    risk_verdict text,                               -- Web Risk result for user-submitted links
    checked_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outbound_clicks (                       -- /go/<offer_id> redirects, for affiliate reconciliation
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offer_id   bigint NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    user_id    bigint REFERENCES users(id) ON DELETE SET NULL,
    via        text NOT NULL CHECK (via IN ('web', 'email', 'push')),
    clicked_at timestamptz NOT NULL DEFAULT now()
);
