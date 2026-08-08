"""Line Movement Report -- who is the market moving WITH, and who is it moving AGAINST?

For every upcoming game across the basketball leagues, shows each team's mean
open->close movement over its last 5 completed games -- SPREADS and TOTALS --
plus the spread differential between the two opponents and a combined totals
lean for the matchup. Totals ride the same odds document as spreads, so they
add zero extra API calls.

Sign conventions:
  SPREAD (team-oriented):  move = open_spread - close_spread
    +  the market moved TOWARD the team (bigger favorite / smaller dog at close)
    -  the market moved AGAINST the team
  TOTAL (game-oriented):   move = close_total - open_total
    +  the total was bet UP toward the over    -  bet DOWN toward the under

Data source: ESPN Core API odds feed, which stores open/current/close per game
(sports.core.api.espn.com .../events/{id}/competitions/{id}/odds). Provider
preference: DraftKings, then ESPN BET, then whatever is available. Games with no
stored line simply don't contribute (n of 5 is reported).

Usage:
    py -3 line_movement_report.py                 # all leagues, next 48h
    py -3 line_movement_report.py --days 5        # widen the upcoming window
    py -3 line_movement_report.py --league wnba   # one league only
    py -3 line_movement_report.py --csv           # also write a dated CSV

Leagues: wnba, nba, cbb (NCAA men), wcbb (NCAA women), gleague, nbl.
Off-season leagues simply return no upcoming games. Pure stdlib.
"""

import argparse
import csv
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0"}
LAST_N = 5
MAX_SEASON_WALK = 3          # how many prior seasons to walk to fill LAST_N games
PROVIDER_PREF = ["DraftKings", "ESPN BET", "ESPN Bet", "Caesars"]

LEAGUES = {
    "wnba":    {"label": "WNBA",         "path": "wnba",                      "college": False},
    "nba":     {"label": "NBA",          "path": "nba",                       "college": False},
    "cbb":     {"label": "NCAA MEN",     "path": "mens-college-basketball",   "college": True},
    "wcbb":    {"label": "NCAA WOMEN",   "path": "womens-college-basketball", "college": True},
    "gleague": {"label": "G LEAGUE",     "path": "nba-development",           "college": False},
    "nbl":     {"label": "AUSTRALIAN NBL", "path": "nbl",                     "college": False},
}


