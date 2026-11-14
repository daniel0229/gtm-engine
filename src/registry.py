#!/usr/bin/env python3
"""Match each store's seller to the Polish company registries (step 1.4a).

For every company whose NIP the enrich step found on the store's pages:
  1. VAT whitelist (wl-api.mf.gov.pl): registered name, address, REGON, KRS number, VAT status.
     Up to 30 NIPs per call; the ministry allows 100 calls a day, so the script counts today's
     calls (from earlier `runs` rows too) and stops before the limit.
  2. KRS (api-krs.ms.gov.pl), only when the whitelist gives a KRS number: legal form, date of entry
     into KRS, management board size and roles. KRS masks board members' names, so no names are
     kept. Sole proprietorships have no KRS entry; their record ends at the whitelist.

Writes:
  companies  registry columns + registry_status (matched / not_found / no_nip_on_site / error),
             registry_raw = what was fetched (bank accounts and people's data left out) + reason
  signals    registered_company (true / false) for every matched company, with the API URL
  runs       one row for this run: counts, API calls, duration, the match report

Domains without a NIP are not touched here: the enrich step marks them no_nip_on_site itself.

At the end it prints the match report for every domain the enrich step has visited: how many were
linked to a registry entry and why the rest dropped out (site not readable, no NIP on the site, NIP
not in the whitelist, VAT not active). That share goes into the README.

Usage (from the repo root):
  .venv/bin/python src/registry.py --nips 7740001454,1234567890   # test: query both APIs, print, write nothing
  .venv/bin/python src/registry.py --dry-run     # show what would be queried, call nothing
  .venv/bin/python src/registry.py               # pending and error companies
  .venv/bin/python src/registry.py --recheck     # every company with a NIP again
  .venv/bin/python src/registry.py --report      # only print the match report

Credentials come from .env: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest import ROOT, Supabase, read_env  # noqa: E402

WL_BASE = "https://wl-api.mf.gov.pl"
KRS_BASE = "https://api-krs.ms.gov.pl"
WL_DAILY_LIMIT = 100       # search calls a day (docs/sources.md)
WL_BATCH = 30              # NIPs per batch call
KRS_MIN_INTERVAL = 1.0     # seconds between KRS calls; no published limit
USER_AGENT = "gtm-engine/0.1 (registry step)"
WARSAW = ZoneInfo("Europe/Warsaw")
PAGE = 1000                # PostgREST rows per page
WRITE_BATCH = 200

# Whitelist fields not kept: bank accounts, and data about people (representatives, partners, PESEL).
WL_DROP = ("accountNumbers", "representatives", "authorizedClerks", "partners", "pesel")
REGISTRY_COLUMNS = ("name", "regon", "krs", "vat_status", "address", "city", "vat_registered_on",
                    "legal_form", "krs_registered_on", "board_size", "board_roles")


# ---------------------------------------------------------------------------------------------
# HTTP with retries
# ---------------------------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int | None, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


def get_json(url: str, tries: int = 4):
    """GET a JSON document. Retries on 429, 5xx and network errors; raises ApiError otherwise."""
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code != 429 and e.code < 500:
                raise ApiError(e.code, body) from None
            err = ApiError(e.code, body)
            wait = int(e.headers.get("Retry-After") or 0) or 5 * 2 ** attempt
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            err = ApiError(None, str(e))
            wait = 5 * 2 ** attempt
        if attempt < tries - 1:
            time.sleep(wait)
    raise err


# ---------------------------------------------------------------------------------------------
# VAT whitelist
# ---------------------------------------------------------------------------------------------

def nip_valid(nip: str) -> bool:
    if not re.fullmatch(r"[0-9]{10}", nip or ""):
        return False
    s = sum(w * int(d) for w, d in zip((6, 5, 7, 2, 3, 4, 5, 6, 7), nip))
    return s % 11 == int(nip[9])  # remainder 10 never matches a digit, so it is rejected too


def wl_url(nips: list[str], day: str) -> str:
    if len(nips) == 1:
        return f"{WL_BASE}/api/search/nip/{nips[0]}?date={day}"
    return f"{WL_BASE}/api/search/nips/{','.join(nips)}?date={day}"


class Whitelist:
    def __init__(self, day: str, calls_left: int) -> None:
        self.day, self.calls_left, self.calls = day, calls_left, 0

    def lookup(self, nips: list[str]) -> dict[str, dict]:
        """NIP -> {"subject": dict | None, "reason": str | None, "error": str | None}.

        A batch the API rejects as a whole (HTTP 400) is split in half until the bad NIP is
        isolated, so one bad number costs a few extra calls instead of the whole batch.
        NIPs left without an answer when the daily budget runs out are not in the result.
        """
        out: dict[str, dict] = {}
        for i in range(0, len(nips), WL_BATCH):
            self._batch(nips[i:i + WL_BATCH], out)
        return out

    def _batch(self, nips: list[str], out: dict) -> None:
        if self.calls_left <= 0:
            return
        self.calls_left -= 1
        self.calls += 1
        try:
            data = get_json(wl_url(nips, self.day))
        except ApiError as e:
            if e.status == 400 and len(nips) > 1:
                mid = len(nips) // 2
                self._batch(nips[:mid], out)
                self._batch(nips[mid:], out)
            elif e.status == 400:
                code = _wl_code(e.body)
                out[nips[0]] = {"subject": None, "reason": f"wl_rejected:{code}", "error": None}
            else:
                for n in nips:
                    out[n] = {"subject": None, "reason": None, "error": str(e)}
            return

        result = data.get("result") or {}
        if "subject" in result:  # single-NIP endpoint
            entries = [{"identifier": nips[0], "subjects": [result["subject"]] if result["subject"] else []}]
        else:
            entries = result.get("entries") or []
        for entry in entries:
            nip = entry.get("identifier")
            if nip not in nips:
                continue
            if entry.get("error"):
                out[nip] = {"subject": None, "reason": f"wl_rejected:{_wl_code(entry['error'])}", "error": None}
                continue
            subjects = entry.get("subjects") or []
            subject = next((s for s in subjects if s.get("nip") == nip), subjects[0] if subjects else None)
            out[nip] = {"subject": subject, "reason": None if subject else "not_in_whitelist", "error": None}
        for n in nips:  # an answer that silently left a NIP out
            out.setdefault(n, {"subject": None, "reason": None, "error": "missing from whitelist response"})


def _wl_code(body) -> str:
    try:
        d = json.loads(body) if isinstance(body, str) else body
        return d.get("code") or "unknown"
    except (ValueError, AttributeError):
        return "unknown"


def city_from(address: str | None) -> str | None:
    """'CHEMIKÓW 7, 09-411 PŁOCK' -> 'Płock'."""
    m = re.search(r"\b\d{2}-\d{3}\s+(.+)$", address or "")
    return m.group(1).strip().title() if m else None


# ---------------------------------------------------------------------------------------------
# KRS
# ---------------------------------------------------------------------------------------------

def krs_url(krs: str, register: str) -> str:
    return f"{KRS_BASE}/api/krs/OdpisAktualny/{krs}?rejestr={register}&format=json"


class Krs:
    def __init__(self) -> None:
        self.calls, self._last = 0, 0.0

    def _get(self, url: str):
        wait = KRS_MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self.calls += 1
        try:
            return get_json(url)
        finally:
            self._last = time.monotonic()

    def extract(self, krs: str) -> dict:
        """{"data": slim extract, "url": ...} or {"error": ...} or {"reason": "krs_not_found"}."""
        for register in ("P", "S"):  # P = businesses, S = associations and foundations
            url = krs_url(krs, register)
            try:
                odpis = self._get(url)["odpis"]
            except ApiError as e:
                if e.status == 404:
                    continue
                return {"error": str(e), "url": url}
            except (KeyError, TypeError):
                return {"error": "unexpected KRS response", "url": url}
            return {"data": slim_krs(odpis), "url": url}
        return {"reason": "krs_not_found", "url": krs_url(krs, "P")}


def slim_krs(odpis: dict) -> dict:
    head = odpis.get("naglowekA") or {}
    dane = odpis.get("dane") or {}
    d1, d2 = dane.get("dzial1") or {}, dane.get("dzial2") or {}
    rep = d2.get("reprezentacja") or {}
    members = [m for m in rep.get("sklad") or [] if not m.get("czyZawieszona")]
    return {
        "rejestr": head.get("rejestr"),
        "stanZDnia": head.get("stanZDnia"),
        "dataRejestracjiWKRS": head.get("dataRejestracjiWKRS"),
        "formaPrawna": (d1.get("danePodmiotu") or {}).get("formaPrawna"),
        "siedzibaIAdres": d1.get("siedzibaIAdres"),
        "organ": rep.get("nazwaOrganu"),
        "funkcje": [m.get("funkcjaWOrganie") for m in members],  # roles only, never names
        "has_board": bool(rep),
    }


def pl_date(value: str | None) -> str | None:
    """'19.07.2001' -> '2001-07-19'."""
    try:
        return dt.datetime.strptime(value, "%d.%m.%Y").date().isoformat() if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# Turning API answers into a companies row
# ---------------------------------------------------------------------------------------------

def registry_fields(wl: dict | None, krs: dict | None, day: str, nip: str) -> dict:
    """Registry columns + status + raw for one NIP."""
    row = {c: None for c in REGISTRY_COLUMNS}
    raw: dict = {"checked_on": day}
    if wl is None:  # daily budget ran out before this NIP; caller skips it
        raise ValueError("no whitelist answer")
    if wl["error"]:
        raw.update(reason="api_error", error=wl["error"])
        return {**row, "registry_status": "error", "registry_raw": raw}
    s = wl["subject"]
    if not s:
        raw.update(reason=wl["reason"], vat_source_url=wl_url([nip], day))
        return {**row, "registry_status": "not_found", "registry_raw": raw}

    address = s.get("workingAddress") or s.get("residenceAddress")
    row.update(name=s.get("name"), regon=s.get("regon") or None, krs=s.get("krs") or None,
               vat_status=s.get("statusVat"), address=address, city=city_from(address),
               vat_registered_on=s.get("registrationLegalDate"))
    raw.update(vat={k: v for k, v in s.items() if k not in WL_DROP}, vat_source_url=wl_url([nip], day))
    if s.get("statusVat") == "Zwolniony":
        raw["reason"] = "vat_exempt"    # trading, below the VAT threshold; not the vat_inactive disqualifier
    elif s.get("statusVat") != "Czynny":
        raw["reason"] = "vat_inactive"  # Niezarejestrowany: not registered or removed
    if krs:
        raw["krs_source_url"] = krs.get("url")
        if "data" in krs:
            k = krs["data"]
            row.update(legal_form=k["formaPrawna"], krs_registered_on=pl_date(k["dataRejestracjiWKRS"]),
                       board_size=len(k["funkcje"]) if k["has_board"] else None,
                       board_roles=k["funkcje"] if k["has_board"] else None)
            raw["krs"] = k
        elif "error" in krs:
            raw["krs_error"] = krs["error"]  # matched anyway; the next run retries KRS only
        else:
            raw["krs_missing"] = krs["reason"]
    return {**row, "registry_status": "matched", "registry_raw": raw}


# ---------------------------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------------------------

def fetch_all(db: Supabase, path: str) -> list[dict]:
    rows, offset = [], 0
    sep = "&" if "?" in path else "?"
    while True:
        page = db.request("GET", f"{path}{sep}limit={PAGE}&offset={offset}") or []
        rows += page
        if len(page) < PAGE:
            return rows
        offset += PAGE


def wl_calls_today(db: Supabase, today_start: dt.datetime) -> int:
    since = today_start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = db.request("GET", f"/runs?select=notes&step=eq.registry&started_at=gte.{since}") or []
    return sum(int((r.get("notes") or {}).get("vat_calls") or 0) for r in runs)


def outcome(r: dict) -> str:
    """One bucket per enriched domain, for the match report."""
    st = r["registry_status"]
    if st == "matched":
        return {"Czynny": "matched_vat_active", "Zwolniony": "matched_vat_exempt"}.get(
            r["vat_status"], "matched_vat_inactive")
    if st == "not_found":
        return f"not_found:{r.get('reason') or 'unknown'}"
    if st in ("error", "no_nip_on_site"):
        return st
    if r["enrich_status"] not in ("ok", "redirected"):
        return f"site_unreadable:{r['enrich_status']}"
    return "nip_waiting_for_registry" if r["nip"] else "pending"


def match_report(db: Supabase) -> dict:
    """Shares of the domains the enrich step has visited (enriched_at set)."""
    rows = fetch_all(db, "/companies?select=nip,enrich_status,enriched_at,registry_status,vat_status,krs,"
                         "legal_form,registry_raw->>reason&order=id")
    visited = [r for r in rows if r["enriched_at"]]
    buckets: dict[str, int] = {}
    for r in visited:
        b = outcome(r)
        buckets[b] = buckets.get(b, 0) + 1
    matched = [r for r in visited if r["registry_status"] == "matched"]
    n = len(visited)
    share = (lambda k: round(k / n, 3) if n else None)
    return {
        "domains": len(rows),
        "enriched": n,
        "buckets": dict(sorted(buckets.items())),
        "matched": len(matched),
        "matched_share": share(len(matched)),
        "matched_vat_active_share": share(buckets.get("matched_vat_active", 0)),
        "matched_with_krs": sum(1 for r in matched if r["krs"]),
        "matched_krs_extract": sum(1 for r in matched if r["legal_form"]),
        "matched_vat_inactive_statuses": sorted({r["vat_status"] or "none" for r in matched
                                                 if r["vat_status"] != "Czynny"}),
    }


def print_report(rep: dict) -> None:
    n, b = rep["enriched"], rep["buckets"]

    def line(label: str, k: int, indent: int = 2) -> None:
        print(f"{' ' * indent}{label:<{40 - indent}}{k:5}  {k / n:6.1%}")

    print(f"\nMatch report: {n} of {rep['domains']} domains visited by enrich")
    if not n:
        return
    line("Linked to a registry entry", rep["matched"])
    line("VAT active", b.get("matched_vat_active", 0), 4)
    line("VAT exempt (Zwolniony)", b.get("matched_vat_exempt", 0), 4)
    line("VAT not active (Niezarejestrowany)", b.get("matched_vat_inactive", 0), 4)
    print(f"    with a KRS number:                  {rep['matched_with_krs']:5}  (KRS extract read: {rep['matched_krs_extract']})")
    print(f"    no KRS (sole trader, s.c.):         {rep['matched'] - rep['matched_with_krs']:5}")
    print("  Dropped out:")
    unread = {k: v for k, v in b.items() if k.startswith("site_unreadable:")}
    line("store site not readable", sum(unread.values()), 4)
    for k, v in unread.items():
        line(k.split(":", 1)[1], v, 6)
    line("no NIP on the store's site", b.get("no_nip_on_site", 0), 4)
    nf = {k: v for k, v in b.items() if k.startswith("not_found:")}
    line("NIP not in the whitelist", sum(nf.values()), 4)
    for k, v in nf.items():
        line(k.split(":", 1)[1], v, 6)
    line("API error (retry later)", b.get("error", 0), 4)
    waiting = b.get("nip_waiting_for_registry", 0) + b.get("pending", 0)
    if waiting:
        line("Not checked yet", waiting)


# ---------------------------------------------------------------------------------------------

def test_nips(nips: list[str], day: str) -> None:
    """--nips: query both APIs for the given NIPs and print what would be written."""
    bad = [n for n in nips if not nip_valid(n)]
    wl = Whitelist(day, calls_left=len(nips))
    answers = wl.lookup([n for n in nips if n not in bad])
    krs = Krs()
    for nip in nips:
        if nip in bad:
            fields = {"registry_status": "not_found", "registry_raw": {"reason": "invalid_nip_checksum"}}
        else:
            a = answers.get(nip)
            k = a["subject"].get("krs") if a and a["subject"] else None
            fields = registry_fields(a, krs.extract(k) if k else None, day, nip)
        print(f"\n== {nip}")
        print(json.dumps(fields, ensure_ascii=False, indent=2))
    print(f"\nCalls: whitelist {wl.calls}, KRS {krs.calls}. Nothing written.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show what would be queried, call no API, write nothing")
    ap.add_argument("--recheck", action="store_true", help="query every company with a NIP, also matched and not_found")
    ap.add_argument("--limit", type=int, help="at most this many NIPs in this run")
    ap.add_argument("--max-wl-calls", type=int, default=WL_DAILY_LIMIT - 5,
                    help="whitelist calls allowed today, counting earlier runs (default: %(default)s of 100)")
    ap.add_argument("--nips", help="comma-separated NIPs to test against both APIs; no database")
    ap.add_argument("--report", action="store_true", help="only print the match report")
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    a = ap.parse_args()

    now = dt.datetime.now(WARSAW)
    day = now.date().isoformat()
    if a.nips:
        test_nips([n.strip() for n in a.nips.split(",") if n.strip()], day)
        return

    env = read_env(a.env)
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env.")
    db = Supabase(url, key)
    if a.report:
        print_report(match_report(db))
        return

    started = dt.datetime.now(dt.timezone.utc)
    with_nip = fetch_all(db, "/companies?select=id,domain,nip,registry_status,krs,legal_form,"
                             "registry_raw&nip=not.is.null&order=id")
    if a.recheck:
        to_check = with_nip
    else:
        to_check = [c for c in with_nip if c["registry_status"] in ("pending", "error", "no_nip_on_site")]
    # Matched earlier, but the KRS call failed: retry KRS only, no whitelist call.
    checked_ids = {c["id"] for c in to_check}
    krs_retry = [c for c in with_nip if c["id"] not in checked_ids and c["registry_status"] == "matched"
                 and c["krs"] and not c["legal_form"] and (c["registry_raw"] or {}).get("krs_error")]

    by_nip: dict[str, list[dict]] = {}
    for c in to_check:
        by_nip.setdefault(c["nip"], []).append(c)
    nips = list(by_nip)
    if a.limit is not None:
        nips = nips[:a.limit]
    invalid = [n for n in nips if not nip_valid(n)]
    valid = [n for n in nips if n not in invalid]

    used_today = wl_calls_today(db, now.replace(hour=0, minute=0, second=0, microsecond=0))
    budget = max(0, a.max_wl_calls - used_today)

    print(f"Companies with a NIP: {len(with_nip)}; to check: {len(to_check)} ({len(by_nip)} unique NIPs)"
          + (f", this run: {len(nips)}" if a.limit is not None else ""))
    print(f"Invalid NIP checksum (no call): {len(invalid)}; KRS-only retries: {len(krs_retry)}")
    print(f"Whitelist calls: {used_today} used today, {budget} left in this run's budget; "
          f"needed ≈ {-(-len(valid) // WL_BATCH)}")
    if a.dry_run:
        print("Dry run: no API called, nothing written.")
        return

    run = db.request("POST", "/runs", {"step": "registry", "started_at": started.isoformat(),
                                       "notes": {"recheck": a.recheck, "limit": a.limit}},
                     prefer="return=representation")[0]

    wl = Whitelist(day, budget)
    answers = wl.lookup(valid)
    krs = Krs()
    krs_cache: dict[str, dict] = {}

    def krs_for(number: str) -> dict:
        if number not in krs_cache:
            krs_cache[number] = krs.extract(number)
        return krs_cache[number]

    checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
    updates: list[dict] = []
    signals: list[dict] = []
    skipped_budget = 0

    def add(company: dict, fields: dict) -> None:
        updates.append({"domain": company["domain"], **fields, "registry_checked_at": checked_at})
        if fields["registry_status"] != "matched":
            return
        raw = fields["registry_raw"]
        from_krs = "krs" in raw  # KRS extract read; otherwise the whitelist's KRS field decides
        signals.append({
            "company_id": company["id"], "type": "registered_company",
            "value": bool(fields["krs"]),
            "source": "krs" if from_krs else "vat_whitelist",
            "source_url": raw["krs_source_url"] if from_krs else raw["vat_source_url"],
            "run_id": run["id"],
        })

    for nip in nips:
        if nip in invalid:
            fields = {**{c: None for c in REGISTRY_COLUMNS}, "registry_status": "not_found",
                      "registry_raw": {"checked_on": day, "reason": "invalid_nip_checksum"}}
        elif nip not in answers:
            skipped_budget += len(by_nip[nip])
            continue
        else:
            ans = answers[nip]
            number = ans["subject"].get("krs") if ans["subject"] else None
            fields = registry_fields(ans, krs_for(number) if number else None, day, nip)
        for company in by_nip[nip]:
            add(company, fields)

    for c in krs_retry:
        res = krs_for(c["krs"])
        raw = {k: v for k, v in (c["registry_raw"] or {}).items() if k not in ("krs_error", "krs_missing")}
        raw["krs_source_url"] = res.get("url")
        row = {"domain": c["domain"], "registry_raw": raw}
        if "data" in res:
            k = res["data"]
            raw["krs"] = k
            row.update(legal_form=k["formaPrawna"], krs_registered_on=pl_date(k["dataRejestracjiWKRS"]),
                       board_size=len(k["funkcje"]) if k["has_board"] else None,
                       board_roles=k["funkcje"] if k["has_board"] else None)
        elif "error" in res:
            raw["krs_error"] = res["error"]
        else:
            raw["krs_missing"] = res["reason"]
        db.request("PATCH", f"/companies?id=eq.{c['id']}", {k: v for k, v in row.items() if k != "domain"},
                   prefer="return=minimal")

    # Upsert on the domain key: every row carries the same columns, existing rows only (no inserts).
    for i in range(0, len(updates), WRITE_BATCH):
        db.request("POST", "/companies?on_conflict=domain", updates[i:i + WRITE_BATCH],
                   prefer="resolution=merge-duplicates,return=minimal")
    for i in range(0, len(signals), WRITE_BATCH):
        db.request("POST", "/signals", signals[i:i + WRITE_BATCH], prefer="return=minimal")

    statuses: dict[str, int] = {}
    for u in updates:
        statuses[u["registry_status"]] = statuses.get(u["registry_status"], 0) + 1
    report = match_report(db)
    db.request("PATCH", f"/runs?id=eq.{run['id']}", {
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "items_total": len(updates) + skipped_budget,
        "items_ok": statuses.get("matched", 0) + statuses.get("not_found", 0),
        "items_failed": statuses.get("error", 0) + skipped_budget,
        "cost_usd": 0,
        "notes": {"recheck": a.recheck, "limit": a.limit, "vat_calls": wl.calls, "krs_calls": krs.calls,
                  "unique_nips": len(nips), "invalid_nip_checksum": len(invalid),
                  "companies_written": statuses,
                  "krs_only_retries": len(krs_retry), "skipped_daily_budget": skipped_budget,
                  "signals_written": len(signals), "report": report},
    }, prefer="return=minimal")

    print(f"\nWritten: {', '.join(f'{k} {v}' for k, v in sorted(statuses.items())) or 'nothing'}; "
          f"signals {len(signals)}")
    print(f"Calls: whitelist {wl.calls}, KRS {krs.calls}")
    if skipped_budget:
        print(f"Daily whitelist budget used up: {skipped_budget} companies left for tomorrow's run.")
    print_report(report)
    print(f"\nRun logged as runs.id = {run['id']}.")


if __name__ == "__main__":
    main()
