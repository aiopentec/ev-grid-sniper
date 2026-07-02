-- ============================================================
-- GridSniper — Supabase schema
-- Run this in: Supabase dashboard → SQL editor
-- ============================================================

-- Enable UUID generation
create extension if not exists "pgcrypto";


-- ── grid_prices ─────────────────────────────────────────────────────────────
-- One row per zone per poll. Negative lmp_dollars_per_mwh = alert trigger.

create table grid_prices (
  id                    uuid primary key default gen_random_uuid(),
  zone_id               text not null,          -- e.g. "CAISO_NP15"
  source                text not null,          -- "CAISO" | "ERCOT" | "EIA"
  lmp_dollars_per_mwh   numeric not null,
  fetched_at            timestamptz not null,
  created_at            timestamptz default now()
);

create unique index grid_prices_zone_time
  on grid_prices(zone_id, fetched_at);

create index grid_prices_lmp
  on grid_prices(lmp_dollars_per_mwh)
  where lmp_dollars_per_mwh < 0;   -- partial index — fast negative-price lookup


-- ── grid_renewables ──────────────────────────────────────────────────────────

create table grid_renewables (
  id                          uuid primary key default gen_random_uuid(),
  zone                        text not null,
  label                       text,
  carbon_intensity_gco2_per_kwh numeric,
  renewable_pct               numeric,
  fetched_at                  timestamptz not null,
  created_at                  timestamptz default now()
);

create unique index grid_renewables_zone_time
  on grid_renewables(zone, fetched_at);


-- ── grid_meta ────────────────────────────────────────────────────────────────
-- Key-value store for pipeline state (e.g. latest renewables %).

create table grid_meta (
  key         text primary key,
  value       text not null,
  updated_at  timestamptz default now()
);


-- ── subscribers ──────────────────────────────────────────────────────────────

create table subscribers (
  id          uuid primary key default gen_random_uuid(),
  phone       text not null unique,             -- E.164 format: "+15105550100"
  zip_code    text not null,
  vehicle     text,                             -- optional: "Tesla Model 3"
  plan        text not null default 'free',     -- 'free' | 'premium'
  active      boolean not null default true,
  joined_at   timestamptz default now(),
  -- Preferences
  min_savings_dollars   numeric default 1.00,  -- only alert if savings >= this
  max_alerts_per_day    int     default 3,
  -- Stripe (for premium tier)
  stripe_customer_id    text,
  stripe_subscription_id text
);

create index subscribers_zip on subscribers(zip_code);
create index subscribers_active on subscribers(active) where active = true;


-- ── alerts_sent ──────────────────────────────────────────────────────────────

create table alerts_sent (
  id                    uuid primary key default gen_random_uuid(),
  subscriber_id         uuid references subscribers(id) on delete cascade,
  zone_id               text not null,
  lmp_dollars_per_mwh   numeric,
  station_id            text,
  station_name          text,
  network               text,
  sms_body              text,
  twilio_sid            text,
  sent_at               timestamptz default now(),
  delivered             boolean default false
);

create index alerts_sent_subscriber on alerts_sent(subscriber_id, sent_at desc);
create index alerts_sent_zone on alerts_sent(zone_id, sent_at desc);

-- Cooldown check: "has this subscriber been alerted for this zone in last N mins?"
-- Used by alert_engine.py::was_recently_alerted()


-- ── affiliate_clicks ─────────────────────────────────────────────────────────

create table affiliate_clicks (
  id          uuid primary key default gen_random_uuid(),
  network     text not null,       -- "EVgo" | "ChargePoint" | "Blink" | "Tesla"
  clicked_at  timestamptz default now()
);

-- Aggregated counts per network (incremented via RPC)
create table affiliate_click_totals (
  network     text primary key,
  total       bigint default 0,
  last_click  timestamptz
);

-- Seed networks
insert into affiliate_click_totals(network, total) values
  ('EVgo', 0), ('ChargePoint', 0), ('Blink', 0), ('Tesla', 0)
on conflict do nothing;


-- ── RPC: increment_affiliate_clicks ──────────────────────────────────────────
-- Called by alert_engine.py after each SMS with an affiliate link.

create or replace function increment_affiliate_clicks(p_network text)
returns void language plpgsql as $$
begin
  insert into affiliate_click_totals(network, total, last_click)
  values (p_network, 1, now())
  on conflict(network) do update
    set total      = affiliate_click_totals.total + 1,
        last_click = now();

  insert into affiliate_clicks(network) values (p_network);
end;
$$;


-- ── pipeline_runs ────────────────────────────────────────────────────────────

create table pipeline_runs (
  id       uuid primary key default gen_random_uuid(),
  ran_at   timestamptz not null,
  status   text not null,    -- "success" | "partial" | "error"
  details  jsonb
);


-- ── Row Level Security ───────────────────────────────────────────────────────
-- Subscribers can only read/update their own row (if you add auth later).
-- The service key bypasses RLS — only use it server-side in GitHub Actions.

alter table subscribers        enable row level security;
alter table alerts_sent        enable row level security;
alter table affiliate_clicks   enable row level security;

-- Public read of aggregate totals (for your dashboard widget)
create policy "public can read affiliate totals"
  on affiliate_click_totals for select using (true);

create policy "public can read grid prices"
  on grid_prices for select using (true);

create policy "public can read grid renewables"
  on grid_renewables for select using (true);


-- ── Handy views ──────────────────────────────────────────────────────────────

-- Latest price per zone
create view latest_grid_prices as
select distinct on (zone_id)
  zone_id, source, lmp_dollars_per_mwh, fetched_at
from grid_prices
order by zone_id, fetched_at desc;

-- Subscriber savings leaderboard
create view subscriber_savings as
select
  s.id,
  s.phone,
  s.zip_code,
  s.plan,
  count(a.id)              as alerts_received,
  round(sum(abs(a.lmp_dollars_per_mwh) * 0.065), 2) as est_savings_dollars
from subscribers s
left join alerts_sent a on a.subscriber_id = s.id
group by s.id, s.phone, s.zip_code, s.plan
order by est_savings_dollars desc;

-- Monthly revenue estimate
create view revenue_estimate as
select
  count(*) filter (where plan = 'premium')          as premium_subscribers,
  count(*) filter (where plan = 'premium') * 4.99   as premium_mrr,
  (select sum(total) * 4.0 from affiliate_click_totals) as affiliate_signups_rev,
  (select sum(total) * 0.25 from affiliate_click_totals) as affiliate_sessions_rev
from subscribers
where active = true;
