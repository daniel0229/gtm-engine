"""Evaluation of the research agent (steps 2.4-2.6).

`sheet` writes a review workbook for one research run: one row per fact the agent wrote (with its
quote and a link that opens the source page at the quote), and one row per store for the reviewer's
judgement of the observations, the hypothesis and the minutes the check took. The workbook names real
stores, so it goes to data/evals/ (gitignored).

`score` reads the filled workbook and prints the evaluation in Markdown with aggregate numbers only:
accuracy of agent facts, false (invented or wrong) facts, share of specific observations, sensible
hypotheses, minutes to check a card, and the facts the quote check dropped before saving. Error
examples are anonymized as Store 1, Store 2, ...

Usage (from the repo root):
  .venv/bin/python src/eval_research.py sheet --run 19
  .venv/bin/python src/eval_research.py score data/evals/research-run-19.xlsx > evals/research-eval-run-19.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote as urlquote

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest import ROOT, Supabase, read_env  # noqa: E402
from research import fetch_all, quote_parts  # noqa: E402

OUT_DIR = ROOT / "data/evals"
VERDICTS = ("prawda", "fałsz", "nie da się sprawdzić")
YES_NO = ("tak", "nie")
HEAD_FILL = PatternFill("solid", fgColor="DDE4EE")
INPUT_FILL = PatternFill("solid", fgColor="FFF6D5")

FACT_HEADERS = ["#", "Sklep", "Pole", "Wartość od agenta", "Cytat", "Źródło", "Dowód", "Ocena", "Konkretna?",
                "Uwagi"]
CARD_HEADERS = ["Sklep", "Wynik", "TL;DR", "Hipoteza problemu", "Uzasadnienie wyniku", "Hipoteza sensowna?",
                "Minuty na sprawdzenie", "Uwagi"]


def text_fragment_link(url: str, quote) -> str:
    """Link that scrolls to and highlights the quote in Chrome, Edge and Safari (#:~:text=)."""
    parts = quote_parts(quote)
    if not parts:
        return url
    words = parts[0].split()
    enc = lambda s: urlquote(s, safe="")  # noqa: E731  ('-' and ',' must be encoded too)
    frag = enc(" ".join(words)) if len(words) <= 8 else f"{enc(' '.join(words[:4]))},{enc(' '.join(words[-4:]))}"
    return f"{url.split('#')[0]}#:~:text={frag}"


def show(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def agent_facts(card: dict) -> list[tuple[str, dict]]:
    """(path, item) for every item the agent wrote with a source: facts, hiring, observations, checks,
    disqualifier findings. Pipeline facts were checked in phase 1 and are not repeated here."""
    out = []
    for sec in ("what_they_sell", "returns", "signals"):
        for k, f in (card.get(sec) or {}).items():
            if isinstance(f, dict) and f.get("origin") == "agent":
                out.append((f"{sec}.{k}", f))
    for o in card.get("observations") or []:
        out.append((f"observations.{o['id']}", o))
    for i, c in enumerate(card.get("checks") or [], 1):
        out.append((f"checks.{i}", c))
    for d in (card.get("fit") or {}).get("disqualifiers") or []:
        if d.get("origin") == "agent":
            out.append((f"disqualifier.{d['id']}", d))
    return out


def fact_value(path: str, item: dict) -> str:
    if path.startswith("observations."):
        return item["text"]
    if path.startswith("checks."):
        return f"{item['signal']}: pipeline {show(item['pipeline_value'])} -> agent {show(item['agent_value'])}"
    if path.startswith("disqualifier."):
        return item.get("note") or item["id"]
    if path == "signals.hiring":
        roles = ", ".join(f"{r['title']} ({r['area']})" for r in item.get("roles") or [])
        return show(item["value"]) + (f"; {roles}" if roles else "")
    return show(item["value"])


def style_header(ws, headers: list[str], widths: list[int]) -> None:
    ws.append(headers)
    for i, w in enumerate(widths, 1):
        cell = ws.cell(row=1, column=i)
        cell.font, cell.fill = Font(bold=True), HEAD_FILL
        ws.column_dimensions[cell.column_letter].width = w
    ws.freeze_panes = "A2"


def make_sheet(db: Supabase, run_id: int, out: Path) -> None:
    rows = fetch_all(db, f"/research?select=id,company_id,card,score,fit&run_id=eq.{run_id}&order=id")
    if not rows:
        sys.exit(f"No research rows for run {run_id}.")
    wb = Workbook()
    info = wb.active
    info.title = "Instrukcja"
    for line in [
        f"Ewaluacja agenta researchu, run {run_id}: {len(rows)} kart.",
        "",
        "Arkusz Fakty: jeden wiersz na fakt agenta. Kliknij link w kolumnie Źródło: strona otworzy się na cytacie (Chrome).",
        "Ocena: prawda = strona mówi to, co wartość od agenta; fałsz = wartość jest zła albo cytat nie znaczy tego, co agent napisał;",
        "nie da się sprawdzić = strona nie działa albo fakt jest tylko w kodzie strony (Dowód = code) i nie chcesz go szukać.",
        "Konkretna? tylko dla obserwacji (observations.*): tak = handlowiec mógłby tego użyć w pierwszym zdaniu wiadomości do tego sklepu.",
        "",
        "Arkusz Karty: jeden wiersz na sklep. Hipoteza sensowna? = czy wynika z faktów w karcie.",
        "Minuty na sprawdzenie: włącz stoper, gdy zaczynasz sklep w arkuszu Fakty, zatrzymaj po ostatnim fakcie (krok 2.6).",
        "Ręczny research w fazie 0 trwał średnio 4,7 min na sklep.",
    ]:
        info.append([line])
    info.column_dimensions["A"].width = 140

    facts = wb.create_sheet("Fakty")
    style_header(facts, FACT_HEADERS, [5, 22, 26, 45, 55, 12, 8, 20, 11, 30])
    cards = wb.create_sheet("Karty")
    style_header(cards, CARD_HEADERS, [22, 8, 60, 60, 60, 12, 12, 30])
    dv = DataValidation(type="list", formula1=f'"{",".join(VERDICTS)}"', allow_blank=True)
    yn = DataValidation(type="list", formula1=f'"{",".join(YES_NO)}"', allow_blank=True)
    yn2 = DataValidation(type="list", formula1=f'"{",".join(YES_NO)}"', allow_blank=True)
    facts.add_data_validation(dv)
    facts.add_data_validation(yn)
    cards.add_data_validation(yn2)

    n = 0
    for r in rows:
        card = r["card"]
        domain = card["domain"]
        for path, item in agent_facts(card):
            n += 1
            q = item.get("quote")
            facts.append([n, domain, path, fact_value(path, item), "\n".join(q) if isinstance(q, list) else q,
                          "otwórz", item.get("evidence") or "text", None, None, None])
            row = facts.max_row
            link = facts.cell(row=row, column=6)
            link.hyperlink = text_fragment_link(item["source_url"], q) if item.get("evidence") != "code" \
                else item["source_url"]
            link.font = Font(color="1F5FBF", underline="single")
            for col in (4, 5):
                facts.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
            facts.cell(row=row, column=8).fill = INPUT_FILL
            dv.add(facts.cell(row=row, column=8))
            if path.startswith("observations."):
                facts.cell(row=row, column=9).fill = INPUT_FILL
                yn.add(facts.cell(row=row, column=9))
        hyp = card.get("problem_hypothesis") or {}
        cards.append([domain, f"{r['score']} {r['fit']}", card.get("tldr"),
                      f"{hyp.get('statement')}\n[{hyp.get('persona')}: {hyp.get('capability')}; {hyp.get('confidence')}]",
                      (card.get("fit") or {}).get("reasoning"), None, None, None])
        row = cards.max_row
        for col in (3, 4, 5):
            cards.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
        for col in (6, 7):
            cards.cell(row=row, column=col).fill = INPUT_FILL
        yn2.add(cards.cell(row=row, column=6))
    facts.auto_filter.ref = facts.dimensions
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"{out.relative_to(ROOT)}: {len(rows)} cards, {n} agent facts to check")


# ---------------------------------------------------------------------------------------------

def number(v) -> float | None:
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else None
    except ValueError:
        return None


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def score_sheet(path: Path, db: Supabase | None) -> None:
    wb = load_workbook(path)
    facts = list(wb["Fakty"].iter_rows(min_row=2, values_only=True))
    cards = list(wb["Karty"].iter_rows(min_row=2, values_only=True))
    run_id = None
    first = wb["Instrukcja"]["A1"].value or ""
    if "run " in first:
        run_id = first.split("run ")[1].split(":")[0]

    anon: dict[str, str] = {}
    for c in cards:
        anon.setdefault(c[0], f"Store {len(anon) + 1}")

    verdict = Counter()
    by_field: dict[str, Counter] = defaultdict(Counter)
    false_examples = []
    obs_specific = Counter()
    unrated = 0
    for row in facts:
        _, domain, field, value, quote, _, evidence, v, specific, note = row[:10]
        group = field.split(".")[0] if field.startswith(("observations", "checks", "disqualifier")) else field
        if v not in VERDICTS:
            unrated += 1
            continue
        verdict[v] += 1
        by_field[group][v] += 1
        if v == "fałsz":
            false_examples.append((anon.get(domain, "Store ?"), field, value, note))
        if field.startswith("observations.") and specific in YES_NO:
            obs_specific[specific] += 1

    checked = verdict["prawda"] + verdict["fałsz"]
    hyp = Counter(c[5] for c in cards if c[5] in YES_NO)
    minutes = [m for m in (number(c[6]) for c in cards) if m is not None]

    notes = {}
    if db and run_id:
        run = db.request("GET", f"/runs?select=notes,items_ok,cost_usd,input_tokens,output_tokens&id=eq.{run_id}")
        notes = run[0] if run else {}

    print(f"# Research agent evaluation, run {run_id}\n")
    print(f"{len(cards)} cards, {len(facts)} agent facts, {unrated} not rated.\n")
    print("| Measure | Result |\n|---|---|")
    print(f"| Agent facts true (of checkable) | {verdict['prawda']} of {checked} ({pct(verdict['prawda'], checked)}) |")
    print(f"| Agent facts false | {verdict['fałsz']} ({pct(verdict['fałsz'], checked)}) |")
    print(f"| Could not check | {verdict['nie da się sprawdzić']} |")
    print(f"| Specific observations | {obs_specific['tak']} of {sum(obs_specific.values())} "
          f"({pct(obs_specific['tak'], sum(obs_specific.values()))}) |")
    print(f"| Sensible problem hypothesis | {hyp['tak']} of {sum(hyp.values())} ({pct(hyp['tak'], sum(hyp.values()))}) |")
    if minutes:
        print(f"| Minutes to check one card (median, mean) | {statistics.median(minutes):.1f}, "
              f"{statistics.mean(minutes):.1f} (manual research: 4.7) |")
    n = notes.get("notes") or {}
    if n:
        print(f"| Facts dropped by the quote check before saving | {n.get('facts_dropped')} of "
              f"{n.get('agent_facts_proposed')} proposed {json.dumps(n.get('dropped_by_reason'))} |")
        print(f"| Time and cost per card | {n.get('avg_seconds')} s, "
              f"{float(notes.get('cost_usd') or 0) / max(notes.get('items_ok') or 1, 1):.3f} USD API-price equivalent |")
    print("\n## By field\n\n| Field | True | False | Could not check |\n|---|---|---|---|")
    for field in sorted(by_field):
        c = by_field[field]
        print(f"| `{field}` | {c['prawda']} | {c['fałsz']} | {c['nie da się sprawdzić']} |")
    if false_examples:
        print("\n## False facts\n\n| Store | Field | Agent value | Reviewer note |\n|---|---|---|---|")
        for store, field, value, note in false_examples:
            print(f"| {store} | `{field}` | {str(value)[:120]} | {note or ''} |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet", help="write the review workbook for a research run")
    s.add_argument("--run", type=int, required=True)
    s.add_argument("--out", type=Path)
    c = sub.add_parser("score", help="read a filled workbook, print the evaluation")
    c.add_argument("workbook", type=Path)
    a = ap.parse_args()
    env = read_env(ROOT / ".env")
    db = Supabase(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"]) \
        if env.get("SUPABASE_URL") and env.get("SUPABASE_SERVICE_ROLE_KEY") else None
    if a.cmd == "sheet":
        if not db:
            sys.exit("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env.")
        make_sheet(db, a.run, a.out or OUT_DIR / f"research-run-{a.run}.xlsx")
    else:
        score_sheet(a.workbook, db)


if __name__ == "__main__":
    main()
