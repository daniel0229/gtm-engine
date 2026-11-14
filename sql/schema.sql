-- GTM Engine: database schema (Supabase / Postgres 15+).
--
-- One row per store domain in `companies`. Everything observed about a store goes to `signals`,
-- one row per fact, always with the URL it came from: a fact without a source does not enter the
-- database. The agent's company cards go to `research`, demo-form submissions to `leads`, outbound
-- drafts to `messages`, and every script run logs its time and cost in `runs`.
--
-- Signal types are not fixed in the database. They are the ids from config/icp.yaml (platform,
-- category_fit, meta_ads_active, ...), so a new scenario does not need a migration. Only the
-- convenience view `company_profile` names them.
--
-- Access: row level security is on for every table and there are no policies, so the public API
-- keys (anon / publishable) can neither read nor write. Scripts and n8n use the service role key or
-- the direct Postgres connection, which bypass RLS. The demo form (step 3.1) gets its own
-- insert-only policy on `leads` when it is built.
--
-- Applied to Supabase as migrations `init_schema`, `messages_company_idx` and `enrich_columns`. This file is kept as
-- the full current schema; every later change is applied as a new migration and copied here.

-- ---------------------------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------------------------

create function public.set_updated_at() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ---------------------------------------------------------------------------------------------
-- runs: one row per script or workflow run, for the "cost and time" numbers
-- ---------------------------------------------------------------------------------------------

create table public.runs (
  id             bigint generated always as identity primary key,
  step           text not null,                    -- ingest, enrich, registry, meta_ads, research, messages, inbound
  started_at     timestamptz not null default now(),
  finished_at    timestamptz,
  items_total    integer check (items_total >= 0),
  items_ok       integer check (items_ok >= 0),
  items_failed   integer check (items_failed >= 0),
  input_tokens   integer check (input_tokens >= 0),
  output_tokens  integer check (output_tokens >= 0),
  cost_usd       numeric(10, 4) check (cost_usd >= 0), -- money spent, or the API-price equivalent for `claude -p`
  notes          jsonb,                             -- arguments, error counts by reason, anything else worth keeping
  check (finished_at is null or finished_at >= started_at)
);

comment on table public.runs is 'One row per script or workflow run: duration, item counts, tokens and cost.';

-- ---------------------------------------------------------------------------------------------
-- companies: one row per store domain (ingest key), plus what the registries say about its seller
-- ---------------------------------------------------------------------------------------------