def fetch_json(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


# ---------------------------------------------------------------- upcoming slate

def fetch_upcoming(league_key, window_hours):
    """Pre-state games tipping within the next window_hours, sorted by tip time."""
    cfg = LEAGUES[league_key]
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=window_hours)
    extra = "&groups=50&limit=500" if cfg["college"] else ""
    games, seen = [], set()
    days = int(window_hours // 24) + 2
    for d in range(days):
        ds = (datetime.now(ET) + timedelta(days=d)).strftime("%Y%m%d")
        sb = fetch_json(
            f"https://site.web.api.espn.com/apis/site/v2/sports/basketball/"
            f"{cfg['path']}/scoreboard?dates={ds}{extra}"
        )
        for ev in (sb or {}).get("events", []):
            eid = ev.get("id")
            if eid in seen:
                continue
            seen.add(eid)
            try:
                if ev["status"]["type"]["state"] != "pre":
                    continue
                tip = datetime.strptime(ev["date"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
                if not (now - timedelta(hours=1) <= tip <= cutoff):
                    continue
                comp = ev["competitions"][0]
                away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
                home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
                games.append({
                    "event_id": eid,
                    "tip": tip,
                    "away_id": away["team"]["id"],
                    "home_id": home["team"]["id"],
                    "away_name": away["team"].get("displayName", away["team"].get("shortDisplayName", "?")),
                    "home_name": home["team"].get("displayName", home["team"].get("shortDisplayName", "?")),
                    "away_abbrev": away["team"].get("abbreviation", ""),
                    "home_abbrev": home["team"].get("abbreviation", ""),
                })
            except Exception:
                continue
    games.sort(key=lambda g: g["tip"])
    return games


# ---------------------------------------------------------- last-5 completed games

def fetch_last_completed(league_key, team_id, n=LAST_N):
    """Last n completed (non-preseason) games for a team: [(event_id, was_home)]."""
    cfg = LEAGUES[league_key]
    base = (f"https://site.web.api.espn.com/apis/site/v2/sports/basketball/"
            f"{cfg['path']}/teams/{team_id}/schedule")
    collected = {}          # event_id -> (date, was_home)
    cur_season = None

    def harvest(feed):
        for ev in (feed or {}).get("events", []):
            try:
                st = ev.get("seasonType", {})
                if str(st.get("id", "")) == "1":       # preseason
                    continue
                comp = ev["competitions"][0]
                status = comp.get("status") or ev.get("status") or {}
                if not status.get("type", {}).get("completed"):
                    continue
                me = next(c for c in comp["competitors"] if str(c["team"]["id"]) == str(team_id))
                collected[ev["id"]] = (ev.get("date", ""), me["homeAway"] == "home")
            except Exception:
                continue

    feed = fetch_json(base)
    if feed:
        cur_season = ((feed.get("season") or {}).get("year")
                      or (feed.get("requestedSeason") or {}).get("year"))
        harvest(feed)
    if cur_season:
        for k in range(1, MAX_SEASON_WALK + 1):
            if len(collected) >= n:
                break
            harvest(fetch_json(f"{base}?season={cur_season - k}"))

    ordered = sorted(collected.items(), key=lambda kv: kv[1][0], reverse=True)[:n]
    return [(eid, was_home) for eid, (_, was_home) in ordered]


# ------------------------------------------------------------------ odds movement

def _parse_line(ps):
    if not isinstance(ps, dict):
        return None
    for key in ("american", "alternateDisplayValue", "displayValue"):
        v = ps.get(key)
        if v is None:
            continue
        s = str(v).strip().replace("−", "-")
        if s.upper() in ("EVEN", "PK", "PICK", "0"):
            return 0.0
        try:
            return float(s)
        except ValueError:
            continue
    return None


def fetch_game_move(league_key, event_id, was_home):
    """(spread_move, total_move) for one completed game, either side None.

    spread_move = open - close from the team's perspective (+ = market moved
    TOWARD the team). total_move = close - open on the game total (+ = the
    total was bet UP toward the over, - = down toward the under). Both come
    from the SAME odds document, so totals add no extra API calls."""
    cfg = LEAGUES[league_key]
    data = fetch_json(
        f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/"
        f"{cfg['path']}/events/{event_id}/competitions/{event_id}/odds?limit=100"
    )
    items = (data or {}).get("items", [])
    if not items:
        return None, None

    def prio(it):
        name = (it.get("provider") or {}).get("name", "")
        return PROVIDER_PREF.index(name) if name in PROVIDER_PREF else len(PROVIDER_PREF)

    side_key = "homeTeamOdds" if was_home else "awayTeamOdds"
    spread_move = total_move = None
    for it in sorted(items, key=prio):
        if spread_move is None:
            side = it.get(side_key) or {}
            opened = _parse_line((side.get("open") or {}).get("pointSpread"))
            closed = _parse_line((side.get("close") or {}).get("pointSpread"))
            if closed is None:
                closed = _parse_line((side.get("current") or {}).get("pointSpread"))
            if opened is not None and closed is not None:
                spread_move = opened - closed
        if total_move is None:
            t_open = _parse_line((it.get("open") or {}).get("total"))
            t_close = _parse_line((it.get("close") or {}).get("total"))
            if t_close is None:
                t_close = _parse_line((it.get("current") or {}).get("total"))
            if t_open is not None and t_close is not None:
                total_move = t_close - t_open
        if spread_move is not None and total_move is not None:
            break
    return spread_move, total_move


def team_movement(league_key, team_id):
    """Mean open->close movement over the last 5 games, spreads AND totals:
    {'s_mean', 's_n', 's_moves', 't_mean', 't_n', 't_moves'}."""
    last = fetch_last_completed(league_key, team_id)
    s_moves, t_moves = [], []
    for eid, was_home in last:
        sm, tm = fetch_game_move(league_key, eid, was_home)
        if sm is not None:
            s_moves.append(round(sm, 1))
        if tm is not None:
            t_moves.append(round(tm, 1))
    return {
        "s_mean": (sum(s_moves) / len(s_moves)) if s_moves else None,
        "s_n": len(s_moves), "s_moves": s_moves,
        "t_mean": (sum(t_moves) / len(t_moves)) if t_moves else None,
        "t_n": len(t_moves), "t_moves": t_moves,
    }


# ----------------------------------------------------------------------- report

def fmt_signed(x, nd=2):
    if x is None:
        return "  n/a"
    return f"{x:+.{nd}f}"


def fmt_moves(moves):
    return ", ".join(("0" if m == 0 else f"{m:+g}") for m in moves) if moves else "-"


def run(league_keys, window_hours, write_csv):
    now_et = datetime.now(ET)
    print("=" * 78)
    print("LINE MOVEMENT REPORT  --  open->close SPREAD + TOTAL moves, last 5 games per team")
    print(f"Window: next {window_hours}h   Generated {now_et.strftime('%a %b %d %Y %I:%M %p ET')}")
    print("Spread sign: + = market moved TOWARD the team    - = moved AGAINST it")
    print("Total sign:  + = total bet UP toward the OVER    - = DOWN toward the UNDER")
    print("=" * 78)

    csv_rows = []
    any_games = False
    for lk in league_keys:
        cfg = LEAGUES[lk]
        games = fetch_upcoming(lk, window_hours)
        if not games:
            continue
        any_games = True
        print(f"\n{cfg['label']}  ({len(games)} upcoming)")
        print("-" * 78)

        team_ids = sorted({g["away_id"] for g in games} | {g["home_id"] for g in games})
        EMPTY = {"s_mean": None, "s_n": 0, "s_moves": [],
                 "t_mean": None, "t_n": 0, "t_moves": []}
        results = {}
        with ThreadPoolExecutor(max_workers=24) as ex:
            futs = {tid: ex.submit(team_movement, lk, tid) for tid in team_ids}
            for tid, fut in futs.items():
                try:
                    results[tid] = fut.result()
                except Exception:
                    results[tid] = dict(EMPTY)

        for g in games:
            a = results.get(g["away_id"], EMPTY)
            h = results.get(g["home_id"], EMPTY)
            tip = g["tip"].astimezone(ET).strftime("%a %b %d %I:%M %p ET")
            print(f"\n{tip}  --  {g['away_name']} @ {g['home_name']}")
            print(f"  SPREAD  {g['away_name']:<26} avg {fmt_signed(a['s_mean'])}  "
                  f"({a['s_n']}/{LAST_N})   moves: {fmt_moves(a['s_moves'])}")
            print(f"          {g['home_name']:<26} avg {fmt_signed(h['s_mean'])}  "
                  f"({h['s_n']}/{LAST_N})   moves: {fmt_moves(h['s_moves'])}")
            if a["s_mean"] is not None and h["s_mean"] is not None:
                diff = a["s_mean"] - h["s_mean"]
                if abs(diff) < 0.05:
                    print("          DIFF:  0.00 (even -- market moving with both sides equally)")
                else:
                    side = g["away_name"] if diff > 0 else g["home_name"]
                    print(f"          DIFF: {fmt_signed(diff)} (away - home) -> market has been moving toward {side}")
            else:
                diff = None
                print("          DIFF: n/a (insufficient line data)")

            print(f"  TOTAL   {g['away_name']:<26} avg {fmt_signed(a['t_mean'])}  "
                  f"({a['t_n']}/{LAST_N})   moves: {fmt_moves(a['t_moves'])}")
            print(f"          {g['home_name']:<26} avg {fmt_signed(h['t_mean'])}  "
                  f"({h['t_n']}/{LAST_N})   moves: {fmt_moves(h['t_moves'])}")
            if a["t_mean"] is not None and h["t_mean"] is not None:
                t_lean = (a["t_mean"] + h["t_mean"]) / 2.0
                if abs(t_lean) < 0.05:
                    print("          LEAN:  0.00 (totals in these teams' games close where they open)")
                else:
                    d = "UP toward the OVER" if t_lean > 0 else "DOWN toward the UNDER"
                    print(f"          LEAN: {fmt_signed(t_lean)} -> totals in these teams' games get bet {d}")
            else:
                t_lean = None
                print("          LEAN: n/a (insufficient totals data)")

            csv_rows.append([
                cfg["label"], g["tip"].astimezone(ET).strftime("%Y-%m-%d %H:%M"),
                g["away_name"], "" if a["s_mean"] is None else round(a["s_mean"], 2), a["s_n"], fmt_moves(a["s_moves"]),
                g["home_name"], "" if h["s_mean"] is None else round(h["s_mean"], 2), h["s_n"], fmt_moves(h["s_moves"]),
                "" if diff is None else round(diff, 2),
                "" if a["t_mean"] is None else round(a["t_mean"], 2), a["t_n"], fmt_moves(a["t_moves"]),
                "" if h["t_mean"] is None else round(h["t_mean"], 2), h["t_n"], fmt_moves(h["t_moves"]),
                "" if t_lean is None else round(t_lean, 2),
            ])

    if not any_games:
        print("\nNo upcoming games in the window for the selected leagues "
              "(try --days to widen it).")

    if write_csv and csv_rows:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           f"line_movement_{now_et.strftime('%Y-%m-%d')}.csv")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["league", "tip_et", "away", "away_avg_move", "away_n", "away_moves",
                        "home", "home_avg_move", "home_n", "home_moves", "diff_away_minus_home",
                        "away_total_avg", "away_total_n", "away_total_moves",
                        "home_total_avg", "home_total_n", "home_total_moves",
                        "total_lean_combined"])
            w.writerows(csv_rows)
        print(f"\nCSV written: {out}")


# ── Web mode (mounted at /linemoves on the basketball-site) ──────────────────
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
h1{color:#4db6ac;font-size:20px;margin:4px 0 6px}
.note{color:#8aa;font-size:12.5px;margin:4px 0 12px}
a.menu{display:inline-block;color:#4db6ac;text-decoration:none;font-size:13px;
       font-weight:600;border:1px solid #4db6ac;border-radius:6px;
       padding:4px 12px;margin-bottom:8px}
a.menu:hover{background:#4db6ac;color:#10151c}
select,input{background:#0e131a;color:#dfe7ef;border:1px solid #33414f;
             border-radius:4px;padding:5px 7px;font-size:13px}
button{background:#4db6ac;color:#10151c;font-weight:700;border:none;
       border-radius:5px;padding:7px 16px;font-size:13px;cursor:pointer}
button:disabled{opacity:0.5;cursor:wait}
#out{background:#0e131a;border:1px solid #2a3542;border-radius:8px;
     padding:12px 14px;margin-top:12px;font-size:12px;line-height:1.55;
     overflow-x:auto;white-space:pre;color:#dfe7ef;min-height:60px}
label{font-size:11px;color:#9ab;margin-right:4px}
</style><title>Line Movement</title>
<a class='menu' href='/'>&larr; Main Menu</a>
<h1>&#128200; Line Movement</h1>
<div class='note'>Each team's mean open&rarr;close movement over its last 5
completed games &mdash; spreads (+ = market moved TOWARD the team) and totals
(+ = bet UP toward the over) &mdash; plus a matchup DIFF/LEAN for every
upcoming game. Runs automatically; re-run after changing the filters.</div>
<label>League</label><select id='lg'><option value=''>All leagues</option>
__OPTS__</select>
<label style='margin-left:10px'>Window (days)</label>
<input id='days' type='number' value='2' min='1' max='7' style='width:60px'>
<button id='go' onclick='go()' style='margin-left:10px'>Run report</button>
<pre id='out'>Loading&hellip;</pre>
<script>
async function go(){
  const b=document.getElementById('go'),o=document.getElementById('out');
  b.disabled=true;o.textContent='Fetching schedules + stored lines (~20-60s)...';
  const lg=document.getElementById('lg').value,
        d=document.getElementById('days').value;
  try{const r=await fetch('run?league='+lg+'&days='+d,{method:'POST'});
      o.textContent=await r.text();}
  catch(e){o.textContent='Error: '+e;}
  b.disabled=false;
}
go();
</script>"""

    @app.route("/", methods=["GET"])
    def index():
        opts = "".join(f"<option value='{k}'>{k}</option>"
                       for k in sorted(LEAGUES))
        return _PAGE.replace("__OPTS__", opts)

    @app.route("/run", methods=["POST"])
    def run_report():
        if not _run_lock.acquire(blocking=False):
            return Response("A report is already running — give it a minute.",
                            mimetype="text/plain")
        try:
            lg = request.args.get("league") or ""
            leagues = [lg] if lg in LEAGUES else list(LEAGUES)
            try:
                days = min(max(float(request.args.get("days") or 2), 0.5), 7)
            except ValueError:
                days = 2.0
            buf = io.StringIO()
            try:
                with redirect_stdout(buf), redirect_stderr(buf):
                    run(leagues, int(days * 24), False)
            except Exception as exc:
                buf.write(f"\n[ERROR] report failed: {exc!r}")
            return Response(buf.getvalue() or "(no output)",
                            mimetype="text/plain")
        finally:
            _run_lock.release()
except ImportError:                      # console mode works without flask
    app = None


def main():
    ap = argparse.ArgumentParser(description="Open->close line movement report")
    ap.add_argument("--league", choices=sorted(LEAGUES), help="one league only (default: all)")
    ap.add_argument("--days", type=float, default=2, help="upcoming window in days (default 2)")
    ap.add_argument("--csv", action="store_true", help="also write a dated CSV next to the script")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    leagues = [args.league] if args.league else list(LEAGUES)
    run(leagues, int(args.days * 24), args.csv)


if __name__ == "__main__":
    main()
