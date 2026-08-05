#!/usr/bin/env python3
"""
mack_model_games.py — Mack Model for GAME markets (sides / totals / ML / team totals).
======================================================================================
Same strategy as mack_model.py (Andrew Mack's Pinnacle-anchor prop scanner), applied
to team/game bets instead of player props:

  * Pinnacle is the sharp anchor. For every two-way game market offered by BOTH
    Pinnacle and the soft book (BetRivers), compute the implied-probability gap
    on each side: diff = (1/pinnacle_price) - (1/soft_price).
  * Compute Pinnacle's average vig per MARKET TYPE across the slate
    (FG spread, 1H total, 1Q moneyline, ... each anchor their own average).
  * Flag markets where a gap exceeds 50% of that average vig AND the two books
    have the SAME line -> bet that side AT THE SOFT BOOK.

Markets scanned (all valid Odds API basketball keys, verified live):
  moneyline (h2h), spread, game total, team totals — each for the
  FULL GAME, FIRST HALF (_h1) and FIRST QUARTER (_q1).           = 12 markets

Leagues: WNBA, NBA, NCAA men, NCAA women, Australian NBL, EuroLeague
(off-season leagues report quietly, like mack_model.py).

Like the props scanner this is a GAME-DAY tool: Pinnacle posts game lines (and
books post period/team-total markets) on the day of the game — run it around
midday ET on a slate day.

Run:  py -3 mack_model_games.py
Flags: --anchor pinnacle --soft betrivers   (override either book)
       --max-events N                       (per-league event cap, default 30)
Key:  THE_ODDS_API_KEY env var, falling back to the shared key.
Cost: ~24 credits per game (12 markets x 2 regions, one request per game).
CSV:  mack_games_YYYY-MM-DD.csv (distinct prefix — never matches the
      mack_model props tracker's mack_model_*.csv glob).
"""

import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, date

try:  # keep the platform's encoding (matches the /tools runner's pipe decode)
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

API_KEY = os.environ.get("THE_ODDS_API_KEY", "fdb2de0728216509287d06490355c922")
BASE = "https://api.the-odds-api.com/v4/sports/"
SPORTS = [
    ("basketball_wnba", "WNBA"),
    ("basketball_nba", "NBA"),
    ("basketball_ncaab", "NCAAM"),
    ("basketball_wncaab", "NCAAW"),
    ("basketball_nbl", "NBL"),
    ("basketball_euroleague", "EURO"),
]
# (suffix, label) for the period variants of every base market
PERIODS = [("", "FG"), ("_h1", "1H"), ("_q1", "1Q")]
BASE_MARKETS = ["h2h", "spreads", "totals", "team_totals"]
MARKET_LABEL = {"h2h": "moneyline", "spreads": "spread",
                "totals": "total", "team_totals": "team total"}
ALL_MARKET_KEYS = [f"{m}{suf}" for suf, _ in PERIODS for m in BASE_MARKETS]
VIG_FRACTION = 0.5          # flag when gap > this fraction of avg anchor vig
DEFAULT_ANCHOR = ("pinnacle", "Pinnacle", "PIN")
DEFAULT_SOFT = ("betrivers", "BetRivers", "BR")
MAX_EVENTS = 30             # per-league cap (college slates are huge) — capped
                            # events are LOGGED, never silently dropped

_quota = {}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        _quota["remaining"] = r.headers.get("x-requests-remaining")
        return json.loads(r.read())


def fetch_events(sport, anchor):
    """Upcoming events that the anchor book has priced (any featured market)."""
    url = (f"{BASE}{sport}/odds/?apiKey={API_KEY}"
           f"&regions=eu,us&bookmakers={anchor}&oddsFormat=decimal")
    try:
        out = _get(url)
    except Exception as e:
        print(f"  [{sport}] events fetch failed: {e}")
        return []
    if isinstance(out, dict):        # API error object
        print(f"  [{sport}] Odds API error: {out.get('message', out)}")
        return []
    # the bulk feed lists every upcoming event; keep only ones the anchor has
    # actually priced (saves ~24 credits per unpriced event on per-event calls)
    return [e for e in out if any(bm.get("key") == anchor
                                  for bm in e.get("bookmakers", []) or [])]


def fetch_event_markets(sport, event_id, anchor, soft):
    """One request per game: all 12 game markets for both books."""
    url = (f"{BASE}{sport}/events/{event_id}/odds?apiKey={API_KEY}"
           f"&regions=eu,us&markets={','.join(ALL_MARKET_KEYS)}"
           f"&bookmakers={anchor},{soft}&oddsFormat=decimal")
    try:
        return _get(url)
    except Exception:
        return {}


