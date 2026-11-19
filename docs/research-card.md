# Research card

The research agent writes one card per store. A salesperson reads it before a call or before an outbound message, and phase 3 posts it to Slack when a demo request comes in. This file defines what the card contains, where each piece comes from, and how the fit score is computed.

**Rule: every fact has a source URL. A fact without a source does not enter the card.** The script that saves cards drops such facts and counts them in the run log.

## What the card answers

| Question | Card section | Where it comes from |
|---|---|---|
| Who runs the store? | `seller` | VAT whitelist and KRS (phase 1, `companies`) |
| What do they sell, and to whom? | `what_they_sell` | Store website (agent), category and product count (phase 1, `signals`) |
| What platform? | `platform` | Page code (phase 1, `signals`) |
| Are they spending on growth? | `signals` | Meta Ad Library (phase 1, `signals`); hiring signal from step 2.3 |
| How do returns work today? | `returns` | Returns page and terms (agent) |
| What stands out about this store? | `observations` (2–3) | Store website (agent) |
| What problem could Odsyłka solve here? | `problem_hypothesis` | Agent, built only from facts already in the card |
| How well do they fit, and why? | `fit` | Score computed by code from `config/icp.yaml`; reasoning written by the agent |
| What could not be found? | `gaps` | Agent |

The card covers every field of the manual research in [`baseline.md`](baseline.md) (category and product count, platform, Meta ads, return policy, legal form and NIP, fit with one sentence why), so the time comparison in step 2.6 is like for like. It adds what manual research skipped: concrete observations, a problem hypothesis, and a score that applies every ICP rule (the manual research missed the excluded-category rule on Store B).

## Fact format

Every fact in the card is an object:

```json
{ "value": "fashion", "source_url": "https://store-a.example/", "quote": "Sukienki, swetry i botki", "origin": "agent" }
```

| Key | Required | Meaning |
|---|---|---|
| `value` | yes | The fact: string, number or boolean. `null` is not allowed; a missing fact goes to `gaps` instead. |
| `source_url` | yes | The exact page or API call the fact was read from. Must be on the store's domain (or a subdomain), `wl-api.mf.gov.pl`, `api-krs.ms.gov.pl` or `facebook.com/ads/library`. |
| `quote` | for `origin: agent` | Up to 30 words copied from the page, in the page's language, or a list of up to 3 such quotes when one fact needs several (a price range, several return channels; `product_types` may have up to 6, one menu label per type). The save script checks that every quote appears in the fetched page text; a fact whose quote is not on the page is dropped as unverified. |
| `evidence` | no | `text` (default) or `code`. Use `code` when the fact is only in the page source, not in the visible text: a returns tool linked from a button (`order.baselinker.com/return/…`), a plugin's config, a Google Form behind a link. The quote is then checked against the raw HTML. |
| `origin` | yes | `pipeline` = copied from `signals` or `companies` (checked in phase 1), `agent` = found in this research. The evaluation in step 2.4 reports accuracy for both. |

Text the agent writes itself (`tldr`, `summary`, `why_it_matters`, the hypothesis, the fit reasoning) is not a fact. It may only restate facts that are already in the card, and it lists them in `based_on` by their path in the card (for example `returns.how_to_return`).

## Sections

### `seller` (from the registries, `origin: pipeline`)

| Field | Example | Source |
|---|---|---|
| `name` | registered name | VAT whitelist |
| `nip` | 10 digits | store's terms or footer (`companies.nip_source_url`) |
| `legal_form` | `SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ`, or `null` for a seller without a KRS entry | KRS |
| `vat_status` | `Czynny` | VAT whitelist |
| `registered_company` | `true` / `false` | KRS / VAT whitelist (ICP signal) |

### `what_they_sell`

| Field | Meaning | Origin |
|---|---|---|
| `summary` | One sentence: main product types and who they are for | agent text, `based_on` |
| `main_category` | One id from `signals.category_fit.values` in `config/icp.yaml` | pipeline; the agent may dispute it (see `checks`) |
| `product_types` | 3–6 product types from the main menu | agent |
| `audience` | Who the store sells to, as the site says it: women, men, kids, a sport, a style. No ages or incomes unless the page states them | agent |
| `price_range` | Lowest and highest price seen, in PLN, with `seen_on` (homepage tiles are featured products, a listing page is closer to the full range) | agent |
| `assortment_size` | Product count from the sitemap | pipeline |
| `sells_b2c` | The site sells to consumers (cart open to anyone, consumer terms) | agent |

