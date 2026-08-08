#!/usr/bin/env python3
r"""
answer_key_edges.py — run the Answer Key over TODAY'S slate and list edges
vs the posted derivative lines.

For every upcoming game (next --hours h) in each league with an Answer Key
dataset: anchor the historical sample on the market's consensus FULL-GAME
line (median across sharp books), then price the ACTUAL posted markets —
moneyline, FG spread/total, 1H spread/total, 1Q spread/total, team totals —
from how the sample games really went, and flag every side whose EV at the
best posted price clears --min-ev.

Usage:
    py -3 answer_key_edges.py                    # WNBA + NBA slates, min EV 2%
    py -3 answer_key_edges.py --league wnba --min-ev 3 --csv

Data: answer_key_{league}.csv (build with answer_key_data.py).
Lines: The Odds API per-event endpoint (~8 credits/game); shared key.
"""
import argparse
import csv
import os
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import answer_key as ak
from market_lines_multi import (ALLOWED_BOOKS, ODDS_API_HOST, SPORT_KEYS,
                                THE_ODDS_API_KEY, _http_json, quota)

DOCS = r"C:\Users\User\Documents"
OUT_DIR = os.environ.get("BBALL_DATA_DIR") or (
    DOCS if os.path.isdir(DOCS) else os.path.dirname(os.path.abspath(__file__)))

MIN_EV = 2.0          # % — surface threshold
HOURS = 30            # upcoming window

# Books an edge may be BET at (Odds API keys). Everything else is used only
# to anchor the sample — see ANCHOR_BOOKS below.
BET_BOOKS = {
    "fanduel": "FanDuel",
    "betrivers": "BetRivers",
    "williamhill_us": "Caesars",     # Caesars' live key (not "caesars")
    "thescore": "theScore",
}
# The sample anchor stays the BROAD sharp-book consensus (ALLOWED_BOOKS): it
# estimates what the market thinks this game IS, which is a better anchor
# from many books than from four. Only prices are restricted to BET_BOOKS.
ANCHOR_BOOKS = ALLOWED_BOOKS
Q_MARKETS = "h2h,spreads,totals,spreads_h1,totals_h1,spreads_q1,totals_q1,team_totals"
H_MARKETS = "h2h,spreads,totals,spreads_h1,totals_h1,team_totals"   # halves leagues


def american_to_dec(a):
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def fmt_odds(a):
    return f"+{a:g}" if a > 0 else f"{a:g}"


def fetch_events(sport_key, hours):
    url = (f"{ODDS_API_HOST}/sports/{sport_key}/events"
           f"?apiKey={THE_ODDS_API_KEY}")
    try:
        evs = _http_json(url)
    except Exception as e:
        print(f"  [warn] events fetch failed: {e}")
        return []
    now = datetime.now(timezone.utc)
    keep = []
    for ev in evs or []:
        try:
            t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if now - timedelta(hours=2) <= t <= now + timedelta(hours=hours):
            ev["_tip"] = t
            keep.append(ev)
    return keep


def fetch_event_odds(sport_key, event_id, markets):
    url = (f"{ODDS_API_HOST}/sports/{sport_key}/events/{event_id}/odds"
           f"?apiKey={THE_ODDS_API_KEY}&regions=us&oddsFormat=american"
           f"&markets={urllib.parse.quote(markets)}")
    try:
        return _http_json(url)
    except Exception:
        return None


