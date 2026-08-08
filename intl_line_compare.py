#!/usr/bin/env python3
"""
intl_line_compare.py — international basketball +EV scanner vs Pinnacle.
========================================================================
Pinnacle is the source of truth: its two-sided price is de-vigged into a fair
probability, and every soft-book price on the SAME line is scored by expected
value against that fair number:

    fair_p (side) = (1/pin_side) / (1/pin_side + 1/pin_other)     [de-vig]
    EV%           = fair_p x soft_decimal - 1

Positive EV = the soft book is paying more than Pinnacle's fair odds say the
side is worth. Anything >= --min-ev (default 1.0%) is listed, best first,
with the book to bet it at.

Soft books: FanDuel, BetRivers, Caesars, theScore.
Markets:    moneyline (h2h), spread, game total — full game, from the bulk
            odds feed (3 credits per league per run).
Leagues:    every ACTIVE non-US basketball league on The Odds API, discovered
            at runtime from the free /sports call (today that means EuroLeague
            ~Oct-May and Australian NBL ~Sep-Mar; anything they add later is
            picked up automatically). US leagues/futures are excluded.

Run:   py -3 intl_line_compare.py                (intl leagues, min EV 1%)
Flags: --min-ev 0.5        list plays down to +0.5% EV
       --sport KEY[,KEY]   override the league set (e.g. basketball_wnba to
                           test the pipeline while intl leagues are dark)
       --csv               also write intl_ev_YYYY-MM-DD.csv
Key:   THE_ODDS_API_KEY env var, falling back to the shared key.
"""

import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, date, timezone

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

API_KEY = os.environ.get("THE_ODDS_API_KEY", "fdb2de0728216509287d06490355c922")
BASE = "https://api.the-odds-api.com/v4"

ANCHOR = "pinnacle"
SOFT_BOOKS = [
    ("fanduel", "FanDuel", "FD"),
    ("betrivers", "BetRivers", "BR"),
    ("williamhill_us", "Caesars", "CZR"),
    ("thescore", "theScore", "SCR"),
]
MARKETS = "h2h,spreads,totals"
# US leagues + futures boards are not "intl" — excluded from discovery.
EXCLUDE_SUBSTR = ("nba", "wnba", "ncaab", "winner", "all_star")

_quota = {}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        _quota["remaining"] = r.headers.get("x-requests-remaining")
        return json.loads(r.read())


def discover_intl_sports():
    """Active non-US basketball sport keys (the /sports call is free)."""
    try:
        sports = _get(f"{BASE}/sports/?apiKey={API_KEY}")
    except Exception as e:
        print(f"  sports list fetch failed: {e}")
        return []
    out = []
    for s in sports if isinstance(sports, list) else []:
        key = s.get("key", "")
        if (s.get("group") == "Basketball" and s.get("active")
                and not any(x in key for x in EXCLUDE_SUBSTR)):
            out.append((key, s.get("title", key)))
    return out


def book_lines(event, book):
    """{(market, entity, line): {side: decimal}} for one bookmaker.

    h2h -> ('h2h','ML',None) sides home/away; spreads keyed by the HOME point
    (away +p pairs with home -p); totals keyed by the points line, sides
    over/under. One-sided soft listings are kept."""
    home, away = event.get("home_team", ""), event.get("away_team", "")
    out = {}
    for bm in event.get("bookmakers", []) or []:
        if bm.get("key") != book:
            continue
        for mkt in bm.get("markets", []) or []:
            mkey = mkt.get("key")
            for o in mkt.get("outcomes", []) or []:
                name, price, point = o.get("name"), o.get("price"), o.get("point")
                if not name or not price:
                    continue
                if mkey == "h2h":
                    side = "home" if name == home else "away" if name == away else None
                    if side:
                        out.setdefault(("h2h", "ML", None), {})[side] = price
                elif mkey == "spreads":
                    if point is None:
                        continue
                    if name == home:
                        out.setdefault(("spreads", "spread", point), {})["home"] = price
                    elif name == away:
                        out.setdefault(("spreads", "spread", -point), {})["away"] = price
                elif mkey == "totals":
                    if point is None:
                        continue
                    side = "over" if name == "Over" else "under"
                    out.setdefault(("totals", "total", point), {})[side] = price
    return out


def devig_pair(p_a: float, p_b: float):
    """Proportional de-vig of a two-sided decimal pair -> (fair_a, fair_b)."""
    ia, ib = 1.0 / p_a, 1.0 / p_b
    s = ia + ib
    return ia / s, ib / s


def american(dec: float) -> str:
    if dec >= 2.0:
        return f"+{round((dec - 1) * 100)}"
    return f"-{round(100 / (dec - 1))}"


