#!/usr/bin/env python3
"""Load the collected store domains into Supabase (step 1.3).

Reads both domain lists from step 1.1:
  data/stores-serp.csv   Google results (src/collect_serp.py), source google_serp
  data/stores-lists.csv  store lists (src/collect_lists.py), sources shopify_blog and shoper

and writes:
  companies        one row per domain (the key), only the domain; registry fields stay empty
                   until the enrich and registry steps fill them
  company_sources  one row per domain and list it came from, with the page it was found on and
                   the list's own details (SERP categories and rank, platform hint)
  runs             one row for this run: counts and duration

Safe to run again: existing domains are left untouched (no duplicates, no updated_at change),
source rows are refreshed in place and keep their first found_at.

Usage (from the repo root):
  .venv/bin/python src/ingest.py --dry-run     # read and check both files, send nothing
  .venv/bin/python src/ingest.py               # write to Supabase

Credentials come from .env: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (the service role or secret
key: RLS has no policies, so the public key can neither read nor write).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SERP_CSV = ROOT / "data/stores-serp.csv"
LISTS_CSV = ROOT / "data/stores-lists.csv"
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")  # same check as companies.domain
BATCH = 500        # rows per insert
LOOKUP_BATCH = 100  # domains per id lookup (keeps the URL short)


# ---------------------------------------------------------------------------------------------
# Reading the CSV files
# ---------------------------------------------------------------------------------------------

def normalize(raw: str) -> str:
    """'https://www.Sklep.pl/x' -> 'sklep.pl'. Non-ASCII names are converted to punycode."""
    d = (raw or "").strip().lower()
    if "://" in d:
        d = urlparse(d).hostname or ""
    d = d.split("/", 1)[0].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    try:
        d = d.encode("idna").decode("ascii")
    except UnicodeError:
        pass  # left as is; the pattern check below rejects it
    return d


def read_serp(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "raw_domain": r["domain"],
                "source": r.get("source") or "google_serp",
                # The example URL is a Google redirect link that expires, so it is not kept.
                "source_url": None,
                "details": {
                    "categories": [c for c in (r.get("categories") or "").split(";") if c],
                    "queries_found_in": int(r["queries_found_in"]),
                    "best_rank": int(r["best_rank"]),
                    "first_query": r.get("first_query") or None,
                },
            })
    return rows


def read_lists(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            details = {"platform_hint": r.get("platform_hint") or None}
            if r.get("note"):
                details["note"] = r["note"]
            rows.append({
                "raw_domain": r["domain"],
                "source": r["source"],
                "source_url": r.get("source_url") or None,
                "details": details,
            })
    return rows


def prepare(rows: list[dict]) -> tuple[list[dict], dict[str, list]]:
    """Normalize domains, keep one row per (domain, source), set the rest aside with a reason."""
    kept: dict[tuple[str, str], dict] = {}
    skipped: dict[str, list] = {"empty_domain": [], "invalid_domain": [], "duplicate": []}
    for r in rows:
        dom = normalize(r["raw_domain"])
        if not dom:
            skipped["empty_domain"].append(r["source_url"] or r["source"])
        elif not DOMAIN_RE.match(dom):
            skipped["invalid_domain"].append(r["raw_domain"])
        elif (dom, r["source"]) in kept:
            skipped["duplicate"].append(f"{dom} ({r['source']})")
        else:
            kept[(dom, r["source"])] = {**r, "domain": dom}
    return list(kept.values()), skipped


# ---------------------------------------------------------------------------------------------
# Supabase REST (PostgREST)
# ---------------------------------------------------------------------------------------------

def read_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


class Supabase:
    def __init__(self, url: str, key: str) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):  # legacy JWT keys also go in Authorization; new sb_secret_ keys do not
            self.headers["Authorization"] = f"Bearer {key}"

    def request(self, method: str, path: str, body=None, prefer: str | None = None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode()
        except urllib.error.HTTPError as e:
            sys.exit(f"Supabase {method} {path.split('?')[0]}: HTTP {e.code} {e.read().decode()[:500]}")
        return json.loads(text) if text else None

    def ids_for(self, domains: list[str]) -> dict[str, int]:
        out = {}
        for i in range(0, len(domains), LOOKUP_BATCH):
            chunk = domains[i:i + LOOKUP_BATCH]
            q = urllib.parse.quote(f"in.({','.join(chunk)})", safe="().,")
            for row in self.request("GET", f"/companies?select=id,domain&domain={q}") or []:
                out[row["domain"]] = row["id"]
        return out

    def insert(self, table: str, rows: list[dict], on_conflict: str, resolution: str) -> None:
        for i in range(0, len(rows), BATCH):
            self.request("POST", f"/{table}?on_conflict={on_conflict}", rows[i:i + BATCH],
                         prefer=f"resolution={resolution},return=minimal")


# ---------------------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="read and check both files, send nothing")
    ap.add_argument("--serp", type=Path, default=SERP_CSV)
    ap.add_argument("--lists", type=Path, default=LISTS_CSV)
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    a = ap.parse_args()

    started = dt.datetime.now(dt.timezone.utc)
    raw = read_serp(a.serp) + read_lists(a.lists)
    rows, skipped = prepare(raw)
    domains = sorted({r["domain"] for r in rows})

    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    sources_per_domain: dict[str, int] = {}
    for r in rows:
        sources_per_domain[r["domain"]] = sources_per_domain.get(r["domain"], 0) + 1
    on_several = sum(1 for n in sources_per_domain.values() if n > 1)

    print(f"Rows read: {len(raw)} ({a.serp.name} + {a.lists.name})")
    print("Per source: " + ", ".join(f"{s} {n}" for s, n in sorted(by_source.items())))
    print(f"Unique domains: {len(domains)}, on more than one list: {on_several}")
    for reason, items in skipped.items():
        if items:
            print(f"Skipped, {reason}: {len(items)}")
            for it in items[:10]:
                print(f"  {it}")
    if a.dry_run:
        print("Dry run: nothing sent.")
        return

    env = read_env(a.env)
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env.")
    db = Supabase(url, key)

    run = db.request("POST", "/runs", {"step": "ingest", "started_at": started.isoformat(),
                                       "notes": {"files": [a.serp.name, a.lists.name]}},
                     prefer="return=representation")[0]

    existing = db.ids_for(domains)
    new = [d for d in domains if d not in existing]
    db.insert("companies", [{"domain": d} for d in new], on_conflict="domain", resolution="ignore-duplicates")
    ids = db.ids_for(domains)
    missing = [d for d in domains if d not in ids]

    sources = [{"company_id": ids[r["domain"]], "source": r["source"], "source_url": r["source_url"],
                "details": r["details"]} for r in rows if r["domain"] in ids]
    db.insert("company_sources", sources, on_conflict="company_id,source", resolution="merge-duplicates")

    failed = sum(len(v) for k, v in skipped.items() if k != "duplicate") + len(missing)
    db.request("PATCH", f"/runs?id=eq.{run['id']}", {
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "items_total": len(raw),
        "items_ok": len(domains) - len(missing),
        "items_failed": failed,
        "cost_usd": 0,
        "notes": {"files": [a.serp.name, a.lists.name], "rows_per_source": by_source,
                  "domains": len(domains), "new_companies": len(new), "already_in_db": len(existing),
                  "on_several_lists": on_several, "source_rows_upserted": len(sources),
                  "skipped": {k: len(v) for k, v in skipped.items()}, "missing_after_insert": missing},
    }, prefer="return=minimal")

    print(f"Companies: {len(new)} new, {len(existing)} already in the database.")
    print(f"Source rows written: {len(sources)}.")
    if missing:
        print(f"Not found after insert ({len(missing)}): {', '.join(missing[:10])}")
    print(f"Run logged as runs.id = {run['id']}.")


if __name__ == "__main__":
    main()