def market_data(doc, market_key, home_team=None, team=None, bet_books=None):
    """-> (consensus_line, {side: [(thr, odds, book), ...]})

    consensus_line: median across ANCHOR_BOOKS (spreads = HOME handicap,
      totals = the total, h2h = None) — anchors the historical sample.
    offers: from BET_BOOKS only, each at THAT BOOK'S OWN posted number.
      Pricing every book at its own line (rather than only at the consensus)
      matters once the bettable set is small: a book sitting half a point off
      the consensus would otherwise be dropped, and a better line at worse
      odds can still be the higher-EV bet. Callers price every offer and keep
      the best.

    `thr` convention: spreads -> a MARGIN threshold (home wins if margin >
      thr, away wins if margin < thr; equals the AWAY handicap either way);
      totals/team totals -> the total; h2h -> None.
    """
    bet_books = bet_books if bet_books is not None else BET_BOOKS
    anchor_pts = []
    offers = defaultdict(list)
    for bm in (doc or {}).get("bookmakers", []) or []:
        bkey = bm.get("key")
        if bkey not in ANCHOR_BOOKS and bkey not in bet_books:
            continue
        for mk in bm.get("markets", []) or []:
            if mk.get("key") != market_key:
                continue
            for oc in mk.get("outcomes", []) or []:
                name, price, pt = oc.get("name"), oc.get("price"), oc.get("point")
                if price is None:
                    continue
                if market_key == "team_totals" and (oc.get("description") or "") != team:
                    continue
                if market_key == "h2h":
                    side, thr = ("home" if name == home_team else "away"), None
                elif market_key.startswith("spreads"):
                    if pt is None:
                        continue
                    side = "home" if name == home_team else "away"
                    thr = -pt if side == "home" else pt
                    if bkey in ANCHOR_BOOKS and side == "home":
                        anchor_pts.append(pt)
                else:                              # totals* / team_totals
                    if pt is None:
                        continue
                    side, thr = (name or "").lower(), pt
                    if bkey in ANCHOR_BOOKS and side == "over":
                        anchor_pts.append(pt)
                if bkey in bet_books:
                    offers[side].append((thr, price, bkey))
    return (median(anchor_pts) if anchor_pts else None), dict(offers)


def emp_counts(xs, line):
    over = sum(1 for x in xs if x > line)
    under = sum(1 for x in xs if x < line)
    return over, under, len(xs) - over - under


def ev_pct(n_win, n_lose, n_push, odds):
    n = n_win + n_lose + n_push
    if n == 0:
        return None
    return 100 * (n_win * (american_to_dec(odds) - 1) - n_lose) / n


def side_name(side, home, away):
    return {"home": home, "away": away,
            "over": "Over", "under": "Under"}.get(side, side)


def normalized_metrics(lg, spread, total, n, w):
    """Fallback for lines OUTSIDE the dataset's support: nearest n games with
    NO distance cap, each game's outcomes transformed to the target line
    first — totals scaled by (target_total / game_closing_total), margins
    shifted by (game_spread - target_spread). Shares/residuals are far more
    stable across scoring eras than raw absolutes, at the cost of assuming
    the scaling structure (so it's labeled, and pure sampling is preferred
    whenever support exists)."""
    import math
    rows = [r for r in ak.load_rows(lg) if not r["neutral"]]
    cand = sorted(((math.hypot(r["spread"] - spread, (r["total"] - total) / w), r)
                   for r in rows), key=lambda x: x[0])[:n]
    sample = [r for _, r in cand]
    per = ak.LEAGUE_CFG[lg]["periods"]
    m = {"margin": [], "total": [], "hpts": [], "apts": [],
         "h1m": [], "h1t": [], "q1m": [], "q1t": []}
    for r in sample:
        st = total / r["total"]
        dm = r["spread"] - spread            # shift toward target expectation
        mg = (r["hs"] - r["as"]) + dm
        tt = (r["hs"] + r["as"]) * st
        m["margin"].append(mg)
        m["total"].append(tt)
        m["hpts"].append((tt + mg) / 2)
        m["apts"].append((tt - mg) / 2)
        if r["ap"] is None:
            continue
        if per == 2:
            h1h, h1a = r["hp"][0], r["ap"][0]
        else:
            h1h, h1a = sum(r["hp"][:2]), sum(r["ap"][:2])
            m["q1m"].append((r["hp"][0] - r["ap"][0]) + dm / 4)
            m["q1t"].append((r["hp"][0] + r["ap"][0]) * st)
        m["h1m"].append((h1h - h1a) + dm / 2)
        m["h1t"].append((h1h + h1a) * st)
    return m, sample


