"""
WNBA Props Projections — Win/Loss back-test.

Grades the TOP 10 picks (by EV%) from each day's
WNBA_Props_Projections_<MonDD>_2026.pdf against actual ESPN box scores.

- Picks + lines + recommended side are parsed from each PDF's
  "Top Props by EV%" ranked table (top 10 rows).
- Real over/under odds are read from the PDF's "All-Book Lines"
  (line-shopping) section at the recommended book (available May 29+).
  Days before that fall back to -110 (flagged with *).
- Actual points / rebounds come from the ESPN WNBA summary boxscore.

Output: per-day W-L-Push + net units, and a season total.
"""

import fitz
import os
import glob
import json
import re
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

DOCS = Path(os.environ.get("BBALL_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
TODAY = datetime.now().strftime("%Y%m%d")
PROP_SET = {"Points", "Rebounds"}
REC_SET = {"OVER", "UNDER"}
ODDS_RE = re.compile(r"^-?\d{3,4}$")
EV_RE = re.compile(r"^[+-]?\d+\.\d+%?$")
NUM_RE = re.compile(r"^\d+(\.\d+)?$")
BOOK_RE = re.compile(r"^[A-Z]{2,4}$")
MON = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05",
       "Jun": "06", "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10",
       "Nov": "11", "Dec": "12"}

ESPN_SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
ESPN_SUM = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"


def norm(name: str) -> str:
    """Normalize a player name: strip accents, lowercase, drop punctuation."""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower().replace(".", "").replace("'", "").replace("-", " ")
    n = re.sub(r"\*\d+", "", n)  # drop thin-sample flag (e.g. "*6") if it leaked in
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())


# ── PDF parsing ──

def parse_top_picks(pdf_path: str, n: int | None = 10) -> list[dict]:
    """Extract picks from the 'Top Props by EV%' ranked table.

    n=10 -> top 10 by EV%; n=None -> every ranked prop (all rows).
    """
    doc = fitz.open(pdf_path)
    full = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    odds_lookup = parse_lineshop(full)
    doc.close()

    lines = [ln.strip() for ln in full.split("\n")]
    # Restrict to the ranked region: from 'Top Props' to first per-game detail
    try:
        start = next(i for i, ln in enumerate(lines) if "Top Props by EV%" in ln)
    except StopIteration:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if "Spread:" in lines[i] or "All-Book Lines" in lines[i]:
            end = i
            break
    region = lines[start:end]

    picks: dict[int, dict] = {}
    for idx in range(len(region)):
        if region[idx] not in PROP_SET:
            continue
        if idx < 4 or idx + 5 >= len(region):
            continue
        rank, player, tm, game = region[idx - 4], region[idx - 3], region[idx - 2], region[idx - 1]
        player = re.sub(r"\s*\*\d+\s*$", "", player).strip()  # strip thin-sample flag
        prop = region[idx]
        line_, sim, edge, rec, ev = region[idx + 1:idx + 6]
        if not rank.isdigit():
            continue
        if "@" not in game or rec not in REC_SET:
            continue
        if not EV_RE.match(ev) or not NUM_RE.match(line_):
            continue
        book = None
        if idx + 6 < len(region) and BOOK_RE.match(region[idx + 6]):
            if idx + 7 < len(region) and region[idx + 7].isdigit():
                book = region[idx + 6]
        r = int(rank)
        if r in picks:
            continue
        line_val = float(line_)
        key = (norm(player), prop, book)
        over_odds, under_odds = odds_lookup.get(key, (None, None))
        if over_odds is None:
            # fallback: any book for this player+prop
            for (p2, pr2, _b), (oo, uo) in odds_lookup.items():
                if p2 == norm(player) and pr2 == prop:
                    over_odds, under_odds = oo, uo
                    break
        picks[r] = {
            "rank": r, "player": player, "tm": tm, "game": game,
            "prop": prop, "line": line_val, "rec": rec,
            "ev": float(ev.rstrip("%")), "book": book,
            "over_odds": over_odds, "under_odds": under_odds,
        }
    ordered = [picks[r] for r in sorted(picks)]
    return ordered if n is None else ordered[:n]