create table public.companies (
  id                   bigint generated always as identity primary key,
  domain               text not null unique
                       check (domain ~ '^[a-z0-9-]+(\.[a-z0-9-]+)+$'),  -- bare, lowercase, no www. or scheme

  -- Seller identity found on the store's own pages (enrich step)
  nip                  text check (nip ~ '^[0-9]{10}$'),
  nip_source_url       text,                        -- terms or footer page the NIP was read from
  check (nip is null or nip_source_url is not null),
  site_url             text,                        -- URL the homepage resolved to (after redirects)
  enrich_status        text check (enrich_status in (
                         'ok',                -- website read
                         'redirected',        -- read, but the homepage redirects to another domain
                         'robots_disallowed', -- robots.txt disallows the homepage
                         'blocked',           -- bot check or HTTP 401/403/429/503
                         'http_error',        -- other HTTP error on the homepage
                         'unreachable'        -- DNS, TLS, connection or timeout error
                       )),
  enriched_at          timestamptz,

  -- VAT whitelist (registry step)
  name                 text,                        -- registered name
  regon                text check (regon ~ '^([0-9]{9}|[0-9]{14})$'),
  krs                  text check (krs ~ '^[0-9]{10}$'),
  vat_status           text,                        -- statusVat as returned: Czynny, Zwolniony, Niezarejestrowany
  address              text,                        -- workingAddress, else residenceAddress
  city                 text,
  vat_registered_on    date,                        -- registrationLegalDate

  -- KRS (registry step, only when the seller has a KRS number)
  legal_form           text,                        -- as in the KRS extract, e.g. SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ
  krs_registered_on    date,
  board_size           smallint check (board_size >= 0),
  board_roles          text[],                      -- functions only; KRS masks the names

  -- Outcome of matching the domain to a registry entry (step 1.4a reports these shares)
  registry_status      text not null default 'pending'
                       check (registry_status in (
                         'pending',         -- not checked yet
                         'matched',         -- found in the VAT whitelist
                         'no_nip_on_site',  -- enrich found no NIP on the store's pages
                         'not_found',       -- NIP not in the VAT whitelist
                         'error'            -- API error, retry later
                       )),
  registry_checked_at  timestamptz,
  registry_raw         jsonb,                       -- {"vat": <whitelist subject>, "krs": <odpis>} as fetched

  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

create index companies_nip_idx on public.companies (nip);   -- not unique: one seller can run several domains
create index companies_registry_status_idx on public.companies (registry_status);
create index companies_enrich_status_idx on public.companies (enrich_status);

create trigger companies_set_updated_at
  before update on public.companies
  for each row execute function public.set_updated_at();

comment on table public.companies is 'One row per store domain, with the seller''s registry data (VAT whitelist, KRS).';

-- ---------------------------------------------------------------------------------------------
-- company_sources: which store list each domain came from (a domain can be on several)
-- ---------------------------------------------------------------------------------------------

create table public.company_sources (
  company_id   bigint not null references public.companies (id) on delete cascade,
  source       text not null,                       -- `source` column of the data/stores-*.csv files: google_serp, shoper, shopify_blog
  source_url   text,                                -- page the domain was found on, when there is one
  details      jsonb,                               -- e.g. {"query": "...", "best_rank": 1, "categories": [...]}
  found_at     timestamptz not null default now(),
  primary key (company_id, source)
);

comment on table public.company_sources is 'Store list(s) each domain was collected from.';

-- ---------------------------------------------------------------------------------------------
-- signals: every observed fact about a store, with its source; append-only history
-- ---------------------------------------------------------------------------------------------

create table public.signals (
  id           bigint generated always as identity primary key,
  company_id   bigint not null references public.companies (id) on delete cascade,
  type         text not null,                       -- id from config/icp.yaml: platform, category_fit, assortment_size, meta_ads_active, ...
  value        jsonb not null,                      -- JSON scalar or object: true, "woocommerce", 5400, {"count": 120}
  source       text not null,                       -- data_sources id from config/icp.yaml: website, meta_ad_library, vat_whitelist, krs
  source_url   text not null,                       -- the page or API call the fact was read from
  details      jsonb,                               -- evidence: matched patterns, keywords, sitemap files, counting method
  observed_at  timestamptz not null default now(),
  run_id       bigint references public.runs (id) on delete set null
);

create index signals_company_type_idx on public.signals (company_id, type, observed_at desc);
create index signals_type_idx on public.signals (type);
create index signals_run_idx on public.signals (run_id);

comment on table public.signals is 'Facts and buying signals about a store, one per row, each with its source URL. New observations are added, old ones kept.';

-- ---------------------------------------------------------------------------------------------
-- research: company cards written by the research agent, with ICP score
-- ---------------------------------------------------------------------------------------------

create table public.research (
  id                bigint generated always as identity primary key,
  company_id        bigint not null references public.companies (id) on delete cascade,
  card              jsonb not null,                 -- the card from docs/research-card.md; every fact carries its source URL
  score             smallint check (score between 0 and 100),
  score_breakdown   jsonb,                          -- points per scoring component in config/icp.yaml
  fit               text check (fit in ('high', 'medium', 'low', 'disqualified')),
  disqualifiers     text[],                         -- disqualifier ids from config/icp.yaml that applied
  agent_version     text not null,                  -- prompt / script version, so before-and-after evals stay apart
  model             text,
  input_tokens      integer check (input_tokens >= 0),
  output_tokens     integer check (output_tokens >= 0),
  duration_seconds  numeric(8, 1) check (duration_seconds >= 0),
  cost_usd          numeric(10, 4) check (cost_usd >= 0), -- API-price equivalent
  run_id            bigint references public.runs (id) on delete set null,
  created_at        timestamptz not null default now()
);

create index research_company_idx on public.research (company_id, created_at desc);
create index research_run_idx on public.research (run_id);

comment on table public.research is 'Company cards from the research agent. A company can have several (re-runs, agent versions); the latest is the current one.';

-- ---------------------------------------------------------------------------------------------
-- leads: demo-form submissions and how the inbound workflow handled them
-- ---------------------------------------------------------------------------------------------

create table public.leads (
  id                   bigint generated always as identity primary key,
  created_at           timestamptz not null default now(),   -- form submitted
  contact_name         text,
  email                text not null,
  domain               text not null,              -- as typed in the form
  company_id           bigint references public.companies (id) on delete set null,
  research_id          bigint references public.research (id) on delete set null,
  status               text not null default 'new'
                       check (status in ('new', 'processing', 'done', 'manual_review', 'error')),
  error                text,                       -- why the lead went to manual review or failed
  hubspot_company_id   text,
  hubspot_contact_id   text,
  slack_sent_at        timestamptz,                -- slack_sent_at - created_at = time to the card on Slack
  reply_draft          text,
  processed_at         timestamptz,
  updated_at           timestamptz not null default now()
);

create index leads_status_idx on public.leads (status);
create index leads_company_idx on public.leads (company_id);
create index leads_research_idx on public.leads (research_id);

create trigger leads_set_updated_at
  before update on public.leads
  for each row execute function public.set_updated_at();

comment on table public.leads is 'Submissions from the demo "Book a demo" form and their processing by the n8n workflow.';

-- ---------------------------------------------------------------------------------------------
-- messages: outbound drafts per company, variant and sequence step, with quality scores
-- ---------------------------------------------------------------------------------------------

create table public.messages (
  id                 bigint generated always as identity primary key,
  company_id         bigint not null references public.companies (id) on delete cascade,
  research_id        bigint references public.research (id) on delete set null,  -- card the message was written from
  experiment         text not null default 'outbound-01',
  variant            text not null check (variant in ('A', 'B')),    -- A = signal-based, B = generic
  step               smallint not null check (step between 1 and 3), -- position in the sequence
  subject            text,
  body               text not null,
  rubric_scores      jsonb,                         -- AI reviewer: {"criterion_id": 0..2, ...} from evals/message-rubric.md
  ai_score           numeric(4, 1),
  human_score        numeric(4, 1),                 -- blind score, only for the sampled pairs
  unsourced_facts    smallint check (unsourced_facts >= 0), -- facts with no source in the card (target: 0)
  hubspot_task_id    text,
  created_at         timestamptz not null default now(),
  unique (experiment, company_id, variant, step)
);

create index messages_company_idx on public.messages (company_id);
create index messages_research_idx on public.messages (research_id);

comment on table public.messages is 'Outbound message drafts (never sent) and their quality scores.';

-- ---------------------------------------------------------------------------------------------
-- Views for SQL questions and Looker Studio (security_invoker: they obey the tables' RLS)
-- ---------------------------------------------------------------------------------------------

create view public.signals_latest
with (security_invoker = on) as
select distinct on (company_id, type)
  company_id, type, value, source, source_url, observed_at
from public.signals
order by company_id, type, observed_at desc, id desc;

comment on view public.signals_latest is 'Most recent observation of each signal type per company.';

create view public.research_latest
with (security_invoker = on) as
select distinct on (company_id) *
from public.research
order by company_id, created_at desc, id desc;

comment on view public.research_latest is 'Most recent company card per company.';

-- One flat row per company. The signal columns use the ICP ids of the demo scenario; a different
-- config/icp.yaml may need different columns here, the tables stay the same.
create view public.company_profile
with (security_invoker = on) as
select
  c.id,
  c.domain,
  c.name,
  c.nip,
  c.krs,
  c.legal_form,
  c.vat_status,
  c.city,
  c.registry_status,
  (c.krs is not null)                                                              as registered_company,
  (select string_agg(cs.source, ', ' order by cs.source)
     from public.company_sources cs where cs.company_id = c.id)                     as list_sources,
  max(s.value #>> '{}') filter (where s.type = 'platform')                         as platform,
  max(s.value #>> '{}') filter (where s.type = 'category_fit')                     as category,
  max(case when s.type = 'assortment_size' and s.value #>> '{}' ~ '^[0-9]+$'
           then (s.value #>> '{}')::integer end)                                   as assortment_size,
  bool_or(case when s.type = 'meta_ads_active' and jsonb_typeof(s.value) = 'boolean'
               then (s.value)::boolean end)                                        as meta_ads_active,
  r.score,
  r.fit,
  r.created_at                                                                     as researched_at,
  c.created_at
from public.companies c
left join public.signals_latest s on s.company_id = c.id
left join public.research_latest r on r.company_id = c.id
group by c.id, r.score, r.fit, r.created_at;

comment on view public.company_profile is 'One row per company: registry data, latest ICP signals and latest score. Feeds sql/queries.sql and Looker Studio.';

-- ---------------------------------------------------------------------------------------------
-- Row level security: on everywhere, no policies (see the header)
-- ---------------------------------------------------------------------------------------------

alter table public.runs            enable row level security;
alter table public.companies       enable row level security;
alter table public.company_sources enable row level security;
alter table public.signals         enable row level security;
alter table public.research        enable row level security;
alter table public.leads           enable row level security;
alter table public.messages        enable row level security;