### `platform`

One id from `signals.platform.values` in `config/icp.yaml`, from the page code (`origin: pipeline`).

### `signals`

| Field | Meaning | Origin |
|---|---|---|
| `meta_ads_active` | `true` / `false`, plus `active_ads` and `oldest_active_ad_start` | pipeline (Meta Ad Library page of the store) |
| `hiring` | `true` if the store's own careers page lists an open role in e-commerce, marketing, customer service or warehouse; `false` if it lists none or only other roles. `roles` lists every open role as `{title, area}`; `relevant_roles` counts the first four areas. Left out (a gap) when there is no careers page on the store's domain. Also saved to `signals` as type `hiring` | agent (step 2.3) |

### `returns`

How a customer returns a product today. This is what Odsyłka sells into, and it was the slowest field in the manual research.

| Field | Values | Origin |
|---|---|---|
| `window_days` | 14, 30, 100, … | agent |
| `free_returns` | `true` / `false` (who pays for the return shipping) | agent |
| `how_to_return` | `email`, `printed_form`, `online_form`, `returns_portal`, `parcel_locker_code`, `in_store`, `contact_first` | agent |
| `returns_tool` | Named tool if one is visible on the page or in its code: `shoper_zwroty`, `retjet`, `furgonetka`, `baselinker_returns`, `easyreturn_withdrawal_button`, another name, or `own_form`. A named returns app is a competitor and changes the hypothesis | agent |
| `withdrawal_function` | `true` if the site has an online "withdraw from contract" form or button (EU Directive 2023/2673, see `why_now` in `config/icp.yaml`) | agent |
| `exchanges` | `true` if the store offers size or product exchanges | agent |
| `refund_days` | Days to refund promised on the returns page (the law allows 14; a shorter promise is a tighter deadline to track) | agent |

### `observations` (2 or 3)

Concrete things about this store that a salesperson could mention in the first sentence of a message. Each one has `text`, `source_url`, `quote` and `why_it_matters` (one sentence tying it to returns, growth or the buying triggers in `config/icp.yaml`).

Good observations are specific to this store and checkable on one page: a printed return form, a size-exchange offer, a sale that starts next week, a "most returned" note in the FAQ, a new collection launch. Generic statements ("the store has a modern design", "they care about customers") are not observations.

### `problem_hypothesis`

| Field | Meaning |
|---|---|
| `statement` | One or two sentences: the returns problem this store probably has, phrased as a hypothesis |
| `persona` | Persona id from `config/icp.yaml` (`head_of_ecommerce`, `returns_lead`, `owner_ceo`) |
| `pain` | One `pain` from `how_we_help` in `config/icp.yaml`, copied exactly |
| `capability` | The matching `capability` from the same entry |
| `based_on` | Paths of the facts and observations the hypothesis rests on (at least 2) |
| `confidence` | `high` (the returns process is visible and manual), `medium` (indirect signs), `low` (no returns facts found) |

### `fit`

The score is **computed by code**, not by the model, from the facts in the card and `scoring` in `config/icp.yaml` (v2). The same facts always give the same score, and the score matches the rules of the `company_icp` view.

| Component | Weight | Points |
|---|---|---|
| `meta_ads_active` | 25 | 25 if `true`, 0 if `false` or not checked |
| `platform` | 20 | 20 × platform value (woocommerce, prestashop 1.0; shopify, idosell 0.8; magento 0.7; shoper 0.5; custom 0.3; unknown 0) |
| `category_fit` | 25 | 25 × category value (fashion, footwear 1.0; accessories, sportswear 0.8; kids 0.7; home_decor 0.6; …) |
| `assortment_size` | 20 | 20 for 1,000–30,000 products; 10 for 500–999 or 30,001–60,000; 0 otherwise or unknown |
| `registered_company` | 10 | 10 if the seller is in KRS, 0 otherwise |

Points are summed and rounded half up to a whole number at the end (kids: 25 × 0.7 = 17.5). Labels use `scoring.thresholds`: `high` from 70, `medium` 40–69, `low` below 40. If any disqualifier or out-of-scope rule from `config/icp.yaml` applies (for example `b2b_only`, `excluded_category`, `marketplace_only`, `dropshipping`), the label is `disqualified`, the score is still computed and stored, and `disqualifiers` lists the ids with the fact that triggered each one.

