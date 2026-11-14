#!/usr/bin/env python3
"""Read each store's own website and record what it says about the store (step 1.4).

For every domain in `companies` the script reads robots.txt, the homepage (with its footer), the
terms (regulamin), contact and about pages, and the sitemaps, then records:

  is_store         the site sells online: cart links, add-to-cart buttons, Product markup, a store platform
  platform         store platform found in the page code (woocommerce, shopify, idosell, ...)
  category_fit     main product category, from keywords in config/category-keywords.yaml
  assortment_size  number of products, counted in the product sitemaps
  NIP              seller's tax number from the footer, contact page or terms, checksum-validated

Every fact keeps the URL of the page it was read from: the signals go to `signals` (source
`website`, with the evidence in `details`), the NIP to `companies.nip` and `nip_source_url`. Each
domain also gets `enrich_status` (ok, redirected, robots_disallowed, blocked, http_error,
unreachable), so the share of stores that could not be read is measured, not guessed.

No AI and no paid API: page code and text are matched with rules, so a run costs nothing and gives
the same result twice. robots.txt is respected; pages it disallows are skipped.

Picking the seller's NIP: terms pages also list the NIPs of payment operators and couriers (PayU,
InPost, ...). Each NIP found gets points for where it is (footer, contact, early in the terms, next
to "Sprzedawcą jest ...") and loses points when a third party is named right before it. All
candidates are kept in the local result file.

Usage (from the repo root):
  .venv/bin/python src/enrich.py --dry-run --domain sklep.pl   # read one store, print, write nothing
  .venv/bin/python src/enrich.py --limit 20                    # first 20 domains not enriched yet
  .venv/bin/python src/enrich.py                               # every domain not enriched yet
  .venv/bin/python src/enrich.py --redo --limit 50             # read already enriched domains again

Safe to stop with Ctrl-C and run again: each domain is saved as soon as it is done, and the next
run picks up the domains still missing. Full results, NIP candidates included, go to
data/enrich/<run>.jsonl (gitignored).

Credentials come from .env: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (as in src/ingest.py).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html as html_lib
import ipaddress
import json
import re
import socket
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest import Supabase, read_env  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_YAML = ROOT / "config/category-keywords.yaml"
OUT_DIR = ROOT / "data/enrich"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 20
DELAY = 1.0                      # seconds between two requests to the same store
RETRY_429_MAX_WAIT = 20          # longest pause before the one retry after HTTP 429
# Shopify serves all its stores from one network and rate-limits per client IP across all of them,
# so requests to any Shopify store share one pause.
SHOPIFY_NET = ipaddress.ip_network("23.227.38.0/24")
SHOPIFY_DELAY = 1.5
_shopify_lock = threading.Lock()
_shopify_last = [0.0]
_host_on_shopify: dict[str, bool] = {}


def on_shopify(host: str) -> bool:
    if host not in _host_on_shopify:
        try:
            ips = {ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
            _host_on_shopify[host] = any(ipaddress.ip_address(ip) in SHOPIFY_NET for ip in ips)
        except (socket.gaierror, ValueError, OSError):
            _host_on_shopify[host] = False
    return _host_on_shopify[host]
MAX_HTML = 5_000_000             # bytes read from one page
MAX_SITEMAP = 40_000_000         # bytes read from one sitemap file
MAX_SITEMAP_FILES = 25           # sitemap files read per store
PAGE_SIZE = 1000                 # rows per Supabase read (PostgREST caps responses at 1000)


# ---------------------------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------------------------

class Fetcher:
    """One store's HTTP client: browser user agent, a pause between requests, robots.txt rules."""

    def __init__(self) -> None:
        self.robots: RobotFileParser | None = None
        self.last = 0.0
        self.requests = 0

    def get(self, url: str, limit: int = MAX_HTML) -> dict:
        r = self._get(url, limit)
        if r["status"] == 429:  # rate limited (Shopify does this on sitemaps): wait once and retry
            retry_after = r["headers"].get("retry-after", "")
            time.sleep(min(int(retry_after) if retry_after.isdigit() else 10, RETRY_429_MAX_WAIT))
            r = self._get(url, limit)
        return r

    def _get(self, url: str, limit: int) -> dict:
        wait = DELAY - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        if on_shopify(urlparse(url).hostname or ""):
            with _shopify_lock:
                wait = SHOPIFY_DELAY - (time.monotonic() - _shopify_last[0])
                if wait > 0:
                    time.sleep(wait)
                _shopify_last[0] = time.monotonic()
        self.requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pl,en;q=0.8",
                                                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        out = {"url": url, "final_url": url, "status": None, "headers": {}, "body": b"", "error": None}
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
                out.update(status=r.status, final_url=r.geturl(), headers={k.lower(): v for k, v in r.headers.items()},
                           body=r.read(limit))
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read(200_000)
            except Exception:
                pass
            out.update(status=e.code, final_url=e.geturl() or url,
                       headers={k.lower(): v for k, v in (e.headers or {}).items()}, body=body)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
            reason = getattr(e, "reason", e)
            out["error"] = f"{type(reason).__name__}: {reason}"[:300]
        except Exception as e:  # malformed responses, odd redirects
            out["error"] = f"{type(e).__name__}: {e}"[:300]
        self.last = time.monotonic()
        if out["body"][:2] == b"\x1f\x8b":
            try:
                out["body"] = gzip.decompress(out["body"])
            except Exception:
                pass
        return out

    def allowed(self, url: str) -> bool:
        return self.robots is None or self.robots.can_fetch("*", url)