def game_edges(lg, ev, doc, n, w, max_dist, bet_books=None):
    """-> dict with anchor/sample info + [edge dicts]; 'no_lines' on failure.
    Uses pure Feustel sampling when the line has support; falls back to the
    normalized wide sample (flagged) when it doesn't."""
    bet_books = bet_books if bet_books is not None else BET_BOOKS
    home, away = ev["home_team"], ev["away_team"]
    fg_spread, _ = market_data(doc, "spreads", home_team=home)
    fg_total, _ = market_data(doc, "totals")
    if fg_spread is None or fg_total is None:
        return {"fail": "no_lines"}
    rows = ak.load_rows(lg)
    sample, radius = ak.sample_games(rows, fg_spread, fg_total, n, w,
                                     "exclude", True, max_dist)
    if len(sample) >= 60:
        basis = "pure"
        m = ak.metrics(sample, lg)
    else:
        basis = "normalized"
        m, sample = normalized_metrics(lg, fg_spread, fg_total, n, w)
        radius = 0.0
    center = (sum(r["spread"] for r in sample) / len(sample),
              sum(r["total"] for r in sample) / len(sample))

    per = ak.LEAGUE_CFG[lg]["periods"]
    checks = [("h2h", "Moneyline", m["margin"], "ml", None),
              ("spreads", "FG spread", m["margin"], "spread", None),
              ("totals", "FG total", m["total"], "total", None),
              ("spreads_h1", "1H spread", m["h1m"], "spread", None),
              ("totals_h1", "1H total", m["h1t"], "total", None)]
    if per == 4:
        checks += [("spreads_q1", "1Q spread", m["q1m"], "spread", None),
                   ("totals_q1", "1Q total", m["q1t"], "total", None)]
    checks += [("team_totals", f"TT {home}", m["hpts"], "total", home),
               ("team_totals", f"TT {away}", m["apts"], "total", away)]

    edges = []
    for mkey, label, xs, kind, team in checks:
        if not xs:
            continue
        _, offers = market_data(doc, mkey, home_team=home, team=team,
                                bet_books=bet_books)
        if not offers:
            continue
        wins = sum(1 for x in xs if x > 0)
        decided = sum(1 for x in xs if x != 0)
        for side, side_offers in offers.items():
            best = None
            for thr, odds, book in side_offers:      # price each book's OWN line
                if kind == "ml":
                    if not decided:
                        continue
                    nw = wins if side == "home" else decided - wins
                    nl, push, disp, fair = decided - nw, 0, "", ""
                elif kind == "spread":
                    o, u, push = emp_counts(xs, thr)
                    nw, nl = (o, u) if side == "home" else (u, o)
                    disp = fmt_odds(-thr if side == "home" else thr)
                    fair = f"{ak.half_line(-median(xs)):+g}"
                else:
                    o, u, push = emp_counts(xs, thr)
                    nw, nl = (o, u) if side == "over" else (u, o)
                    disp = f"{'o' if side == 'over' else 'u'}{thr:g}"
                    fair = f"{ak.half_line(median(xs)):g}"
                e = ev_pct(nw, nl, push, odds)
                if e is None:
                    continue
                cand = {"label": label, "line": disp,
                        "side": side_name(side, home, away),
                        "odds": odds, "book": bet_books.get(book, book),
                        "ev": e, "emp": 100 * nw / (nw + nl) if nw + nl else 0,
                        "fair": fair}
                if best is None or cand["ev"] > best["ev"]:
                    best = cand
            if best:
                edges.append(best)
    return {"anchor": (fg_spread, fg_total), "n": len(sample),
            "radius": radius, "center": center, "edges": edges,
            "basis": basis}


