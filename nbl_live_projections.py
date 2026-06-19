"""
Live Australian NBL Projections Dashboard

Flask web app that shows live NBL games with current scores,
estimated possessions, and projected final scores based on each team's
PPG, OPPG, and estimated pace, with home court advantage adjustment.

Ratings source: ESPN Core API (season PPG/OPPG per team)

Usage:
    py -3 nbl_live_projections.py              # today's games, port 5004
    py -3 nbl_live_projections.py --port 8080  # custom port
    py -3 nbl_live_projections.py --date 2026-04-01  # specific date
"""

import argparse
import os
import json
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template_string, url_for

# ── Constants ──

ET = ZoneInfo("America/New_York")
AEST = ZoneInfo("Australia/Sydney")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues"
LEAGUE_SLUG = "nbl"

# ── PointsBet API (player props) ──
POINTSBET_API = "https://api.au.pointsbet.com"
POINTSBET_NBL_COMP = "7589"  # NBL competition key on PointsBet

# ── NBL game constants ──

REGULATION_MIN = 40.0   # 4 × 10-minute quarters
QUARTER_MIN = 10.0
OT_MIN = 5.0
HALF_POSS_SHARE = 0.49  # ~49% of total possessions in 1H
HCA_POINTS = 3.5        # Home court advantage in points

# ── Blowout regression to the mean ──
BLOWOUT_THRESHOLD = 15
BLOWOUT_MAX_REGRESS = 0.40
BLOWOUT_LEAD_CAP = 30

# ── Global state ──

DATE_OVERRIDE: str | None = None
RATINGS: dict[str, dict] = {}
RATINGS_LOADED_AT: datetime | None = None
NATIONAL_AVG_PPG: float = 91.0

# PointsBet props cache
_props_cache_lock = threading.Lock()
_props_cache: dict[str, tuple[list[dict], float]] = {}  # event_key -> (props, timestamp)
_PROPS_CACHE_TTL = 120  # 2 minutes

app = Flask(__name__)


# ── Data loading ──

def _fetch_json(url: str, timeout: int = 15) -> dict:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_teams() -> list[dict]:
    """Fetch all NBL teams from ESPN."""
    url = f"{ESPN_BASE}/{LEAGUE_SLUG}/teams?limit=100"
    data = _fetch_json(url)
    teams = []
    for entry in data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
        t = entry.get("team", {})
        teams.append({
            "id": t.get("id", ""),
            "location": t.get("location", ""),
            "name": t.get("name", ""),
            "abbreviation": t.get("abbreviation", ""),
            "displayName": t.get("displayName", ""),
        })
    return teams


def fetch_team_record(team_id: str) -> dict | None:
    """Fetch season PPG/OPPG from ESPN Core API."""
    url = f"{ESPN_CORE}/{LEAGUE_SLUG}/seasons/2026/types/2/teams/{team_id}/records/0"
    try:
        data = _fetch_json(url, timeout=10)
    except Exception:
        try:
            url2 = f"{ESPN_CORE}/{LEAGUE_SLUG}/seasons/2025/types/2/teams/{team_id}/records/0"
            data = _fetch_json(url2, timeout=10)
        except Exception:
            return None

    stats = {s.get("name", ""): s.get("value", 0) for s in data.get("stats", [])}
    ppg = stats.get("avgPointsFor", 0)
    oppg = stats.get("avgPointsAgainst", 0)
    if not ppg:
        return None

    pace_est = (ppg + oppg) / 2.0
    return {
        "ppg": round(ppg, 1),
        "oppg": round(oppg, 1),
        "pace": round(pace_est, 1),
        "w": int(stats.get("wins", 0)),
        "l": int(stats.get("losses", 0)),
        "gp": int(stats.get("gamesPlayed", 0)),
    }


def load_all_ratings() -> None:
    """Load ratings for all NBL teams from ESPN."""
    global RATINGS, RATINGS_LOADED_AT, NATIONAL_AVG_PPG

    print("Loading NBL team ratings from ESPN...")
    teams = fetch_teams()
    if not teams:
        print("  [WARN] No teams found.")
        RATINGS_LOADED_AT = datetime.now(ET)
        return

    ratings: dict[str, dict] = {}

    def _fetch_one(team: dict) -> tuple[str, dict | None]:
        name = team["location"] or team["displayName"]
        stats = fetch_team_record(team["id"])
        return name, stats

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_one, t) for t in teams]
        for fut in futures:
            try:
                name, stats = fut.result(timeout=15)
                if stats and name:
                    ratings[name] = stats
            except Exception:
                pass

    # Compute percentiles
    all_ppg = sorted(r["ppg"] for r in ratings.values())
    all_oppg = sorted(r["oppg"] for r in ratings.values())
    all_pace = sorted(r["pace"] for r in ratings.values())
    n = len(ratings)

    for name, r in ratings.items():
        r["ppg_pct"] = int(sum(1 for v in all_ppg if v <= r["ppg"]) / n * 100) if n else 50
        r["oppg_pct"] = 100 - int(sum(1 for v in all_oppg if v <= r["oppg"]) / n * 100) if n else 50
        r["pace_pct"] = int(sum(1 for v in all_pace if v <= r["pace"]) / n * 100) if n else 50

    NATIONAL_AVG_PPG = sum(all_ppg) / len(all_ppg) if all_ppg else 91.0
    RATINGS = ratings
    RATINGS_LOADED_AT = datetime.now(ET)
    print(f"  Got stats for {len(ratings)}/{len(teams)} teams.")
    print(f"  League Avg PPG: {NATIONAL_AVG_PPG:.1f}")


# ── Team resolution ──

def resolve_team(espn_name: str) -> str:
    if espn_name in RATINGS:
        return espn_name
    low = espn_name.lower()
    for key in RATINGS:
        if key.lower() == low or low in key.lower() or key.lower() in low:
            return key
    return espn_name