def side_label(market, side, line, home, away):
    if market == "h2h":
        return f"{home if side == 'home' else away} ML"
    if market == "spreads":
        pt = line if side == "home" else -line
        return f"{home if side == 'home' else away} {pt:+g}"
    return f"{side.capitalize()} {line:g}"


def tip_str(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if ET:
            dt = dt.astimezone(ET)
        return dt.strftime("%a %m/%d %I:%M %p ET")
    except (ValueError, TypeError):
        return str(iso or "")


def scan_sport(sport_key: str, title: str, min_ev: float) -> list[dict]:
    books = ",".join([ANCHOR] + [b[0] for b in SOFT_BOOKS])
    url = (f"{BASE}/sports/{sport_key}/odds/?apiKey={API_KEY}"
           f"&regions=eu,us&markets={MARKETS}&bookmakers={books}"
           f"&oddsFormat=decimal")
    try:
        events = _get(url)
    except Exception as e:
        print(f"  [{title}] odds fetch failed: {e}")
        return []
    if isinstance(events, dict):
        print(f"  [{title}] Odds API error: {events.get('message', events)}")
        return []
    if not events:
        print(f"  [{title}] no upcoming events with odds.")
        return []

    plays, pairs = [], 0
    for ev in events:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        pin = book_lines(ev, ANCHOR)
        soft = {abbr: book_lines(ev, bkey) for bkey, _, abbr in SOFT_BOOKS}
        for key, pp in pin.items():
            market, entity, line = key
            sides = list(pp)
            if len(sides) != 2:
                continue                    # need the full Pinnacle pair to de-vig
            s1, s2 = sides
            fair = dict(zip((s1, s2), devig_pair(pp[s1], pp[s2])))
            pairs += 1
            for _, blabel, abbr in SOFT_BOOKS:
                sp = soft[abbr].get(key)
                if not sp:
                    continue                # book doesn't post this exact line
                for side in (s1, s2):
                    dec = sp.get(side)
                    if not dec or dec <= 1.0:
                        continue
                    ev_pct = fair[side] * dec - 1.0
                    if ev_pct < min_ev / 100.0:
                        continue
                    plays.append({
                        "league": title, "sport": sport_key,
                        "game": f"{away} @ {home}",
                        "tip": tip_str(ev.get("commence_time")),
                        "market": {"h2h": "moneyline", "spreads": "spread",
                                   "totals": "total"}[market],
                        "bet": side_label(market, side, line, home, away),
                        "line": "" if line is None else line,
                        "side": side,
                        "book": abbr, "book_label": blabel,
                        "odds_dec": dec, "odds_am": american(dec),
                        "pin_dec": pp[side], "pin_am": american(pp[side]),
                        "fair_pct": round(fair[side] * 100, 1),
                        "ev_pct": round(ev_pct * 100, 2),
                    })
    print(f"  [{title}] {len(events)} event(s), {pairs} Pinnacle pairs scanned, "
          f"{len(plays)} +EV play(s) >= {min_ev:g}%")
    return plays


def report(min_ev=1.0, sport="", write_csv=False):
    print("=" * 78)
    print(f"  INTL LINE COMPARE — Pinnacle fair line vs "
          f"{'/'.join(b[1] for b in SOFT_BOOKS)}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')} | de-vigged Pinnacle = "
          f"source of truth | min EV {min_ev:g}%")
    print("=" * 78)

    if sport:
        targets = [(k.strip(), k.strip()) for k in sport.split(",") if k.strip()]
    else:
        targets = discover_intl_sports()
        if not targets:
            print("\n  No ACTIVE international basketball on The Odds API right now.")
            print("  (Australian NBL returns ~September, EuroLeague ~October — this")
            print("  discovers them automatically once their books go up.)")
            return

    plays = []
    for key, title in targets:
        plays.extend(scan_sport(key, title, min_ev))

    if not plays:
        print(f"\n  No +EV plays >= {min_ev:g}% right now.")
    else:
        plays.sort(key=lambda r: -r["ev_pct"])
        print(f"\n  {len(plays)} +EV PLAYS (vs de-vigged Pinnacle)")
        print(f"  {'Bet':<28} {'Book':<5} {'Odds':>7} {'PIN':>7} {'Fair%':>6} "
              f"{'EV%':>6}  {'Market':<9} {'Game / tip'}")
        print("  " + "-" * 110)
        for r in plays:
            print(f"  {r['bet'][:28]:<28} {r['book']:<5} {r['odds_am']:>7} "
                  f"{r['pin_am']:>7} {r['fair_pct']:>5.1f}% {r['ev_pct']:>5.2f}%  "
                  f"{r['market']:<9} {r['game']}  [{r['tip']}]")

        if write_csv:
            try:
                out_dir = os.environ.get("BBALL_DATA_DIR", r"C:\Users\User\Documents")
                path = os.path.join(out_dir, f"intl_ev_{date.today().isoformat()}.csv")
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=list(plays[0].keys()))
                    w.writeheader()
                    w.writerows(plays)
                print(f"\n  CSV saved: {path}")
            except Exception as e:
                print(f"\n  CSV write failed: {e}")

    if _quota.get("remaining"):
        print(f"\n  Odds API credits remaining: {_quota['remaining']}")