def parse_lineshop(full: str) -> dict:
    """Parse the 'All-Book Lines' section -> {(player,prop,book): (over,under)}."""
    if "All-Book Lines" not in full:
        return {}
    lines = [ln.strip() for ln in full.split("\n")]
    start = next(i for i, ln in enumerate(lines) if "All-Book Lines" in ln)
    lines = lines[start:]
    title_re = re.compile(
        r"^(?P<player>.+?)\s*\(\s*(?P<tm>[A-Z]{2,4})\s*\).+?(?P<prop>Points|Rebounds|Pts\+Reb).+?Sim"
    )
    out: dict = {}
    cur_player = cur_prop = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = title_re.match(ln)
        if m:
            cur_player = norm(m.group("player"))
            cur_prop = m.group("prop")
            i += 1
            continue
        # book row: BOOK, line, over_odds, over_ev, under_odds, under_ev
        if cur_player and BOOK_RE.match(ln) and i + 5 < len(lines):
            t = lines[i + 1:i + 6]
            if (NUM_RE.match(t[0]) and ODDS_RE.match(t[1]) and
                    EV_RE.match(t[2]) and ODDS_RE.match(t[3]) and EV_RE.match(t[4])):
                key = (cur_player, cur_prop, ln)
                if key not in out:
                    out[key] = (int(t[1]), int(t[3]))
                i += 6
                continue
        i += 1
    return out


# ── ESPN actuals ──

_actuals_cache: dict[str, dict] = {}


def fetch_actuals(yyyymmdd: str) -> tuple[dict, bool]:
    """Return ({norm_name: (pts, reb, played)}, all_final) for a date."""
    if yyyymmdd in _actuals_cache:
        d = _actuals_cache[yyyymmdd]
        return d["players"], d["all_final"]
    players: dict[str, tuple] = {}
    all_final = True
    try:
        sb = get_json(f"{ESPN_SB}?dates={yyyymmdd}")
    except Exception:
        return players, False
    events = sb.get("events", [])
    for ev in events:
        comp = ev["competitions"][0]
        state = comp["status"]["type"]["state"]
        if state != "post":
            all_final = False
            continue
        try:
            summ = get_json(f"{ESPN_SUM}?event={ev['id']}")
        except Exception:
            all_final = False
            continue
        for team in summ.get("boxscore", {}).get("players", []):
            for grp in team.get("statistics", []):
                keys = grp.get("keys") or grp.get("names") or []
                try:
                    pi = keys.index("points")
                    ri = keys.index("rebounds")
                except ValueError:
                    continue
                for ath in grp.get("athletes", []):
                    nm = norm(ath.get("athlete", {}).get("displayName", ""))
                    if not nm:
                        continue
                    dnp = ath.get("didNotPlay", False)
                    stats = ath.get("stats", [])
                    if dnp or not stats or len(stats) <= max(pi, ri):
                        players[nm] = (None, None, False)
                        continue
                    try:
                        pts = int(stats[pi]); reb = int(stats[ri])
                        players[nm] = (pts, reb, True)
                    except (ValueError, TypeError):
                        players[nm] = (None, None, False)
        time.sleep(0.15)
    _actuals_cache[yyyymmdd] = {"players": players, "all_final": all_final}
    return players, all_final


def american_to_units_win(odds: int) -> float:
    """Profit (in units, 1u risk) on a winning bet at given American odds."""
    if odds is None:
        odds = -110
    return 100.0 / abs(odds) if odds < 0 else odds / 100.0


def grade_pick(pick: dict, actuals: dict) -> dict:
    """Grade one pick. Returns dict with result and units."""
    name = norm(pick["player"])
    rec = actuals.get(name)
    if rec is None:
        # try last-name + first-initial fallback
        parts = name.split()
        cand = [v for k, v in actuals.items()
                if k.split()[-1:] == parts[-1:] and k.split()[:1] == parts[:1]]
        rec = cand[0] if len(cand) == 1 else None
    if rec is None or not rec[2]:
        return {"result": "VOID", "units": 0.0, "actual": None}
    pts, reb, _ = rec
    val = pts if pick["prop"] == "Points" else reb
    line = pick["line"]
    odds = pick["over_odds"] if pick["rec"] == "OVER" else pick["under_odds"]
    if val == line:
        return {"result": "PUSH", "units": 0.0, "actual": val}
    if pick["rec"] == "OVER":
        won = val > line
    else:
        won = val < line
    units = american_to_units_win(odds) if won else -1.0
    return {"result": "WIN" if won else "LOSS", "units": units, "actual": val}


def _select(f: str, top_n: int | None, min_ev: float | None) -> list[dict]:
    """Pick the day's universe: EV-threshold filter, or top-N by EV%."""
    if min_ev is not None:
        return [p for p in parse_top_picks(f, None) if p["ev"] > min_ev]
    return parse_top_picks(f, top_n if top_n is not None else 10)


