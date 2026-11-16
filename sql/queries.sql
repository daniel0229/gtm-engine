-- GTM Engine: business questions answered from the database (step 1.6).
--
-- Run in the Supabase SQL editor or any Postgres client. They read the views in sql/schema.sql:
-- `company_icp` (one row per company with the ICP rules from config/icp.yaml applied) and
-- `signals`. icp_status is `fit`, `out` or `needs_check` (the pipeline has not collected enough
-- yet: registry not checked, assortment not counted, site not readable), so "not known yet" is
-- never counted as "does not fit".
--
-- Queries 1-5 answer business questions; query 6 checks data quality (the phase 1 target:
-- at least 90% of stores with the key fields filled).


-- =============================================================================================
-- 1. How many of the collected stores fit the ICP, and where do the others drop out?
--    Market sizing for the demo company: the funnel from collected domains to ICP-fit stores.
--    Each company counts once, at the first ICP rule it fails, in the order the pipeline checks.
-- =============================================================================================
with steps (step_no, icp_reason, step) as (
  values
    (1,  'not_enriched',           'Website not read yet'),
    (2,  'site_unreadable',        'Website blocked or down'),
    (3,  'not_a_store',            'Not an online store'),
    (4,  'excluded_category',      'Excluded category (food, digital, services)'),
    (5,  'category_outside_icp',   'Category outside the ICP'),
    (6,  'size_unknown',           'Assortment not counted'),
    (7,  'too_small',              'Under 500 products'),
    (8,  'too_large',              'Over 60,000 products'),
    (9,  'no_nip_on_site',         'No NIP on the website'),
    (10, 'registry_pending',       'Registry not checked yet'),
    (11, 'seller_not_in_registry', 'NIP not in the VAT whitelist'),
    (12, 'vat_inactive',           'VAT status not active')
),
dropped as (
  select icp_reason, count(*) as companies from public.company_icp group by icp_reason
),
total as (select count(*) as n from public.company_icp)
select
  s.step_no,
  s.step,
  case when s.icp_reason in ('not_enriched', 'site_unreadable', 'size_unknown', 'registry_pending')
       then 'needs_check' else 'out' end                                     as status,
  coalesce(d.companies, 0)                                                   as dropped_here,
  t.n - sum(coalesce(d.companies, 0)) over (order by s.step_no)              as still_in
from steps s
cross join total t
left join dropped d on d.icp_reason = s.icp_reason
union all
select 13, 'Fits the ICP', 'fit', null,
       (select count(*) from public.company_icp where icp_status = 'fit')
order by step_no;


-- =============================================================================================
-- 2. Which store platforms should the demo company build integrations for first?
--    Stores and ICP-fit stores per platform, with how many run Meta ads. Platforms with many
--    ICP-fit stores and few built-in returns tools (see platform weights in config/icp.yaml) go
--    first. `meta_ads_not_checked` > 0 means step 1.5 has not covered those stores yet.
-- =============================================================================================
select
  coalesce(platform, 'unknown')                                              as platform,
  count(*)                                                                   as stores,
  count(*) filter (where icp_status = 'fit')                                 as icp_fit,
  count(*) filter (where icp_status = 'needs_check')                         as needs_check,
  round(100.0 * count(*) filter (where icp_status = 'fit') / count(*), 1)    as icp_fit_pct,
  count(*) filter (where meta_ads_active)                                    as with_meta_ads,
  count(*) filter (where meta_ads_active is null)                            as meta_ads_not_checked
from public.company_icp
where is_store
group by 1
order by icp_fit desc, stores desc;


-- =============================================================================================
-- 3. In which product categories are the mid-sized stores?
--    Stores per category and assortment size band. `core` = 1,000-30,000 products (full points
--    in config/icp.yaml), `edge` = 500-999 or 30,001-60,000 (half points). Shows which categories
--    have enough stores of the right size to be worth a dedicated campaign.
-- =============================================================================================
select
  coalesce(category, 'unknown')                                              as category,
  count(*)                                                                   as stores,
  count(*) filter (where size_band = 'core')                                 as size_core,
  count(*) filter (where size_band = 'edge')                                 as size_edge,
  count(*) filter (where size_band = 'too_small')                            as too_small,
  count(*) filter (where size_band = 'too_large')                            as too_large,
  count(*) filter (where size_band is null)                                  as size_unknown,
  percentile_cont(0.5) within group (order by assortment_size)::integer      as median_products,
  count(*) filter (where icp_status = 'fit')                                 as icp_fit
