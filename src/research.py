"""Research agent (step 2.2): one research card per store, as defined in docs/research-card.md.

For each domain:
  1. load the company and its latest signals from Supabase (pipeline facts, checked in phase 1),
  2. fetch up to MAX_PAGES pages of the store: home, returns, terms, delivery, FAQ, about, one listing,
  3. ask Claude (`claude -p`, no tools, JSON schema) for the agent part of the card, with a quote per fact,
  4. check every agent fact: its source URL is a fetched page and every quote is on that page (visible
     text, or raw HTML for `evidence: code`); facts that fail are dropped and counted, never saved,
  5. compute the fit score from config/icp.yaml in code (the model only writes the reasoning),
  6. save the card to `research` and the run to `runs`. With --dry-run nothing is written to Supabase.

Every card, with the dropped facts next to it, is also written to data/research/ (gitignored).

Usage (from the repo root):
  .venv/bin/python src/research.py store-a.example --dry-run
  .venv/bin/python src/research.py --sample 30 --seed eval-1
  .venv/bin/python src/research.py --limit 50 --workers 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html as html_lib
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich import (PAGE_RULES, Fetcher, bare, decode, fold, is_blocked, parse,  # noqa: E402
                    read_robots, same_site)
from ingest import ROOT, Supabase, read_env  # noqa: E402

ICP_YAML = ROOT / "config/icp.yaml"
PROMPT_FILE = ROOT / "prompts/research.md"
OUT_DIR = ROOT / "data/research"
CARD_VERSION = 1
AGENT_VERSION = "v1"            # bump with every prompt or logic change; the prompt hash is appended
DEFAULT_MODEL = "sonnet"
CLAUDE_TIMEOUT = 600            # seconds for one `claude -p` call
MAX_PAGES = 10                  # pages fetched per store, homepage included (docs/research-card.md)
PAGE_CHARS = 12_000             # characters of one page's text sent to the model
TERMS_CHARS = 14_000            # terms pages are long: only the returns-related paragraphs are sent
QUOTE_MAX_WORDS = 30
MAX_QUOTES = {"what_they_sell.product_types": 6, "signals.hiring": 6}  # one menu label per product type; other facts: 3
PAGE_SIZE = 1000                # PostgREST rows per page

PIPELINE_HOSTS = ("wl-api.mf.gov.pl", "api-krs.ms.gov.pl", "www.facebook.com", "facebook.com")
HOW_TO_RETURN = {"email", "printed_form", "online_form", "returns_portal", "parcel_locker_code", "in_store",
                 "contact_first"}
HIRING_ROLES = {"ecommerce", "marketing", "customer_service", "warehouse_logistics", "other"}
AGENT_DISQUALIFIERS = {"b2b_only", "marketplace_only", "excluded_category", "dropshipping", "outside_poland"}
CATEGORY_IDS = {"fashion", "footwear", "accessories", "sportswear", "kids", "home_decor", "electronics", "beauty",
                "other", "food", "digital_goods", "services"}


# ---------------------------------------------------------------------------------------------
# Pages to read
# ---------------------------------------------------------------------------------------------

# kind -> (pattern on folded link text, pattern on folded URL path). Order = fetch priority.
RESEARCH_RULES = {
    "returns": (r"zwrot|wymian|odstapieni|returns?\b|refund", r"zwrot|wymian|odstapien|return|refund"),
    "terms": PAGE_RULES["terms"],
    "delivery": (r"dostaw|wysylk|shipping|delivery", r"dostaw|wysylk|shipping|delivery"),
    "faq": (r"^faq|czeste pytania|najczesciej zadawane|pomoc$|centrum pomocy", r"faq|pytania|pomoc|help"),
    "careers": (r"\bkariera\b|^praca\b|praca (w|dla)\b|oferty pracy|dolacz do (zespolu|teamu|naszego zespolu)|"
                r"rekrutacj|pracuj z nami|^careers?\b|^jobs?\b", r"kariera|[-/]praca\b|careers?|/jobs?\b|rekrutacj|oferty-pracy"),
    "about": PAGE_RULES["about"],
}
FALLBACK = {
    "returns": ["/zwroty", "/zwroty-i-wymiany", "/pages/zwroty", "/policies/refund-policy"],
    "terms": ["/regulamin", "/policies/terms-of-service"],
}
# Links that look like a page kind but are something else.
SKIP_LINK_RE = re.compile(r"\.(pdf|jpe?g|png|docx?)$|login|logowanie|konto|account|koszyk|cart|newsletter|blog/.+")
# A listing page for the price range: a sale or new-in page, else a category from the menu.
LISTING_TEXT_RE = re.compile(r"^(wyprzedaz|outlet|sale|promocje|nowosci|new in|nowe|bestsellery?)\b")
LISTING_PATH_RE = re.compile(r"/(collections?|kategori[ae]|category|categories|c|k|pl/c|produkty)/[^/]+/?$")
# Follow-up from the returns page: an online return or withdrawal form.
RETURN_FORM_RE = re.compile(r"formularz|zwrot online|zglos zwrot|zarejestruj zwrot|odstap|portal|return form")
CONTACT_RE = re.compile(r"kontakt|contact")
# Links, scripts and form targets in the page code that point to a returns tool.
CODE_HINT_RE = re.compile(
    r"""(?:href|src|action)\s*=\s*["']([^"']*(?:retjet|baselinker\.com/return|order\.baselinker|easyreturn|"""
    r"""furgonetka|zwrot|return|odstap|docs\.google\.com/forms|forms\.gle)[^"']*)["']""", re.I)