def run_config(top_n: int | None = 10, min_ev: float | None = None):
    """Grade one universe across all days. Returns (season, rows, no_play_days)."""
    files = sorted(
        glob.glob(str(DOCS / "WNBA_Props_Projections_*_2026.pdf")),
        key=lambda f: _fdate(f),
    )
    rows = []
    season = {"w": 0, "l": 0, "p": 0, "void": 0, "units": 0.0,
              "units_real": 0.0, "w_real": 0, "l_real": 0, "p_real": 0}
    no_play_days = 0

    for f in files:
        ymd = _fdate(f)
        if ymd >= TODAY:
            continue  # today / future not gradeable
        picks = _select(f, top_n, min_ev)
        if not picks:
            no_play_days += 1
            continue
        actuals, all_final = fetch_actuals(ymd)
        if not actuals:
            continue
        real_odds = any(p["over_odds"] is not None or p["under_odds"] is not None for p in picks)
        d = {"w": 0, "l": 0, "p": 0, "void": 0, "units": 0.0}
        for p in picks:
            g = grade_pick(p, actuals)
            if g["result"] == "VOID":
                d["void"] += 1
                continue
            if g["result"] == "PUSH":
                d["p"] += 1
            elif g["result"] == "WIN":
                d["w"] += 1
            else:
                d["l"] += 1
            d["units"] += g["units"]
        winpct = d["w"] / (d["w"] + d["l"]) * 100 if (d["w"] + d["l"]) else 0.0
        rows.append({
            "date": f"{ymd[4:6]}/{ymd[6:8]}", "n": len(picks),
            "w": d["w"], "l": d["l"], "p": d["p"],
            "void": d["void"], "units": d["units"], "winpct": winpct,
            "real": real_odds, "graded": d["w"] + d["l"] + d["p"],
        })
        season["w"] += d["w"]; season["l"] += d["l"]; season["p"] += d["p"]
        season["void"] += d["void"]; season["units"] += d["units"]
        if real_odds:
            season["units_real"] += d["units"]
            season["w_real"] += d["w"]; season["l_real"] += d["l"]; season["p_real"] += d["p"]

    return season, rows, no_play_days


def _roi(w, l, p, units):
    bets = w + l + p
    return units / bets * 100 if bets else 0.0


def sweep():
    """Evaluate a range of universes and print a comparison, ranked by real-odds ROI."""
    configs = [
        ("Top 3 / day", dict(top_n=3)),
        ("Top 5 / day", dict(top_n=5)),
        ("Top 7 / day", dict(top_n=7)),
        ("Top 10 / day", dict(top_n=10)),
        ("Top 15 / day", dict(top_n=15)),
        ("Top 20 / day", dict(top_n=20)),
        ("EV% > 10", dict(min_ev=10.0)),
        ("EV% > 15", dict(min_ev=15.0)),
        ("EV% > 20", dict(min_ev=20.0)),
        ("EV% > 25", dict(min_ev=25.0)),
        ("EV% > 30", dict(min_ev=30.0)),
        ("EV% > 40", dict(min_ev=40.0)),
    ]
    results = []
    for label, kw in configs:
        season, rows, _ = run_config(**kw)
        full_bets = season["w"] + season["l"] + season["p"]
        full_wp = season["w"] / (season["w"] + season["l"]) * 100 if (season["w"] + season["l"]) else 0.0
        full_roi = _roi(season["w"], season["l"], season["p"], season["units"])
        rd = season["w_real"] + season["l_real"]
        real_bets = rd + season["p_real"]
        real_wp = season["w_real"] / rd * 100 if rd else 0.0
        real_roi = _roi(season["w_real"], season["l_real"], season["p_real"], season["units_real"])
        results.append({
            "label": label, "full_bets": full_bets,
            "full_rec": f"{season['w']}-{season['l']}", "full_wp": full_wp,
            "full_u": season["units"], "full_roi": full_roi,
            "real_bets": real_bets, "real_rec": f"{season['w_real']}-{season['l_real']}",
            "real_wp": real_wp, "real_u": season["units_real"], "real_roi": real_roi,
        })

    print()
    print("=" * 94)
    print("  WNBA PROPS — THRESHOLD SWEEP — net units at actual odds (real era) / -110 proxy (full)")
    print("=" * 94)
    print(f"  {'Universe':14} | {'FULL SEASON (incl. May proxy)':^34} | {'REAL-ODDS ERA (May 29+)':^34}")
    print(f"  {'':14} | {'Bets':>5} {'W-L':>9} {'Win%':>6} {'Units':>7} | "
          f"{'Bets':>5} {'W-L':>8} {'Win%':>6} {'Units':>7} {'ROI':>6}")
    print("  " + "-" * 90)
    for r in sorted(results, key=lambda x: x["real_roi"], reverse=True):
        print(f"  {r['label']:14} | {r['full_bets']:>5} {r['full_rec']:>9} {r['full_wp']:>5.1f}% "
              f"{r['full_u']:>+7.2f} | {r['real_bets']:>5} {r['real_rec']:>8} {r['real_wp']:>5.1f}% "
              f"{r['real_u']:>+7.2f} {r['real_roi']:>+5.1f}%")
    print("  " + "-" * 90)
    print("  Ranked by real-odds-era ROI (the trustworthy signal). Break-even at ~ -110 juice = 52.4% win.")
    print("=" * 94)


