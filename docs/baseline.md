# Baseline: manual research time

How long it takes a person to research one store by hand. The AI research agent is compared against this number later.

**Rules**
- Pick 3 stores that look like the ICP (`config/icp.yaml`): Polish, own domain, mid-sized, ideally fashion or footwear.
- Start the timer when you open the store's homepage and stop it when the fit decision is written down. Count everything in between: Googling, checking the page code, the Meta Ad Library.
- Use only public sources, the same ones the agent will have.
- This repo is public, so store names, domains and NIPs stay out of it (a NIP identifies the company in public registries). Write them as Store A, B and C. The real domains and NIPs live in `data/baseline-stores.md`, which is gitignored.

## Per store

### Store A

| Field | Answer | Source |
|---|---|---|
| What they sell (category, roughly how many products) | fashion, clothes, shoes | Store homepage |
| Platform (WooCommerce, Shopify, IdoSell…) | custom | Wappalyzer (Chrome extension) |
| Running ads now? (Meta Ad Library: yes/no, how many) | Yes, 120 | Meta Ad Library |
| Return policy (days, free returns or not, how returns are submitted) | 30 days, form on the site, customer pays for return | Separate returns page on the site |
| Legal form and NIP found on the site? | Sp. z o.o., NIP found (kept in private notes) | Store's terms and conditions (regulamin) |
| ICP fit (strong / weak / no) and one sentence why | strong, running ads, B2C |  |

- Start: `13:08` · End: `13:13` · **Minutes: 5**
- What took the longest: Looking for platform its running on

### Store B

| Field | Answer | Source |
|---|---|---|
| What they sell (category, roughly how many products) | coffee, tea, maintenance for coffee machines | Store homepage |
| Platform (WooCommerce, Shopify, IdoSell…) | IdoSell | Wappalyzer (Chrome extension) |
| Running ads now? (Meta Ad Library: yes/no, how many) | Yes, about 83 | Meta Ad Library |
| Return policy (days, free returns or not, how returns are submitted) | 14 days, store is paying for return, cheapest available method, email or inpost form | Store's terms and conditions (regulamin) |
| Legal form and NIP found on the site? | JDG (sole proprietorship), NIP found (kept in private notes) | Store's terms and conditions (regulamin) |
| ICP fit (strong / weak / no) and one sentence why | no: coffee and tea are food, an excluded category, and a sole proprietorship is not a KRS-registered company | `config/icp.yaml` (categories, firmographics) |

- Start: `13:14` · End: `13:20` · **Minutes: 6**
- What took the longest: Return policy
- Correction (2026-11-13): first written down as "strong" (running ads, B2C, IdoSell). The excluded category and the legal-form rule were missed, which shows how easy it is to skip a rule when researching by hand.

### Store C

| Field | Answer | Source |
|---|---|---|
| What they sell (category, roughly how many products) | fashion, clothes | Store homepage |
| Platform (WooCommerce, Shopify, IdoSell…) | Shopify | Wappalyzer (Chrome extension) |
| Running ads now? (Meta Ad Library: yes/no, how many) | Yes, about 150 | Meta Ad Library |
| Return policy (days, free returns or not, how returns are submitted) | 14 days, return portal, free return | Store's terms and conditions (regulamin) |
| Legal form and NIP found on the site? | Sp. z o.o., NIP found (kept in private notes) | Store's terms and conditions (regulamin) |
| ICP fit (strong / weak / no) and one sentence why | strong, B2C, Running ads, shopify |  |

- Start: `13:29` · End: `13:32` · **Minutes: 3**
- What took the longest: Nothing

## Result

| Store | Minutes |
|---|---|
| A | 5 |
| B | 6 |
| C | 3 |
| **Average** | **4.7** |

Date measured: 2026-11-13
