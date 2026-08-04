"""Line Movement Report -- who is the market moving WITH, and who is it moving AGAINST?

For every upcoming game across the basketball leagues, shows each team's mean
open->close SPREAD movement over its last 5 completed games, plus the differential
between the two opponents.

Sign convention (team-oriented):
    move = open_spread - close_spread   (spreads from that team's perspective)
    +  the market moved TOWARD the team (bigger favorite / smaller dog at close)
    -  the market moved AGAINST the team

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
    """open - close spread movement from the team's perspective, or None."""
    cfg = LEAGUES[league_key]
    data = fetch_json(
        f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/"
        f"{cfg['path']}/events/{event_id}/competitions/{event_id}/odds?limit=100"
    )
    items = (data or {}).get("items", [])
    if not items:
        return None

    def prio(it):
        name = (it.get("provider") or {}).get("name", "")
        return PROVIDER_PREF.index(name) if name in PROVIDER_PREF else len(PROVIDER_PREF)

    side_key = "homeTeamOdds" if was_home else "awayTeamOdds"
    for it in sorted(items, key=prio):
        side = it.get(side_key) or {}
        opened = _parse_line((side.get("open") or {}).get("pointSpread"))
        closed = _parse_line((side.get("close") or {}).get("pointSpread"))
        if closed is None:
            closed = _parse_line((side.get("current") or {}).get("pointSpread"))
        if opened is not None and closed is not None:
            return opened - closed
    return None


def team_movement(league_key, team_id):
    """Mean open->close movement over the last 5 games: (mean|None, n, [moves])."""
    last = fetch_last_completed(league_key, team_id)
    moves = []
    for eid, was_home in last:
        mv = fetch_game_move(league_key, eid, was_home)
        if mv is not None:
            moves.append(round(mv, 1))
    if not moves:
        return None, 0, []
    return sum(moves) / len(moves), len(moves), moves


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
    print("LINE MOVEMENT REPORT  --  open->close spread moves, last 5 games per team")
    print(f"Window: next {window_hours}h   Generated {now_et.strftime('%a %b %d %Y %I:%M %p ET')}")
    print("Sign: + = market moved TOWARD the team by close   - = moved AGAINST it")
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
        results = {}
        with ThreadPoolExecutor(max_workers=24) as ex:
            futs = {tid: ex.submit(team_movement, lk, tid) for tid in team_ids}
            for tid, fut in futs.items():
                try:
                    results[tid] = fut.result()
                except Exception:
                    results[tid] = (None, 0, [])

        for g in games:
            a_mean, a_n, a_moves = results.get(g["away_id"], (None, 0, []))
            h_mean, h_n, h_moves = results.get(g["home_id"], (None, 0, []))
            tip = g["tip"].astimezone(ET).strftime("%a %b %d %I:%M %p ET")
            print(f"\n{tip}  --  {g['away_name']} @ {g['home_name']}")
            print(f"  {g['away_name']:<26} avg {fmt_signed(a_mean)}  ({a_n}/{LAST_N})   moves: {fmt_moves(a_moves)}")
            print(f"  {g['home_name']:<26} avg {fmt_signed(h_mean)}  ({h_n}/{LAST_N})   moves: {fmt_moves(h_moves)}")
            if a_mean is not None and h_mean is not None:
                diff = a_mean - h_mean
                if abs(diff) < 0.05:
                    print("  DIFF:  0.00 (even -- market moving with both sides equally)")
                else:
                    side = g["away_name"] if diff > 0 else g["home_name"]
                    print(f"  DIFF: {fmt_signed(diff)} (away - home) -> market has been moving toward {side}")
            else:
                diff = None
                print("  DIFF: n/a (insufficient line data)")
            csv_rows.append([
                cfg["label"], g["tip"].astimezone(ET).strftime("%Y-%m-%d %H:%M"),
                g["away_name"], "" if a_mean is None else round(a_mean, 2), a_n, fmt_moves(a_moves),
                g["home_name"], "" if h_mean is None else round(h_mean, 2), h_n, fmt_moves(h_moves),
                "" if diff is None else round(diff, 2),
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
                        "home", "home_avg_move", "home_n", "home_moves", "diff_away_minus_home"])
            w.writerows(csv_rows)
        print(f"\nCSV written: {out}")


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