def run(leagues, min_ev, hours, n, w, write_csv, max_dist, bet_books=None):
    bet_books = bet_books if bet_books is not None else BET_BOOKS
    all_rows = []
    stamp = datetime.now().strftime("%Y-%m-%d")
    print(f"\nANSWER KEY EDGES — {stamp}  (min EV {min_ev:g}%, sample N={n}, "
          f"total wt 1/{w:g}, max dist {max_dist:g})")
    print(f"  Bettable books: {', '.join(sorted(bet_books.values()))}"
          f"   (sample anchored on the broad sharp-book consensus)")
    for lg in leagues:
        if not ak.load_rows(lg):
            print(f"\n{ak.LEAGUE_CFG[lg]['label']}: no Answer Key dataset — "
                  f"run answer_key_data.py --league {lg}")
            continue
        sport = SPORT_KEYS.get(lg)
        events = fetch_events(sport, hours)
        label = ak.LEAGUE_CFG[lg]["label"]
        if not events:
            print(f"\n{label}: no games in the next {hours}h.")
            continue
        markets = Q_MARKETS if ak.LEAGUE_CFG[lg]["periods"] == 4 else H_MARKETS
        print(f"\n{label} — {len(events)} game(s)")
        for ev in sorted(events, key=lambda e: e["_tip"]):
            doc = fetch_event_odds(sport, ev["id"], markets)
            got = (game_edges(lg, ev, doc, n, w, max_dist, bet_books)
                   if doc else {"fail": "no_lines"})
            tip = ev["_tip"].astimezone().strftime("%I:%M %p").lstrip("0")
            head = f"  {ev['away_team']} @ {ev['home_team']}  ({tip})"
            if got.get("fail") == "no_lines":
                print(f"{head}  — no lines posted yet")
                continue
            (s, t), sn, radius = got["anchor"], got["n"], got["radius"]
            cs, ct = got["center"]
            edges = got["edges"]
            if got["basis"] == "normalized":
                tag = (f"  [NORMALIZED: line outside dataset support "
                       f"(center {cs:+.1f}/{ct:.1f}) — outcomes rescaled to "
                       f"this line; discount vs pure samples]")
            else:
                tag = f"   sample {sn} (radius {radius:.2f})"
            print(f"{head}   market {s:+g} / {t:g}{tag}")
            hits = sorted([e for e in edges if e["ev"] is not None
                           and e["ev"] >= min_ev],
                          key=lambda e: -e["ev"])
            if not hits:
                best = max((e for e in edges if e["ev"] is not None),
                           default=None, key=lambda e: e["ev"])
                extra = (f" (best: {best['label']} {best['line']} "
                         f"{best['side']} {best['ev']:+.1f}%)") if best else ""
                print(f"    no edges >= {min_ev:g}%{extra}")
                continue
            for e in hits:
                fair = f"  emp fair {e['fair']}" if e["fair"] else ""
                print(f"    {e['label']:<11} {e['line']:<7} {e['side']:<12} "
                      f"{fmt_odds(e['odds']):>6} ({e['book']})  "
                      f"emp {e['emp']:.1f}%  EV {e['ev']:+.1f}%{fair}")
                all_rows.append({"date": stamp, "league": lg,
                                 "game": f"{ev['away_team']} @ {ev['home_team']}",
                                 "market": e["label"], "line": e["line"],
                                 "side": e["side"], "odds": e["odds"],
                                 "book": e["book"], "emp_pct": round(e["emp"], 1),
                                 "ev_pct": round(e["ev"], 1),
                                 "anchor": f"{s:+g}/{t:g}", "sample_n": sn})
    if all_rows:
        print(f"\nTOP EDGES ACROSS SLATE")
        for r in sorted(all_rows, key=lambda x: -x["ev_pct"])[:15]:
            print(f"  {r['ev_pct']:+5.1f}%  {r['market']:<11} {r['line']:<7} "
                  f"{r['side']:<12} {fmt_odds(r['odds'])} ({r['book']})  "
                  f"{r['game']}")
        if write_csv:
            path = os.path.join(OUT_DIR, f"answer_key_edges_{stamp}.csv")
            with open(path, "w", encoding="utf-8", newline="") as f:
                wtr = csv.DictWriter(f, fieldnames=list(all_rows[0]))
                wtr.writeheader()
                wtr.writerows(all_rows)
            print(f"\nCSV: {path}")
    print(f"\nNote: empirical probs come from ~{200} similar games, so single-"
          f"digit EVs are within sampling noise (+/-~7% at 2 SE) — treat the "
          f"list as leads, not locks. Odds API quota: {quota()}")


def main():
    ap = argparse.ArgumentParser(description="Answer Key slate edge scanner")
    ap.add_argument("--league", choices=sorted(SPORT_KEYS),
                    help="one league only (default: every league with data)")
    ap.add_argument("--min-ev", type=float, default=MIN_EV)
    ap.add_argument("--hours", type=int, default=HOURS)
    ap.add_argument("--n", type=int, default=ak.DEFAULT_N)
    ap.add_argument("--w", type=float, default=ak.DEFAULT_W)
    ap.add_argument("--max-dist", type=float, default=ak.DEFAULT_MAX_DIST,
                    help="sample radius cap (0 = uncapped, not recommended)")
    ap.add_argument("--books", help="comma-separated Odds API book keys to bet "
                                    f"(default: {','.join(sorted(BET_BOOKS))})")
    ap.add_argument("--csv", action="store_true")
    a = ap.parse_args()
    if not THE_ODDS_API_KEY:
        print("THE_ODDS_API_KEY not set")
        sys.exit(1)
    books = BET_BOOKS
    if a.books:
        books = {k.strip(): BET_BOOKS.get(k.strip(), k.strip())
                 for k in a.books.split(",") if k.strip()}
    leagues = [a.league] if a.league else [lg for lg in ("wnba", "nba", "cbb", "wcbb")
                                           if os.path.exists(ak.csv_path(lg))]
    run(leagues, a.min_ev, a.hours, a.n, a.w, a.csv, a.max_dist, books)


if __name__ == "__main__":
    main()