from public.company_icp
where is_store
group by 1
order by count(*) filter (where size_band in ('core', 'edge')) desc, stores desc;


-- =============================================================================================
-- 4. Do ICP-fit stores advertise on Meta, and how many ads do they run?
--    Active ads are the buying signal (paid traffic = growing orders and returns, and a budget).
--    Compares ICP-fit stores with the others; the ad count comes from the latest
--    meta_ads_active signal (details.active_ads), written by src/meta_ads.py.
-- =============================================================================================
with ads as (
  select distinct on (company_id)
    company_id,
    (value)::boolean                                                         as active,
    (details ->> 'active_ads')::integer                                      as active_ads
  from public.signals
  where type = 'meta_ads_active' and jsonb_typeof(value) = 'boolean'
  order by company_id, observed_at desc, id desc
)
select
  i.icp_status,
  count(*)                                                                   as stores,
  count(a.company_id)                                                        as checked,
  count(*) filter (where a.active)                                           as with_active_ads,
  round(100.0 * count(*) filter (where a.active) / nullif(count(a.company_id), 0), 1)
                                                                             as with_ads_pct_of_checked,
  percentile_cont(0.5) within group (order by a.active_ads)
    filter (where a.active)                                                  as median_ads_if_active,
  max(a.active_ads)                                                          as max_ads
from public.company_icp i
left join ads a on a.company_id = i.id
where i.is_store
group by i.icp_status
order by case i.icp_status when 'fit' then 1 when 'needs_check' then 2 else 3 end;


-- =============================================================================================
-- 5. Which store list is worth collecting more domains from?
--    For each source list (step 1.1): domains collected, how many turned out to be stores, and
--    how many fit the ICP. A domain on two lists counts for both. The list with the highest
--    share of ICP-fit stores per domain collected is the best place to look for more.
-- =============================================================================================
select
  cs.source,
  count(*)                                                                   as domains,
  count(*) filter (where i.is_store)                                         as stores,
  count(*) filter (where i.icp_status = 'fit')                               as icp_fit,
  count(*) filter (where i.icp_status = 'needs_check')                       as needs_check,
  round(100.0 * count(*) filter (where i.icp_status = 'fit') / count(*), 1)  as icp_fit_pct_of_domains
from public.company_sources cs
join public.company_icp i on i.id = cs.company_id
group by cs.source
order by icp_fit_pct_of_domains desc;


-- =============================================================================================
-- 6. Data quality: what share of stores has each key field filled?
--    Phase 1 target: at least 90% of stores with the key fields. Base = companies the enrich
--    step recognised as stores. A field counts as filled when the pipeline found a value
--    (platform `unknown` and category `other` count as missing).
-- =============================================================================================
select
  count(*)                                                                   as stores,
  round(100.0 * count(*) filter (where platform is not null and platform <> 'unknown') / count(*), 1)
                                                                             as platform_pct,
  round(100.0 * count(*) filter (where category is not null and category <> 'other') / count(*), 1)
                                                                             as category_pct,
  round(100.0 * count(*) filter (where assortment_size is not null) / count(*), 1)
                                                                             as assortment_pct,
  round(100.0 * count(*) filter (where nip is not null) / count(*), 1)       as nip_pct,
  round(100.0 * count(*) filter (where registry_status = 'matched') / count(*), 1)
                                                                             as registry_matched_pct,
  round(100.0 * count(*) filter (where meta_ads_active is not null) / count(*), 1)
                                                                             as meta_ads_checked_pct,
  round(100.0 * count(*) filter (where platform is not null and platform <> 'unknown'
                                   and category is not null and category <> 'other'
                                   and assortment_size is not null and nip is not null) / count(*), 1)
                                                                             as all_website_fields_pct
from public.company_icp
where is_store;