def pct_class(pct: int) -> str:
    if pct >= 80: return "pct-very-high"
    if pct >= 60: return "pct-high"
    if pct >= 40: return "pct-avg"
    if pct >= 20: return "pct-low"
    return "pct-very-low"


# ── PointsBet player props ──

def _pointsbet_fetch(url: str) -> dict | None:
    """Fetch JSON from PointsBet API."""
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    })
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def fetch_pointsbet_events() -> list[dict]:
    """Fetch all NBL events from PointsBet."""
    url = (f"{POINTSBET_API}/api/v2/competitions/{POINTSBET_NBL_COMP}"
           f"/events/featured?includeLive=true")
    data = _pointsbet_fetch(url)
    if not data:
        return []
    return data.get("events", [])


def fetch_pointsbet_props(event_key: int | str) -> list[dict]:
    """
    Fetch player prop markets for a PointsBet event.
    Returns list of dicts: {player, prop_type, line, over_price, under_price}
    """
    now = time.monotonic()
    key = str(event_key)

    with _props_cache_lock:
        cached = _props_cache.get(key)
        if cached and now - cached[1] < _PROPS_CACHE_TTL:
            return cached[0]

    url = f"{POINTSBET_API}/api/mes/v3/events/{key}"
    data = _pointsbet_fetch(url)
    if not data:
        return []

    markets = data.get("fixedOddsMarkets", [])
    props = []

    for mkt in markets:
        group = mkt.get("groupName", "")
        if group != "Player Props":
            continue

        mkt_name = mkt.get("name", "")
        # Determine prop type from market name
        prop_type = None
        if "Points" in mkt_name and "Rebound" not in mkt_name and "Assist" not in mkt_name:
            prop_type = "PTS"
        elif "Rebound" in mkt_name and "Point" not in mkt_name:
            prop_type = "REB"
        elif "Assist" in mkt_name and "Point" not in mkt_name:
            prop_type = "AST"
        elif "Points + Rebounds" in mkt_name or "Pts + Reb" in mkt_name:
            prop_type = "P+R"
        elif "Points + Assists" in mkt_name or "Pts + Ast" in mkt_name:
            prop_type = "P+A"
        elif "Rebounds + Assists" in mkt_name or "Reb + Ast" in mkt_name:
            prop_type = "R+A"
        elif "Points + Rebounds + Assists" in mkt_name or "PRA" in mkt_name:
            prop_type = "PRA"
        elif "Three" in mkt_name or "3-Pointer" in mkt_name or "3PM" in mkt_name:
            prop_type = "3PM"
        elif "Steal" in mkt_name:
            prop_type = "STL"
        elif "Block" in mkt_name:
            prop_type = "BLK"
        else:
            prop_type = mkt_name  # fallback to raw name

        outcomes = mkt.get("outcomes", [])
        if len(outcomes) < 2:
            continue

        # Over/Under pair: outcomes[0] = Over, outcomes[1] = Under
        over = under = None
        for o in outcomes:
            name_low = o.get("name", "").lower()
            if "over" in name_low:
                over = o
            elif "under" in name_low:
                under = o
        if not over or not under:
            # Fallback: first = over, second = under
            over, under = outcomes[0], outcomes[1]

        player_name = over.get("name", "").replace("Over ", "").replace("Under ", "")
        # Extract from the outcome name or the player field
        if over.get("playerId"):
            # Sometimes player name is separate
            pass
        # Clean up player name — remove Over/Under prefix
        for prefix in ["Over ", "Under ", "over ", "under "]:
            player_name = player_name.replace(prefix, "")

        line = over.get("points", 0) or under.get("points", 0)
        if not line:
            # Try to get from market name (e.g. "Player Points Over/Under 19.5")
            m = re.search(r"([\d.]+)\s*$", mkt_name)
            if m:
                line = float(m.group(1))

        props.append({
            "player": player_name.strip(),
            "prop_type": prop_type,
            "line": float(line) if line else 0,
            "over_price": over.get("price", 0),
            "under_price": under.get("price", 0),
        })

    with _props_cache_lock:
        _props_cache[key] = (props, now)

    return props


def match_pointsbet_event(game: dict, pb_events: list[dict]) -> dict | None:
    """Match an ESPN game to a PointsBet event by team names."""
    away = game["away_name"].lower()
    home = game["home_name"].lower()
    for ev in pb_events:
        ev_name = ev.get("name", "").lower()
        ev_home = ev.get("homeTeam", "").lower() if ev.get("homeTeam") else ""
        ev_away = ev.get("awayTeam", "").lower() if ev.get("awayTeam") else ""
        # Match by team name substring
        if ((away in ev_away or ev_away in away or away in ev_name) and
                (home in ev_home or ev_home in home or home in ev_name)):
            return ev
        # Also try short names
        away_parts = away.split()
        home_parts = home.split()
        if any(p in ev_name for p in away_parts if len(p) > 3) and \
           any(p in ev_name for p in home_parts if len(p) > 3):
            return ev
    return None


# ── ESPN scoreboard ──

def _get_linescore(competitor: dict, period_index: int) -> int | None:
    ls = competitor.get("linescores", [])
    if period_index < len(ls):
        val = ls[period_index].get("value", None)
        if val is not None:
            return int(val)
    return None


