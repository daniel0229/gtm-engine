#!/usr/bin/env python3
"""Collect store domains from public store lists (sources 1 and 2 in docs/sources.md).

  shopify_blog  links to stores in the Shopify Polish blog article on stores built with Shopify
  shoper        client pages in the Shoper sitemap; the store's domain is encoded in the URL slug
                (/klienci/chochliki-sklep-pl), so each slug is turned into candidate domains and
                the first candidate that answers over HTTPS is kept

Domains in config/domain-blocklist.yaml are dropped. Output: data/stores-lists.csv, one row per
domain, with the page it came from and a platform hint (the platform that lists it).

Usage (from the repo root):
  .venv/bin/python src/collect_lists.py
"""
from __future__ import annotations

import csv
import re
import socket
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
BLOCKLIST = ROOT / "config/domain-blocklist.yaml"
OUT = ROOT / "data/stores-lists.csv"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

SHOPIFY_ARTICLE = "https://www.shopify.com/pl/blog/sklepy-na-shopify"
SHOPER_SITEMAP = "https://www.shoper.pl/sitemap.xml"
# Links in the Shopify article that are not stores (Shopify's own sites, social networks).
NOT_STORES = ("shopify.", "shop.app", "shop.dev", "devdegree.ca", "facebook.", "instagram.", "twitter.",
              "x.com", "linkedin.", "youtube.", "tiktok.", "pinterest.", "google.", "apple.com", "cdn.")
# Second-level parts that, with the last slug part, form a public suffix (sklep.pl, com.pl).
SECOND_LEVEL = {"com", "sklep", "net", "org", "info", "biz", "waw", "shop", "co", "media"}


def get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pl,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def bare(domain: str) -> str:
    d = (domain or "").lower().strip().rstrip(".")
    return d[4:] if d.startswith("www.") else d


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith("http"):
                self.hrefs.append(href)


def shopify_blog() -> list[dict]:
    p = Links()
    p.feed(get(SHOPIFY_ARTICLE))
    rows, seen = [], set()
    for href in p.hrefs:
        dom = bare(urlparse(href).hostname or "")
        if not dom or dom in seen or any(dom.startswith(n) or f".{n}" in f".{dom}" for n in NOT_STORES):
            continue
        seen.add(dom)
        rows.append({"domain": dom, "source": "shopify_blog", "source_url": SHOPIFY_ARTICLE,
                     "platform_hint": "shopify", "note": ""})
    return rows


def candidates(slug: str) -> list[str]:
    """chochliki-sklep-pl -> [chochliki.sklep.pl, chochliki-sklep.pl]; e-armet-pl -> [e-armet.pl]."""
    parts = slug.split("-")
    out = []
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL:
        out.append("-".join(parts[:-2]) + "." + parts[-2] + "." + parts[-1])
    out.append("-".join(parts[:-1]) + "." + parts[-1])
    # Rare: a dot inside the name part (www-like subdomains). Try every other split last.
    for i in range(1, len(parts) - 1):
        c = "-".join(parts[:i]) + "." + ".".join(["-".join(parts[i:-1]), parts[-1]])
        if c not in out:
            out.append(c)
    return out


def answers(domain: str) -> tuple[bool, str]:
    """True if the domain resolves and serves any HTTP response; also returns where it redirects."""
    try:
        socket.getaddrinfo(domain, 443)
    except socket.gaierror:
        return False, ""
    for scheme in ("https", "http"):
        try:
            req = urllib.request.Request(f"{scheme}://{domain}/", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
                return True, bare(urlparse(r.geturl()).hostname or "")
        except urllib.error.HTTPError as e:  # 403 from a bot check still means the store exists
            return True, bare(urlparse(e.geturl() or "").hostname or domain)
        except Exception:
            continue
    return False, ""


def resolve(slug: str) -> dict:
    for c in candidates(slug):
        ok, final = answers(c)
        if ok:
            note = f"redirects to {final}" if final and final != c else ""
            return {"domain": c, "note": note}
    return {"domain": "", "note": "no candidate answered: " + ", ".join(candidates(slug))}


def shoper() -> list[dict]:
    urls = re.findall(r"<loc>(https://www\.shoper\.pl/klienci/[^<]+)</loc>", get(SHOPER_SITEMAP))
    slugs = [u.rstrip("/").rsplit("/", 1)[1] for u in urls]
    with ThreadPoolExecutor(max_workers=10) as pool:
        resolved = list(pool.map(resolve, slugs))
    return [{"domain": r["domain"], "source": "shoper", "source_url": u, "platform_hint": "shoper",
             "note": r["note"]} for u, r in zip(urls, resolved)]


def main() -> None:
    bl_conf = yaml.safe_load(BLOCKLIST.read_text(encoding="utf-8"))
    blocklist = {bare(d) for ds in bl_conf.values() if isinstance(ds, list) for d in ds}

    rows = shopify_blog() + shoper()
    kept, unresolved, dropped, seen = [], [], [], set()
    for r in rows:
        if not r["domain"]:
            unresolved.append(r)
        elif any(r["domain"] == b or r["domain"].endswith("." + b) for b in blocklist):
            dropped.append(r)
        elif r["domain"] not in seen:
            seen.add(r["domain"])
            kept.append(r)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "source", "source_url", "platform_hint", "note"])
        w.writeheader()
        w.writerows(kept + unresolved)

    for src in ("shopify_blog", "shoper"):
        n = sum(1 for r in rows if r["source"] == src)
        k = sum(1 for r in kept if r["source"] == src)
        print(f"{src}: {n} found, {k} kept")
    print(f"Unresolved: {len(unresolved)}; dropped by blocklist: {len(dropped)}")
    for r in unresolved:
        print(f"  {r['source_url']}: {r['note']}")
    print(f"-> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
