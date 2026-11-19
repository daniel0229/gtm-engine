"""HubSpot sync: the latest research card of each company becomes a HubSpot company record.

1. makes sure the property group "GTM Engine" and its company properties exist (created through the
   CRM properties API; enumeration options follow config/icp.yaml),
2. reads the latest card per company from `research_latest` in Supabase,
3. upserts one HubSpot company per store in batches of 100, keyed by the unique property
   `gtm_company_id` (= companies.id), so a second run updates records instead of duplicating them,
4. logs the run in `runs`.

Nothing is sent to the stores: HubSpot here is the CRM a salesperson would work from.
The token is HUBSPOT_TOKEN in .env (a private app with the crm.objects.companies and
crm.schemas.companies read/write scopes).

Usage (from the repo root):
  .venv/bin/python src/hubspot_sync.py --dry-run
  .venv/bin/python src/hubspot_sync.py --min-score 70
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest import ROOT, Supabase, read_env  # noqa: E402
from research import HOW_TO_RETURN, fetch_all  # noqa: E402

API = "https://api.hubapi.com"
GROUP = {"name": "gtm_engine", "label": "GTM Engine", "displayOrder": 1}
BATCH = 100                    # records per batch upsert (HubSpot maximum)
MIN_INTERVAL = 0.15            # seconds between calls; HubSpot Free allows 100 calls per 10 s
TEXT_MAX = 5000                # characters kept in a textarea property


class HubSpotError(Exception):
    pass


class HubSpot:
    def __init__(self, token: str) -> None:
        self.token, self.last, self.calls = token, 0.0, 0

    def request(self, method: str, path: str, body=None, ok404: bool = False):
        wait = MIN_INTERVAL - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            self.calls += 1
            req = urllib.request.Request(
                API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    self.last = time.monotonic()
                    text = r.read().decode()
                    return json.loads(text) if text else None
            except urllib.error.HTTPError as e:
                self.last = time.monotonic()
                if e.code == 404 and ok404:
                    return None
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise HubSpotError(f"{method} {path}: HTTP {e.code} {e.read().decode()[:600]}")
        raise HubSpotError(f"{method} {path}: gave up after retries")


# ---------------------------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------------------------

def options(values: list[str]) -> list[dict]:
    return [{"label": v, "value": v, "displayOrder": i} for i, v in enumerate(values)]


BOOL = {"type": "bool", "fieldType": "booleancheckbox",
        "options": [{"label": "Yes", "value": "true", "displayOrder": 0},
                    {"label": "No", "value": "false", "displayOrder": 1}]}


def property_specs(icp: dict) -> list[dict]:
    sigs = {s["id"]: s for s in icp["signals"]}
    platforms = list(sigs["platform"]["values"])
    categories = list(sigs["category_fit"]["values"]) + list(icp["target"]["categories"]["exclude"])
    personas = [p["id"] for p in icp.get("personas") or []]
    text = {"type": "string", "fieldType": "text"}
    area = {"type": "string", "fieldType": "textarea"}
    num = {"type": "number", "fieldType": "number"}

    def enum(values, multi=False):
        return {"type": "enumeration", "fieldType": "checkbox" if multi else "select", "options": options(values)}

    specs = [
        ("gtm_company_id", "GTM company id", {**text, "hasUniqueValue": True},
         "companies.id in the GTM Engine database; the key of the sync"),
        ("gtm_fit_score", "Fit score", num, "ICP fit 0-100, computed by code from config/icp.yaml"),
        ("gtm_fit_label", "Fit label", enum(["high", "medium", "low", "disqualified"]), "From the fit score"),
        ("gtm_platform", "Store platform", enum(platforms + ["other"]), "Detected in the page code"),
        ("gtm_category", "Main category", enum(categories), "Main product category"),
        ("gtm_assortment_size", "Product count", num, "Products counted in the store's sitemap"),
        ("gtm_meta_ads_active", "Active Meta ads", BOOL, "Store runs ads in Meta Ad Library (PL)"),
        ("gtm_meta_active_ads", "Active Meta ads (count)", num, "Number of active ads"),
        ("gtm_hiring", "Hiring (e-commerce roles)", BOOL,
         "Open role in e-commerce, marketing, customer service or warehouse on the store's careers page"),
        ("gtm_returns_tool", "Returns tool", text, "Returns tool seen on the site, or own_form"),
        ("gtm_how_to_return", "How to return", enum(sorted(HOW_TO_RETURN), multi=True), "Return channels"),
        ("gtm_return_window_days", "Return window (days)", num, "Days to return, as the store states"),
        ("gtm_free_returns", "Free returns", BOOL, "Store pays for return shipping"),
        ("gtm_withdrawal_function", "Online withdrawal function", BOOL,
         "Online 'withdraw from contract' form or button (EU Directive 2023/2673)"),
        ("gtm_tldr", "Research summary", area, "Two-sentence summary of the research card"),
        ("gtm_problem_hypothesis", "Problem hypothesis", area, "The returns problem this store probably has"),
        ("gtm_persona", "Persona to contact", enum(personas), "Persona id from config/icp.yaml"),
        ("gtm_fit_reasoning", "Fit reasoning", area, "Why the score is what it is"),
        ("gtm_researched_at", "Researched on", {"type": "date", "fieldType": "date"}, "Date of the card"),
        ("gtm_agent_version", "Research agent version", text, "Prompt and script version that wrote the card"),
    ]
    return [{"name": n, "label": label, "groupName": GROUP["name"], "description": desc, **spec}
            for n, label, spec, desc in specs]


def ensure_schema(hs: HubSpot, specs: list[dict], dry_run: bool) -> dict:
    """Create the group and missing properties; add missing enumeration options. Returns what changed."""
    changes = {"group": False, "created": [], "options_added": []}
    if not hs.request("GET", f"/crm/v3/properties/companies/groups/{GROUP['name']}", ok404=True):
        changes["group"] = True
        if not dry_run:
            hs.request("POST", "/crm/v3/properties/companies/groups", GROUP)
    for spec in specs:
        cur = None if changes["group"] else hs.request("GET", f"/crm/v3/properties/companies/{spec['name']}",
                                                       ok404=True)
        if not cur:
            changes["created"].append(spec["name"])
            if not dry_run:
                hs.request("POST", "/crm/v3/properties/companies", spec)
            continue
        if spec["type"] == "enumeration":
            have = {o["value"] for o in cur.get("options") or []}
            missing = [o for o in spec["options"] if o["value"] not in have]
            if missing:
                changes["options_added"].append(spec["name"])
                if not dry_run:
                    hs.request("PATCH", f"/crm/v3/properties/companies/{spec['name']}",
                               {"options": (cur.get("options") or []) + missing})
    return changes


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------

def val(card: dict, *path):
    cur = card
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur.get("value") if isinstance(cur, dict) and "value" in cur else cur


def boolstr(v) -> str | None:
    return None if v is None else ("true" if v else "false")


def clip(s: str | None) -> str | None:
    return s[:TEXT_MAX] if s else s


def company_properties(row: dict, company: dict, icp: dict) -> dict:
    card = row["card"]
    platforms = set((next(s for s in icp["signals"] if s["id"] == "platform"))["values"])
    platform = val(card, "platform")
    hyp = card.get("problem_hypothesis") or {}
    how = val(card, "returns", "how_to_return")
    hiring = (card.get("signals") or {}).get("hiring")
    meta = (card.get("signals") or {}).get("meta_ads_active") or {}
    props = {
        "gtm_company_id": str(company["id"]),
        "name": company["domain"],  # the store, not the registered name: a sole trader's name is a person's name
        "domain": company["domain"],
        "website": company.get("site_url") or f"https://{company['domain']}/",
        "city": company.get("city"),
        "country": "Poland",
        "gtm_fit_score": card["fit"]["score"],
        "gtm_fit_label": card["fit"]["label"],
        "gtm_platform": (platform if platform in platforms else "other") if platform else None,
        "gtm_category": val(card, "what_they_sell", "main_category"),
        "gtm_assortment_size": val(card, "what_they_sell", "assortment_size"),
        "gtm_meta_ads_active": boolstr(meta.get("value")),
        "gtm_meta_active_ads": meta.get("active_ads"),
        "gtm_hiring": boolstr(hiring["value"]) if hiring else None,
        "gtm_returns_tool": val(card, "returns", "returns_tool"),
        "gtm_how_to_return": ";".join(how if isinstance(how, list) else [how]) if how else None,
        "gtm_return_window_days": val(card, "returns", "window_days"),
        "gtm_free_returns": boolstr(val(card, "returns", "free_returns")),
        "gtm_withdrawal_function": boolstr(val(card, "returns", "withdrawal_function")),
        "gtm_tldr": clip(card.get("tldr")),
        "gtm_problem_hypothesis": clip(hyp.get("statement")),
        "gtm_persona": hyp.get("persona"),
        "gtm_fit_reasoning": clip((card.get("fit") or {}).get("reasoning")),
        "gtm_researched_at": (row.get("created_at") or "")[:10] or None,
        "gtm_agent_version": row.get("agent_version"),
    }
    return {k: (str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
            for k, v in props.items() if v is not None}


def load_rows(db: Supabase, min_score: int | None, limit: int | None) -> list[tuple[dict, dict]]:
    research = fetch_all(db, "/research_latest?select=id,company_id,card,score,fit,agent_version,created_at"
                             "&order=company_id")
    if min_score is not None:
        research = [r for r in research if (r["score"] or 0) >= min_score]
    companies = {c["id"]: c for c in fetch_all(db, "/companies?select=id,domain,name,city,site_url&order=id")}
    rows = [(r, companies[r["company_id"]]) for r in research if r["company_id"] in companies]
    return rows[:limit] if limit else rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-score", type=int, help="only companies with at least this fit score")
    ap.add_argument("--limit", type=int, help="at most this many companies")
    ap.add_argument("--dry-run", action="store_true", help="read and print; create nothing in HubSpot or Supabase")
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    a = ap.parse_args()

    env = read_env(a.env)
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "HUBSPOT_TOKEN"):
        if not env.get(k):
            sys.exit(f"Missing {k} in .env.")
    db = Supabase(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    hs = HubSpot(env["HUBSPOT_TOKEN"])
    icp = yaml.safe_load((ROOT / "config/icp.yaml").read_text())

    started = dt.datetime.now(dt.timezone.utc)
    rows = load_rows(db, a.min_score, a.limit)
    print(f"{len(rows)} companies with a research card{' (dry run)' if a.dry_run else ''}")
    changes = ensure_schema(hs, property_specs(icp), a.dry_run)
    print(f"Schema: group {'created' if changes['group'] else 'exists'}, "
          f"{len(changes['created'])} properties to create, options added to {changes['options_added'] or 'none'}")
    inputs = [{"idProperty": "gtm_company_id", "id": str(c["id"]), "properties": company_properties(r, c, icp)}
              for r, c in rows]
    if a.dry_run:
        if inputs:
            print("First record:")
            print(json.dumps(inputs[0], ensure_ascii=False, indent=1))
        return

    run_id = db.request("POST", "/runs", {"step": "hubspot_sync", "started_at": started.isoformat(),
                                          "items_total": len(inputs),
                                          "notes": {"args": {k: str(v) for k, v in vars(a).items() if k != "env"}}},
                        prefer="return=representation")[0]["id"]
    created = updated = failed = 0
    errors = []
    for i in range(0, len(inputs), BATCH):
        chunk = inputs[i:i + BATCH]
        try:
            res = hs.request("POST", "/crm/v3/objects/companies/batch/upsert", {"inputs": chunk})
        except HubSpotError as e:
            failed += len(chunk)
            errors.append(str(e)[:500])
            print(f"Batch {i // BATCH + 1}: failed: {str(e)[:300]}")
            continue
        for r in res.get("results") or []:
            if r.get("new"):
                created += 1
            else:
                updated += 1
        print(f"Batch {i // BATCH + 1}: {len(chunk)} companies")
    summary = {"companies": len(inputs), "created": created, "updated": updated, "failed": failed,
               "schema": changes, "api_calls": hs.calls, "errors": errors[:10]}
    db.request("PATCH", f"/runs?id=eq.{run_id}", {
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(), "items_total": len(inputs),
        "items_ok": created + updated, "items_failed": failed, "cost_usd": 0,
        "notes": {"args": {k: str(v) for k, v in vars(a).items() if k != "env"}, **summary}},
        prefer="return=minimal")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