def fetch_scoreboard(date_str: str) -> list[dict]:
    """Fetch all NBL games for a given date."""
    url = f"{ESPN_BASE}/{LEAGUE_SLUG}/scoreboard?dates={date_str}"
    try:
        data = _fetch_json(url)
    except Exception as e:
        print(f"  [WARN] Scoreboard fetch failed: {e}")
        return []

    games = []
    for event in data.get("events", []):
        comp = event["competitions"][0]
        status = comp["status"]

        home = away = None
        for c in comp["competitors"]:
            if c["homeAway"] == "home":
                home = c
            else:
                away = c
        if not home or not away:
            continue

        utc_str = event["date"]
        utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        et_dt = utc_dt.astimezone(ET)
        aest_dt = utc_dt.astimezone(AEST)
        _t_et = (et_dt.strftime("%#I:%M %p") if sys.platform == "win32"
                 else et_dt.strftime("%-I:%M %p"))
        _base_date = (datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").date()
                      if DATE_OVERRIDE else datetime.now(ET).date())
        time_str_et = _t_et if et_dt.date() == _base_date else (et_dt.strftime("%a ") + _t_et)
        time_str_aest = (
            aest_dt.strftime("%#I:%M %p") if sys.platform == "win32"
            else aest_dt.strftime("%-I:%M %p")
        )

        # 1H score = Q1 + Q2
        away_q1 = _get_linescore(away, 0)
        away_q2 = _get_linescore(away, 1)
        home_q1 = _get_linescore(home, 0)
        home_q2 = _get_linescore(home, 1)
        away_1h = (away_q1 + away_q2) if away_q1 is not None and away_q2 is not None else None
        home_1h = (home_q1 + home_q2) if home_q1 is not None and home_q2 is not None else None

        neutral = comp.get("neutralSite", False)

        game = {
            "game_id": event["id"],
            "state": status["type"]["state"],
            "clock_seconds": status.get("clock", 0) or 0,
            "display_clock": status.get("displayClock", "0:00"),
            "period": status.get("period", 0),
            "status_detail": status["type"].get("shortDetail", ""),
            "away_name": away["team"].get("location", away["team"].get("shortDisplayName", "Away")),
            "away_abbrev": away["team"].get("abbreviation", ""),
            "away_score": int(away.get("score", "0") or "0"),
            "away_logo": away["team"].get("logo", ""),
            "home_name": home["team"].get("location", home["team"].get("shortDisplayName", "Home")),
            "home_abbrev": home["team"].get("abbreviation", ""),
            "home_score": int(home.get("score", "0") or "0"),
            "home_logo": home["team"].get("logo", ""),
            "away_1h_score": away_1h,
            "home_1h_score": home_1h,
            "neutral_site": neutral,
            "start_time_str": time_str_et,
            "start_time_aest": time_str_aest,
            "start_time_sort": et_dt.hour * 100 + et_dt.minute,
            "start_epoch": et_dt.timestamp(),
        }
        games.append(game)

    return games


# ── Projection engine ──