def book_pairs(event_data, book, home, away):
    """{(market_key, entity, line): {side: price}} for one bookmaker.

    Side keys are 'home'/'away' for moneylines & spreads, 'over'/'under' for
    totals & team totals. Spreads are keyed by the HOME line (home point p pairs
    with away point -p). One-sided listings are kept (soft books sometimes post
    one side via the API); the anchor pair check happens in the scorer.
    """
    out = {}
    for bm in event_data.get("bookmakers", []) or []:
        if bm.get("key") != book:
            continue
        for mkt in bm.get("markets", []) or []:
            mkey = mkt.get("key")
            if mkey not in ALL_MARKET_KEYS:
                continue
            base = mkey.split("_h1")[0].split("_q1")[0]
            for o in mkt.get("outcomes", []) or []:
                name, price, point = o.get("name"), o.get("price"), o.get("point")
                if not name or not price:
                    continue
                if base == "h2h":
                    side = "home" if name == home else "away" if name == away else None
                    if side:
                        out.setdefault((mkey, "ML", None), {})[side] = price
                elif base == "spreads":
                    if point is None:
                        continue
                    if name == home:
                        out.setdefault((mkey, "spread", point), {})["home"] = price
                    elif name == away:   # away +p pairs with home -p
                        out.setdefault((mkey, "spread", -point), {})["away"] = price
                elif base == "totals":
                    if point is None:
                        continue
                    side = "over" if name == "Over" else "under"
                    out.setdefault((mkey, "total", point), {})[side] = price
                elif base == "team_totals":
                    team, side = o.get("description"), \
                        ("over" if name == "Over" else "under")
                    if team and point is not None:
                        out.setdefault((mkey, team, point), {})[side] = price
    return out


def american(dec) -> str:
    """Decimal -> American display ('+150', '-110'). CSVs keep decimal —
    the games tracker grades from those fields — console display only."""
    try:
        d = float(dec)
    except (TypeError, ValueError):
        return "-"
    if d <= 1.0:
        return "-"
    return f"+{round((d - 1) * 100)}" if d >= 2.0 else f"-{round(100 / (d - 1))}"


def period_of(mkey):
    return "1H" if mkey.endswith("_h1") else "1Q" if mkey.endswith("_q1") else "FG"


def market_type(mkey):
    base = mkey.split("_h1")[0].split("_q1")[0]
    return f"{period_of(mkey)} {MARKET_LABEL[base]}"


def bet_name(mkey, entity, line, side, home, away):
    base = mkey.split("_h1")[0].split("_q1")[0]
    if base == "h2h":
        return f"{home if side == 'home' else away} ML"
    if base == "spreads":
        pt = line if side == "home" else -line
        return f"{home if side == 'home' else away} {pt:+g}"
    if base == "totals":
        return f"{side.capitalize()} {line:g}"
    return f"{entity} {side.capitalize()} {line:g}"     # team total


def scan_sport(sport, league, anchor, soft, max_events):
    events = fetch_events(sport, anchor[0])
    if not events:
        print(f"  [{league}] no events with {anchor[1]} odds right now.")
        return []
    if len(events) > max_events:
        print(f"  [{league}] {len(events)} game(s) — CAPPED at {max_events} "
              f"(earliest tips first; use --max-events to widen)")
        events = sorted(events, key=lambda e: e.get("commence_time", ""))[:max_events]
    else:
        print(f"  [{league}] {len(events)} game(s) — fetching "
              f"{len(ALL_MARKET_KEYS)} game markets each...")

    raw = {mk: [] for mk in ALL_MARKET_KEYS}
    for ev in events:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        data = fetch_event_markets(sport, ev["id"], anchor[0], soft[0])
        pin = book_pairs(data, anchor[0], home, away)
        sb = book_pairs(data, soft[0], home, away)
        for (mkey, entity, line), pp in pin.items():
            sides = list(pp)
            if len(sides) < 2:
                continue              # need the full anchor pair (vig anchor)
            sp = sb.get((mkey, entity, line))
            if not sp:
                continue              # exact same line at the soft book only
            s1, s2 = sides[0], sides[1]
            raw[mkey].append({
                "league": league, "market": market_type(mkey), "mkey": mkey,
                "entity": entity, "line": line,
                "date": ev.get("commence_time", "")[:10],
                "game": f"{away} @ {home}", "home": home, "away": away,
                "sides": {s: {"pin": pp[s], "soft": sp.get(s),
                              "diff": (1 / pp[s] - 1 / sp[s]) if sp.get(s) else None}
                          for s in (s1, s2)},
                "pin_vig": (1 / pp[s1] + 1 / pp[s2]) - 1,
            })

    flagged = []
    for mkey in ALL_MARKET_KEYS:
        mrows = raw[mkey]
        if not mrows:
            print(f"    {market_type(mkey):<14} no markets at BOTH books")
            continue
        avg_vig = sum(r["pin_vig"] for r in mrows) / len(mrows)
        thr = VIG_FRACTION * avg_vig
        hits = 0
        for r in mrows:
            sides = {s: d["diff"] for s, d in r["sides"].items()
                     if d["diff"] is not None}
            best = max(sides, key=sides.get, default=None)
            if best is not None and sides[best] > thr:
                r["bet_side"] = best
                r["bet"] = bet_name(r["mkey"], r["entity"], r["line"], best,
                                    r["home"], r["away"])
                r["edge"] = sides[best]
                r["soft_odds"] = r["sides"][best]["soft"]
                r["pin_odds"] = r["sides"][best]["pin"]
                flagged.append(r)
                hits += 1
        print(f"    {market_type(mkey):<14} {len(mrows):>3} at both books | "
              f"avg {anchor[2]} vig {avg_vig * 100:4.1f}% | {hits} flagged")
    return flagged