TERMS_KEEP_RE = re.compile(r"zwrot|odstąp|odstap|wymian|reklamac|konsument|zwróc|zwroc|koszt|przesył|przesyl")


def link_candidates(page, base_url: str, site: str) -> list[dict]:
    out = []
    for link in page.links:
        href = link["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if not same_site(url, site) or url.rstrip("/") == base_url.rstrip("/"):
            continue
        out.append({"url": url, "text": fold(link["text"]), "path": fold(urlparse(url).path)})
    return out


def best_link(cands: list[dict], text_re: str, href_re: str, taken: set[str]) -> str | None:
    best, best_score = None, 0
    for c in cands:
        if c["url"] in taken or SKIP_LINK_RE.search(c["path"]):
            continue
        score = 2 * bool(re.search(text_re, c["text"])) + bool(re.search(href_re, c["path"]))
        if not score:
            continue
        score += len(c["text"]) <= 25
        if score > best_score:
            best, best_score = c["url"], score
    return best


def listing_link(cands: list[dict], taken: set[str], platform: str | None, home: str) -> str | None:
    if platform == "shopify":
        return urljoin(home, "/collections/all")
    for c in cands:
        if c["url"] not in taken and LISTING_TEXT_RE.search(c["text"]):
            return c["url"]
    for c in cands:
        if c["url"] not in taken and LISTING_PATH_RE.search(urlparse(c["url"]).path.lower()):
            return c["url"]
    return None


def terms_excerpt(text: str) -> str:
    """Opening of the terms (seller identity) plus every paragraph about returns and withdrawal."""
    lines = text.split("\n")
    head, keep, size = lines[:15], [], 0
    for i, line in enumerate(lines[15:], 15):
        if TERMS_KEEP_RE.search(line.lower()):
            keep.append(i)
    picked = sorted({j for i in keep for j in (i - 1, i, i + 1) if 15 <= j < len(lines)})
    out = head + (["[...]"] if picked else [])
    last = None
    for j in picked:
        if last is not None and j != last + 1:
            out.append("[...]")
        out.append(lines[j])
        last = j
        size += len(lines[j])
        if size > TERMS_CHARS:
            break
    return "\n".join(out)


def code_hints(raw_html: str, url: str) -> list[str]:
    seen, out = set(), []
    for m in CODE_HINT_RE.finditer(raw_html):
        v = html_lib.unescape(m.group(1)).strip()
        if v and v not in seen and len(v) < 300 and not v.startswith("#"):
            seen.add(v)
            out.append(v)
    return out[:15]


def fetch_pages(domain: str, site_url: str | None, nip_url: str | None, platform: str | None) -> dict:
    """Fetch the store's pages. Returns {pages: [{kind, url, text, raw}], home_status, requests}."""
    f = Fetcher()
    read_robots(f, domain)  # sets the fetcher's robots.txt rules
    home = site_url or f"https://{domain}/"
    host = bare(urlparse(home).hostname or domain)
    site = domain if host == domain or host.endswith("." + domain) else host  # as in enrich
    pages: list[dict] = []
    taken: set[str] = set()

    def get(kind: str, url: str) -> dict | None:
        if len(pages) >= MAX_PAGES or url in taken or not f.allowed(url):
            return None
        taken.add(url)
        r = f.get(url)
        if r["error"] or r["status"] != 200 or is_blocked(r):
            return None
        if "html" not in r["headers"].get("content-type", "text/html"):
            return None
        raw = decode(r)
        p = parse(raw)
        final = r["final_url"]
        if final != url and final in {x["url"] for x in pages}:
            return None  # redirected to a page already read (often the homepage)
        page = {"kind": kind, "url": final, "requested": url, "text": p.text, "raw": raw, "parsed": p}
        pages.append(page)
        taken.add(final)
        return page

    h = get("home", home)
    if not h:
        return {"pages": [], "home_ok": False, "requests": f.requests, "site": site}
    cands = link_candidates(h["parsed"], h["url"], site)
    for kind, (text_re, href_re) in RESEARCH_RULES.items():
        url = best_link(cands, text_re, href_re, taken)
        if kind == "terms" and nip_url and same_site(nip_url, site):
            url = url if url and url.rstrip("/") == nip_url.rstrip("/") else (url or nip_url)
        page = get(kind, url) if url else None
        if not page and kind in FALLBACK:
            for path in FALLBACK[kind]:
                page = get(kind, urljoin(h["url"], path))
                if page:
                    break
        if kind == "terms" and nip_url and same_site(nip_url, site) and nip_url not in taken:
            get("terms_seller", nip_url)  # the page the NIP was read from, if it is another document
        if kind == "returns" and page:
            sub = [c for c in link_candidates(page["parsed"], page["url"], site)
                   if not CONTACT_RE.search(c["text"] + " " + c["path"])]
            form = best_link(sub, RETURN_FORM_RE.pattern, r"formularz|zwrot|return|odstap", taken)
            if form:
                get("return_form", form)
            exchange = best_link(sub + cands, r"wymian|exchange", r"wymian|exchange", taken)
            if exchange:
                get("exchanges", exchange)
    listing = listing_link(cands, taken, platform, h["url"])
    if listing:
        get("listing", listing)
    for p in pages:
        p.pop("parsed", None)
    return {"pages": pages, "home_ok": True, "requests": f.requests, "site": site}


# ---------------------------------------------------------------------------------------------
# Quote checking
# ---------------------------------------------------------------------------------------------

_TRANS = str.maketrans({" ": " ", " ": " ", " ": " ", "„": '"', "”": '"', "“": '"', "«": '"',
                        "»": '"', "‘": "'", "’": "'", "‚": "'", "–": "-", "—": "-", "‑": "-", "…": "..."})


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", html_lib.unescape(s)).translate(_TRANS)
    return " ".join(s.split()).casefold()


def quote_parts(q) -> list[str]:
    """A quote may be a string or a list; '...' inside a quote separates parts found separately."""
    items = q if isinstance(q, list) else [q]
    parts = []
    for item in items:
        for part in re.split(r"\s*(?:\.\.\.|…|\[\.\.\.\])\s*", str(item)):
            if part.strip():
                parts.append(part.strip())
    return parts


def check_quote(q, page: dict, evidence: str, max_quotes: int = 3) -> str | None:
    """None if every part of the quote is on the page, else the reason."""
    if q in (None, "", []):
        return "no_quote"
    items = q if isinstance(q, list) else [q]
    if len(items) > max_quotes:
        return "too_many_quotes"
    if any(len(str(i).split()) > QUOTE_MAX_WORDS + 5 for i in items):
        return "quote_too_long"
    hay = norm(page["raw"]) if evidence == "code" else page["norm_text"]
    hay_raw = norm(page["raw"]) if evidence != "code" else hay
    for part in quote_parts(q):
        n = norm(part)
        if n not in hay and not (evidence != "code" and n in hay_raw and len(n) >= 12):
            return "quote_not_on_page"
    return None


# ---------------------------------------------------------------------------------------------
# Pipeline facts and score (code, from config/icp.yaml)
# ---------------------------------------------------------------------------------------------

def pfact(value, url: str | None, **extra) -> dict | None:
    if value is None or not url:
        return None
    return {"value": value, **extra, "source_url": url, "origin": "pipeline"}


def pipeline_part(c: dict, sig: dict) -> tuple[dict, list[dict]]:
    raw = c.get("registry_raw") or {}
    vat_url = raw.get("vat_source_url")
    krs_url = raw.get("krs_source_url")
    reg = sig.get("registered_company")
    seller = {
        "name": pfact(c.get("name"), vat_url),
        "nip": pfact(c.get("nip"), c.get("nip_source_url")),
        "legal_form": pfact(c.get("legal_form"), krs_url),
        "vat_status": pfact(c.get("vat_status"), vat_url),
        "registered_company": pfact(reg["value"], reg["source_url"]) if reg else pfact(bool(c.get("krs")), vat_url),
    }
    meta = sig.get("meta_ads_active")
    meta_fact = None
    if meta:
        d = meta.get("details") or {}
        extra = {}
        if d.get("active_ads") is not None:
            extra["active_ads"] = d["active_ads"]
        if d.get("oldest_active_ad_start"):
            extra["oldest_active_ad_start"] = d["oldest_active_ad_start"]
        meta_fact = pfact(meta["value"], meta["source_url"], **extra)
    part = {
        "seller": seller,
        "what_they_sell": {
            "main_category": pfact(sig["category_fit"]["value"], sig["category_fit"]["source_url"])
            if "category_fit" in sig else None,
            "assortment_size": pfact(sig["assortment_size"]["value"], sig["assortment_size"]["source_url"])
            if "assortment_size" in sig else None,
        },
        "platform": pfact(sig["platform"]["value"], sig["platform"]["source_url"]) if "platform" in sig else None,
        "signals": {"meta_ads_active": meta_fact},
    }
    gaps = []
    for path, val in [("seller.name", seller["name"]), ("seller.nip", seller["nip"]),
                      ("seller.vat_status", seller["vat_status"]),
                      ("what_they_sell.main_category", part["what_they_sell"]["main_category"]),
                      ("what_they_sell.assortment_size", part["what_they_sell"]["assortment_size"]),
                      ("platform", part["platform"]), ("signals.meta_ads_active", meta_fact)]:
        if val is None:
            gaps.append({"field": path, "note": "Not known from the pipeline (phase 1)", "pages_checked": []})
    # legal_form stays null for a seller without a KRS entry (card format); other missing fields are gaps
    part["seller"] = {k: v for k, v in seller.items() if v is not None or k == "legal_form"}
    part["what_they_sell"] = {k: v for k, v in part["what_they_sell"].items() if v is not None}
    part["signals"] = {k: v for k, v in part["signals"].items() if v is not None}
    if part["platform"] is None:
        del part["platform"]
    return part, gaps


def score_card(icp: dict, part: dict) -> dict:
    """Fit score from scoring + signals in config/icp.yaml. Same facts, same score."""
    sigs = {s["id"]: s for s in icp["signals"]}
    get = {
        "meta_ads_active": ("signals.meta_ads_active", part["signals"].get("meta_ads_active")),
        "platform": ("platform", part.get("platform")),
        "category_fit": ("what_they_sell.main_category", part["what_they_sell"].get("main_category")),
        "assortment_size": ("what_they_sell.assortment_size", part["what_they_sell"].get("assortment_size")),
        "registered_company": ("seller.registered_company", part["seller"].get("registered_company")),
    }
    comps, total = [], Decimal(0)
    for comp in icp["scoring"]["components"]:
        ref, weight = comp["ref"], comp["weight"]
        path, fact = get[ref]
        value = fact["value"] if fact else None
        if ref == "platform":
            values = sigs["platform"]["values"]
            factor = values.get(value, values["custom"]) if value else values["unknown"]
        elif ref == "category_fit":
            factor = sigs["category_fit"]["values"].get(value, 0) if value else 0
        elif ref == "assortment_size":
            n = value if isinstance(value, int) else None
            factor = 1 if n and 1000 <= n <= 30000 else 0.5 if n and (500 <= n <= 999 or 30001 <= n <= 60000) else 0
        else:  # boolean signals
            factor = 1 if value is True else 0
        points = Decimal(str(weight)) * Decimal(str(factor))
        total += points
        comps.append({"ref": ref, "weight": weight, "value": value,
                      "points": int(points) if points == points.to_integral() else float(points), "based_on": path})
    score = int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    th = icp["scoring"]["thresholds"]
    label = "high" if score >= th["high"] else "medium" if score >= th["low"] else "low"
    return {"score": score, "label": label, "components": comps}


def pipeline_disqualifiers(icp: dict, part: dict) -> list[dict]:
    out = []
    seller, wts = part["seller"], part["what_they_sell"]
    vat = seller.get("vat_status")
    if vat and vat["value"] not in ("Czynny", "Zwolniony"):  # Zwolniony = trading below the VAT threshold
        out.append({"id": "vat_inactive", "based_on": "seller.vat_status"})
    if not seller.get("nip"):
        out.append({"id": "no_seller_id", "based_on": "seller.nip"})
    cat = wts.get("main_category")
    if cat and cat["value"] in icp["target"]["categories"]["exclude"]:
        out.append({"id": "excluded_category", "based_on": "what_they_sell.main_category"})
    size = wts.get("assortment_size")
    if size and isinstance(size["value"], int):
        if size["value"] < 500:
            out.append({"id": "small_stores", "based_on": "what_they_sell.assortment_size"})
        elif size["value"] > 60000:
            out.append({"id": "enterprise", "based_on": "what_they_sell.assortment_size"})
    return out


# ---------------------------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------------------------

FACT = {"type": "object", "properties": {
    "value": {}, "source_url": {"type": "string"},
    "quote": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
    "evidence": {"type": "string", "enum": ["text", "code"]}}, "required": ["value", "source_url", "quote"]}
QUOTED = {"source_url": {"type": "string"},
          "quote": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
          "evidence": {"type": "string", "enum": ["text", "code"]}}
SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "string"},
        "what_they_sell": {"type": "object", "properties": {
            "summary": {"type": "object", "properties": {"text": {"type": "string"},
                                                         "based_on": {"type": "array", "items": {"type": "string"}}},
                        "required": ["text", "based_on"]},
            "product_types": FACT, "audience": FACT, "price_range": FACT, "sells_b2c": FACT}},
        "hiring": {"type": "object", "properties": {
            "value": {"type": "boolean"},
            "roles": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "area": {"type": "string", "enum": sorted(HIRING_ROLES)}},
                "required": ["title", "area"]}},
            **QUOTED}, "required": ["value", "roles", "source_url", "quote"]},
        "returns": {"type": "object", "properties": {k: FACT for k in (
            "window_days", "free_returns", "how_to_return", "returns_tool", "withdrawal_function", "exchanges",
            "refund_days")}},
        "observations": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "text": {"type": "string"}, "why_it_matters": {"type": "string"}, **QUOTED},
            "required": ["id", "text", "source_url", "quote", "why_it_matters"]}},
        "problem_hypothesis": {"type": "object", "properties": {
            "statement": {"type": "string"}, "how_we_help": {"type": "integer"},
            "based_on": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
            "required": ["statement", "how_we_help", "based_on", "confidence"]},
        "fit_reasoning": {"type": "string"},
        "disqualifier_findings": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string", "enum": sorted(AGENT_DISQUALIFIERS)}, "note": {"type": "string"}, **QUOTED},
            "required": ["id", "source_url", "quote"]}},
        "checks": {"type": "array", "items": {"type": "object", "properties": {
            "signal": {"type": "string"}, "pipeline_value": {}, "agent_value": {}, "note": {"type": "string"},
            **QUOTED}, "required": ["signal", "pipeline_value", "agent_value", "source_url", "quote"]}},
        "gaps": {"type": "array", "items": {"type": "object", "properties": {
            "field": {"type": "string"}, "note": {"type": "string"},
            "pages_checked": {"type": "array", "items": {"type": "string"}}}, "required": ["field", "note"]}},
    },
    "required": ["tldr", "what_they_sell", "returns", "observations", "problem_hypothesis", "fit_reasoning",
                 "disqualifier_findings", "checks", "gaps"],
}


