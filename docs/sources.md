# Data sources

Where the GTM Engine gets its data, what each source allows and what its limits are. Every record in the database points back to one of the sources listed here.

The domains collected from these lists are kept in `data/` (gitignored): `stores-serp.csv` (Google results) and `stores-lists.csv` (store lists). Together: 1,235 unique domains, 14 found in both. This file lists only the public pages they come from.

## 1. Store lists

Public pages that list many Polish online stores in one place. A script reads them, collects the domains and keeps the source of each one. Filtering against the ICP happens later (enrich and registry steps), so a list may include stores that will not fit.

**How to add a source:** paste the URL and a short note into the table. The other columns are filled in when the source is checked.


| #   | Source    | URL                                                                                                    | Type | What's on it (note)               | Stores (approx.) | Extraction (HTML / pagination / JS / manual) | Terms and robots.txt allow it? | Checked on |
| --- | --------- | ------------------------------------------------------------------------------------------------------ | ---- | --------------------------------- | ---------------- | -------------------------------------------- | ------------------------------ | ---------- |
| 1 | Shopify blog: Polish stores on Shopify | https://www.shopify.com/pl/blog/sklepy-na-shopify | ranking | Article listing Polish stores running on Shopify, with links | 51 collected (2026-11-14) | HTML, store links in the article; Shopify's own sites dropped; [`src/collect_lists.py`](../src/collect_lists.py) | yes | 2026-11-14 |
| 2 | Shoper client gallery | https://www.shoper.pl/klienci (URLs from https://www.shoper.pl/sitemap.xml) | platform showcase | One page per client store, the store's domain is in the URL slug (`/klienci/pinkbox-com-pl`) | 92 in the sitemap, 91 resolved (2026-11-14) | [`src/collect_lists.py`](../src/collect_lists.py): the sitemap lists every client page; the pages themselves sit behind a Cloudflare check (403), so the domain is read from the slug (`chochliki-sklep-pl` → `chochliki.sklep.pl`) and the first candidate that answers over HTTP is kept; 1 slug matched no live domain, 1 store now redirects to a different domain (noted in the output) | robots.txt allows `/`; sitemap is public | 2026-11-13 |
| 3 | Google search results for category queries | queried through the DataForSEO SERP API (Google, Poland, Polish) | other | Organic top results for queries like "sklep internetowy obuwie damskie", several dozen per ICP category; marketplaces and large chains filtered out | 1,107 unique domains after the blocklist (2026-11-13, 0.60 USD); ICP fit is decided later | API; queries in [`config/store-search-queries.yaml`](../config/store-search-queries.yaml); about 0.002 USD per 10 results (observed 2026-11-09), so 150 queries × top 20 ≈ 0.60 USD | paid API, results used only to collect domains | 2026-11-13 |


Type: `platform showcase` (a store platform's or agency's client list), `directory` (a review site or price comparison catalogue), `ranking` (an industry ranking or article), `other`.

### Rejected sources


| Source | URL | Why rejected |
| --- | --- | --- |
| IdoSell case studies | https://www.idosell.com/en/shop/casestudy/ | only a dozen or so stores |
| Opineo and Ceneo store rankings | (press coverage of past editions) | mostly old editions and the largest stores, outside the mid-sized ICP |
| StoreLeads | https://storeleads.app/reports/shopify/PL/top-stores | full list needs an account; collecting it with a script likely breaks the terms |
| Trusted Shops | https://business.trustedshops.pl | no public list of certified Polish stores found |


## 2. Company registries

Both are queried by number only (NIP, KRS), so they enrich a store already found and cannot produce a list of stores. Sample responses: [`api-samples/`](api-samples/README.md).

### VAT whitelist (Ministry of Finance)


| Item                                             | Value                                                                                                     | Source            |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------- | ----------------- |
| Base URL                                         | `https://wl-api.mf.gov.pl`                                                                                | api-samples       |
| API key                                          | not needed                                                                                                | tested 2026-11-12 |
| Daily request limit | 100 `search` calls a day; over the limit, access may be blocked until midnight | [gov.pl/web/kas/api-wykazu-podatnikow-vat](https://www.gov.pl/web/kas/api-wykazu-podatnikow-vat), checked 2026-11-13 |
| Max NIPs in one batch call (`/api/search/nips/`) | 30, so up to 3,000 NIPs a day, enough for the whole list in one run | same page |
| Rate-limit headers in responses                  | none seen                                                                                                 | tested 2026-11-12 |
| Fields used                                      | `name`, `nip`, `regon`, `krs`, `statusVat`, `residenceAddress`, `workingAddress`, `registrationLegalDate` | api-samples       |


### KRS (Ministry of Justice)


| Item          | Value                                                                                   | Source            |
| ------------- | --------------------------------------------------------------------------------------- | ----------------- |
| Base URL      | `https://api-krs.ms.gov.pl`                                                             | api-samples       |
| API key       | not needed                                                                              | tested 2026-11-12 |
| Request limit | no published limit found on the docs portal ([prs.ms.gov.pl/krs/openApi](https://prs.ms.gov.pl/krs/openApi)) or elsewhere; the script keeps to 1 request a second and backs off on HTTP 429 | checked 2026-11-13 |
| Fields used   | legal form, date of entry into KRS, registered address, management board size and roles | api-samples       |
| Limitation    | board members' names and PESEL are masked (`"F*****"`)                                  | tested 2026-11-12 |
| Not covered   | sole proprietorships (JDG) have no KRS entry; their record ends at the VAT whitelist    | –                 |


## 3. Signals


| Signal             | Source                                                    | How it is read                             | Limits                           |
| ------------------ | --------------------------------------------------------- | ------------------------------------------ | -------------------------------- |
| Active Meta ads    | Meta Ad Library                                           | *to decide: Ad Library API or public page* | *to confirm*                     |
| Store platform     | Store's own page code; IP address for Shopify             | [`src/enrich.py`](../src/enrich.py): weighted patterns in the homepage code (a single stray mention is not enough), the `generator` meta tag, response headers; a host in Shopify's network (23.227.38.0/24) counts as Shopify | pages rendered only by JavaScript show little code |
| Product category   | Homepage title, meta description, h1, menu links          | keywords per category in [`config/category-keywords.yaml`](../config/category-keywords.yaml), plus the category of the Google query that found the domain | "other" when no category scores 3 points |
| Assortment size    | Store's sitemaps (from robots.txt, else `/sitemap.xml`)   | products in product sitemaps, or product URLs counted by the platform's URL pattern (one per product id, so language copies count once); above 25 sitemap files the rest is estimated | no sitemap or unknown URL pattern = no value |
| Is it a store      | Homepage code                                             | cart links, add-to-cart buttons, Product markup, a store platform | – |
| Seller NIP         | Homepage footer and JSON-LD, contact, about, terms (regulamin), privacy policy as a last resort | checksum-validated; each NIP gets points for where it stands (footer, contact, start of the terms, after "Sprzedawcą jest") and loses points after a third party's name (PayU, Przelewy24, InPost, IdoSell, ...) | terms loaded only by JavaScript or as PDF are not read |

The enrich step respects each store's robots.txt and waits 1 s between requests to the same store. Shopify rate-limits per client IP across all its stores (HTTP 429), so requests to Shopify-hosted stores share one 1.5 s pause, and a store answering 429 is retried on the next run instead of being marked as read.