def decode(resp: dict) -> str:
    body = resp["body"]
    m = re.search(r"charset=([\w-]+)", resp["headers"].get("content-type", ""), re.I) \
        or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4000], re.I)
    enc = m.group(1) if m else "utf-8"
    enc = enc.decode() if isinstance(enc, bytes) else enc
    try:
        return body.decode(enc, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def bare(host: str) -> str:
    h = (host or "").lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def same_site(url: str, site: str) -> bool:
    h = bare(urlparse(url).hostname or "")
    return h == site or h.endswith("." + site)


def is_blocked(resp: dict) -> bool:
    if resp["status"] in (401, 403, 429, 503):
        return True
    head = resp["body"][:20000].lower()
    return resp["status"] == 200 and len(resp["body"]) < 30000 and any(
        s in head for s in (b"cf-chl", b"challenge-platform", b"just a moment", b"captcha-delivery", b"ddos-guard"))


# ---------------------------------------------------------------------------------------------
# HTML parsing (standard library only)
# ---------------------------------------------------------------------------------------------

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
BREAK_TAGS = {"p", "br", "div", "li", "td", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
              "header", "footer", "nav", "ul", "ol", "table", "address", "span"}


class Page(HTMLParser):
    """Title, meta tags, h1, links with their text, visible text, footer text and JSON-LD blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.h1, self.links, self.jsonld = "", {}, [], [], []
        self._text, self._footer = [], []
        self._skip = 0
        self._in_title = self._in_h1 = False
        self._a: dict | None = None
        self._ld: list | None = None
        self._footer_tag = 0              # depth inside <footer>
        self._div_depth = 0
        self._footer_div: int | None = None  # div depth where a div with "footer" in id/class opened

    @property
    def in_footer(self) -> bool:
        return self._footer_tag > 0 or self._footer_div is not None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
            return
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._ld = []
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
            self.h1.append("")
        elif tag == "a":
            self._a = {"href": a.get("href", ""), "text": []}
        elif tag == "footer":
            self._footer_tag += 1
        elif tag == "div":
            self._div_depth += 1
            marker = (a.get("id", "") + " " + a.get("class", "")).lower()
            if self._footer_div is None and "footer" in marker:
                self._footer_div = self._div_depth
        if tag in BREAK_TAGS:
            self._add("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            if tag == "script" and self._ld is not None:
                self.jsonld.append("".join(self._ld))
                self._ld = None
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "a" and self._a is not None:
            self.links.append({"href": self._a["href"], "text": " ".join("".join(self._a["text"]).split())})
            self._a = None
        elif tag == "footer":
            self._footer_tag = max(0, self._footer_tag - 1)
        elif tag == "div":
            if self._footer_div is not None and self._div_depth <= self._footer_div:
                self._footer_div = None
            self._div_depth = max(0, self._div_depth - 1)
        if tag in BREAK_TAGS:
            self._add("\n")

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        if self._in_h1 and self.h1:
            self.h1[-1] += data
        if self._a is not None:
            self._a["text"].append(data)
        self._add(data)

    def _add(self, s: str) -> None:
        self._text.append(s)
        if self.in_footer:
            self._footer.append(s)

    @staticmethod
    def _clean(parts: list[str]) -> str:
        lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
        return "\n".join(line for line in lines if line)

    @property
    def text(self) -> str:
        return self._clean(self._text)

    @property
    def footer(self) -> str:
        return self._clean(self._footer)


def parse(html: str) -> Page:
    p = Page()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass  # keep whatever was parsed before the broken markup
    return p


def fold(s: str) -> str:
    """Lowercase and strip Polish diacritics: 'Odzież Damska' -> 'odziez damska'."""
    s = s.lower().replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


# ---------------------------------------------------------------------------------------------
# Platform and "is it a store"
# ---------------------------------------------------------------------------------------------

# (platform id, [(pattern in lowercased HTML, weight)]). A platform needs PLATFORM_MIN points, so one
# stray mention (a Shopify-hosted image on a custom store, a blog post about PrestaShop) is not enough;
# weight 2 marks code only the platform itself produces. Ids from icp.yaml signals.platform where they
# exist; other store platforms keep their own id and scoring treats them as "custom".
PLATFORM_MIN = 2
PLATFORMS = [
    ("shopify", [(r"shopify\.theme", 2), (r"\.myshopify\.com", 2), (r"id=[\"']shopify-section-", 2),
                 (r"cdn\.shopify\.com", 1)]),
    ("woocommerce", [(r"wp-content/plugins/woocommerce", 2), (r"woocommerce-page", 1), (r"wc-block", 1),
                     (r"\bwoocommerce\b", 1)]),
    ("prestashop", [(r"var prestashop\s*=", 2), (r"/modules/ps_", 1), (r"\bprestashop\b", 1)]),
    ("idosell", [(r"\bidosell\b", 1), (r"iai-shop", 1), (r"/gfx/pol/", 1), (r"\biai_", 1)]),
    ("shoper", [(r"\bshoper\b", 1), (r"dreamcommerce", 1), (r"shoper\.pl", 1)]),
    ("magento", [(r"mage/cookies", 2), (r"/static/version\d+/frontend/", 2), (r"\bmagento\b", 1)]),
    ("wix", [(r"_wixcssimports", 2), (r"wixstatic\.com", 1)]),
    ("sky-shop", [(r"sky-shop", 2), (r"skyshop\.pl", 2)]),
    ("shoplo", [(r"\bshoplo\b", 2)]),
    ("atomstore", [(r"\batomstore\b", 2)]),
    ("redcart", [(r"\bredcart\b", 2)]),
    ("selly", [(r"selly\.pl", 2)]),
    ("comarch", [(r"comarch e-sklep", 2), (r"comarch-esklep", 2)]),
    ("sote", [(r"soteshop", 2), (r"sote\.pl", 2)]),
]
HEADER_PLATFORMS = {"x-shopid": "shopify", "x-shopify-stage": "shopify", "x-magento-cache-debug": "magento"}
STORE_MARKERS = [
    ("add_to_cart", r"add[-_ ]to[-_ ]cart"),
    ("dodaj_do_koszyka", r"do koszyka"),
    ("cart_link", r"href=[\"'][^\"']*/(koszyk|cart|basket)\b"),
    ("product_markup", r"\"@type\"\s*:\s*\"(product|offer|productgroup)\""),
    ("og_product", r"og:type[\"']\s+content=[\"']product"),
]


def detect_platform(html_lower: str, headers: dict, generator: str | None) -> tuple[str | None, list[str]]:
    for h, plat in HEADER_PLATFORMS.items():
        if h in headers:
            return plat, [f"header {h}"]
    gen = (generator or "").lower()
    best, best_score, best_hits = None, 0, []
    for plat, rules in PLATFORMS:
        hits = [p for p, _ in rules if re.search(p, html_lower)]
        score = sum(w for p, w in rules if p in hits)
        if plat.replace("-", "") in gen.replace("-", ""):
            score, hits = score + PLATFORM_MIN, hits + [f"generator {generator}"]
        if score > best_score:
            best, best_score, best_hits = plat, score, hits
    if best_score < PLATFORM_MIN:
        return None, best_hits and [f"too weak for {best}: {', '.join(best_hits)}"]
    return best, best_hits


def detect_store(html_lower: str, platform: str | None) -> list[str]:
    evidence = [name for name, p in STORE_MARKERS if re.search(p, html_lower)]
    if platform:
        evidence.append(f"platform {platform}")
    return evidence


# ---------------------------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------------------------

def load_categories(path: Path) -> dict:
    conf = yaml.safe_load(path.read_text(encoding="utf-8"))
    conf["compiled"] = {cat: [re.compile(r"\b" + kw) for kw in kws] for cat, kws in conf["categories"].items()}
    return conf


def classify(page: Page, serp_hints: list[str], conf: dict) -> tuple[str, dict]:
    w = conf["weights"]
    pieces = [("title", page.title), ("meta_description", page.meta.get("description", ""))]
    pieces += [("h1", h) for h in page.h1[:3]]
    seen = set()
    for link in page.links:
        t = fold(link["text"])
        if t and len(t) <= 40 and t not in seen:
            seen.add(t)
            pieces.append(("nav_link", t))
    scores: Counter = Counter()
    matched: dict[str, list[str]] = {}
    for kind, text in pieces:
        t = fold(text)
        if not t:
            continue
        for cat, patterns in conf["compiled"].items():
            for p in patterns:
                m = p.search(t)
                if m:
                    scores[cat] += w[kind]
                    matched.setdefault(cat, []).append(m.group(0) if kind == "nav_link" else f"{kind}: {m.group(0)}")
                    break  # one hit per category per piece of text
    for cat in serp_hints:
        if cat in conf["compiled"]:
            scores[cat] += w["serp_hint"]
            matched.setdefault(cat, []).append("serp_hint")
    ranked = scores.most_common()
    category = ranked[0][0] if ranked and ranked[0][1] >= conf["min_score"] else "other"
    return category, {"scores": dict(ranked[:5]),
                      "matched": {c: sorted(set(v))[:12] for c, v in matched.items() if c in dict(ranked[:3])},
                      "serp_hints": serp_hints}


# ---------------------------------------------------------------------------------------------
# NIP
# ---------------------------------------------------------------------------------------------

NIP_RE = re.compile(r"\b(?:NIP|VAT[\s-]?(?:ID|UE|EU)?|Tax ID)\b[^0-9]{0,15}?(?:PL\s?)?((?:\d[\s-]?){9}\d)\b", re.I)
SELLER_RE = re.compile(r"sprzedawc|sprzedajac|uslugodawc|wlasciciel|sklep\w* (internetow\w* )?(jest )?prowadzon|"
                       r"prowadzi(my)? sklep|prowadzac\w* sklep|administrator(em)? (danych|serwisu|sklepu)|"
                       r"operator(em)? sklepu|dane (firmy|sprzedawcy)|dane kontaktowe")
THIRD_PARTY_RE = re.compile(
    r"payu|przelewy ?24|paypro|blue ?media|autopay|tpay|krajowy integrator|paypal|stripe|imoje|paynow|"
    r"klarna|twisto|paypo|inpost|\bdpd\b|\bdhl\b|\bgls\b|poczta polska|furgonetka|apaczka|orlen paczka|"
    r"shoper|dreamcommerce|idosell|\biai\b|shopify|comarch|uokik|rzecznik|trusted ?shops|opineo|ceneo|"
    r"google|meta platforms|facebook|biuro rachunkowe|ing bank|santander|mbank|\bpko\b|pekao|millennium")
NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)


def valid_nip(nip: str) -> bool:
    if len(nip) != 10 or not nip.isdigit() or len(set(nip)) == 1:
        return False
    check = sum(int(d) * w for d, w in zip(nip, NIP_WEIGHTS)) % 11
    return check != 10 and check == int(nip[9])


def find_nips(text: str, url: str, where: str) -> list[dict]:
    out = []
    for m in NIP_RE.finditer(text):
        nip = re.sub(r"\D", "", m.group(1))
        if not valid_nip(nip):
            continue
        # Register clauses ("wpisana do rejestru ... prowadzonego przez Sąd Rejonowy ... KRS ...") are long,
        # so the company name can be 300+ characters before its NIP.
        before = fold(text[max(0, m.start() - 400):m.start()])
        seller = [x.end() for x in SELLER_RE.finditer(before)]
        third_hits = list(THIRD_PARTY_RE.finditer(before))
        third = [x.end() for x in third_hits]
        score = {"jsonld": 4, "footer": 3, "contact": 3, "about": 2, "home": 1, "privacy": 1}.get(where, 0)
        if where == "terms":
            score = 3 if m.start() < max(3000, len(text) * 0.2) else 1
        if seller:
            score += 2
        if third and (not seller or third[-1] > seller[-1]):
            score -= 4
        out.append({"nip": nip, "url": url, "where": where, "pos": m.start(), "score": score,
                    "third_party": third_hits[-1].group(0) if third else None,
                    "context": " ".join(text[max(0, m.start() - 120):m.end()].split())})
    return out


def jsonld_nips(blocks: list[str], url: str) -> list[dict]:
    out = []
    for raw in blocks:
        for m in re.finditer(r"\"(?:vatID|taxID)\"\s*:\s*\"([^\"]+)\"", raw):
            nip = re.sub(r"\D", "", m.group(1))
            if valid_nip(nip):
                out.append({"nip": nip, "url": url, "where": "jsonld", "pos": 0, "score": 4, "context": m.group(0)})
    return out


def pick_nip(cands: list[dict]) -> dict | None:
    if not cands:
        return None
    total: dict[str, int] = {}
    best: dict[str, dict] = {}
    for c in cands:
        total[c["nip"]] = max(total.get(c["nip"], -99), c["score"])
        if c["nip"] not in best or c["score"] > best[c["nip"]]["score"]:
            best[c["nip"]] = c
    counts = Counter(c["nip"] for c in cands)
    nip = max(total, key=lambda n: (total[n], counts[n], -best[n]["pos"]))
    return best[nip] if total[nip] > 0 else None


# ---------------------------------------------------------------------------------------------
# Pages to read
# ---------------------------------------------------------------------------------------------

PAGE_RULES = {
    "terms": (r"regulamin|terms|warunki[ -](sprzedazy|zakupow)|ogolne[ -]warunki", r"regulamin|terms-of-service|/terms|warunki"),
    "contact": (r"^kontakt|contact", r"kontakt|contact"),
    "about": (r"o nas|o firmie|o marce|kim jestesmy|about", r"o-nas|o-firmie|o-marce|about"),
    "privacy": (r"polityk[ai] prywatnosci|privacy", r"prywatnosc|privacy"),
}
FALLBACK_PATHS = {"terms": ["/regulamin", "/policies/terms-of-service"], "contact": ["/kontakt", "/pages/kontakt"]}


# Links that match a rule but lead to the wrong document: promotion or contest terms, PDFs.
WRONG_PAGE_RE = re.compile(r"promocj|konkurs|newsletter|lojalnosc|rabat|opini|reklamacj|zwrot|dostaw|cookie|\.pdf$")


def find_pages(page: Page, base_url: str, site: str) -> dict[str, str]:
    """Best link per page kind: matching text beats matching URL, a short exact label wins ties."""
    found: dict[str, str] = {}
    for kind, (text_re, href_re) in PAGE_RULES.items():
        best, best_score = None, 0
        for link in page.links:
            href = link["href"].strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            url = urljoin(base_url, href).split("#")[0]
            if not same_site(url, site) or url.rstrip("/") == base_url.rstrip("/"):
                continue
            text, path = fold(link["text"]), fold(urlparse(url).path)
            score = 2 * bool(re.search(text_re, text)) + bool(re.search(href_re, path))
            if not score:
                continue
            score += len(text) <= 25
            if WRONG_PAGE_RE.search(text) or WRONG_PAGE_RE.search(path):
                score -= 5
            if score > best_score:
                best, best_score = url, score
        if best:
            found[kind] = best
    return found


# ---------------------------------------------------------------------------------------------
# Assortment from sitemaps
# ---------------------------------------------------------------------------------------------

LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.S)
PRODUCT_SITEMAP_RE = re.compile(r"product|produkt|towar")
LOCALE_PREFIX_RE = re.compile(r"^/[a-z]{2}(-[a-z]{2})?/")
# Product URL patterns when no sitemap is named after products. The group is the product id, so
# the same product in several languages counts once.
PRODUCT_URL = {
    "shopify": re.compile(r"/products/([^/?#]+)$"),
    "idosell": re.compile(r"/product-[a-z]{3}-(\d+)-"),
    "shoper": re.compile(r"/p/[^/]+/(\d+)$"),
    "prestashop": re.compile(r"/(\d+)-[^/]+\.html$"),
    "woocommerce": re.compile(r"/(?:produkt|product|sklep/[^/]+)/([^/]+)/?$"),
}


def read_sitemap(f: Fetcher, url: str) -> dict | None:
    r = f.get(url, MAX_SITEMAP)
    if r["status"] != 200 or not r["body"]:
        return None
    xml = r["body"].decode("utf-8", "replace")
    return {"url": r["final_url"], "index": "<sitemapindex" in xml[:3000].lower(),
            "locs": [html_lib.unescape(u.strip()) for u in LOC_RE.findall(xml)]}


LOCALE_PARAM_RE = re.compile(r"locale/([a-z]{2}_[a-z]{2})", re.I)  # Shoper: .../products/locale/pl_PL/page/1


def main_language(urls: list[str]) -> list[str]:
    """Drop per-language copies (/en/sitemap_products_1.xml, .../locale/en_US/...) of sitemap files."""
    plain = [u for u in urls if not LOCALE_PREFIX_RE.match(urlparse(u).path)] or urls
    locales = {u: m.group(1).lower() for u in plain if (m := LOCALE_PARAM_RE.search(u))}
    if len(set(locales.values())) > 1:
        keep = "pl_pl" if "pl_pl" in locales.values() else Counter(locales.values()).most_common(1)[0][0]
        plain = [u for u in plain if locales.get(u, keep) == keep]
    return plain


def count_products(f: Fetcher, robots_text: str, home: str, site: str, platform: str | None) -> dict | None:
    declared = list(dict.fromkeys(re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots_text or "")))
    declared = [u for u in declared if same_site(u, site)][:5]
    docs = [d for u in declared if f.allowed(u) and (d := read_sitemap(f, u))]
    if not docs:
        defaults = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml"]
        if platform == "shoper":
            defaults.insert(0, "/console/integration/execute/name/GoogleSitemap")
        for path in defaults:
            u = urljoin(home, path)
            if f.allowed(u) and (d := read_sitemap(f, u)):
                docs.append(d)
                break
    if not docs:
        return None
    files_read = len(docs)
    index_url = next((d["url"] for d in docs if d["index"]), None)
    children = list(dict.fromkeys(u for d in docs if d["index"] for u in d["locs"] if same_site(u, site)))
    urlsets = [d for d in docs if not d["index"]]
    is_product = lambda u: bool(PRODUCT_SITEMAP_RE.search(fold(urlparse(u).path)))  # noqa: E731
    product_docs = [d for d in urlsets if is_product(d["url"])]
    product_children = main_language([u for u in children if is_product(u)])
    pattern = PRODUCT_URL.get(platform)

    if product_docs or product_children:
        method, docs_used, queue = "product_sitemaps", product_docs, product_children
    elif pattern:
        method, docs_used, queue = f"url_pattern_{platform}", urlsets, main_language(children)
    else:
        return None

    used = [d["url"] for d in docs_used]
    urls = [u for d in docs_used for u in d["locs"]]
    while queue and files_read < MAX_SITEMAP_FILES:
        u = queue.pop(0)
        if not f.allowed(u):
            continue
        d = read_sitemap(f, u)
        files_read += 1
        if not d:
            continue
        if d["index"]:  # index of indexes
            queue += [c for c in main_language(d["locs"]) if same_site(c, site) and c not in queue]
            continue
        used.append(d["url"])
        urls += d["locs"]

    # With a known product URL pattern, count product ids: the same product in several languages
    # counts once. In product sitemaps the pattern is used only when it fits most URLs.
    pattern_ids = {m.group(1) for u in urls if (m := pattern.search(urlparse(u).path))} if pattern else set()
    if pattern and (method != "product_sitemaps" or len(pattern_ids) >= 0.5 * len(set(urls))):
        ids, counted_by = pattern_ids, "product_id"
    else:
        ids, counted_by = set(urls) - {home, home.rstrip("/")}, "url"
    if not ids:
        return None
    # More product files than MAX_SITEMAP_FILES: scale up by the files not read (they hold about as
    # many products each, the last one fewer, so this slightly overestimates).
    estimated = bool(queue) and bool(used)
    count = round(len(ids) * (len(used) + len(queue)) / len(used)) if estimated else len(ids)
    return {"count": count, "counted": len(ids), "estimated": estimated, "files_not_read": len(queue),
            "method": method, "source_url": index_url or (used[0] if used else home),
            "sitemaps": used[:10], "sitemaps_read": len(used), "counted_by": counted_by,
            "urls_seen": len(urls)}


# ---------------------------------------------------------------------------------------------
# One store
# ---------------------------------------------------------------------------------------------

def enrich(domain: str, serp_hints: list[str], cat_conf: dict) -> dict:
    started = time.monotonic()
    f = Fetcher()
    res = {"domain": domain, "status": None, "site_url": None, "signals": [], "nip": None,
           "nip_candidates": [], "pages": {}, "error": None}

    # robots.txt first (redirects follow to the www. host when there is one)
    robots_text = ""
    rob = f.get(f"https://{domain}/robots.txt")
    if rob["error"]:
        rob = f.get(f"http://{domain}/robots.txt")
    if rob["status"] == 200 and b"<html" not in rob["body"][:500].lower():
        robots_text = decode(rob)
        f.robots = RobotFileParser()
        f.robots.parse(robots_text.splitlines())

    home = None
    for url in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
        if not f.allowed(url):
            res.update(status="robots_disallowed", error=f"robots.txt disallows {url}")
            return finish(res, f, started)
        home = f.get(url)
        if not home["error"]:
            break
    if home["error"]:
        res.update(status="unreachable", error=home["error"])
        return finish(res, f, started)
    res["site_url"] = home["final_url"]
    if is_blocked(home):
        res.update(status="blocked", error=f"HTTP {home['status']}")
        return finish(res, f, started)
    if home["status"] != 200:
        res.update(status="http_error", error=f"HTTP {home['status']}")
        return finish(res, f, started)

    final_host = bare(urlparse(home["final_url"]).hostname or "")
    site = final_host if not (final_host == domain or final_host.endswith("." + domain)) else domain
    res["status"] = "ok" if site == domain else "redirected"
    home_url = home["final_url"]
    html = decode(home)
    page = parse(html)
    html_lower = html.lower()
    res["pages"]["home"] = home_url

    platform, platform_hits = detect_platform(html_lower, home["headers"], page.meta.get("generator"))
    if platform is None and on_shopify(urlparse(home_url).hostname or ""):
        platform, platform_hits = "shopify", platform_hits + ["IP in Shopify network 23.227.38.0/24"]
    store_evidence = detect_store(html_lower, platform)
    is_store = bool(store_evidence)
    res["signals"].append({"type": "is_store", "value": is_store, "source_url": home_url,
                           "details": {"evidence": store_evidence}})
    res["signals"].append({"type": "platform", "value": platform or ("custom" if is_store else "unknown"),
                           "source_url": home_url,
                           "details": {"evidence": platform_hits, "generator": page.meta.get("generator")}})
    category, cat_details = classify(page, serp_hints, cat_conf)
    res["signals"].append({"type": "category_fit", "value": category, "source_url": home_url, "details": cat_details})

    # NIP: homepage footer and JSON-LD, then contact, about, terms; privacy policy only if still nothing
    cands = jsonld_nips(page.jsonld, home_url) + find_nips(page.footer, home_url, "footer")
    footer_nips = {c["nip"] for c in cands}
    cands += [c for c in find_nips(page.text, home_url, "home") if c["nip"] not in footer_nips]
    links = find_pages(page, home_url, site)
    for kind in ("contact", "about", "terms", "privacy"):
        if kind == "privacy" and pick_nip(cands):
            break
        urls = [links[kind]] if kind in links else [urljoin(home_url, p) for p in FALLBACK_PATHS.get(kind, [])]
        for url in urls:
            if not f.allowed(url):
                res["pages"][kind] = f"robots_disallowed {url}"
                continue
            r = f.get(url)
            if r["status"] == 200 and not r["error"]:
                res["pages"][kind] = r["final_url"]
                cands += find_nips(parse(decode(r)).text, r["final_url"], kind)
                break
    res["nip_candidates"] = cands
    chosen = pick_nip(cands)
    if chosen:
        res["nip"] = {"nip": chosen["nip"], "source_url": chosen["url"], "where": chosen["where"],
                      "score": chosen["score"], "context": chosen["context"],
                      "other_nips": sorted({c["nip"] for c in cands} - {chosen["nip"]})}

    try:
        assortment = count_products(f, robots_text, home_url, site, platform)
    except Exception as e:
        assortment, res["error"] = None, f"sitemap: {type(e).__name__}: {e}"[:300]
    if assortment:
        res["signals"].append({"type": "assortment_size", "value": assortment["count"],
                               "source_url": assortment["source_url"],
                               "details": {k: v for k, v in assortment.items() if k not in ("count", "source_url")}})
    return finish(res, f, started)


def finish(res: dict, f: Fetcher, started: float) -> dict:
    res["requests"] = f.requests
    res["seconds"] = round(time.monotonic() - started, 1)
    return res


# ---------------------------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------------------------

def load_companies(db: Supabase, domains: list[str], redo: bool, limit: int | None) -> list[dict]:
    rows, offset = [], 0
    flt = "" if redo or domains else "&enriched_at=is.null"
    while True:
        page = db.request("GET", f"/companies?select=id,domain,nip,registry_status{flt}"
                                 f"&order=id&limit={PAGE_SIZE}&offset={offset}") or []
        rows += page
        offset += PAGE_SIZE
        if len(page) < PAGE_SIZE:
            break
    if domains:
        wanted = set(domains)
        rows = [r for r in rows if r["domain"] in wanted]
    return rows[:limit] if limit else rows


def load_serp_hints(db: Supabase) -> dict[int, list[str]]:
    hints, offset = {}, 0
    while True:
        page = db.request("GET", f"/company_sources?select=company_id,details&source=eq.google_serp"
                                 f"&order=company_id&limit={PAGE_SIZE}&offset={offset}") or []
        for r in page:
            hints[r["company_id"]] = (r.get("details") or {}).get("categories") or []
        offset += PAGE_SIZE
        if len(page) < PAGE_SIZE:
            break
    return hints


def save(db: Supabase, company: dict, res: dict, run_id: int) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    # HTTP 429 is a rate limit, not a refusal: leave enriched_at empty so the next run tries again.
    rate_limited = res["status"] == "blocked" and res["error"] == "HTTP 429"
    patch = {"enrich_status": res["status"], "enriched_at": None if rate_limited else now,
             "site_url": res["site_url"]}
    if res["nip"]:
        patch.update(nip=res["nip"]["nip"], nip_source_url=res["nip"]["source_url"])
        if res["nip"]["nip"] != company.get("nip"):
            patch["registry_status"] = "pending"
    elif res["status"] in ("ok", "redirected") and company.get("registry_status") == "pending" and not company.get("nip"):
        patch["registry_status"] = "no_nip_on_site"
    db.request("PATCH", f"/companies?id=eq.{company['id']}", patch, prefer="return=minimal")
    if res["signals"]:
        db.request("POST", "/signals", [{"company_id": company["id"], "type": s["type"], "value": s["value"],
                                         "source": "website", "source_url": s["source_url"],
                                         "details": s["details"], "run_id": run_id} for s in res["signals"]],
                   prefer="return=minimal")


# ---------------------------------------------------------------------------------------------

def summarize(results: list[dict]) -> dict:
    status = Counter(r["status"] for r in results)
    read = [r for r in results if r["status"] in ("ok", "redirected")]
    sig = lambda r, t: next((s["value"] for s in r["signals"] if s["type"] == t), None)  # noqa: E731
    nip_domains: dict[str, list[str]] = {}
    for r in results:
        if r["nip"]:
            nip_domains.setdefault(r["nip"]["nip"], []).append(r["domain"])
    return {
        "domains": len(results),
        "status": dict(status),
        "read": len(read),
        "is_store": sum(1 for r in read if sig(r, "is_store")),
        "nip_found": sum(1 for r in read if r["nip"]),
        "nip_where": dict(Counter(r["nip"]["where"] for r in read if r["nip"])),
        "assortment_found": sum(1 for r in read if sig(r, "assortment_size") is not None),
        "platforms": dict(Counter(sig(r, "platform") for r in read).most_common()),
        "categories": dict(Counter(sig(r, "category_fit") for r in read).most_common()),
        "nips_on_several_domains": {n: d for n, d in nip_domains.items() if len(d) > 1},
        "requests": sum(r.get("requests", 0) for r in results),
        "avg_seconds_per_domain": round(sum(r.get("seconds", 0) for r in results) / max(1, len(results)), 1),
    }


def print_result(r: dict) -> None:
    sig = {s["type"]: s for s in r["signals"]}
    parts = [f"{r['status']}"]
    for t in ("is_store", "platform", "category_fit", "assortment_size"):
        if t in sig:
            parts.append(f"{t}={sig[t]['value']}")
    parts.append(f"nip={r['nip']['nip'] + ' (' + r['nip']['where'] + ')' if r['nip'] else '-'}")
    if r["error"]:
        parts.append(f"error={r['error'][:80]}")
    print(f"  {r['domain']}: " + ", ".join(parts) + f"  [{r['seconds']} s]", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", action="append", default=[], help="only this domain (repeatable)")
    ap.add_argument("--limit", type=int, help="at most this many domains")
    ap.add_argument("--redo", action="store_true", help="also domains already enriched")
    ap.add_argument("--workers", type=int, default=8, help="stores read in parallel (default 8)")
    ap.add_argument("--dry-run", action="store_true", help="read the stores, print, write nothing to Supabase")
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    a = ap.parse_args()

    cat_conf = load_categories(CATEGORIES_YAML)
    env = read_env(a.env)
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY")
    db = Supabase(url, key) if url and key else None
    if not db and not (a.dry_run and a.domain):
        sys.exit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env (a dry run with --domain works without).")

    if db:
        companies = load_companies(db, a.domain, a.redo, a.limit)
        hints = load_serp_hints(db)
    else:
        companies, hints = [{"id": None, "domain": d, "nip": None, "registry_status": "pending"} for d in a.domain], {}
    missing = sorted(set(a.domain) - {c["domain"] for c in companies})
    if missing:
        print(f"Not in companies, skipped: {', '.join(missing)}")
    if not companies:
        print("Nothing to enrich.")
        return

    started = dt.datetime.now(dt.timezone.utc)
    run_id = None
    if db and not a.dry_run:
        run_id = db.request("POST", "/runs", {"step": "enrich", "started_at": started.isoformat(),
                                              "items_total": len(companies),
                                              "notes": {"args": {k: str(v) for k, v in vars(a).items()}}},
                            prefer="return=representation")[0]["id"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / (f"run-{run_id}.jsonl" if run_id else f"dry-run-{started:%Y%m%d-%H%M%S}.jsonl")
    print(f"Enriching {len(companies)} domains with {a.workers} workers"
          f"{' (dry run)' if a.dry_run else f', run {run_id}'} -> {out_path.relative_to(ROOT)}", flush=True)

    results: list[dict] = []
    failed_saves: list[str] = []
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=a.workers)
    try:
        futures = {pool.submit(enrich, c["domain"], hints.get(c["id"], []), cat_conf): c for c in companies}
        with out_path.open("a", encoding="utf-8") as out:
            for i, fut in enumerate(as_completed(futures), 1):
                c = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:  # a bug for one store must not stop the run
                    res = {"domain": c["domain"], "status": None, "site_url": None, "signals": [], "nip": None,
                           "nip_candidates": [], "pages": {}, "error": f"crash: {type(e).__name__}: {e}"[:300],
                           "requests": 0, "seconds": 0}
                with lock:
                    results.append(res)
                    out.write(json.dumps(res, ensure_ascii=False) + "\n")
                    out.flush()
                print(f"[{i}/{len(companies)}]", end="")
                print_result(res)
                if db and not a.dry_run and res["status"]:
                    try:
                        save(db, c, res, run_id)
                    except SystemExit as e:  # Supabase.request exits on HTTP errors
                        failed_saves.append(f"{c['domain']}: {e}")
                        res["save_error"] = True
    except KeyboardInterrupt:
        print("\nStopped: saving what is done. Run again to continue.")
        pool.shutdown(wait=False, cancel_futures=True)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        summary = summarize(results)
        summary["save_errors"] = failed_saves[:20]
        if run_id:
            ok = sum(1 for r in results if r["status"] in ("ok", "redirected") and not r.get("save_error"))
            db.request("PATCH", f"/runs?id=eq.{run_id}", {
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "items_total": len(companies), "items_ok": ok,
                "items_failed": len(results) - ok, "cost_usd": 0,
                "notes": {"args": {k: str(v) for k, v in vars(a).items()}, "output": out_path.name,
                          "unfinished": len(companies) - len(results), **summary},
            }, prefer="return=minimal")
        print("\nSummary:")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
