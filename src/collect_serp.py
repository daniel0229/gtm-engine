#!/usr/bin/env python3
"""Collect Polish online-store domains from Google results (source 3 in docs/sources.md).

Reads the queries in config/store-search-queries.yaml, fetches the organic Google results for each
one through the DataForSEO SERP API, drops the domains in config/domain-blocklist.yaml and writes
one row per remaining domain to data/stores-serp.csv.

Usage (from the repo root):
  .venv/bin/python src/collect_serp.py --dry-run          # count queries and estimate cost, send nothing
  .venv/bin/python src/collect_serp.py --limit 10         # first 10 queries only
  .venv/bin/python src/collect_serp.py                    # all queries

Raw responses are cached in data/serp/<query-slug>.json, so a query is paid for only once;
--refresh fetches again. Every paid call is logged with its cost in data/serp/cost.json.
Credentials come from .env (DATAFORSEO_USERNAME, DATAFORSEO_PASSWORD).
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "config/store-search-queries.yaml"
BLOCKLIST = ROOT / "config/domain-blocklist.yaml"
CACHE = ROOT / "data/serp"
OUT = ROOT / "data/stores-serp.csv"
API = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
USD_PER_10_RESULTS = 0.002  # observed 2026-11-09; the real cost is read from each response


def read_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    t = t.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def bare(domain: str) -> str:
    d = (domain or "").lower().strip().rstrip(".")
    return d[4:] if d.startswith("www.") else d


def domain_of(item: dict) -> str:
    """DataForSEO often returns google.com/goto?url=... links; the real domain is then in the breadcrumb."""
    dom = bare(item.get("domain"))
    if dom.startswith("google.") or "goto" in (item.get("url") or ""):
        m = re.match(r"https?://([^ ›/]+)", re.sub(r"\s+", " ", item.get("breadcrumb") or "").strip())
        return bare(m.group(1)) if m else ""
    return dom


def blocked(domain: str, blocklist: dict[str, str]) -> str | None:
    """Return the blocklist group a domain falls in (also for its subdomains), or None."""
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        group = blocklist.get(".".join(parts[i:]))
        if group:
            return group
    return None


def fetch(query: str, cfg: dict, auth: str) -> dict:
    body = [{"keyword": query, "location_code": cfg["location_code"], "language_code": cfg["language_code"],
             "device": cfg["device"], "depth": cfg["depth"]}]
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": auth, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="count queries and estimate cost, send nothing")
    ap.add_argument("--limit", type=int, help="use only the first N queries (in file order, across categories)")
    ap.add_argument("--budget", type=float, default=1.00, help="stop before sending if the estimate is higher (USD)")
    ap.add_argument("--refresh", action="store_true", help="fetch again even if a cached response exists")
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    a = ap.parse_args()

    conf = yaml.safe_load(QUERIES.read_text(encoding="utf-8"))
    cfg = conf["search"]
    jobs = [(cat, q) for cat, qs in conf["queries"].items() for q in qs]
    if a.limit:
        # Spread a small sample across categories instead of taking the first category only.
        by_cat: dict[str, list] = {}
        for cat, q in jobs:
            by_cat.setdefault(cat, []).append(q)
        sample, i = [], 0
        while len(sample) < a.limit and any(i < len(v) for v in by_cat.values()):
            sample += [(c, v[i]) for c, v in by_cat.items() if i < len(v)]
            i += 1
        jobs = sample[: a.limit]

    CACHE.mkdir(parents=True, exist_ok=True)
    todo = [(c, q) for c, q in jobs if a.refresh or not (CACHE / f"{slug(q)}.json").exists()]
    estimate = len(todo) * USD_PER_10_RESULTS * (cfg["depth"] / 10)
    print(f"Queries: {len(jobs)} selected, {len(jobs) - len(todo)} already cached, {len(todo)} to fetch.")
    print(f"Estimated cost: {estimate:.3f} USD (budget {a.budget:.2f}).")
    if a.dry_run:
        return
    if estimate > a.budget:
        sys.exit("Estimate is above the budget. Raise --budget only on purpose.")

    if todo:
        env = read_env(a.env)
        auth = "Basic " + base64.b64encode(
            f"{env['DATAFORSEO_USERNAME']}:{env['DATAFORSEO_PASSWORD']}".encode()).decode()

        def run(job):
            cat, q = job
            try:
                resp = fetch(q, cfg, auth)
            except urllib.error.URLError as e:
                return q, None, str(e)
            task = (resp.get("tasks") or [{}])[0]
            if task.get("status_code") != 20000:
                return q, None, f"{task.get('status_code')} {task.get('status_message')}"
            (CACHE / f"{slug(q)}.json").write_text(json.dumps(
                {"query": q, "category": cat, "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                 "response": task}, ensure_ascii=False))
            return q, task.get("cost", 0.0), None

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(run, todo))

        ledger_path = CACHE / "cost.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"calls": [], "total_usd": 0.0}
        spent = round(sum(c for _, c, _ in results if c), 4)
        ledger["calls"].append({"at": dt.datetime.now().isoformat(timespec="seconds"),
                                "queries": sum(1 for _, c, _ in results if c is not None), "cost_usd": spent})
        ledger["total_usd"] = round(ledger["total_usd"] + spent, 4)
        ledger_path.write_text(json.dumps(ledger, indent=1))
        errors = [(q, e) for q, _, e in results if e]
        print(f"Fetched {len(todo) - len(errors)} queries, cost {spent} USD (total so far {ledger['total_usd']} USD).")
        for q, e in errors:
            print(f"  error: {q}: {e}")

    # Build the domain list from every cached response of the selected queries.
    bl_conf = yaml.safe_load(BLOCKLIST.read_text(encoding="utf-8"))
    blocklist = {bare(d): group for group, ds in bl_conf.items() if isinstance(ds, list) for d in ds}
    domains: dict[str, dict] = {}
    dropped: dict[str, int] = {}
    results_seen = 0
    for cat, q in jobs:
        path = CACHE / f"{slug(q)}.json"
        if not path.exists():
            continue
        task = json.loads(path.read_text())["response"]
        items = ((task.get("result") or [{}])[0].get("items")) or []
        for it in items:
            if it.get("type") != "organic":
                continue
            results_seen += 1
            dom = domain_of(it)
            if not dom:
                continue
            group = blocked(dom, blocklist)
            if group:
                dropped[group] = dropped.get(group, 0) + 1
                continue
            row = domains.setdefault(dom, {"domain": dom, "categories": set(), "queries": 0, "best_rank": 99,
                                           "first_query": q, "example_url": it.get("url") or ""})
            row["categories"].add(cat)
            row["queries"] += 1
            if it.get("rank_group", 99) < row["best_rank"]:
                row["best_rank"] = it.get("rank_group", 99)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["domain", "categories", "queries_found_in", "best_rank", "first_query", "example_url", "source"])
        for r in sorted(domains.values(), key=lambda r: (-r["queries"], r["best_rank"], r["domain"])):
            w.writerow([r["domain"], ";".join(sorted(r["categories"])), r["queries"], r["best_rank"],
                        r["first_query"], r["example_url"], "google_serp"])

    print(f"Organic results read: {results_seen}. Dropped by blocklist: "
          + (", ".join(f"{g} {n}" for g, n in sorted(dropped.items())) or "none") + ".")
    print(f"Unique domains kept: {len(domains)} -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
