"""
WNBA Props — forward performance tracker.

Appends each settled day's graded picks to a running CSV so the sample
builds over time (toward statistical significance) instead of being
re-derived from PDFs every time.

For each gradeable date it stores the union of the two universes we care
about — the top 10 by EV% and every play with EV% > 25 — one row per
pick, tagged with `in_top10` / `in_ev25`, plus the actual stat, result,
and units at the recommended-book odds. Re-running is idempotent: dates
already in the CSV are skipped.

Usage:
    py -3 wnba_props_track.py                # catch up all settled days not yet tracked
    py -3 wnba_props_track.py --date 2026-06-14   # (re)grade one specific day
    py -3 wnba_props_track.py --rebuild      # wipe and rebuild from all PDFs
    py -3 wnba_props_track.py --summary      # just print the cumulative summary
"""

import argparse
import csv
import glob
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import wnba_props_grade as G

try:  # ensure non-ASCII glyphs print on the Windows console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DOCS = Path(os.environ.get("BBALL_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
TRACKER = DOCS / "wnba_props_tracker.csv"
TODAY = datetime.now().strftime("%Y%m%d")
BREAKEVEN = 52.38  # -110 juice break-even win %
INV_MON = {v: k for k, v in G.MON.items()}

FIELDS = ["date", "player", "tm", "game", "prop", "line", "rec", "book",
          "odds", "ev", "rank", "in_top10", "in_ev25",
          "actual", "result", "units", "real_odds"]


def pdf_for(ymd: str) -> Path:
    """2026MMDD -> the day's projections PDF path."""
    return DOCS / f"WNBA_Props_Projections_{INV_MON[ymd[4:6]]}{ymd[6:8]}_2026.pdf"


def gradeable_dates() -> list[str]:
    """All dates with a PDF that are strictly before today, sorted."""
    out = []
    for f in glob.glob(str(DOCS / "WNBA_Props_Projections_*_2026.pdf")):
        ymd = G._fdate(f)
        if ymd < TODAY:
            out.append(ymd)
    return sorted(out)


def tracked_dates() -> set[str]:
    if not TRACKER.exists():
        return set()
    with open(TRACKER, "r", newline="", encoding="utf-8") as f:
        return {row["date"] for row in csv.DictReader(f)}


def grade_date(ymd: str) -> list[dict]:
    """Return one row dict per pick in (top10 ∪ EV>25) for the date."""
    pdf = pdf_for(ymd)
    if not pdf.exists():
        return []
    all_picks = G.parse_top_picks(str(pdf), None)  # full ranked list
    if not all_picks:
        return []
    actuals, all_final = G.fetch_actuals(ymd)
    if not actuals or not all_final:
        # only record fully-settled slates
        if not actuals:
            return []
    union = [p for p in all_picks if p["rank"] <= 10 or p["ev"] > 25.0]
    rows = []
    for p in union:
        g = G.grade_pick(p, actuals)
        odds_used = p["over_odds"] if p["rec"] == "OVER" else p["under_odds"]
        real = odds_used is not None
        rows.append({
            "date": ymd, "player": p["player"], "tm": p["tm"], "game": p["game"],
            "prop": p["prop"], "line": p["line"], "rec": p["rec"],
            "book": p["book"] or "", "odds": odds_used if real else -110,
            "ev": round(p["ev"], 1), "rank": p["rank"],
            "in_top10": p["rank"] <= 10, "in_ev25": p["ev"] > 25.0,
            "actual": "" if g["actual"] is None else g["actual"],
            "result": g["result"], "units": round(g["units"], 4),
            "real_odds": real,
        })
    return rows