def project_game(game: dict) -> dict:
    """Compute projections for a single game."""
    away_key = resolve_team(game["away_name"])
    home_key = resolve_team(game["home_name"])

    default = {"ppg": NATIONAL_AVG_PPG, "oppg": NATIONAL_AVG_PPG, "pace": NATIONAL_AVG_PPG,
               "ppg_pct": 50, "oppg_pct": 50, "pace_pct": 50}
    away_r = RATINGS.get(away_key, default)
    home_r = RATINGS.get(home_key, default)

    has_away = away_key in RATINGS
    has_home = home_key in RATINGS

    away_ppg = away_r["ppg"]
    away_oppg = away_r["oppg"]
    away_pace = away_r["pace"]
    home_ppg = home_r["ppg"]
    home_oppg = home_r["oppg"]
    home_pace = home_r["pace"]

    # Game pace
    game_pace = (away_pace + home_pace) / 2.0

    # Opponent-adjusted projected scoring
    avg_ppg = NATIONAL_AVG_PPG if NATIONAL_AVG_PPG > 0 else 91.0
    away_proj_full = (away_ppg * home_oppg) / avg_ppg
    home_proj_full = (home_ppg * away_oppg) / avg_ppg

    # HCA adjustment (0 for neutral site)
    hca = 0.0 if game.get("neutral_site") else HCA_POINTS
    away_proj_full -= hca / 2.0
    home_proj_full += hca / 2.0

    # Time calculations
    period = game["period"]
    clock_sec = float(game["clock_seconds"])
    state = game["state"]
    detail = game.get("status_detail", "").lower()

    if state == "pre":
        time_elapsed = 0.0
        total_game_min = REGULATION_MIN
    elif state == "post":
        if period <= 4:
            total_game_min = REGULATION_MIN
        else:
            total_game_min = REGULATION_MIN + OT_MIN * (period - 4)
        time_elapsed = total_game_min
    else:
        clock_min = clock_sec / 60.0
        if "halftime" in detail or ("half" in detail and clock_min < 0.1):
            time_elapsed = REGULATION_MIN / 2.0
            total_game_min = REGULATION_MIN
        elif "end" in detail and period <= 4:
            time_elapsed = period * QUARTER_MIN
            total_game_min = REGULATION_MIN
        elif period <= 4:
            time_elapsed = (period - 1) * QUARTER_MIN + (QUARTER_MIN - clock_min)
            total_game_min = REGULATION_MIN
        else:
            ot_num = period - 4
            time_elapsed = REGULATION_MIN + (ot_num - 1) * OT_MIN + (OT_MIN - clock_min)
            total_game_min = REGULATION_MIN + OT_MIN * ot_num

    time_remaining = max(0.0, total_game_min - time_elapsed)

    # Points per minute rates
    away_ppm = away_proj_full / REGULATION_MIN
    home_ppm = home_proj_full / REGULATION_MIN

    # Blowout regression
    blowout_regress = 0.0
    if state == "in" and time_elapsed > 0:
        lead = abs(game["home_score"] - game["away_score"])
        if lead >= BLOWOUT_THRESHOLD:
            lead_frac = min((lead - BLOWOUT_THRESHOLD) / (BLOWOUT_LEAD_CAP - BLOWOUT_THRESHOLD), 1.0)
            time_frac = time_remaining / REGULATION_MIN
            blowout_regress = BLOWOUT_MAX_REGRESS * lead_frac * time_frac

    league_avg_ppm = avg_ppg / REGULATION_MIN
    if game["away_score"] < game["home_score"]:
        away_ppm_adj = away_ppm + blowout_regress * (league_avg_ppm - away_ppm)
        home_ppm_adj = home_ppm - blowout_regress * (home_ppm - league_avg_ppm)
    elif game["away_score"] > game["home_score"]:
        away_ppm_adj = away_ppm - blowout_regress * (away_ppm - league_avg_ppm)
        home_ppm_adj = home_ppm + blowout_regress * (league_avg_ppm - home_ppm)
    else:
        away_ppm_adj = away_ppm
        home_ppm_adj = home_ppm

    # Projected remaining
    away_proj_remaining = time_remaining * away_ppm_adj
    home_proj_remaining = time_remaining * home_ppm_adj

    # HCA remaining adjustment
    hca_remaining = (hca / 2.0) * (time_remaining / REGULATION_MIN) if state == "in" else 0.0

    away_final = game["away_score"] + away_proj_remaining
    home_final = game["home_score"] + home_proj_remaining

    proj_total = away_final + home_final
    proj_spread = home_final - away_final

    # 1H projection
    half_min = REGULATION_MIN / 2.0
    if state == "pre":
        away_1h_proj = half_min * (away_proj_full / REGULATION_MIN)
        home_1h_proj = half_min * (home_proj_full / REGULATION_MIN)
    elif period <= 2 and state == "in":
        if period == 1:
            elapsed_1h = QUARTER_MIN - (clock_sec / 60.0)
        else:
            elapsed_1h = QUARTER_MIN + (QUARTER_MIN - (clock_sec / 60.0))
        remaining_1h = max(half_min - elapsed_1h, 0.0)
        away_1h_proj = game["away_score"] + remaining_1h * away_ppm_adj
        home_1h_proj = game["home_score"] + remaining_1h * home_ppm_adj
    else:
        a1h = game.get("away_1h_score")
        h1h = game.get("home_1h_score")
        if a1h is not None and h1h is not None:
            away_1h_proj = float(a1h)
            home_1h_proj = float(h1h)
        else:
            away_1h_proj = half_min * (away_proj_full / REGULATION_MIN)
            home_1h_proj = half_min * (home_proj_full / REGULATION_MIN)

    proj_1h_total = away_1h_proj + home_1h_proj
    proj_1h_spread = home_1h_proj - away_1h_proj
    h1_is_actual = (state != "pre" and period >= 3 and game.get("away_1h_score") is not None)

    # Possessions estimate
    poss_per_min = game_pace / REGULATION_MIN
    poss_so_far = poss_per_min * time_elapsed
    poss_remaining = poss_per_min * time_remaining

    return {
        **game,
        "has_away_data": has_away,
        "has_home_data": has_home,
        "away_ppg": round(away_ppg, 1),
        "away_oppg": round(away_oppg, 1),
        "away_pace": round(away_pace, 1),
        "home_ppg": round(home_ppg, 1),
        "home_oppg": round(home_oppg, 1),
        "home_pace": round(home_pace, 1),
        "away_ppg_pct": away_r.get("ppg_pct", 50),
        "away_oppg_pct": away_r.get("oppg_pct", 50),
        "away_pace_pct": away_r.get("pace_pct", 50),
        "home_ppg_pct": home_r.get("ppg_pct", 50),
        "home_oppg_pct": home_r.get("oppg_pct", 50),
        "home_pace_pct": home_r.get("pace_pct", 50),
        "away_ppg_cls": pct_class(away_r.get("ppg_pct", 50)),
        "away_oppg_cls": pct_class(away_r.get("oppg_pct", 50)),
        "away_pace_cls": pct_class(away_r.get("pace_pct", 50)),
        "home_ppg_cls": pct_class(home_r.get("ppg_pct", 50)),
        "home_oppg_cls": pct_class(home_r.get("oppg_pct", 50)),
        "home_pace_cls": pct_class(home_r.get("pace_pct", 50)),
        "game_pace": round(game_pace, 1),
        "time_elapsed": round(time_elapsed, 1),
        "time_remaining": round(time_remaining, 1),
        "poss_so_far": round(poss_so_far, 1),
        "poss_remaining": round(poss_remaining, 1),
        "away_proj_remaining": round(away_proj_remaining, 1),
        "home_proj_remaining": round(home_proj_remaining, 1),
        "away_final": round(away_final, 1),
        "home_final": round(home_final, 1),
        "away_full_proj": round(away_proj_full, 1),
        "home_full_proj": round(home_proj_full, 1),
        "proj_total": round(proj_total, 1),
        "proj_spread": round(proj_spread, 1),
        "away_1h_proj": round(away_1h_proj, 1),
        "home_1h_proj": round(home_1h_proj, 1),
        "proj_1h_total": round(proj_1h_total, 1),
        "proj_1h_spread": round(proj_1h_spread, 1),
        "h1_is_actual": h1_is_actual,
        "blowout_regress": round(blowout_regress * 100, 1),
        "hca_display": round(hca / 2.0, 1),
    }