def main(min_ev: float | None = None, top_n: int | None = None):
    season, rows, no_play_days = run_config(
        top_n=(10 if top_n is None and min_ev is None else top_n), min_ev=min_ev)

    # ── Print report ──
    universe = (f"ALL PLAYS WITH EV% > {min_ev:.0f}" if min_ev is not None
                else f"TOP {top_n or 10} BY EV% PER DAY")
    print()
    print("=" * 72)
    print(f"  WNBA PROPS — {universe} — WIN/LOSS BACK-TEST")
    print("=" * 72)
    print(f"  {'Date':6} {'N':>3} {'W':>3} {'L':>3} {'P':>2} {'Void':>4}  {'Win%':>6}  {'Units':>8}  Odds")
    print("  " + "-" * 68)
    for r in rows:
        star = "real" if r["real"] else " -110*"
        print(f"  {r['date']:6} {r['n']:>3} {r['w']:>3} {r['l']:>3} {r['p']:>2} {r['void']:>4}  "
              f"{r['winpct']:>5.1f}%  {r['units']:>+8.2f}  {star}")
    print("  " + "-" * 68)
    tot_dec = season["w"] + season["l"]
    tot_n = season["w"] + season["l"] + season["p"] + season["void"]
    tot_wp = season["w"] / tot_dec * 100 if tot_dec else 0.0
    roi = season["units"] / (season["w"] + season["l"] + season["p"]) * 100 if (season["w"] + season["l"] + season["p"]) else 0.0
    print(f"  {'TOTAL':6} {tot_n:>3} {season['w']:>3} {season['l']:>3} {season['p']:>2} {season['void']:>4}  "
          f"{tot_wp:>5.1f}%  {season['units']:>+8.2f}")
    print()
    if min_ev is not None and no_play_days:
        print(f"  Days with no qualifying play (no EV% > {min_ev:.0f}): {no_play_days}")
    print(f"  Season record (decisions): {season['w']}-{season['l']}"
          f"{('-' + str(season['p'])) if season['p'] else ''}  ({tot_wp:.1f}% win)")
    print(f"  Net units (1u/bet): {season['units']:+.2f}  |  ROI: {roi:+.1f}%  "
          f"|  Pushes: {season['p']}  |  Voids/DNP: {season['void']}")
    rd = season["w_real"] + season["l_real"]
    rwp = season["w_real"] / rd * 100 if rd else 0.0
    rroi = season["units_real"] / (rd + season["p_real"]) * 100 if (rd + season["p_real"]) else 0.0
    print()
    print("  -- Real-odds era only (May 29+, actual recommended-book prices) --")
    print(f"  Record: {season['w_real']}-{season['l_real']}"
          f"{('-' + str(season['p_real'])) if season['p_real'] else ''}  ({rwp:.1f}% win)  "
          f"|  Units: {season['units_real']:+.2f}  |  ROI: {rroi:+.1f}%")
    print()
    print("  * -110 = pre-May-29 days had no multi-book odds stored; units use -110 proxy.")
    print("  Void = projected player did not play (scratch/DNP) -> no action.")
    print("=" * 72)


def _fdate(path: str) -> str:
    m = re.search(r"_([A-Z][a-z]{2})(\d{2})_2026", path)
    return f"2026{MON[m.group(1)]}{m.group(2)}"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="WNBA props win/loss back-test")
    ap.add_argument("--min-ev", type=float, default=None,
                    help="Grade all plays with EV%% strictly greater than this "
                         "(e.g. 20). Omit for top-N-by-EV%% per day.")
    ap.add_argument("--top-n", type=int, default=None,
                    help="Grade the top N picks by EV%% each day (default 10).")
    ap.add_argument("--sweep", action="store_true",
                    help="Sweep multiple universes and rank by real-odds ROI.")
    a = ap.parse_args()
    if a.sweep:
        sweep()
    else:
        main(min_ev=a.min_ev, top_n=a.top_n)