def main():
    ap = argparse.ArgumentParser(description="Mack Model for game markets")
    ap.add_argument("--anchor", default=DEFAULT_ANCHOR[0],
                    help="sharp anchor book key (default pinnacle)")
    ap.add_argument("--soft", default=DEFAULT_SOFT[0],
                    help="soft book key to shop (default betrivers)")
    ap.add_argument("--max-events", type=int, default=MAX_EVENTS,
                    help=f"per-league event cap (default {MAX_EVENTS})")
    args = ap.parse_args()
    anchor = DEFAULT_ANCHOR if args.anchor == DEFAULT_ANCHOR[0] else \
        (args.anchor, args.anchor.title(), args.anchor[:3].upper())
    soft = DEFAULT_SOFT if args.soft == DEFAULT_SOFT[0] else \
        (args.soft, args.soft.title(), args.soft[:3].upper())

    print("=" * 78)
    print(f"  MACK MODEL: GAME MARKETS — {anchor[1]} vs {soft[1]} "
          f"({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("  Sides, totals, moneylines & team totals — full game, 1st half, 1st qtr.")
    print(f"  Flags markets where the implied-prob gap > {VIG_FRACTION:.0%} of avg "
          f"{anchor[1]} vig")
    print(f"  for that market type, at matching lines. Bet the flagged side AT "
          f"{soft[1].upper()}.")
    print("=" * 78)

    all_rows = []
    for sport, league in SPORTS:
        all_rows.extend(scan_sport(sport, league, anchor, soft, args.max_events))

    if not all_rows:
        print(f"\nNo qualifying bets right now. {anchor[1]} posts game lines (and "
              "books post period/team-total")
        print("markets) on game day — try again around midday ET on a slate day.")
    else:
        all_rows.sort(key=lambda r: -r["edge"])
        print(f"\n  {len(all_rows)} QUALIFYING BETS (bet at {soft[1]})")
        print(f"  {'Bet':<32} {'Lg':<6} {'Market':<14} "
              f"{soft[2] + ' odds':>8} {anchor[2] + ' odds':>9} {'Edge':>6}  Game")
        print("  " + "-" * 108)
        for r in all_rows:
            print(f"  {r['bet'][:32]:<32} {r['league']:<6} {r['market']:<14} "
                  f"{american(r['soft_odds']):>8} {american(r['pin_odds']):>9} "
                  f"{r['edge'] * 100:>5.1f}%  {r['game']}")

        # best-effort CSV (ephemeral on Render; persistent locally). Distinct
        # mack_games_ prefix so the props tracker's glob can never pick it up.
        try:
            out_dir = os.environ.get("BBALL_DATA_DIR", r"C:\Users\User\Documents")
            path = os.path.join(out_dir, f"mack_games_{date.today().isoformat()}.csv")
            fields = ["league", "market", "mkey", "entity", "line", "date", "game",
                      "bet", "bet_side", "soft_odds", "pin_odds", "edge", "pin_vig"]
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(all_rows)
            print(f"  CSV saved: {path}")
        except Exception:
            pass

    if _quota.get("remaining"):
        print(f"\n  Odds API credits remaining: {_quota['remaining']}")

    # Grade any prior settled slates into the games tracker (local only —
    # mack_games_track.py doesn't exist on Render, so this silently no-ops).
    try:
        import mack_games_track
        mack_games_track.auto_update()
    except Exception:
        pass


if __name__ == "__main__":
    main()