def strip_shared(text: str, shared: set[str]) -> str:
    """Drop lines that are also on the homepage (menu, footer), so each page sends only its own content."""
    return "\n".join(line for line in text.split("\n") if line not in shared or len(line) > 200)


def page_block(p: dict, shared: set[str]) -> str:
    text = p["text"] if p["kind"] == "home" else strip_shared(p["text"], shared)
    text = terms_excerpt(text) if p["kind"] in ("terms", "terms_seller") else text
    if len(text) > PAGE_CHARS:
        text = text[:PAGE_CHARS] + "\n[... page text cut ...]"
    hints = code_hints(p["raw"], p["url"])
    out = f"## PAGE ({p['kind']}): {p['url']}\n\n{text}\n"
    if hints:
        out += "\n### Links and forms in the page code\n" + "\n".join(f"- {h}" for h in hints) + "\n"
    return out


def build_prompt(icp: dict, domain: str, part: dict, fit: dict, disq: list[dict], pages: list[dict]) -> str:
    shared = set(next((p["text"] for p in pages if p["kind"] == "home"), "").split("\n"))
    helps = "\n".join(f"{i}. persona {h['persona']}: pain \"{h['pain']}\" -> {h['capability']}"
                      for i, h in enumerate(icp["how_we_help"], 1))
    return (
        f"# Store: {domain}\n\n"
        f"## Pipeline facts (checked by code in phase 1)\n```json\n{json.dumps(part, ensure_ascii=False, indent=1)}\n```\n\n"
        f"## Fit score (computed by code from config/icp.yaml, fixed)\n"
        f"Score {fit['score']}/100, label {fit['label']}. Components: "
        + "; ".join(f"{c['ref']} = {c['value']} -> {c['points']}/{c['weight']}" for c in fit["components"])
        + (f". Disqualifiers from pipeline: {', '.join(d['id'] for d in disq)}" if disq else "")
        + "\n\n## How Odsyłka helps (pick one number for problem_hypothesis.how_we_help)\n"
        f"{helps}\n\n"
        f"Odsyłka is a returns management SaaS for mid-sized Polish online stores: {icp['company']['product']['problem']}\n\n"
        f"# Pages of the store ({len(pages)})\n"
        "Lines that repeat the homepage (menu, footer) are left out of the other pages.\n\n"
        + "\n".join(page_block(p, shared) for p in pages)
    )