The agent writes `reasoning`: two or three sentences on why the score is what it is and what would change it, citing facts by path. Points never depend on that text.

### `checks`

When the agent's reading of the site disagrees with a phase 1 signal (for example the keyword rule said `accessories` but the menu is all shoes), it records `{ "signal": "category_fit", "pipeline_value": "accessories", "agent_value": "footwear", "source_url": ..., "quote": ... }`. The score uses the pipeline value until the disagreement is reviewed, so the evaluation can measure both.

### `gaps` and `pages_read`

`gaps` lists what the agent looked for and did not find, with the pages it checked (for example "no returns page; checked /regulamin, /zwroty, /faq"). A missing fact is written here, never guessed. `pages_read` lists every URL the agent fetched, at most 10 per store.

## Example (fictional store)

```json
{
  "card_version": 1,
  "domain": "store-a.example",
  "tldr": "Women's fashion on Shopify, about 2,300 products, 18 active Meta ads. Returns by e-mail with a printed form, size exchanges offered, Black Friday sale ahead. Fit 86/100 (high).",
  "seller": {
    "name": { "value": "Store A Anna Example", "source_url": "https://wl-api.mf.gov.pl/api/search/nip/1234567890?date=2026-11-17", "origin": "pipeline" },
    "nip": { "value": "1234567890", "source_url": "https://store-a.example/pages/regulamin", "origin": "pipeline" },
    "legal_form": null,
    "vat_status": { "value": "Czynny", "source_url": "https://wl-api.mf.gov.pl/api/search/nip/1234567890?date=2026-11-17", "origin": "pipeline" },
    "registered_company": { "value": false, "source_url": "https://wl-api.mf.gov.pl/api/search/nip/1234567890?date=2026-11-17", "origin": "pipeline" }
  },
  "what_they_sell": {
    "summary": { "text": "Women's dresses, knitwear and boots in the mid price range.", "based_on": ["what_they_sell.product_types", "what_they_sell.audience", "what_they_sell.price_range"] },
    "main_category": { "value": "fashion", "source_url": "https://store-a.example/", "origin": "pipeline" },
    "product_types": { "value": ["dresses", "knitwear", "coats", "boots"], "source_url": "https://store-a.example/", "quote": "Sukienki Swetry Płaszcze Botki", "origin": "agent" },
    "audience": { "value": "women", "source_url": "https://store-a.example/", "quote": "Moda dla kobiet, szyta w Polsce", "origin": "agent" },
    "price_range": { "value": { "min": 129, "max": 459, "currency": "PLN" }, "source_url": "https://store-a.example/collections/all", "quote": "od 129,00 zł … 459,00 zł", "origin": "agent" },
    "assortment_size": { "value": 2300, "source_url": "https://store-a.example/sitemap_products_1.xml", "origin": "pipeline" },
    "sells_b2c": { "value": true, "source_url": "https://store-a.example/pages/regulamin", "quote": "Umowa zawierana jest z Konsumentem", "origin": "agent" }
  },
  "platform": { "value": "shopify", "source_url": "https://store-a.example/", "origin": "pipeline" },
  "signals": {
    "meta_ads_active": { "value": true, "active_ads": 18, "oldest_active_ad_start": "2026-09-02", "source_url": "https://www.facebook.com/ads/library/?active_status=active&country=PL&view_all_page_id=PAGE_ID", "origin": "pipeline" }
  },
  "returns": {
    "window_days": { "value": 30, "source_url": "https://store-a.example/pages/zwroty", "quote": "Masz 30 dni na zwrot", "origin": "agent" },
    "free_returns": { "value": false, "source_url": "https://store-a.example/pages/zwroty", "quote": "Koszt odesłania towaru pokrywa Klient", "origin": "agent" },
    "how_to_return": { "value": ["printed_form", "email"], "source_url": "https://store-a.example/pages/zwroty", "quote": "Wydrukuj formularz zwrotu, dołącz go do paczki i wyślij numer zamówienia na adres e-mail", "origin": "agent" },
    "returns_tool": { "value": "own_form", "source_url": "https://store-a.example/pages/zwroty", "quote": "Pobierz formularz zwrotu (PDF)", "origin": "agent" },
    "exchanges": { "value": true, "source_url": "https://store-a.example/pages/zwroty", "quote": "Wymiana rozmiaru jest bezpłatna", "origin": "agent" }
  },
  "observations": [
    { "id": "o1", "text": "Returns need a printed PDF form plus an e-mail with the order number.", "source_url": "https://store-a.example/pages/zwroty", "quote": "Wydrukuj formularz zwrotu, dołącz go do paczki i wyślij numer zamówienia na adres e-mail", "why_it_matters": "Every return is matched to an order by hand from an e-mail and a paper form." },
    { "id": "o2", "text": "Size exchanges are free.", "source_url": "https://store-a.example/pages/zwroty", "quote": "Wymiana rozmiaru jest bezpłatna", "why_it_matters": "Customers are invited to order and swap sizes, so return volume follows sales volume." },
    { "id": "o3", "text": "A Black Friday sale of up to 30% runs from 24 November.", "source_url": "https://store-a.example/", "quote": "Black Friday do -30% od 24.11", "why_it_matters": "A sale peak now means a returns peak in December (buying trigger in config/icp.yaml)." }
  ],
  "problem_hypothesis": {
    "statement": "Returns run on a printed form and e-mail, with free size exchanges and a Black Friday sale coming, so the team probably copies return data by hand and will be short of time in December.",
    "persona": "returns_lead",
    "pain": "Copies the same data between e-mail, spreadsheet, store admin and bank",
    "capability": "One returns panel with order data, label, inspection and refund",
    "based_on": ["returns.how_to_return", "observations.o1", "observations.o2", "observations.o3"],
    "confidence": "high"
  },
  "fit": {
    "score": 86,
    "label": "high",
    "components": [
      { "ref": "meta_ads_active", "weight": 25, "value": true, "points": 25, "based_on": "signals.meta_ads_active" },
      { "ref": "platform", "weight": 20, "value": "shopify", "points": 16, "based_on": "platform" },
      { "ref": "category_fit", "weight": 25, "value": "fashion", "points": 25, "based_on": "what_they_sell.main_category" },
      { "ref": "assortment_size", "weight": 20, "value": 2300, "points": 20, "based_on": "what_they_sell.assortment_size" },
      { "ref": "registered_company", "weight": 10, "value": false, "points": 0, "based_on": "seller.registered_company" }
    ],
    "disqualifiers": [],
    "reasoning": "Fashion store in the core size band with active Meta ads, so returns are a daily process. It loses 4 points for Shopify, where returns apps already compete, and 10 for a seller without a KRS entry, which usually means a smaller team and budget."
  },
  "checks": [],
  "gaps": [
    { "field": "returns.withdrawal_function", "note": "No online withdrawal form or button found", "pages_checked": ["https://store-a.example/pages/zwroty", "https://store-a.example/pages/regulamin", "https://store-a.example/pages/faq"] }
  ],
  "pages_read": ["https://store-a.example/", "https://store-a.example/collections/all", "https://store-a.example/pages/zwroty", "https://store-a.example/pages/regulamin", "https://store-a.example/pages/faq"]
}
```

## How the card is stored

One row in `research` (`sql/schema.sql`) per card:

| Column | Filled from |
|---|---|
| `card` | the whole JSON above |
| `score`, `fit` | `fit.score`, `fit.label` |
| `score_breakdown` | `fit.components` as `{ "meta_ads_active": 25, "platform": 16, … }` |
| `disqualifiers` | ids in `fit.disqualifiers` |
| `agent_version`, `model`, `input_tokens`, `output_tokens`, `duration_seconds`, `cost_usd`, `run_id` | the research run (`cost_usd` is the API-price equivalent of the `claude -p` call) |

Facts dropped by the save script (no source, source outside the allowed hosts, quote not found on the page) are counted in `runs.notes` for that run, so the evaluation can report them.

## Evaluation hooks (step 2.4)

Every fact has one URL and, for agent facts, a quote, so a reviewer checks it by opening one page and finding one sentence. The evaluation marks each fact `true`, `false` or `cannot_check` and reports accuracy separately for `origin: pipeline` and `origin: agent`, plus the share of observations that are specific to the store.
