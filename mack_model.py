#!/usr/bin/env python3
"""
mack_model.py — "Mack Model" Pinnacle-vs-soft-books prop scanner.
=================================================================
Python port of Andrew Mack's (@gingfacekillah) "NBA Props with R" project
(and our WNBA rework in Documents\\WNBA_props), so it can run from the
basketball-site /tools page (Render has no R) and locally.

Strategy (the R version's, extended to a multi-book soft side):
  * Pinnacle is the sharp anchor. For every player prop where Pinnacle
    posts BOTH sides, take the BEST price per side across the soft books
    (SOFT_BOOKS below: BetRivers, FanDuel, Caesars) at the SAME line, and
    compute the implied-probability gap:
    diff = (1/pinnacle_price) - (1/best_soft_price).
  * Compute Pinnacle's average vig per prop type across the slate.
  * Flag props where a gap exceeds 50% of that average vig -> bet that
    side AT THE BOOK QUOTED in the output's Book column.

Why multi-book (Jul 31 2026): BetRivers exposes over-only prop prices via
The Odds API, so a BR-only scan could never flag an under. FanDuel and
Caesars post both sides -> unders are now in play, and every flag carries
the best available price + where to find it.

Scans WNBA and NBA (whichever has events; NBA is silent in the off-season).
Markets: points, threes, rebounds, assists, PRA.

Run:  py -3 mack_model.py            (console table; CSV written best-effort)
Key:  THE_ODDS_API_KEY env var, falling back to the shared key.
Cost: ~1 credit per game per market (~30/run on a 6-game slate) — unchanged
by the extra books (bookmaker groups of 10 bill as one region).
"""

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, date

try:  # keep the platform's encoding (matches the /tools runner's pipe decode);
    sys.stdout.reconfigure(errors="replace")  # never crash on odd player names
except Exception:
    pass

API_KEY = os.environ.get("THE_ODDS_API_KEY", "fdb2de0728216509287d06490355c922")
BASE = "https://api.the-odds-api.com/v4/sports/"
SPORTS = [("basketball_wnba", "WNBA"), ("basketball_nba", "NBA")]
MARKETS = [
    ("player_points", "points"),
    ("player_threes", "3_points"),
    ("player_rebounds", "rebounds"),
    ("player_assists", "assists"),
    ("player_points_rebounds_assists", "PRA"),
]
VIG_FRACTION = 0.5          # flag when gap > this fraction of avg Pinnacle vig
# The soft books to shop against Pinnacle (The Odds API key, label, abbrev).
# The scanner takes the BEST price per side across these at Pinnacle's line
# and reports which book has it. Add/remove books by editing this list
# (Caesars' Odds API key is williamhill_us).
SOFT_BOOKS = [
    ("betrivers", "BetRivers", "BR"),
    ("fanduel", "FanDuel", "FD"),
    ("williamhill_us", "Caesars", "CZR"),
]
SOFT_LABEL = "/".join(b[1] for b in SOFT_BOOKS)


def american(dec) -> str:
    """Decimal -> American display ('+150', '-110'). CSVs keep decimal —
    the trackers grade from those fields — this is console display only."""
    try:
        d = float(dec)
    except (TypeError, ValueError):
        return "-"
    if d <= 1.0:
        return "-"
    return f"+{round((d - 1) * 100)}" if d >= 2.0 else f"-{round(100 / (d - 1))}"

_quota = {}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        _quota["remaining"] = r.headers.get("x-requests-remaining")
        return json.loads(r.read())


def fetch_events(sport):
    url = (f"{BASE}{sport}/odds/?apiKey={API_KEY}"
           f"&regions=eu,us&bookmakers=pinnacle&oddsFormat=decimal")
    try:
        out = _get(url)
    except Exception as e:
        print(f"  [{sport}] events fetch failed: {e}")
        return []
    if isinstance(out, dict):        # API error object
        print(f"  [{sport}] Odds API error: {out.get('message', out)}")
        return []
    return out


def fetch_event_market(sport, event_id, market_key):
    books = ",".join(["pinnacle"] + [b[0] for b in SOFT_BOOKS])
    url = (f"{BASE}{sport}/events/{event_id}/odds?apiKey={API_KEY}"
           f"&regions=eu,us&markets={market_key}"
           f"&bookmakers={books}&oddsFormat=decimal")
    try:
        return _get(url)
    except Exception:
        return {}


def book_props(event_data, book):
    """{(player, line): {"over": price, "under": price}} for one bookmaker.
    Keyed by line so alt-line ladders survive, and one-sided listings are
    kept (BetRivers posts over-only prices via the API)."""
    out = {}
    for bm in event_data.get("bookmakers", []) or []:
        if bm.get("key") != book:
            continue
        for mkt in bm.get("markets", []) or []:
            for o in mkt.get("outcomes", []) or []:
                player, point = o.get("description"), o.get("point")
                price = o.get("price")
                if not player or point is None or not price:
                    continue
                side = "over" if o.get("name") == "Over" else "under"
                out.setdefault((player, point), {})[side] = price
    return out


