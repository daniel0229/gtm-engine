#!/usr/bin/env python3
"""Check whether each store runs active ads on Facebook and Instagram (step 1.5).

Source: the Meta Ad Library API (graph.facebook.com/<version>/ads_archive). For ads delivered in
the EU it returns all ads, not only political ones, so Polish stores are covered. Three limits
shape this script:

  1. No search by website. The API searches ad text (`search_terms`) or Facebook Page ids
     (`search_page_ids`, up to 10 per call). So for each store:
       a. search active ads shown in Poland for the store's domain, then for its name (the domain
          without the TLD, e.g. "butikmila" for butikmila.pl), stopping at the first query that matches;
       b. keep only ads whose link caption is the store's domain (the address printed under the
          ad, e.g. "BUTIKMILA.PL"), or whose Page name equals the store's name;
       c. take the Page ids of those ads and count all active ads of those Pages in Poland.
  2. No spend or impressions for non-political ads, so the signal is "active ads: yes / no, how
     many", which is what config/icp.yaml asks for.
  3. No published rate limit. The script waits between calls, reads Meta's usage headers and
     slows down near the limit, and on a rate-limit error pauses and retries. When it has to
     stop, everything done so far is saved and the next run continues.

Writes:
  signals  meta_ads_active (true / false), source meta_ad_library, source_url = the public Ad
           Library page anyone can open to check it (no token in it), details = active ad
           count, Pages, how they were matched, queries used
  runs     one row for this run: counts, API calls, rate-limit pauses
  data/meta_ads/<run>.jsonl  full result per store (gitignored)

"false" means: no active ad shown in Poland links to the store's domain or carries its name.
A store whose ads print no link caption and whose Page has a different name is missed; the check
against docs/baseline.md measures how often that happens.

Usage (from the repo root):
  .venv/bin/python src/meta_ads.py --check-token                # is the token valid, when does it expire
  .venv/bin/python src/meta_ads.py --dry-run --domain sklep.pl  # query one store, print, write nothing
  .venv/bin/python src/meta_ads.py --limit 20                   # first 20 stores not checked yet
  .venv/bin/python src/meta_ads.py                              # every store not checked yet
  .venv/bin/python src/meta_ads.py --redo --limit 50            # check already checked stores again

Stores = companies whose latest is_store signal (enrich step) is true. Safe to stop with Ctrl-C.

Credentials come from .env: META_ACCESS_TOKEN (60-day user token of an app with Ad Library API
access), SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest import ROOT, Supabase, read_env  # noqa: E402

GRAPH = "https://graph.facebook.com/v27.0"
COUNTRY = "PL"
OUT_DIR = ROOT / "data/meta_ads"
PAGE_SIZE = 1000             # PostgREST rows per page
MIN_INTERVAL = 2.0           # seconds between two API calls; Meta publishes no fixed limit
SLOW_INTERVAL = 20.0         # used while Meta's usage header is above USAGE_SLOW percent
USAGE_SLOW = 75
RATE_LIMIT_WAITS = (60, 300, 900)  # pauses after a rate-limit error before giving up the run
SEARCH_LIMIT = 100           # ads per page of search results
SEARCH_PAGES = 2             # result pages read per query (the matching ads come first or not at all)
COUNT_LIMIT = 250            # ads per page when counting a Page's active ads
MAX_COUNT_PAGES = 20         # stop counting above 5,000 ads (marked as a lower bound)
MIN_NAME_LEN = 4             # shorter store names are too generic to search for
SEARCH_FIELDS = "id,page_id,page_name,ad_creative_link_captions,ad_delivery_start_time"
COUNT_FIELDS = "id,page_id,page_name,ad_delivery_start_time"
LIBRARY = "https://www.facebook.com/ads/library/"

# Graph API error codes (developers.facebook.com/docs/graph-api/guides/error-handling)
RATE_LIMIT_CODES = {4, 17, 32, 613, 80000, 80004}
TOKEN_CODES = {102, 190}
NO_ACCESS_CODES = {10, 200}


class MetaError(Exception):
    def __init__(self, err: dict, status: int | None = None) -> None:
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        self.message = err.get("message") or str(err)
        self.status = status
        super().__init__(f"Meta API error {self.code}/{self.subcode}: {self.message}")


class StopRun(Exception):
    """The run cannot go on (token expired, no access, rate limit kept hitting)."""


# ---------------------------------------------------------------------------------------------
# Graph API client
# ---------------------------------------------------------------------------------------------

class Meta:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls = 0
        self.rate_limit_pauses = 0
        self.interval = MIN_INTERVAL
        self._last = 0.0

    def _wait(self) -> None:
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)

    def _read_usage(self, headers) -> None:
        """Slow down when any usage share Meta reports is above USAGE_SLOW percent."""
        top = 0
        for name in ("x-app-usage", "x-ad-account-usage", "x-business-use-case-usage"):
            raw = headers.get(name)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for v in _numbers(data):
                top = max(top, v)
        self.interval = SLOW_INTERVAL if top >= USAGE_SLOW else MIN_INTERVAL

    def get(self, path: str, params: dict) -> dict:
        url = f"{GRAPH}/{path}?" + urllib.parse.urlencode({**params, "access_token": self.token})
        return self.get_url(url)

    def get_url(self, url: str) -> dict:
        """GET a Graph URL (paging.next URLs already carry the token). Retries rate limits."""
        for attempt in range(len(RATE_LIMIT_WAITS) + 1):
            self._wait()
            self.calls += 1
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    self._last = time.monotonic()
                    self._read_usage(r.headers)
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                self._last = time.monotonic()
                self._read_usage(e.headers)
                body = e.read().decode(errors="replace")
                try:
                    err = MetaError(json.loads(body).get("error") or {}, e.code)
                except ValueError:
                    err = MetaError({"message": body[:300]}, e.code)
                if err.code in TOKEN_CODES:
                    raise StopRun(f"Token rejected ({err}). Generate a new 60-day token and put it in .env.")
                if err.code in NO_ACCESS_CODES:
                    raise StopRun(f"No Ad Library API access ({err}). Finish the steps at {LIBRARY}api/.")
                if err.code not in RATE_LIMIT_CODES and (e.code < 500):
                    raise err
            except (urllib.error.URLError, TimeoutError) as e:
                self._last = time.monotonic()
                err = MetaError({"message": f"network: {e}"})
            if attempt < len(RATE_LIMIT_WAITS):
                pause = RATE_LIMIT_WAITS[attempt]
                self.rate_limit_pauses += 1
                print(f"    {err}; pausing {pause} s", flush=True)
                time.sleep(pause)
        raise StopRun(f"Meta API kept failing after {len(RATE_LIMIT_WAITS)} pauses: {err}")

    def ads(self, params: dict, max_pages: int) -> tuple[list[dict], bool]:
        """Ads for one ads_archive query, following paging. Returns (ads, more_left)."""
        data = self.get("ads_archive", params)
        ads = list(data.get("data") or [])
        pages = 1
        while (data.get("paging") or {}).get("next") and pages < max_pages:
            data = self.get_url(data["paging"]["next"])
            ads += data.get("data") or []
            pages += 1
        return ads, bool((data.get("paging") or {}).get("next"))

    def token_info(self) -> dict:
        data = self.get("debug_token", {"input_token": self.token})
        return data.get("data") or {}


def _numbers(obj):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _numbers(v)


# ---------------------------------------------------------------------------------------------
# Matching ads to a store
# ---------------------------------------------------------------------------------------------

def fold(s: str) -> str:
    """'Butik Miła®' -> 'butikmila': lowercase ASCII letters and digits only."""
    s = unicodedata.normalize("NFKD", (s or "").replace("ł", "l").replace("Ł", "L"))
    return re.sub(r"[^a-z0-9]", "", s.encode("ascii", "ignore").decode().lower())


def caption_host(caption: str) -> str | None:
    """'HTTPS://WWW.ButikMila.pl/kolekcja' -> 'butikmila.pl'; None when the caption is not an address."""
    c = (caption or "").strip().lower()
    c = re.sub(r"^[a-z]+://", "", c)
    c = c.split("/", 1)[0].split("?", 1)[0].strip(". ")
    if c.startswith("www."):
        c = c[4:]
    return c if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", c) else None


def store_name(domain: str) -> str:
    """'butik-mila.com.pl' -> 'butik-mila' (registrable label, used as a search term)."""
    parts = domain.split(".")
    second_level = {"com", "net", "org", "edu", "gov", "sklep", "shop", "waw", "co"}
    if len(parts) >= 3 and parts[-2] in second_level:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else domain


def match_ads(ads: list[dict], domains: set[str], name: str) -> list[dict]:
    """Ads that belong to the store: link caption on one of its domains, or Page name = store name."""
    folded_name = fold(name)
    out = []
    for ad in ads:
        hosts = {caption_host(c) for c in ad.get("ad_creative_link_captions") or []} - {None}
        if any(h == d or h.endswith("." + d) for h in hosts for d in domains):
            out.append({**ad, "matched_by": "link_caption"})
        elif folded_name and fold(ad.get("page_name") or "") == folded_name:
            out.append({**ad, "matched_by": "page_name"})
    return out


def queries_for(domain: str) -> list[dict]:
    name = store_name(domain)
    qs = [{"search_terms": domain, "search_type": "KEYWORD_EXACT_PHRASE"}]
    if len(fold(name)) >= MIN_NAME_LEN:
        qs.append({"search_terms": name.replace("-", " "), "search_type": "KEYWORD_UNORDERED"})
    return qs


def library_url(page_id: str | None = None, query: str | None = None) -> str:
    """Public Ad Library page showing the same active ads in Poland (for a person to verify)."""
    p = {"active_status": "active", "ad_type": "all", "country": COUNTRY, "media_type": "all"}
    if page_id:
        p["view_all_page_id"] = page_id
        p["search_type"] = "page"
    else:
        p.update(q=query or "", search_type="keyword_exact_phrase")
    return LIBRARY + "?" + urllib.parse.urlencode(p)


# ---------------------------------------------------------------------------------------------
# One store
# ---------------------------------------------------------------------------------------------

def base_params() -> dict:
    return {"ad_reached_countries": json.dumps([COUNTRY]), "ad_type": "ALL", "ad_active_status": "ACTIVE"}


def check_store(meta: Meta, domain: str, site_url: str | None) -> dict:
    started, calls0 = time.monotonic(), meta.calls
    domains = {domain}
    if site_url:  # the store redirects to another domain (enrich_status redirected)
        host = caption_host(site_url)
        if host:
            domains.add(host)
    name = store_name(domain)
    res = {"domain": domain, "active": False, "active_ads": 0, "pages": [], "matched_by": None,
           "queries": [], "matched_ads": 0, "source_url": library_url(query=domain), "error": None}

    matched: list[dict] = []
    for q in queries_for(domain):
        ads, more = meta.ads({**base_params(), **q, "fields": SEARCH_FIELDS, "limit": SEARCH_LIMIT},
                             SEARCH_PAGES)
        hits = match_ads(ads, domains, name)
        res["queries"].append({**q, "ads_returned": len(ads), "more_results": more, "matched": len(hits)})
        if hits:
            matched = hits
            break
    if not matched:
        return finish(res, meta, calls0, started)

    res["matched_ads"] = len(matched)
    res["matched_by"] = sorted({a["matched_by"] for a in matched})
    page_names = {a["page_id"]: a.get("page_name") for a in matched if a.get("page_id")}
    page_ids = sorted(page_names, key=lambda p: -sum(1 for a in matched if a.get("page_id") == p))[:10]

    ads, more = meta.ads({**base_params(), "search_page_ids": json.dumps([int(p) for p in page_ids]),
                          "fields": COUNT_FIELDS, "limit": COUNT_LIMIT}, MAX_COUNT_PAGES)
    per_page: dict[str, int] = {}
    for a in ads:
        per_page[a.get("page_id")] = per_page.get(a.get("page_id"), 0) + 1
    res["pages"] = [{"page_id": p, "page_name": page_names.get(p), "active_ads": per_page.get(p, 0),
                     "library_url": library_url(page_id=p)} for p in page_ids]
    # The matched ads are active ads shown in Poland, so the store is active even if the count
    # call returned fewer (an ad of an 11th Page, or the archive changing between the two calls).
    res["active_ads"] = max(len(ads), len(matched))
    res["active_ads_lower_bound"] = more  # True when counting stopped at MAX_COUNT_PAGES
    res["active"] = True
    res["source_url"] = res["pages"][0]["library_url"]
    starts = sorted(a["ad_delivery_start_time"] for a in ads if a.get("ad_delivery_start_time"))
    res["oldest_active_ad_start"] = starts[0] if starts else None
    return finish(res, meta, calls0, started)


def finish(res: dict, meta: Meta, calls0: int, started: float) -> dict:
    res["api_calls"] = meta.calls - calls0
    res["seconds"] = round(time.monotonic() - started, 1)
    return res


def signal_row(company_id: int, res: dict, run_id: int | None) -> dict:
    details = {k: res[k] for k in ("active_ads", "pages", "matched_by", "matched_ads", "queries")}
    for k in ("active_ads_lower_bound", "oldest_active_ad_start"):
        if k in res:
            details[k] = res[k]
    details["country"] = COUNTRY
    return {"company_id": company_id, "type": "meta_ads_active", "value": res["active"],
            "source": "meta_ad_library", "source_url": res["source_url"], "details": details, "run_id": run_id}


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


def load_stores(db: Supabase, domains: list[str], redo: bool, limit: int | None) -> list[dict]:
    stores = {r["company_id"] for r in fetch_all(
        db, "/signals_latest?select=company_id&type=eq.is_store&value=eq.true&order=company_id")}
    done = set() if redo or domains else {r["company_id"] for r in fetch_all(
        db, "/signals?select=company_id&type=eq.meta_ads_active&order=id")}
    rows = fetch_all(db, "/companies?select=id,domain,site_url,enrich_status&order=id")
    if domains:
        wanted = set(domains)
        rows = [r for r in rows if r["domain"] in wanted]
    else:
        rows = [r for r in rows if r["id"] in stores and r["id"] not in done]
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------------------------------------

def print_result(i: int, n: int, r: dict) -> None:
    if r["error"]:
        state = f"error: {r['error'][:100]}"
    elif r["active"]:
        pages = ", ".join(f"{p['page_name']} ({p['active_ads']})" for p in r["pages"])
        more = "+" if r.get("active_ads_lower_bound") else ""
        state = f"ACTIVE {r['active_ads']}{more} ads, by {'/'.join(r['matched_by'])}: {pages}"
    else:
        state = "no active ads found"
    print(f"[{i}/{n}] {r['domain']}: {state}  [{r['api_calls']} calls, {r['seconds']} s]", flush=True)


def check_token(meta: Meta) -> None:
    info = meta.token_info()
    exp = info.get("expires_at")
    when = dt.datetime.fromtimestamp(exp) if exp else None
    print(f"App: {info.get('application')} ({info.get('app_id')}), type {info.get('type')}, "
          f"valid: {info.get('is_valid')}")
    if when:
        days = (when - dt.datetime.now()).days
        print(f"Expires: {when:%Y-%m-%d %H:%M} ({days} days left)")
        if days < 7:
            print("Less than a week left: generate a new token and extend it (Access Token Tool).")
    else:
        print("Expires: never (or not reported)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", action="append", default=[], help="only this domain (repeatable)")
    ap.add_argument("--limit", type=int, help="at most this many stores")
    ap.add_argument("--redo", action="store_true", help="also stores already checked")
    ap.add_argument("--dry-run", action="store_true", help="query the API and print, write nothing to Supabase")
    ap.add_argument("--check-token", action="store_true", help="show the token's app and expiry, then stop")
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    a = ap.parse_args()

    env = read_env(a.env)
    token = env.get("META_ACCESS_TOKEN")
    if not token:
        sys.exit("Missing META_ACCESS_TOKEN in .env.")
    meta = Meta(token)
    try:
        check_token(meta)
    except (MetaError, StopRun) as e:
        sys.exit(f"Token check failed: {e}")
    if a.check_token:
        return

    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY")
    db = Supabase(url, key) if url and key else None
    if not db and not (a.dry_run and a.domain):
        sys.exit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env (a dry run with --domain works without).")
    if db:
        companies = load_stores(db, a.domain, a.redo, a.limit)
    else:
        companies = [{"id": None, "domain": d, "site_url": None, "enrich_status": None} for d in a.domain]
    missing = sorted(set(a.domain) - {c["domain"] for c in companies})
    if missing:
        print(f"Not in companies, skipped: {', '.join(missing)}")
    if not companies:
        print("Nothing to check.")
        return

    started = dt.datetime.now(dt.timezone.utc)
    run_id = None
    args = {k: str(v) for k, v in vars(a).items()}
    if db and not a.dry_run:
        run_id = db.request("POST", "/runs", {"step": "meta_ads", "started_at": started.isoformat(),
                                              "items_total": len(companies), "notes": {"args": args}},
                            prefer="return=representation")[0]["id"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / (f"run-{run_id}.jsonl" if run_id else f"dry-run-{started:%Y%m%d-%H%M%S}.jsonl")
    print(f"Checking {len(companies)} stores in the Meta Ad Library ({COUNTRY})"
          f"{' (dry run)' if a.dry_run else f', run {run_id}'} -> {out_path.relative_to(ROOT)}", flush=True)

    results: list[dict] = []
    stopped = None
    try:
        with out_path.open("a", encoding="utf-8") as out:
            for i, c in enumerate(companies, 1):
                site = c["site_url"] if c.get("enrich_status") == "redirected" else None
                try:
                    res = check_store(meta, c["domain"], site)
                except MetaError as e:  # bad request for this store; the rest can go on
                    res = {"domain": c["domain"], "active": None, "error": str(e), "api_calls": 0, "seconds": 0}
                results.append(res)
                out.write(json.dumps(res, ensure_ascii=False) + "\n")
                out.flush()
                print_result(i, len(companies), res)
                if db and not a.dry_run and not res["error"]:
                    db.request("POST", "/signals", [signal_row(c["id"], res, run_id)], prefer="return=minimal")
    except StopRun as e:
        stopped = str(e)
        print(f"\nStopped: {e}\nEverything checked so far is saved; run again to continue.")
    except KeyboardInterrupt:
        stopped = "interrupted"
        print("\nStopped by hand. Everything checked so far is saved; run again to continue.")
    finally:
        ok = [r for r in results if not r["error"]]
        summary = {
            "stores": len(companies), "checked": len(ok), "errors": len(results) - len(ok),
            "unfinished": len(companies) - len(results),
            "active": sum(1 for r in ok if r["active"]),
            "matched_by": {k: sum(1 for r in ok if k in (r.get("matched_by") or []))
                           for k in ("link_caption", "page_name")},
            "matched_on_query": {q: sum(1 for r in ok if r["queries"] and r["queries"][-1]["matched"]
                                        and r["queries"][-1]["search_type"] == q)
                                 for q in ("KEYWORD_EXACT_PHRASE", "KEYWORD_UNORDERED")},
            "api_calls": meta.calls, "rate_limit_pauses": meta.rate_limit_pauses,
            "avg_seconds_per_store": round(sum(r["seconds"] for r in results) / max(1, len(results)), 1),
            "stopped": stopped,
        }
        if run_id:
            db.request("PATCH", f"/runs?id=eq.{run_id}", {
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "items_total": len(companies), "items_ok": len(ok),
                "items_failed": len(companies) - len(ok), "cost_usd": 0,
                "notes": {"args": args, "output": out_path.name, **summary},
            }, prefer="return=minimal")
        print("\nSummary:")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