def call_claude(system: str, prompt: str, model: str) -> dict:
    cmd = ["claude", "-p", "--output-format", "json", "--model", model, "--tools", "",
           "--system-prompt", system, "--setting-sources", "", "--strict-mcp-config",
           "--no-session-persistence", "--json-schema", json.dumps(SCHEMA)]
    with tempfile.TemporaryDirectory() as tmp:  # empty cwd: no CLAUDE.md or project settings
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=tmp)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p exit {r.returncode}: {(r.stderr or r.stdout)[:500]}")
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"claude -p error: {str(out.get('result') or out.get('subtype'))[:500]}")
    return out


def usage_of(out: dict) -> dict:
    """Tokens summed over every model the call used. `claude -p` also makes a small helper call to a
    smaller model, so the model that wrote the card is the one with the most output tokens."""
    mu = out.get("modelUsage") or {}
    model = max(mu, key=lambda m: mu[m].get("outputTokens") or 0) if mu else None
    return {"model": model,
            "input_tokens": sum((v.get("inputTokens") or 0) + (v.get("cacheReadInputTokens") or 0)
                                + (v.get("cacheCreationInputTokens") or 0) for v in mu.values()),
            "output_tokens": sum(v.get("outputTokens") or 0 for v in mu.values()),
            "cost_usd": round(out.get("total_cost_usd") or 0, 4),
            "usage_by_model": {m: {k: v.get(k) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                                                         "cacheCreationInputTokens", "costUSD")}
                               for m, v in mu.items()}}