def _read_all() -> list[dict]:
    if not TRACKER.exists():
        return []
    with open(TRACKER, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_all(rows: list[dict]) -> None:
    """Atomically rewrite the tracker CSV (temp file + os.replace)."""
    tmp = TRACKER.with_name(TRACKER.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    os.replace(tmp, TRACKER)


def append_rows(rows: list[dict]) -> None:
    """Upsert one (or more) day's picks into the tracker.

    Drops any existing rows for the same date(s) first, then writes — so
    a date can never be double-counted. Atomic (temp file + os.replace),
    so an interrupted or overlapping rerun can't corrupt or duplicate.
    """
    if not rows:
        return
    dates = {r["date"] for r in rows}
    kept = [r for r in _read_all() if r["date"] not in dates]
    _write_all(kept + rows)


def dedupe_tracker() -> int:
    """Safety cleaner: collapse any duplicate pick-rows, keeping the last
    seen per (date, player, prop, rec, line, rank). Returns rows removed."""
    rows = _read_all()
    if not rows:
        return 0
    seen: dict = {}
    for r in rows:
        key = (r["date"], r["player"], r["prop"], r["rec"], r["line"], r["rank"])
        seen[key] = r  # later occurrence wins
    deduped = list(seen.values())
    removed = len(rows) - len(deduped)
    if removed:
        deduped.sort(key=lambda r: (r["date"], int(r["rank"])))
        _write_all(deduped)
    return removed


def _stats(rows: list[dict]):
    """W/L/P/Void, units, win%, ROI, and a z-score vs break-even."""
    w = sum(1 for r in rows if r["result"] == "WIN")
    l = sum(1 for r in rows if r["result"] == "LOSS")
    p = sum(1 for r in rows if r["result"] == "PUSH")
    v = sum(1 for r in rows if r["result"] == "VOID")
    units = sum(float(r["units"]) for r in rows)
    dec = w + l
    bets = w + l + p
    wp = w / dec * 100 if dec else 0.0
    roi = units / bets * 100 if bets else 0.0
    # one-sided z vs break-even on decisions
    if dec:
        mean = dec * BREAKEVEN / 100
        sd = math.sqrt(dec * (BREAKEVEN / 100) * (1 - BREAKEVEN / 100))
        z = (w - mean) / sd if sd else 0.0
    else:
        z = 0.0
    return dict(w=w, l=l, p=p, v=v, units=units, wp=wp, roi=roi, dec=dec, bets=bets, z=z)


# The v2026-07-01 model overhaul went live Jul 1 2026 — picks for dates from then
# on are new-model (older PDFs predate the rec-gate rework / model-version stamp).
NEW_MODEL_SINCE = "20260701"


def _summary_block(rows: list[dict], title: str) -> None:
    """Print one 4-universe table for `rows` under `title` (no footer)."""
    def b(x):  # csv stores booleans as 'True'/'False'
        return str(x) == "True"

    dates = sorted(set(r["date"] for r in rows))
    real_rows = [r for r in rows if b(r["real_odds"])]
    universes = [
        ("Top 10 / day (all tracked)", [r for r in rows if b(r["in_top10"])]),
        ("Top 10 / day (real odds)", [r for r in real_rows if b(r["in_top10"])]),
        ("EV% > 25 (all tracked)", [r for r in rows if b(r["in_ev25"])]),
        ("EV% > 25 (real odds)", [r for r in real_rows if b(r["in_ev25"])]),
    ]
    print("=" * 84)
    print(f"  {title}")
    if dates:
        print(f"  {len(dates)} days  ({dates[0][4:6]}/{dates[0][6:8]} – {dates[-1][4:6]}/{dates[-1][6:8]})"
              f"  |  file: {TRACKER.name}")
    print("=" * 84)
    print(f"  {'Universe':28} {'Bets':>5} {'W-L':>9} {'Win%':>6} {'Units':>8} {'ROI':>7} {'z':>6}")
    print("  " + "-" * 78)
    for label, rs in universes:
        s = _stats(rs)
        sig = "  <- 95%+" if s["z"] >= 1.645 else ""
        print(f"  {label:28} {s['bets']:>5} {s['w']:>3}-{s['l']:<5} {s['wp']:>5.1f}% "
              f"{s['units']:>+8.2f} {s['roi']:>+6.1f}% {s['z']:>+6.2f}{sig}")
    print("  " + "-" * 78)


def print_summary(since: str | None = None) -> None:
    if not TRACKER.exists():
        print("No tracker yet. Run without --summary to seed it.")
        return
    with open(TRACKER, "r", newline="", encoding="utf-8") as f:
        allrows = list(csv.DictReader(f))
    if not allrows:
        print("Tracker is empty.")
        return
    print()
    if since:
        rows = [r for r in allrows if r["date"] >= since]
        if not rows:
            print(f"No tracked picks since {since[:4]}-{since[4:6]}-{since[6:8]}.")
            return
        _summary_block(rows, f"WNBA PROPS — SINCE {since[:4]}-{since[4:6]}-{since[6:8]}")
    else:
        _summary_block(allrows, "WNBA PROPS — CUMULATIVE (ALL MODELS)")
        new_rows = [r for r in allrows if r["date"] >= NEW_MODEL_SINCE]
        if new_rows:
            print()
            _summary_block(new_rows, "WNBA PROPS — NEW MODEL ONLY  (v2026-07-01, Jul 1 2026+)")
    print(f"  Break-even win% at -110 ≈ {BREAKEVEN:.1f}%.  z = std-devs above break-even on decisions;")
    print("  z >= 1.645 ≈ one-sided 95% confidence the edge is real (not just variance).")
    print("=" * 84)


def main():
    ap = argparse.ArgumentParser(description="WNBA props forward tracker")
    ap.add_argument("--date", type=str, default=None, help="Grade one date YYYY-MM-DD")
    ap.add_argument("--rebuild", action="store_true", help="Wipe and rebuild from all PDFs")
    ap.add_argument("--summary", action="store_true", help="Print cumulative summary only")
    ap.add_argument("--new-model", action="store_true",
                    help="Summary for just the new-model era (v2026-07-01, Jul 1 2026+)")
    ap.add_argument("--since", type=str, default=None, help="Summary for dates >= YYYY-MM-DD")
    ap.add_argument("--dedupe", action="store_true", help="Collapse any duplicate rows (safety)")
    a = ap.parse_args()

    if a.dedupe:
        removed = dedupe_tracker()
        print(f"Removed {removed} duplicate row(s).")
        print_summary()
        return

    if a.summary or a.new_model or a.since:
        since = (a.since.replace("-", "") if a.since
                 else NEW_MODEL_SINCE if a.new_model else None)
        print_summary(since=since)
        return

    if a.rebuild and TRACKER.exists():
        TRACKER.unlink()

    if a.date:
        targets = [a.date.replace("-", "")]
    else:
        have = set() if a.rebuild else tracked_dates()
        targets = [d for d in gradeable_dates() if d not in have]

    if not targets:
        print("Tracker already up to date — no new settled slates.")
        print_summary()
        return

    total_new = 0
    for ymd in targets:
        rows = grade_date(ymd)
        if rows:
            append_rows(rows)
            total_new += len(rows)
            w = sum(1 for r in rows if r["result"] == "WIN")
            l = sum(1 for r in rows if r["result"] == "LOSS")
            print(f"  + {ymd[4:6]}/{ymd[6:8]}: {len(rows):>3} picks tracked  ({w}-{l})")
        else:
            print(f"  · {ymd[4:6]}/{ymd[6:8]}: skipped (no data / not settled)")
    print(f"\nAppended {total_new} pick-rows across {len(targets)} date(s).")
    print_summary()


if __name__ == "__main__":
    main()