# ── Web mode (mounted at /intlev on the basketball-site) ─────────────────────
try:
    import io
    import threading
    from contextlib import redirect_stderr, redirect_stdout

    from flask import Flask, Response, request

    app = Flask(__name__)
    _run_lock = threading.Lock()

    _PAGE = """<style>
body{background:#10151c;color:#dfe7ef;font-family:Segoe UI,Arial,sans-serif;
     margin:0;padding:14px 18px}
h1{color:#00cec9;font-size:20px;margin:4px 0 6px}
.note{color:#8aa;font-size:12.5px;margin:4px 0 12px}
a.menu{display:inline-block;color:#00cec9;text-decoration:none;font-size:13px;
       font-weight:600;border:1px solid #00cec9;border-radius:6px;
       padding:4px 12px;margin-bottom:8px}
a.menu:hover{background:#00cec9;color:#10151c}
input{background:#0e131a;color:#dfe7ef;border:1px solid #33414f;
      border-radius:4px;padding:5px 7px;font-size:13px}
button{background:#00cec9;color:#10151c;font-weight:700;border:none;
       border-radius:5px;padding:7px 16px;font-size:13px;cursor:pointer}
button:disabled{opacity:0.5;cursor:wait}
#out{background:#0e131a;border:1px solid #2a3542;border-radius:8px;
     padding:12px 14px;margin-top:12px;font-size:12px;line-height:1.55;
     overflow-x:auto;white-space:pre;color:#dfe7ef;min-height:60px}
label{font-size:11px;color:#9ab;margin-right:4px}
</style><title>Intl Line Compare</title>
<a class='menu' href='/'>&larr; Main Menu</a>
<h1>&#127760; Intl Line Compare</h1>
<div class='note'>De-vigged Pinnacle = fair; every FanDuel / BetRivers /
Caesars / theScore price on the SAME line is scored by EV%. Leagues are
auto-discovered from The Odds API (EuroLeague ~Oct&ndash;May, Australian NBL
~Sep&ndash;Mar) &mdash; a quiet page just means nothing is in season. Runs
automatically on load.</div>
<label>Min EV %</label>
<input id='minev' type='number' value='1.0' step='0.5' min='0' style='width:70px'>
<button id='go' onclick='go()' style='margin-left:10px'>Run scan</button>
<pre id='out'>Loading&hellip;</pre>
<script>
async function go(){
  const b=document.getElementById('go'),o=document.getElementById('out');
  b.disabled=true;o.textContent='Scanning active intl leagues...';
  const m=document.getElementById('minev').value;
  try{const r=await fetch('run?minev='+m,{method:'POST'});
      o.textContent=await r.text();}
  catch(e){o.textContent='Error: '+e;}
  b.disabled=false;
}
go();
</script>"""

    @app.route("/", methods=["GET"])
    def index():
        return _PAGE

    @app.route("/run", methods=["POST"])
    def run_scan():
        if not _run_lock.acquire(blocking=False):
            return Response("A scan is already running — give it a minute.",
                            mimetype="text/plain")
        try:
            try:
                min_ev = max(float(request.args.get("minev") or 1.0), 0.0)
            except ValueError:
                min_ev = 1.0
            buf = io.StringIO()
            try:
                with redirect_stdout(buf), redirect_stderr(buf):
                    report(min_ev=min_ev)
            except Exception as exc:
                buf.write(f"\n[ERROR] scan failed: {exc!r}")
            return Response(buf.getvalue() or "(no output)",
                            mimetype="text/plain")
        finally:
            _run_lock.release()
except ImportError:                      # console mode works without flask
    app = None


def main():
    ap = argparse.ArgumentParser(description="Intl basketball +EV scanner vs Pinnacle")
    ap.add_argument("--min-ev", type=float, default=1.0,
                    help="minimum EV%% to list (default 1.0)")
    ap.add_argument("--sport", default="",
                    help="comma-separated Odds API sport keys to force "
                         "(default: auto-discover active intl basketball)")
    ap.add_argument("--csv", action="store_true", help="also write a dated CSV")
    a = ap.parse_args()
    report(a.min_ev, a.sport, a.csv)


if __name__ == "__main__":
    main()