# ---------------------------------------------------------------------------------------------
# Checking the agent part and assembling the card
# ---------------------------------------------------------------------------------------------

def value_problem(path: str, v) -> str | None:
    """Type and allowed-value checks per field; None if fine."""
    if v is None:
        return "null_value"
    if path == "what_they_sell.product_types":
        return None if isinstance(v, list) and v and all(isinstance(x, str) for x in v) else "bad_value"
    if path == "what_they_sell.price_range":
        ok = isinstance(v, dict) and all(isinstance(v.get(k), (int, float)) for k in ("min", "max")) \
            and v["min"] <= v["max"]
        return None if ok else "bad_value"
    if path in ("what_they_sell.sells_b2c", "returns.free_returns", "returns.withdrawal_function",
                "returns.exchanges"):
        return None if isinstance(v, bool) else "bad_value"
    if path in ("returns.window_days", "returns.refund_days"):
        return None if isinstance(v, int) and 0 < v <= 365 else "bad_value"
    if path == "returns.how_to_return":
        vals = v if isinstance(v, list) else [v]
        return None if vals and all(x in HOW_TO_RETURN for x in vals) else "bad_value"
    if path == "signals.hiring":
        return None if isinstance(v, bool) else "bad_value"
    if path in ("what_they_sell.audience", "returns.returns_tool"):
        return None if isinstance(v, str) and v.strip() else "bad_value"
    return None


class Checker:
    def __init__(self, domain: str, site: str, pages: list[dict]) -> None:
        self.domain, self.site = domain, site
        self.pages: dict[str, dict] = {}
        for p in pages:
            p["norm_text"] = norm(p["text"])
            for u in (p["url"], p["requested"]):
                self.pages[u] = p
                self.pages[u.rstrip("/")] = p
        self.dropped: list[dict] = []

    def page_for(self, url: str) -> dict | None:
        return self.pages.get(url) or self.pages.get((url or "").rstrip("/"))

    def check(self, path: str, item: dict) -> bool:
        """Source and quote check; records the reason when the item is dropped."""
        url = item.get("source_url") or ""
        reason = None
        if not url:
            reason = "no_source"
        elif not (same_site(url, self.domain) or same_site(url, self.site)):
            reason = "source_off_domain"
        else:
            page = self.page_for(url)
            if not page:
                reason = "source_not_read"
            else:
                reason = check_quote(item.get("quote"), page, item.get("evidence") or "text",
                                     MAX_QUOTES.get(path, 3))
        if reason:
            self.dropped.append({"path": path, "reason": reason, "item": item})
            return False
        return True

    def fact(self, path: str, f: dict | None) -> dict | None:
        if not isinstance(f, dict):
            return None
        problem = value_problem(path, f.get("value"))
        if problem:
            self.dropped.append({"path": path, "reason": problem, "item": f})
            return None
        if not self.check(path, f):
            return None
        out = {"value": f["value"], "source_url": f["source_url"], "quote": f["quote"]}
        if f.get("evidence") == "code":
            out["evidence"] = "code"
        out["origin"] = "agent"
        return out