def scan_sport(sport, league):
    events = fetch_events(sport)
    if not events:
        print(f"  [{league}] no events with Pinnacle odds right now.")
        return []
    print(f"  [{league}] {len(events)} game(s) — fetching "
          f"{len(MARKETS)} prop markets each...")

    rows = []
    for mkey, mlabel in MARKETS:
        market_rows = []
        for ev in events:
            data = fetch_event_market(sport, ev["id"], mkey)
            pin = book_props(data, "pinnacle")
            soft_by_book = {abbr: book_props(data, bkey)
                            for bkey, _, abbr in SOFT_BOOKS}
            for (player, line), pp in pin.items():
                if "over" not in pp or "under" not in pp:
                    continue          # need the full Pinnacle pair (vig anchor)
                # best price per side across the soft books, at Pinnacle's line
                best = {}             # side -> (price, book_abbr)
                # csv detail (uniform keys so DictWriter never sees extras)
                per_book = {f"{abbr.lower()}_{side}": None
                            for _, _, abbr in SOFT_BOOKS
                            for side in ("over", "under")}
                for _, _, abbr in SOFT_BOOKS:
                    sp = soft_by_book[abbr].get((player, line))
                    if not sp:
                        continue
                    for side in ("over", "under"):
                        p = sp.get(side)
                        per_book[f"{abbr.lower()}_{side}"] = p
                        if p and (side not in best or p > best[side][0]):
                            best[side] = (p, abbr)
                if not best:
                    continue          # no soft book posts this exact line
                over_diff = (1 / pp["over"] - 1 / best["over"][0]
                             if "over" in best else None)
                under_diff = (1 / pp["under"] - 1 / best["under"][0]
                              if "under" in best else None)
                market_rows.append({
                    "player": player, "league": league, "prop": mlabel,
                    "date": ev.get("commence_time", "")[:10],
                    "game": f'{ev.get("away_team", "")} @ {ev.get("home_team", "")}',
                    "soft_line": line, "pin_line": line,
                    "soft_over": best.get("over", (None,))[0], "pin_over": pp["over"],
                    "soft_under": best.get("under", (None,))[0], "pin_under": pp["under"],
                    "over_book": best.get("over", (None, ""))[1],
                    "under_book": best.get("under", (None, ""))[1],
                    "over_diff": over_diff, "under_diff": under_diff,
                    "pin_vig": (1 / pp["over"] + 1 / pp["under"]) - 1,
                    **per_book,
                })
        if not market_rows:
            print(f"    {mlabel:<9} no props at Pinnacle + a soft book")
            continue
        avg_vig = sum(r["pin_vig"] for r in market_rows) / len(market_rows)
        thr = VIG_FRACTION * avg_vig
        flagged = []
        for r in market_rows:
            sides = {s: d for s, d in (("over", r["over_diff"]),
                                       ("under", r["under_diff"]))
                     if d is not None}
            best_side = max(sides, key=sides.get, default=None)
            if best_side is not None and sides[best_side] > thr:
                r["bet"], r["edge"] = best_side, sides[best_side]
                r["book"] = r["over_book"] if best_side == "over" else r["under_book"]
                flagged.append(r)
        print(f"    {mlabel:<9} {len(market_rows):>3} props matched | "
              f"avg Pinnacle vig {avg_vig * 100:4.1f}% | {len(flagged)} flagged")
        rows.extend(flagged)
    return rows


def main():
    print("=" * 78)
    print(f"  MACK MODEL — Pinnacle vs {SOFT_LABEL} prop scanner "
          f"({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("  Flags props where the implied-probability gap > "
          f"{VIG_FRACTION:.0%} of avg Pinnacle vig, at matching")
    print("  lines. Best soft price per side is used; bet AT THE BOOK "
          "in the Book column.")
    print("=" * 78)

    all_rows = []
    for sport, league in SPORTS:
        all_rows.extend(scan_sport(sport, league))

    if not all_rows:
        print("\nNo qualifying props right now. Pinnacle posts player props on "
              "game day — try again around midday ET on a slate day.")
    else:
        all_rows.sort(key=lambda r: -r["edge"])
        print(f"\n  {len(all_rows)} QUALIFYING PROPS (best price + book shown)")
        print(f"  {'Player':<24} {'Lg':<5} {'Prop':<9} {'Line':>5} {'Bet':<6} "
              f"{'Odds':>6} {'Book':<5} {'PIN odds':>9} {'Edge':>6}  Game")
        print("  " + "-" * 108)
        for r in all_rows:
            odds_soft = r["soft_over"] if r["bet"] == "over" else r["soft_under"]
            odds_pin = r["pin_over"] if r["bet"] == "over" else r["pin_under"]
            print(f"  {r['player'][:24]:<24} {r['league']:<5} {r['prop']:<9} "
                  f"{r['soft_line']:>5} {r['bet']:<6} {american(odds_soft):>6} "
                  f"{r['book']:<5} {american(odds_pin):>9} "
                  f"{r['edge'] * 100:>5.1f}%  {r['game']}")
        n_over = sum(1 for r in all_rows if r["bet"] == "over")
        print(f"\n  Over/Under split: {n_over} over / {len(all_rows) - n_over} under")
        by_book = {}
        for r in all_rows:
            by_book[r["book"]] = by_book.get(r["book"], 0) + 1
        print("  Best-book split: " + ", ".join(
            f"{k} {v}" for k, v in sorted(by_book.items())))

        # best-effort CSV (ephemeral on Render; persistent locally)
        try:
            out_dir = os.environ.get("BBALL_DATA_DIR", r"C:\Users\User\Documents")
            path = os.path.join(out_dir, f"mack_model_{date.today().isoformat()}.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
                w.writeheader()
                w.writerows(all_rows)
            print(f"  CSV saved: {path}")
        except Exception:
            pass

    if _quota.get("remaining"):
        print(f"\n  Odds API credits remaining: {_quota['remaining']}")

    # keep the local W/L tracker current (no-op where the tracker module
    # isn't present, e.g. on the website server)
    try:
        import mack_model_track as T
        T.auto_update()
    except Exception:
        pass


if __name__ == "__main__":
    main()