# ── HTML template ──

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="theme-color" content="#0f1923">
<title>NBL Live Projections</title>
<style>
  :root {
    --bg: #0f1923;
    --card-bg: #1a2634;
    --card-border: #2a3a4a;
    --text: #e8edf2;
    --text-muted: #8899aa;
    --accent: #fdcb6e;
    --green: #4caf50;
    --blue: #2196f3;
    --amber: #ffc107;
    --header-bg: #0a1218;
    --gold: #fdcb6e;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }
  header {
    background: var(--header-bg);
    border-bottom: 2px solid var(--gold);
    padding: 16px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
  }
  header h1 { font-size: 1.4em; font-weight: 700; }
  header h1 span { color: var(--gold); }
  .header-meta {
    font-size: 0.8em;
    color: var(--text-muted);
    display: flex;
    gap: 16px;
    align-items: center;
    flex-wrap: wrap;
  }
  .header-meta a {
    color: var(--blue);
    text-decoration: none;
    border: 1px solid var(--blue);
    padding: 2px 10px;
    border-radius: 4px;
    font-size: 0.9em;
  }
  .header-meta a:hover { background: rgba(33,150,243,0.15); }
  #countdown-wrap {
    background: rgba(253,203,110,0.15);
    color: var(--gold);
    padding: 2px 10px;
    border-radius: 4px;
    font-weight: 600;
    font-size: 0.9em;
  }
  .container { max-width: 1100px; margin: 0 auto; padding: 16px; }

  .section-header {
    font-size: 1.1em;
    font-weight: 700;
    padding: 10px 14px;
    margin: 20px 0 8px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    gap: 10px;
    border-bottom: 2px solid var(--gold);
  }
  .section-header.upcoming { border-bottom-color: var(--blue); }
  .section-header.completed { border-bottom-color: var(--text-muted); }
  .count {
    background: var(--gold);
    color: #000;
    font-size: 0.75em;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 700;
  }
  .section-header.upcoming .count { background: var(--blue); color: #fff; }
  .section-header.completed .count { background: var(--text-muted); color: #000; }
  .toggle-btn {
    margin-left: auto;
    background: none;
    border: 1px solid var(--text-muted);
    color: var(--text-muted);
    padding: 2px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75em;
  }

  /* Game cards */
  .game-card {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 10px;
    margin-bottom: 14px;
    padding: 14px 18px;
    position: relative;
  }
  .game-card .status-tag {
    position: absolute;
    top: 10px;
    right: 14px;
    font-size: 0.75em;
    font-weight: 700;
    color: var(--gold);
  }
  .neutral-tag {
    font-size: 0.65em;
    background: var(--amber);
    color: #000;
    padding: 1px 6px;
    border-radius: 4px;
    margin-left: 6px;
    font-weight: 700;
  }
  .teams-row {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 16px;
    margin-bottom: 10px;
  }
  .team-side {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 180px;
  }
  .team-side.away { justify-content: flex-end; text-align: right; }
  .team-side.home { justify-content: flex-start; text-align: left; }
  .team-logo { width: 36px; height: 36px; object-fit: contain; }
  .team-name { font-weight: 700; font-size: 1.05em; }
  .score-block {
    text-align: center;
    min-width: 100px;
  }
  .score-display {
    font-size: 1.8em;
    font-weight: 800;
    letter-spacing: 2px;
    font-variant-numeric: tabular-nums;
  }
  .score-display .winning { color: var(--gold); }
  .clock-display {
    font-size: 0.8em;
    color: var(--text-muted);
    margin-top: 2px;
  }

  /* Projection row */
  .proj-row {
    display: flex;
    justify-content: center;
    gap: 18px;
    flex-wrap: wrap;
    margin: 8px 0;
    padding: 8px 0;
    border-top: 1px solid var(--card-border);
  }
  .proj-stat { text-align: center; }
  .proj-label {
    font-size: 0.65em;
    text-transform: uppercase;
    color: var(--text-muted);
    letter-spacing: 0.5px;
  }
  .proj-val {
    font-size: 1.1em;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .spread-home { color: var(--green); }
  .spread-away { color: #ff6666; }

  /* Detail row */
  .detail-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    font-size: 0.78em;
    color: var(--text-muted);
    border-top: 1px solid var(--card-border);
    padding-top: 8px;
    margin-top: 4px;
    flex-wrap: wrap;
    gap: 6px;
  }
  .detail-team { flex: 1; min-width: 200px; }
  .detail-center { text-align: center; flex: 0 0 auto; }
  .pct-badge {
    display: inline-block;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 0.9em;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .pct-very-high { background: rgba(255,102,102,0.25); color: #ff6666; }
  .pct-high { background: rgba(255,170,51,0.25); color: #ffaa33; }
  .pct-avg { background: rgba(255,193,7,0.2); color: #ffc107; }
  .pct-low { background: rgba(102,187,106,0.25); color: #66bb6a; }
  .pct-very-low { background: rgba(100,181,246,0.25); color: #64b5f6; }

  /* Tables */
  .table-wrap { overflow-x: auto; }
  .compact-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82em;
    font-variant-numeric: tabular-nums;
    min-width: 900px;
  }
  .compact-table th {
    text-transform: uppercase;
    font-size: 0.8em;
    color: var(--text-muted);
    padding: 6px 8px;
    border-bottom: 1px solid var(--card-border);
    white-space: nowrap;
  }
  .compact-table td {
    padding: 5px 8px;
    border-bottom: 1px solid rgba(42,58,74,0.5);
    white-space: nowrap;
  }
  .compact-table tr:hover { background: rgba(255,255,255,0.03); }
  .team-cell { font-weight: 600; }

  .hidden { display: none; }
  .no-games {
    text-align: center;
    color: var(--text-muted);
    padding: 30px;
    font-size: 1.1em;
  }
  .rtm-badge {
    font-size: 0.7em;
    background: rgba(255,193,7,0.2);
    color: var(--amber);
    padding: 1px 6px;
    border-radius: 4px;
  }
  .error-banner {
    background: rgba(255,68,68,0.15);
    color: #ff4444;
    padding: 8px 14px;
    border-radius: 6px;
    margin-bottom: 12px;
    font-size: 0.85em;
  }
  /* Props section */
  .props-section {
    margin-top: 8px;
    border-top: 1px solid var(--card-border);
    padding-top: 4px;
  }
  .props-header {
    font-size: 0.78em;
    color: var(--gold);
    cursor: pointer;
    padding: 4px 0;
    user-select: none;
  }
  .props-header:hover { text-decoration: underline; }
  .props-body { display: none; }
  .props-section.open .props-body { display: block; }
  .props-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.78em;
    margin-top: 4px;
  }
  .props-table th {
    text-align: left;
    color: var(--text-muted);
    padding: 3px 8px;
    border-bottom: 1px solid var(--card-border);
    font-weight: 600;
  }
  .props-table td {
    padding: 3px 8px;
    border-bottom: 1px solid rgba(42,58,74,0.4);
  }
  .over-price { color: var(--green); }
  .under-price { color: #ff6b6b; }
  .props-row td { padding: 0; border: none; }
  .props-row .props-section { margin: 0 12px 8px 12px; border-top: none; }
  @media (max-width: 700px) {
    .teams-row { flex-direction: column; gap: 6px; }
    .team-side { min-width: auto; justify-content: center !important; }
    .proj-row { gap: 10px; }
    .detail-row { flex-direction: column; }
  }
</style>
</head>
<body>
<header>
  <h1><span>NBL</span> Live Projections</h1>
  <div class="header-meta">
    <a href="/" style="font-weight:600">&larr; Main Menu</a>
    <span>{{ date_display }}</span>
    <span>{{ total_games }} game{{ "s" if total_games != 1 }}</span>
    <span>HCA: &plusmn;{{ "%.1f"|format(hca_half) }} pts/side</span>
    <span>RTM: {{ blowout_threshold }}+ pt lead</span>
    <a href="refresh">Refresh Ratings</a>
    <span id="countdown-wrap">Next update: <span id="countdown">30</span>s</span>
  </div>
</header>

<div class="container">
  {% if error %}
  <div class="error-banner">{{ error }}</div>
  {% endif %}

  <!-- LIVE GAMES -->
  <div class="section-header">
    Live Games <span class="count" id="live-count">{{ live|length }}</span>
  </div>
  <div id="live-container">
  {% include "live_partial" %}
  </div>

  <!-- UPCOMING -->
  <div class="section-header upcoming">
    Upcoming <span class="count" id="upcoming-count">{{ upcoming|length }}</span>
    <button class="toggle-btn" onclick="toggleSection('upcoming')">show/hide</button>
  </div>
  <div id="upcoming-container">
  {% include "upcoming_partial" %}
  </div>

  <!-- COMPLETED -->
  <div class="section-header completed">
    Completed <span class="count" id="completed-count">{{ completed|length }}</span>
    <button class="toggle-btn" onclick="toggleSection('completed')">show/hide</button>
  </div>
  <div id="completed-container" class="hidden">
  {% include "completed_partial" %}
  </div>
</div>

<script>
const LIVE_INTERVAL = 30;
const IDLE_INTERVAL = 300;
let interval = {{ live|length }} > 0 ? LIVE_INTERVAL : IDLE_INTERVAL;
let countdown = interval;

function toggleSection(id) {
  const el = document.getElementById(id + '-container');
  if (el) el.classList.toggle('hidden');
}

function tick() {
  countdown--;
  const el = document.getElementById('countdown');
  if (el) el.textContent = countdown;
  if (countdown <= 0) {
    countdown = interval;
    fetch('api/games').then(r => r.json()).then(data => {
      document.getElementById('live-container').innerHTML = data.live_html;
      document.getElementById('upcoming-container').innerHTML = data.upcoming_html;
      document.getElementById('completed-container').innerHTML = data.completed_html;
      const lc = document.getElementById('live-count');
      const uc = document.getElementById('upcoming-count');
      const cc = document.getElementById('completed-count');
      if (lc) lc.textContent = data.live_count;
      if (uc) uc.textContent = data.upcoming_count;
      if (cc) cc.textContent = data.completed_count;
      interval = data.live_count > 0 ? LIVE_INTERVAL : IDLE_INTERVAL;
      countdown = interval;
    }).catch(() => { countdown = interval; });
  }
}
setInterval(tick, 1000);
</script>
</body>
</html>
"""

LIVE_PARTIAL = r"""{% if games %}
  {% for g in games %}
  <div class="game-card">
    <div class="status-tag">
      {{ g.status_detail }}
      {% if g.neutral_site %}<span class="neutral-tag">NEUTRAL</span>{% endif %}
    </div>
    <div class="teams-row">
      <div class="team-side away">
        <span class="team-name">{{ g.away_name }}{{ "" if g.has_away_data else " &#9888;" }}</span>
        {% if g.away_logo %}<img class="team-logo" src="{{ g.away_logo }}" alt="">{% endif %}
      </div>
      <div class="score-block">
        <div class="score-display">
          <span class="{{ 'winning' if g.away_score > g.home_score else '' }}">{{ g.away_score }}</span>
          &ndash;
          <span class="{{ 'winning' if g.home_score > g.away_score else '' }}">{{ g.home_score }}</span>
        </div>
        <div class="clock-display">Q{{ g.period }} &middot; {{ g.display_clock }}</div>
      </div>
      <div class="team-side home">
        {% if g.home_logo %}<img class="team-logo" src="{{ g.home_logo }}" alt="">{% endif %}
        <span class="team-name">{{ g.home_name }}{{ "" if g.has_home_data else " &#9888;" }}</span>
      </div>
    </div>
    <div class="proj-row">
      <div class="proj-stat">
        <div class="proj-label">Poss</div>
        <div class="proj-val">{{ g.poss_so_far }} / {{ g.poss_remaining }}</div>
      </div>
      <div class="proj-stat">
        <div class="proj-label">{{ "1H Actual" if g.h1_is_actual else "1H Proj" }}</div>
        <div class="proj-val">{{ g.away_1h_proj|int }} - {{ g.home_1h_proj|int }}</div>
      </div>
      <div class="proj-stat">
        <div class="proj-label">1H Total</div>
        <div class="proj-val">{{ g.proj_1h_total|int }}</div>
      </div>
      <div class="proj-stat">
        <div class="proj-label">Proj Final</div>
        <div class="proj-val">{{ g.away_final|int }} - {{ g.home_final|int }}</div>
      </div>
      <div class="proj-stat">
        <div class="proj-label">Spread</div>
        <div class="proj-val {{ 'spread-home' if g.proj_spread > 0 else 'spread-away' }}">
          {{ "H" if g.proj_spread > 0 else "A" }} {{ "%.1f"|format(g.proj_spread|abs) }}
        </div>
      </div>
      <div class="proj-stat">
        <div class="proj-label">Total</div>
        <div class="proj-val">{{ g.proj_total|int }}</div>
      </div>
    </div>
    <div class="detail-row">
      <div class="detail-team">
        {{ g.away_name }}: PPG <span class="pct-badge {{ g.away_ppg_cls }}">{{ g.away_ppg }}</span>
        OPPG <span class="pct-badge {{ g.away_oppg_cls }}">{{ g.away_oppg }}</span>
        Pace <span class="pct-badge {{ g.away_pace_cls }}">{{ g.away_pace }}</span>
      </div>
      <div class="detail-center">
        Game Pace: {{ g.game_pace }}
        {% if g.hca_display > 0 %} &middot; HCA: &plusmn;{{ g.hca_display }}{% endif %}
        {% if g.blowout_regress > 0 %}<span class="rtm-badge">RTM {{ g.blowout_regress }}%</span>{% endif %}
      </div>
      <div class="detail-team" style="text-align:right;">
        {{ g.home_name }}: PPG <span class="pct-badge {{ g.home_ppg_cls }}">{{ g.home_ppg }}</span>
        OPPG <span class="pct-badge {{ g.home_oppg_cls }}">{{ g.home_oppg }}</span>
        Pace <span class="pct-badge {{ g.home_pace_cls }}">{{ g.home_pace }}</span>
      </div>
    </div>
    {% if g.props %}
    <div class="props-section">
      <div class="props-header" onclick="this.parentElement.classList.toggle('open')">
        Player Props ({{ g.props|length }}) &#9660;
      </div>
      <div class="props-body">
        <table class="props-table">
          <tr><th>Player</th><th>Prop</th><th>Line</th><th>Over</th><th>Under</th></tr>
          {% for p in g.props %}
          <tr>
            <td>{{ p.player }}</td>
            <td>{{ p.prop_type }}</td>
            <td>{{ p.line }}</td>
            <td class="over-price">{{ "%.2f"|format(p.over_price) }}</td>
            <td class="under-price">{{ "%.2f"|format(p.under_price) }}</td>
          </tr>
          {% endfor %}
        </table>
      </div>
    </div>
    {% endif %}
  </div>
  {% endfor %}
{% else %}
  <div class="no-games">No live games right now</div>
{% endif %}
"""

UPCOMING_PARTIAL = r"""{% if upcoming %}
  <div class="table-wrap">
  <table class="compact-table">
    <tr>
      <th>Time (ET)</th><th>Time (AEST)</th>
      <th>Away</th><th>PPG</th><th>OPPG</th><th>Pace</th>
      <th>Home</th><th>PPG</th><th>OPPG</th><th>Pace</th>
      <th>1H Proj</th><th>1H Tot</th><th>Proj Score</th><th>Spread</th><th>Total</th>
    </tr>
    {% for g in upcoming %}
    <tr>
      <td>{{ g.start_time_str }}</td>
      <td>{{ g.start_time_aest }}</td>
      <td class="team-cell">{{ g.away_name }}{{ "" if g.has_away_data else " &#9888;" }}</td>
      <td><span class="pct-badge {{ g.away_ppg_cls }}">{{ g.away_ppg }}</span></td>
      <td><span class="pct-badge {{ g.away_oppg_cls }}">{{ g.away_oppg }}</span></td>
      <td><span class="pct-badge {{ g.away_pace_cls }}">{{ g.away_pace }}</span></td>
      <td class="team-cell">{{ g.home_name }}{{ "" if g.has_home_data else " &#9888;" }}</td>
      <td><span class="pct-badge {{ g.home_ppg_cls }}">{{ g.home_ppg }}</span></td>
      <td><span class="pct-badge {{ g.home_oppg_cls }}">{{ g.home_oppg }}</span></td>
      <td><span class="pct-badge {{ g.home_pace_cls }}">{{ g.home_pace }}</span></td>
      <td>{{ g.away_1h_proj|int }} - {{ g.home_1h_proj|int }}</td>
      <td>{{ g.proj_1h_total|int }}</td>
      <td>{{ g.away_full_proj|int }} - {{ g.home_full_proj|int }}</td>
      <td class="{{ 'spread-home' if g.proj_spread > 0 else 'spread-away' }}">
        {{ "H" if g.proj_spread > 0 else "A" }} {{ "%.1f"|format(g.proj_spread|abs) }}
      </td>
      <td>{{ g.proj_total|int }}</td>
    </tr>
    {% if g.props %}
    <tr class="props-row">
      <td colspan="15">
        <div class="props-section">
          <div class="props-header" onclick="this.parentElement.classList.toggle('open')">
            Player Props ({{ g.props|length }}) &#9660;
          </div>
          <div class="props-body">
            <table class="props-table">
              <tr><th>Player</th><th>Prop</th><th>Line</th><th>Over</th><th>Under</th></tr>
              {% for p in g.props %}
              <tr>
                <td>{{ p.player }}</td>
                <td>{{ p.prop_type }}</td>
                <td>{{ p.line }}</td>
                <td class="over-price">{{ "%.2f"|format(p.over_price) }}</td>
                <td class="under-price">{{ "%.2f"|format(p.under_price) }}</td>
              </tr>
              {% endfor %}
            </table>
          </div>
        </div>
      </td>
    </tr>
    {% endif %}
    {% endfor %}
  </table>
  </div>
{% else %}
  <div class="no-games">No upcoming games</div>
{% endif %}
"""

COMPLETED_PARTIAL = r"""{% if completed %}
  <div class="table-wrap">
  <table class="compact-table">
    <tr>
      <th>Away</th><th>Score</th><th>Home</th><th>Score</th><th>Status</th>
    </tr>
    {% for g in completed %}
    <tr>
      <td class="team-cell" style="{{ 'font-weight:800' if g.away_score > g.home_score else '' }}">{{ g.away_name }}</td>
      <td style="{{ 'font-weight:800' if g.away_score > g.home_score else '' }}">{{ g.away_score }}</td>
      <td class="team-cell" style="{{ 'font-weight:800' if g.home_score > g.away_score else '' }}">{{ g.home_name }}</td>
      <td style="{{ 'font-weight:800' if g.home_score > g.away_score else '' }}">{{ g.home_score }}</td>
      <td style="color:var(--text-muted);">{{ g.status_detail }}</td>
    </tr>
    {% endfor %}
  </table>
  </div>
{% else %}
  <div class="no-games">No completed games</div>
{% endif %}
"""


# ── Flask app + caching ──

_cache_lock = threading.Lock()
_cache: dict = {"games": [], "fetched_at": 0.0, "error": None}
CACHE_TTL = 20


# Show every game tipping off within this rolling window (not just "today").
WINDOW_HOURS = 48
WINDOW_DAYS = 3          # calendar days to fetch to cover a rolling 48h window


def get_date_str() -> tuple[str, datetime]:
    if DATE_OVERRIDE:
        dt = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").replace(tzinfo=ET)
    else:
        dt = datetime.now(ET)
    return dt.strftime("%Y%m%d"), dt


def _fetch_scoreboard_cached() -> tuple[list[dict], str | None]:
    now = time.monotonic()
    with _cache_lock:
        if now - _cache["fetched_at"] < CACHE_TTL and _cache["games"]:
            return _cache["games"], _cache["error"]

    _, base_dt = get_date_str()
    error = None
    games, seen = [], set()
    try:
        for _i in range(WINDOW_DAYS):       # fetch enough calendar days to span 48h
            ds = (base_dt + timedelta(days=_i)).strftime("%Y%m%d")
            for g in fetch_scoreboard(ds):
                if g["game_id"] not in seen:
                    seen.add(g["game_id"])
                    games.append(g)
    except Exception as e:
        error = str(e)
        games = _cache.get("games", [])

    with _cache_lock:
        _cache["games"] = games
        _cache["fetched_at"] = time.monotonic()
        _cache["error"] = error

    return games, error


def fetch_and_project() -> tuple[list, list, list, str, str | None]:
    _, dt = get_date_str()
    date_display = dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")

    games, error = _fetch_scoreboard_cached()
    projected = [project_game(g) for g in games]

    # Attach PointsBet player props
    try:
        pb_events = fetch_pointsbet_events()
    except Exception:
        pb_events = []

    if pb_events:
        for g in projected:
            if g["state"] == "post":
                g["props"] = []
                continue
            ev = match_pointsbet_event(g, pb_events)
            if ev:
                g["pointsbet_key"] = ev.get("key")
                try:
                    g["props"] = fetch_pointsbet_props(ev["key"])
                except Exception:
                    g["props"] = []
            else:
                g["props"] = []
    else:
        for g in projected:
            g["props"] = []

    _, _base_dt = get_date_str()
    _cutoff = _base_dt.timestamp() + WINDOW_HOURS * 3600
    live = sorted([g for g in projected if g["state"] == "in"], key=lambda g: g["time_elapsed"], reverse=True)
    upcoming = sorted([g for g in projected if g["state"] == "pre" and g.get("start_epoch", 0) <= _cutoff], key=lambda g: g.get("start_epoch", g["start_time_sort"]))
    completed = sorted([g for g in projected if g["state"] == "post"], key=lambda g: g.get("start_epoch", g["start_time_sort"]), reverse=True)

    return live, upcoming, completed, date_display, error


def _render_with_partials(template: str, **kwargs) -> str:
    rendered = template.replace(
        '{% include "live_partial" %}', LIVE_PARTIAL
    ).replace(
        '{% include "upcoming_partial" %}', UPCOMING_PARTIAL
    ).replace(
        '{% include "completed_partial" %}', COMPLETED_PARTIAL
    )
    return render_template_string(rendered, **kwargs)


@app.route("/")
def index():
    if not RATINGS_LOADED_AT:
        load_all_ratings()

    live, upcoming, completed, date_display, error = fetch_and_project()

    return _render_with_partials(
        HTML_TEMPLATE,
        live=live,
        upcoming=upcoming,
        completed=completed,
        games=live,
        date_display=date_display,
        total_games=len(live) + len(upcoming) + len(completed),
        hca_half=HCA_POINTS / 2.0,
        blowout_threshold=BLOWOUT_THRESHOLD,
        error=error,
    )


@app.route("/api/games")
def api_games():
    if not RATINGS_LOADED_AT:
        load_all_ratings()

    live, upcoming, completed, _, error = fetch_and_project()

    return jsonify({
        "live_html": render_template_string(LIVE_PARTIAL, games=live),
        "upcoming_html": render_template_string(UPCOMING_PARTIAL, upcoming=upcoming),
        "completed_html": render_template_string(COMPLETED_PARTIAL, completed=completed),
        "live_count": len(live),
        "upcoming_count": len(upcoming),
        "completed_count": len(completed),
        "updated_at": datetime.now(ET).strftime("%I:%M:%S %p ET"),
        "error": error,
    })


@app.route("/refresh")
def refresh_ratings():
    try:
        load_all_ratings()
    except Exception as e:
        print(f"  Refresh failed: {e}")
    return redirect(url_for("index"))


# ── Entry point ──

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Australian NBL Live Projections")
    parser.add_argument("--port", type=int, default=5004, help="Port (default: 5004)")
    parser.add_argument("--date", type=str, default=None, help="Date override YYYY-MM-DD")
    args = parser.parse_args()

    if args.date:
        DATE_OVERRIDE = args.date

    print("=" * 52)
    print("  Australian NBL Live Projections Dashboard")
    print("=" * 52)
    print()
    print(f"  Format: 4 x {QUARTER_MIN:.0f}-min quarters, {OT_MIN:.0f}-min OT")
    print(f"  HCA: {HCA_POINTS} pts ({HCA_POINTS/2:.1f} per side)")
    print(f"  Blowout RTM: {BLOWOUT_THRESHOLD}+ pt lead, up to {BLOWOUT_MAX_REGRESS*100:.0f}% at {BLOWOUT_LEAD_CAP}+ pts")
    print()

    load_all_ratings()

    date_str, dt = get_date_str()
    print(f"\nDate: {dt.strftime('%A, %B')} {dt.day}, {dt.year}")
    print(f"Server: http://localhost:{args.port}")
    print("Press Ctrl+C to stop.\n")

    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