def assemble(icp: dict, domain: str, site: str, part: dict, fit: dict, disq: list[dict], gaps0: list[dict],
             agent: dict, pages: list[dict]) -> tuple[dict, list[dict], dict]:
    ck = Checker(domain, site, pages)
    card = {"card_version": CARD_VERSION, "domain": domain, "tldr": None}
    card.update(json.loads(json.dumps(part)))  # deep copy: seller, what_they_sell, platform, signals

    wts_a = agent.get("what_they_sell") or {}
    for k in ("product_types", "audience", "price_range", "sells_b2c"):
        f = ck.fact(f"what_they_sell.{k}", wts_a.get(k))
        if f:
            card["what_they_sell"][k] = f
    returns = {}
    for k, f in (agent.get("returns") or {}).items():
        f = ck.fact(f"returns.{k}", f)
        if f:
            returns[k] = f
    card["returns"] = returns

    hiring = agent.get("hiring")
    if isinstance(hiring, dict):
        roles = [r for r in hiring.get("roles") or [] if isinstance(r, dict) and r.get("area") in HIRING_ROLES]
        f = ck.fact("signals.hiring", hiring)
        relevant = any(r["area"] != "other" for r in roles)
        if f and f["value"] != relevant:  # value must follow from the roles listed
            ck.dropped.append({"path": "signals.hiring", "reason": "inconsistent_roles", "item": hiring})
            f = None
        if f:
            f["roles"] = roles
            f["relevant_roles"] = sum(1 for r in roles if r["area"] != "other")
            card["signals"]["hiring"] = f

    obs = []
    for i, o in enumerate(agent.get("observations") or [], 1):
        o = dict(o)
        o["id"] = f"o{len(obs) + 1}"
        if ck.check(f"observations[{i}]", o):
            item = {"id": o["id"], "text": o["text"], "source_url": o["source_url"], "quote": o["quote"]}
            if o.get("evidence") == "code":
                item["evidence"] = "code"
            item.update(origin="agent", why_it_matters=o.get("why_it_matters"))
            obs.append(item)
    card["observations"] = obs[:3]

    # Paths that exist after the checks; agent text may cite only these.
    existing = set()
    for sec in ("seller", "what_they_sell", "returns", "signals"):
        for k in card.get(sec) or {}:
            existing.add(f"{sec}.{k}")
    if "platform" in card:
        existing.add("platform")
    existing |= {f"observations.{o['id']}" for o in obs}
    # Observation ids may have shifted after drops: map the model's ids to the kept ones by text.
    remap = {}
    for o in agent.get("observations") or []:
        kept = next((k for k in obs if k["text"] == o.get("text")), None)
        if kept and o.get("id"):
            remap[f"observations.{o['id']}"] = f"observations.{kept['id']}"
    pruned = {"count": 0}

    def keep_paths(paths) -> list[str]:
        out = []
        for p in paths or []:
            p = remap.get(p, p)
            if p in existing and p not in out:
                out.append(p)
            else:
                pruned["count"] += 1
        return out

    summ = wts_a.get("summary") or {}
    if summ.get("text"):
        card["what_they_sell"]["summary"] = {"text": summ["text"], "based_on": keep_paths(summ.get("based_on"))}
        card["what_they_sell"] = {"summary": card["what_they_sell"].pop("summary"), **card["what_they_sell"]}

    hyp = agent.get("problem_hypothesis") or {}
    helps = icp["how_we_help"]
    idx = hyp.get("how_we_help")
    if isinstance(idx, int) and 1 <= idx <= len(helps):
        h = helps[idx - 1]
        card["problem_hypothesis"] = {
            "statement": hyp.get("statement"), "persona": h["persona"], "pain": h["pain"],
            "capability": h["capability"], "based_on": keep_paths(hyp.get("based_on")),
            "confidence": hyp.get("confidence")}
    else:
        ck.dropped.append({"path": "problem_hypothesis", "reason": "bad_how_we_help", "item": hyp})

    found = []
    for i, d in enumerate(agent.get("disqualifier_findings") or [], 1):
        if d.get("id") in AGENT_DISQUALIFIERS and ck.check(f"disqualifier_findings[{i}]", d):
            found.append({"id": d["id"], "source_url": d["source_url"], "quote": d["quote"], "note": d.get("note"),
                          "origin": "agent"})
    all_disq = disq + [d for d in found if d["id"] not in {x["id"] for x in disq}]
    card["fit"] = {
        "score": fit["score"],
        "label": "disqualified" if all_disq else fit["label"],
        "components": fit["components"],
        "disqualifiers": all_disq,
        "reasoning": agent.get("fit_reasoning"),
    }

    checks = []
    for i, c in enumerate(agent.get("checks") or [], 1):
        if str(c.get("agent_value")).strip().lower() == str(c.get("pipeline_value")).strip().lower():
            continue  # the agent agrees with the pipeline: nothing to review
        if ck.check(f"checks[{i}]", c):
            item = {k: c.get(k) for k in ("signal", "pipeline_value", "agent_value", "source_url", "quote")}
            if c.get("evidence") == "code":
                item["evidence"] = "code"
            item.update(origin="agent", note=c.get("note"))
            checks.append(item)
    card["checks"] = checks

    card["gaps"] = gaps0 + [g for g in agent.get("gaps") or [] if isinstance(g, dict) and g.get("field")]
    for d in ck.dropped:  # a dropped field is a gap too, so the card never pretends it looked and found nothing
        if d["path"].startswith(("what_they_sell.", "returns.", "signals.")):
            card["gaps"].append({"field": d["path"], "note": f"Found but not verified ({d['reason']}); left out",
                                 "pages_checked": [d["item"].get("source_url")] if d["item"].get("source_url") else []})
    card["pages_read"] = [p["url"] for p in pages]

    tldr = (agent.get("tldr") or "").strip()
    label = card["fit"]["label"]
    card["tldr"] = f"{tldr} Fit {fit['score']}/100 ({label}).".strip()
    stats = {"agent_facts_proposed": sum(1 for _ in _agent_items(agent)), "dropped": len(ck.dropped),
             "based_on_pruned": pruned["count"]}
    return card, ck.dropped, stats


def _agent_items(agent: dict):
    for k in ("product_types", "audience", "price_range", "sells_b2c"):
        if (agent.get("what_they_sell") or {}).get(k):
            yield k
    yield from (agent.get("returns") or {}).keys()
    if agent.get("hiring"):
        yield "hiring"
    yield from agent.get("observations") or []
    yield from agent.get("disqualifier_findings") or []
    yield from agent.get("checks") or []


# ---------------------------------------------------------------------------------------------
# One store
# ---------------------------------------------------------------------------------------------

def research(c: dict, sig: dict, icp: dict, system: str, model: str) -> dict:
    t0 = time.monotonic()
    res = {"domain": c["domain"], "company_id": c["id"], "status": None, "error": None}
    part, gaps0 = pipeline_part(c, sig)
    fit = score_card(icp, part)
    disq = pipeline_disqualifiers(icp, part)
    got = fetch_pages(c["domain"], c.get("site_url"), c.get("nip_source_url"),
                      (sig.get("platform") or {}).get("value"))
    res["requests"] = got["requests"]
    res["fetch_seconds"] = round(time.monotonic() - t0, 1)
    if not got["home_ok"]:
        res.update(status="site_unreadable", seconds=round(time.monotonic() - t0, 1))
        return res
    pages = got["pages"]
    prompt = build_prompt(icp, c["domain"], part, fit, disq, pages)
    t1 = time.monotonic()
    try:
        out = call_claude(system, prompt, model)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        res.update(status="model_error", error=str(e)[:500], seconds=round(time.monotonic() - t0, 1))
        return res
    res["model_seconds"] = round(time.monotonic() - t1, 1)
    res.update(usage_of(out))
    card, dropped, stats = assemble(icp, c["domain"], got["site"], part, fit, disq, gaps0,
                                     out["structured_output"], pages)
    res.update(status="ok", card=card, dropped=dropped, stats=stats, prompt_chars=len(prompt),
               agent_output=out["structured_output"],  # raw model answer, local file only (evaluation)
               seconds=round(time.monotonic() - t0, 1))
    return res


# ---------------------------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------------------------

def fetch_all(db: Supabase, path: str) -> list[dict]:
    rows, offset = [], 0
    sep = "&" if "?" in path else "?"
    while True:
        page = db.request("GET", f"{path}{sep}limit={PAGE_SIZE}&offset={offset}") or []
        rows += page
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


COMPANY_COLS = ("id,domain,site_url,nip,nip_source_url,name,krs,legal_form,vat_status,registry_status,"
                "registry_raw")


def load_companies(db: Supabase, a) -> list[dict]:
    if a.domains:
        q = ",".join(a.domains)
        rows = fetch_all(db, f"/companies?select={COMPANY_COLS}&domain=in.({q})&order=id")
    else:
        fit_ids = {r["id"] for r in fetch_all(db, "/company_icp?select=id&icp_status=eq.fit&order=id")}
        rows = [r for r in fetch_all(db, f"/companies?select={COMPANY_COLS}&order=id") if r["id"] in fit_ids]
        if not a.redo and not a.sample:
            done = {r["company_id"] for r in fetch_all(db, "/research?select=company_id&order=id")}
            rows = [r for r in rows if r["id"] not in done]
        if a.sample:  # stable random sample: order by md5(seed + id), as in the phase 1 manual check
            rows.sort(key=lambda r: hashlib.md5(f"{a.seed}:{r['id']}".encode()).hexdigest())
            rows = rows[:a.sample]
    return rows[:a.limit] if a.limit else rows


def load_signals(db: Supabase, ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {i: {} for i in ids}
    for i in range(0, len(ids), 100):
        chunk = ",".join(str(x) for x in ids[i:i + 100])
        rows = fetch_all(db, f"/signals?select=company_id,type,value,source_url,details,observed_at,id"
                             f"&company_id=in.({chunk})&order=observed_at.desc,id.desc")
        for r in rows:
            out[r["company_id"]].setdefault(r["type"], r)  # newest first: keep the latest per type
    return out


def research_row(res: dict, run_id: int, agent_version: str) -> dict:
    card = res["card"]
    return {
        "company_id": res["company_id"], "card": card, "score": card["fit"]["score"], "fit": card["fit"]["label"],
        "score_breakdown": {c["ref"]: c["points"] for c in card["fit"]["components"]},
        "disqualifiers": [d["id"] for d in card["fit"]["disqualifiers"]],
        "agent_version": agent_version, "model": res.get("model"), "input_tokens": res.get("input_tokens"),
        "output_tokens": res.get("output_tokens"), "duration_seconds": res.get("seconds"),
        "cost_usd": res.get("cost_usd"), "run_id": run_id,
    }


# ---------------------------------------------------------------------------------------------

def print_result(i: int, n: int, r: dict) -> None:
    if r["status"] != "ok":
        print(f"[{i}/{n}] {r['domain']}: {r['status']} {r.get('error') or ''}", flush=True)
        return
    c = r["card"]
    print(f"[{i}/{n}] {r['domain']}: {c['fit']['score']} {c['fit']['label']}, "
          f"{len(c.get('returns', {}))} returns facts, {len(c['observations'])} obs, "
          f"dropped {len(r['dropped'])}, {len(c['pages_read'])} pages, {r['seconds']} s, "
          f"{r.get('input_tokens')}/{r.get('output_tokens')} tok, ${r.get('cost_usd')}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("domains", nargs="*", help="store domains (default: ICP-fit stores without a card)")
    ap.add_argument("--sample", type=int, help="a stable random sample of this many ICP-fit stores")
    ap.add_argument("--seed", default="research", help="seed of --sample (same seed, same stores)")
    ap.add_argument("--limit", type=int, help="at most this many stores")
    ap.add_argument("--redo", action="store_true", help="also stores that already have a card")
    ap.add_argument("--workers", type=int, default=2, help="stores researched in parallel (default 2)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"model for claude -p (default {DEFAULT_MODEL})")
    ap.add_argument("--dry-run", action="store_true", help="research and print; write nothing to Supabase")
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    a = ap.parse_args()

    icp = yaml.safe_load(ICP_YAML.read_text())
    system = PROMPT_FILE.read_text()
    agent_version = f"{AGENT_VERSION}-{hashlib.sha1((system + json.dumps(SCHEMA)).encode()).hexdigest()[:7]}"
    env = read_env(a.env)
    if not (env.get("SUPABASE_URL") and env.get("SUPABASE_SERVICE_ROLE_KEY")):
        sys.exit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env (needed to read the pipeline facts).")
    db = Supabase(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])

    companies = load_companies(db, a)
    missing = sorted(set(a.domains) - {c["domain"] for c in companies})
    if missing:
        print(f"Not in companies, skipped: {', '.join(missing)}")
    if not companies:
        print("Nothing to research.")
        return
    sigs = load_signals(db, [c["id"] for c in companies])

    started = dt.datetime.now(dt.timezone.utc)
    args_note = {k: str(v) for k, v in vars(a).items() if k != "env"}
    run_id = None
    if not a.dry_run:
        run_id = db.request("POST", "/runs", {"step": "research", "started_at": started.isoformat(),
                                              "items_total": len(companies),
                                              "notes": {"args": args_note, "agent_version": agent_version}},
                            prefer="return=representation")[0]["id"]
    out_dir = OUT_DIR / (f"run-{run_id}" if run_id else f"dry-run-{started:%Y%m%d-%H%M%S}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Researching {len(companies)} stores with {a.workers} workers, model {a.model}, agent {agent_version}"
          f"{' (dry run)' if a.dry_run else f', run {run_id}'} -> {out_dir.relative_to(ROOT)}", flush=True)

    results, save_errors = [], []
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=a.workers)
    try:
        futures = {pool.submit(research, c, sigs.get(c["id"], {}), icp, system, a.model): c for c in companies}
        for i, fut in enumerate(as_completed(futures), 1):
            c = futures[fut]
            try:
                res = fut.result()
            except Exception as e:  # a bug for one store must not stop the run
                res = {"domain": c["domain"], "company_id": c["id"], "status": "crash",
                       "error": f"{type(e).__name__}: {e}"[:500]}
            with lock:
                results.append(res)
                (out_dir / f"{c['domain']}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
            print_result(i, len(companies), res)
            if run_id and res["status"] == "ok":
                try:
                    db.request("POST", "/research", research_row(res, run_id, agent_version),
                               prefer="return=minimal")
                    hiring = res["card"]["signals"].get("hiring")
                    if hiring:  # step 2.3: the hiring signal also goes to `signals`, for SQL and the dashboard
                        db.request("POST", "/signals", {
                            "company_id": res["company_id"], "type": "hiring", "value": hiring["value"],
                            "source": "website", "source_url": hiring["source_url"], "run_id": run_id,
                            "details": {"roles": hiring["roles"], "relevant_roles": hiring["relevant_roles"],
                                        "quote": hiring["quote"], "agent_version": agent_version}},
                            prefer="return=minimal")
                except SystemExit as e:  # Supabase.request exits on HTTP errors
                    save_errors.append(f"{c['domain']}: {e}"[:300])
                    res["save_error"] = True
    except KeyboardInterrupt:
        print("\nStopped: saving what is done.")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        ok = [r for r in results if r["status"] == "ok" and not r.get("save_error")]
        dropped: dict[str, int] = {}
        for r in ok:
            for d in r["dropped"]:
                dropped[d["reason"]] = dropped.get(d["reason"], 0) + 1
        summary = {
            "stores": len(results), "ok": len(ok),
            "failed": {s: sum(1 for r in results if r["status"] == s) for s in
                       {r["status"] for r in results if r["status"] != "ok"}},
            "agent_facts_proposed": sum(r["stats"]["agent_facts_proposed"] for r in ok),
            "facts_dropped": sum(len(r["dropped"]) for r in ok), "dropped_by_reason": dropped,
            "based_on_pruned": sum(r["stats"]["based_on_pruned"] for r in ok),
            "input_tokens": sum(r.get("input_tokens") or 0 for r in ok),
            "output_tokens": sum(r.get("output_tokens") or 0 for r in ok),
            "cost_usd": round(sum(r.get("cost_usd") or 0 for r in ok), 4),
            "avg_seconds": round(sum(r["seconds"] for r in ok) / len(ok), 1) if ok else None,
            "save_errors": save_errors[:20],
        }
        if run_id:
            db.request("PATCH", f"/runs?id=eq.{run_id}", {
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(), "items_total": len(companies),
                "items_ok": len(ok), "items_failed": len(results) - len(ok),
                "input_tokens": summary["input_tokens"], "output_tokens": summary["output_tokens"],
                "cost_usd": summary["cost_usd"],
                "notes": {"args": args_note, "agent_version": agent_version, "model": a.model,
                          "output": out_dir.name, "unfinished": len(companies) - len(results), **summary},
            }, prefer="return=minimal")
        print("\nSummary:")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
