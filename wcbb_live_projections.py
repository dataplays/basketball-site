"""
Live Women's College Basketball Projections Dashboard

Flask web app that shows live NCAA women's basketball games with current scores,
estimated possessions, and projected final scores based on each team's
pace, offensive efficiency (OE), and defensive efficiency (DE),
including a home court advantage adjustment.

Women's CBB uses 4 × 10-minute quarters (not 2 × 20-minute halves).
Foul bonus triggers at 5 fouls per quarter (2 free throws, no 1-and-1).

Usage:
    py -3 wcbb_live_projections.py              # today's games, port 5002
    py -3 wcbb_live_projections.py --port 8080  # custom port
    py -3 wcbb_live_projections.py --date 2026-03-01  # specific date
"""

import argparse
import os
import csv
import json
import re
import sys
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template_string, url_for

# ── Paths & constants ──

PACE_CSV = (Path(os.environ.get("BBALL_DATA_DIR", str(Path(__file__).resolve().parent / "data"))) / "wcbb_pace_ratings_2026.csv")
ET = ZoneInfo("America/New_York")

ESPN_API = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/"
    "womens-college-basketball/scoreboard"
)
WN_OE_URL = "https://www.warrennolan.com/basketballw/2026/stats-adv-offensive-rating"
WN_DE_URL = "https://www.warrennolan.com/basketballw/2026/stats-adv-defensive-rating"

REGULATION_MIN = 40.0     # 4 × 10-minute quarters
QUARTER_MIN = 10.0
OT_MIN = 5.0
HALF_POSS_SHARE = 0.4725  # ~47.25% of total possessions in 1H (Q1+Q2)
HCA_POINTS = 3.5          # Home court advantage in points

# ── Regression to the mean (blowout adjustment) ──
# When a team builds a big lead, benches starters / runs clock, so remaining
# scoring regresses toward league-average PPP. Tuned for women's CBB scoring
# (~68 pts/team, 40-min game): onset a touch earlier than men's, capped lower.
BLOWOUT_THRESHOLD = 12   # point lead where regression kicks in
BLOWOUT_MAX_REGRESS = 0.35  # max fraction to pull PPP toward league avg
BLOWOUT_LEAD_CAP = 28    # leads beyond this get max regression

# ── Live box-score pace estimate ──
#   Poss = FGA - ORB + TOV + 0.44 x FTA  (per team, averaged across both)
FT_POSS_COEF = 0.44
LIVE_PACE_MIN_ELAPSED = 3.0  # min game-minutes before extrapolating

# ── Team name mappings (ESPN → WarrenNolan) ──

ESPN_TO_WN = {
    "American University": "American",
    "App State": "Appalachian State",
    "Florida Gulf Coast": "FGCU",
    "Florida International": "FIU",
    "Grambling": "Grambling State",
    "Hawai'i": "Hawaii",
    "Kansas City": "UMKC",
    "Long Island University": "Long Island",
    "Loyola Marymount": "Loyola-Marymount",
    "Miami": "Miami (FL)",
    "NC State": "North Carolina State",
    "Pennsylvania": "Penn",
    "Presbyterian": "Presbyterian College",
    "Queens University": "Queens",
    "SE Louisiana": "Southeastern Louisiana",
    "SIU Edwardsville": "SIUE",
    "Saint Francis": "Saint Francis (PA)",
    "Saint Mary's": "Saint Mary's College",
    "Sam Houston": "Sam Houston State",
    "San José State": "San Jose State",
    "San Jose State": "San Jose State",
    "Southeast Missouri State": "Southeast Missouri",
    "St. Bonaventure": "Saint Bonaventure",
    "St. John's": "Saint John's",
    "St. Thomas-Minnesota": "Saint Thomas",
    "UAlbany": "Albany",
    "UConn": "Connecticut",
    "UL Monroe": "ULM",
    "UNC Greensboro": "UNCG",
    "UNC Wilmington": "UNCW",
    "UT Arlington": "UTA",
    "UT Martin": "Tennessee-Martin",
    "UT Rio Grande Valley": "UTRGV",
    "Detroit Mercy": "Detroit",
    "Little Rock": "Little Rock",
    "Purdue Fort Wayne": "Purdue Fort Wayne",
    "Seattle U": "Seattle University",
    "LIU": "Long Island",
    "USC Upstate": "South Carolina Upstate",
    "Bethune-Cookman": "Bethune-Cookman",
    "IU Indy": "IU Indianapolis",
    "Southern University": "Southern",
    "Massachusetts": "UMass",
}

# ── Cached ratings (loaded once, refreshable) ──

RATINGS: dict = {
    "pace": {},
    "oe": {},
    "de": {},
    "national_avg_oe": 100.0,
    "source": "",
    "loaded_at": None,
}


# ── Data loading functions ──

def load_pace() -> dict[str, tuple[float, int]]:
    """Load pace ratings from CSV. Returns {team: (adj_pace, rank)}."""
    pace: dict[str, tuple[float, int]] = {}
    if not PACE_CSV.exists():
        print(f"  WARNING: pace CSV not found at {PACE_CSV}. "
              "Using median-pace fallback for all teams.")
        return pace
    with open(PACE_CSV, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pace[row["Team"].strip()] = (float(row["AdjPace"]), int(row["Rank"]))
    return pace


def scrape_wn_ratings(url: str) -> dict[str, tuple[float, int]]:
    """Scrape WarrenNolan advanced stats page. Returns {team: (rating, rank)}."""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8")

    pattern = re.compile(
        r'<td class="data-cell data-center data-medium\s*"[^>]*>\s*(\d+)\s*</td>'
        r'.*?'
        r'<div class="name-subcontainer"><a[^>]*>([^<]+)</a></div>'
        r'.*?'
        r'<td class="data-cell data-center data-medium\s*"[^>]*>\s*([\d.]+)\s*</td>',
        re.DOTALL,
    )
    data: dict[str, tuple[float, int]] = {}
    for m in pattern.finditer(html):
        rank = int(m.group(1))
        team = m.group(2).strip()
        rating = float(m.group(3))
        data[team] = (rating, rank)
    return data


def load_all_ratings() -> None:
    """Load pace, OE, DE into the global RATINGS cache."""
    global RATINGS
    print("Loading pace data from CSV...")
    pace = load_pace()
    print(f"  Loaded {len(pace)} teams.")

    source = "WarrenNolan"
    try:
        print("Scraping OE from WarrenNolan (women)...")
        oe = scrape_wn_ratings(WN_OE_URL)
        print(f"  Got {len(oe)} teams.")
        print("Scraping DE from WarrenNolan (women)...")
        de = scrape_wn_ratings(WN_DE_URL)
        print(f"  Got {len(de)} teams.")
    except (URLError, OSError, Exception) as e:
        print(f"  WarrenNolan unavailable: {e}")
        print("  No fallback available for women's ratings.")
        oe, de = {}, {}
        source = "None (WarrenNolan failed)"

    # National average OE (mean of all teams)
    if oe:
        nat_avg = sum(v[0] for v in oe.values()) / len(oe)
    else:
        nat_avg = 100.0

    RATINGS = {
        "pace": pace,
        "oe": oe,
        "de": de,
        "national_avg_oe": round(nat_avg, 2),
        "source": source,
        "loaded_at": datetime.now(ET),
    }
    print(f"Ratings loaded. National avg OE: {RATINGS['national_avg_oe']}")


def resolve_team(espn_name: str) -> str:
    """Map ESPN location name to WarrenNolan name."""
    pace = RATINGS["pace"]
    if espn_name in pace:
        return espn_name
    # Check OE/DE dicts too (may have slightly different coverage)
    if espn_name in RATINGS["oe"]:
        return espn_name
    mapped = ESPN_TO_WN.get(espn_name)
    if mapped:
        return mapped
    return espn_name


def pace_pct_class(pct: int) -> str:
    """Return CSS class for pace percentile color tier."""
    if pct >= 80:
        return "pace-very-fast"
    if pct >= 60:
        return "pace-fast"
    if pct >= 40:
        return "pace-avg"
    if pct >= 20:
        return "pace-slow"
    return "pace-very-slow"


# ── ESPN live scoreboard ──

def _get_linescore(competitor: dict, period_index: int) -> int | None:
    """Extract a period score from ESPN competitor linescores. Returns None if unavailable."""
    ls = competitor.get("linescores", [])
    if period_index < len(ls):
        val = ls[period_index].get("value", None)
        if val is not None:
            return int(val)
    return None


def _get_1h_score(competitor: dict) -> int | None:
    """Get 1H score = Q1 + Q2 for women's basketball (4-quarter format)."""
    q1 = _get_linescore(competitor, 0)
    q2 = _get_linescore(competitor, 1)
    if q1 is not None and q2 is not None:
        return q1 + q2
    return None


def fetch_live_scoreboard(date_str: str) -> list[dict]:
    """
    Fetch all D1 women's games with live scores from ESPN API.
    date_str: YYYYMMDD format.
    Returns list of game dicts.
    """
    url = f"{ESPN_API}?dates={date_str}&groups=50&limit=400"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

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
        _t = (et_dt.strftime("%-I:%M %p") if sys.platform != "win32"
              else et_dt.strftime("%#I:%M %p"))
        _base_date = (datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").date()
                      if DATE_OVERRIDE else datetime.now(ET).date())
        time_str = _t if et_dt.date() == _base_date else (et_dt.strftime("%a ") + _t)

        game = {
            "game_id": event["id"],
            "state": status["type"]["state"],  # "pre", "in", "post"
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
            "away_1h_score": _get_1h_score(away),
            "home_1h_score": _get_1h_score(home),
            "start_time_str": time_str,
            "start_time_sort": et_dt.hour * 100 + et_dt.minute,
            "start_epoch": et_dt.timestamp(),
            "neutral_site": bool(comp.get("neutralSite", False)),
        }
        games.append(game)

    return games


# ── Game detail / foul tracking ──

ESPN_SUMMARY = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/"
    "womens-college-basketball/summary"
)

_foul_cache_lock = threading.Lock()
_foul_cache: dict[str, tuple[dict, float]] = {}  # game_id -> (foul_data, timestamp)
FOUL_CACHE_TTL = 25  # seconds


def fetch_game_fouls(game_id: str) -> dict:
    """
    Fetch per-quarter team fouls from ESPN summary play-by-play.
    Women's CBB uses 4 quarters — fouls reset each quarter.
    Returns {"away_q1": N, "away_q2": N, "away_q3": N, "away_q4": N,
             "home_q1": N, "home_q2": N, "home_q3": N, "home_q4": N,
             "away_total": N, "home_total": N}.
    """
    # Check cache
    now = time.monotonic()
    with _foul_cache_lock:
        if game_id in _foul_cache:
            data, ts = _foul_cache[game_id]
            if now - ts < FOUL_CACHE_TTL:
                return data

    empty = {"away_q1": 0, "away_q2": 0, "away_q3": 0, "away_q4": 0,
             "home_q1": 0, "home_q2": 0, "home_q3": 0, "home_q4": 0,
             "away_total": 0, "home_total": 0}
    try:
        url = f"{ESPN_SUMMARY}?event={game_id}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read())
    except Exception:
        return empty

    # Identify home/away team IDs
    try:
        header = raw.get("header", {})
        comps = header.get("competitions", [{}])[0].get("competitors", [])
        team_ids: dict[str, str] = {}  # team_id -> "home" or "away"
        for c in comps:
            side = c.get("homeAway", "")
            tid = c.get("id", "")
            if side and tid:
                team_ids[tid] = side
    except Exception:
        return empty

    if not team_ids:
        return empty

    # Count fouls from play-by-play
    counts = {"away_q1": 0, "away_q2": 0, "away_q3": 0, "away_q4": 0,
              "home_q1": 0, "home_q2": 0, "home_q3": 0, "home_q4": 0}
    plays = raw.get("plays", [])
    for play in plays:
        ptype = play.get("type", {}).get("text", "")
        if "Foul" not in ptype or "Technical" in ptype:
            continue  # only count personal / shooting / flagrant fouls
        team_obj = play.get("team", {})
        tid = team_obj.get("id", "")
        period = play.get("period", {}).get("number", 0)
        side = team_ids.get(tid, "")
        if not side:
            continue

        if period == 1:
            counts[f"{side}_q1"] += 1
        elif period == 2:
            counts[f"{side}_q2"] += 1
        elif period == 3:
            counts[f"{side}_q3"] += 1
        elif period == 4:
            counts[f"{side}_q4"] += 1
        # OT fouls go into q4 bucket (no quarter reset in OT)

    counts["away_total"] = counts["away_q1"] + counts["away_q2"] + counts["away_q3"] + counts["away_q4"]
    counts["home_total"] = counts["home_q1"] + counts["home_q2"] + counts["home_q3"] + counts["home_q4"]

    try:
        box = _extract_box_stats(raw)
        if box:
            counts.update(box)
    except Exception:
        pass

    with _foul_cache_lock:
        _foul_cache[game_id] = (counts, time.monotonic())

    return counts


def _extract_box_stats(raw: dict) -> dict:
    """Pull per-team FGA, ORB, TOV, FTA from the summary boxscore.

    Returns {away_fga, away_orb, away_tov, away_fta, home_...} or {}
    unless all four stats are available for both teams.
    """
    teams = raw.get("boxscore", {}).get("teams", [])
    if len(teams) != 2:
        return {}

    comps = raw.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
    side_by_id = {c.get("id", ""): c.get("homeAway", "") for c in comps}

    def attempts(val: str) -> int | None:
        try:
            return int(val.split("-")[1])
        except (IndexError, ValueError, AttributeError):
            return None

    def whole(val: str) -> int | None:
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None

    out: dict = {}
    for t in teams:
        side = t.get("homeAway", "") or side_by_id.get(t.get("team", {}).get("id", ""), "")
        if side not in ("away", "home"):
            continue
        stats = {s.get("name", ""): s.get("displayValue", "")
                 for s in t.get("statistics", [])}
        fga = attempts(stats.get("fieldGoalsMade-fieldGoalsAttempted", ""))
        fta = attempts(stats.get("freeThrowsMade-freeThrowsAttempted", ""))
        orb = whole(stats.get("offensiveRebounds", ""))
        tov = whole(stats.get("totalTurnovers", "") or stats.get("turnovers", ""))
        if None in (fga, fta, orb, tov):
            continue
        out[f"{side}_fga"] = fga
        out[f"{side}_orb"] = orb
        out[f"{side}_tov"] = tov
        out[f"{side}_fta"] = fta

    return out if len(out) == 8 else {}


def fetch_fouls_for_live_games(games: list[dict]) -> dict[str, dict]:
    """Fetch fouls for all live games in parallel. Returns {game_id: foul_data}."""
    from concurrent.futures import ThreadPoolExecutor
    live_ids = [g["game_id"] for g in games if g["state"] == "in"]
    if not live_ids:
        return {}

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(live_ids), 8)) as pool:
        futures = {pool.submit(fetch_game_fouls, gid): gid for gid in live_ids}
        for fut in futures:
            gid = futures[fut]
            try:
                results[gid] = fut.result(timeout=10)
            except Exception:
                results[gid] = {"away_q1": 0, "away_q2": 0, "away_q3": 0, "away_q4": 0,
                                "home_q1": 0, "home_q2": 0, "home_q3": 0, "home_q4": 0,
                                "away_total": 0, "home_total": 0}
    return results


# ── Projection engine ──

def project_game(game: dict) -> dict:
    """
    Compute projections for a single game.
    Women's CBB: 4 × 10-minute quarters, 40 total minutes.
    Returns the game dict enriched with projection fields.
    """
    r = RATINGS
    away_wn = resolve_team(game["away_name"])
    home_wn = resolve_team(game["home_name"])

    # Look up ratings (fallback to median-ish values)
    median_pace = 69.0
    median_eff = 100.0
    total_pace_teams = len(r["pace"]) or 365
    median_rank = total_pace_teams // 2

    away_pace_data = r["pace"].get(away_wn, (median_pace, median_rank))
    home_pace_data = r["pace"].get(home_wn, (median_pace, median_rank))
    away_pace_val = away_pace_data[0]
    away_pace_rank = away_pace_data[1]
    home_pace_val = home_pace_data[0]
    home_pace_rank = home_pace_data[1]

    away_pace_pct = round((total_pace_teams - away_pace_rank + 1) / total_pace_teams * 100)
    home_pace_pct = round((total_pace_teams - home_pace_rank + 1) / total_pace_teams * 100)

    away_oe = r["oe"].get(away_wn, (median_eff, 182))[0]
    home_oe = r["oe"].get(home_wn, (median_eff, 182))[0]
    away_de = r["de"].get(away_wn, (median_eff, 182))[0]
    home_de = r["de"].get(home_wn, (median_eff, 182))[0]

    has_away_data = away_wn in r["pace"] or away_wn in r["oe"]
    has_home_data = home_wn in r["pace"] or home_wn in r["oe"]

    # Game pace = average of both teams
    game_pace = (away_pace_val + home_pace_val) / 2

    # Time calculations — 4 quarters of 10 minutes each
    period = game["period"]
    clock_sec = float(game["clock_seconds"])
    state = game["state"]
    detail = game.get("status_detail", "").lower()

    if state == "pre":
        time_elapsed = 0.0
        total_game_min = REGULATION_MIN
    elif state == "post":
        # For completed games, estimate total minutes based on period
        if period <= 4:
            total_game_min = REGULATION_MIN
        else:
            total_game_min = REGULATION_MIN + OT_MIN * (period - 4)
        time_elapsed = total_game_min
    else:
        # Live game
        clock_min = clock_sec / 60.0

        if "half" in detail and clock_min < 0.1:
            # Halftime (between Q2 and Q3)
            time_elapsed = 2 * QUARTER_MIN  # 20 minutes
            total_game_min = REGULATION_MIN
        elif "end" in detail and period <= 4:
            # End of a quarter
            time_elapsed = period * QUARTER_MIN
            total_game_min = REGULATION_MIN
        elif period <= 4:
            # In a regulation quarter
            time_elapsed = (period - 1) * QUARTER_MIN + (QUARTER_MIN - clock_min)
            total_game_min = REGULATION_MIN
        else:
            # Overtime
            ot_num = period - 4
            time_elapsed = REGULATION_MIN + OT_MIN * (ot_num - 1) + (OT_MIN - clock_min)
            total_game_min = REGULATION_MIN + OT_MIN * ot_num

    time_remaining = max(0.0, total_game_min - time_elapsed)

    # Possessions
    poss_so_far = game_pace * (time_elapsed / REGULATION_MIN)
    poss_remaining = game_pace * (time_remaining / REGULATION_MIN)
    total_expected_poss = game_pace * (total_game_min / REGULATION_MIN)

    # Projected remaining points (opponent-adjusted)
    nat_avg = r["national_avg_oe"]
    away_ppp = (away_oe * home_de) / nat_avg / 100.0
    home_ppp = (home_oe * away_de) / nat_avg / 100.0

    # Home court advantage: zero for neutral site games
    hca_pts = 0.0 if game.get("neutral_site", False) else HCA_POINTS

    # Scale by remaining time proportion
    hca_remaining = (hca_pts / 2.0) * (time_remaining / REGULATION_MIN)

    # ── Blowout regression to the mean ──
    # When a team has a big lead mid-game, benches starters / runs clock.
    # Pull the leading team's PPP toward league avg and the trailing team's up.
    blowout_regress = 0.0
    if state == "in" and time_elapsed > 0:
        lead = abs(game["home_score"] - game["away_score"])
        if lead >= BLOWOUT_THRESHOLD:
            lead_frac = min((lead - BLOWOUT_THRESHOLD) /
                            (BLOWOUT_LEAD_CAP - BLOWOUT_THRESHOLD), 1.0)
            time_frac = time_remaining / REGULATION_MIN  # more time left = more regression
            blowout_regress = BLOWOUT_MAX_REGRESS * lead_frac * time_frac

    league_avg_ppp = 1.0  # league-average PPP ≈ 1.0 by definition
    away_ppp_adj = (away_ppp + blowout_regress * (league_avg_ppp - away_ppp)
                    if game["away_score"] < game["home_score"]
                    else away_ppp - blowout_regress * (away_ppp - league_avg_ppp)
                    if game["away_score"] > game["home_score"] else away_ppp)
    home_ppp_adj = (home_ppp + blowout_regress * (league_avg_ppp - home_ppp)
                    if game["home_score"] < game["away_score"]
                    else home_ppp - blowout_regress * (home_ppp - league_avg_ppp)
                    if game["home_score"] > game["away_score"] else home_ppp)

    away_proj_remaining = poss_remaining * away_ppp_adj - hca_remaining
    home_proj_remaining = poss_remaining * home_ppp_adj + hca_remaining

    away_final = game["away_score"] + away_proj_remaining
    home_final = game["home_score"] + home_proj_remaining

    proj_total = away_final + home_final
    proj_spread = home_final - away_final  # positive = home favored

    # Pre-game full projection (for upcoming games)
    hca_full = hca_pts / 2.0
    away_full_proj = total_expected_poss * away_ppp - hca_full
    home_full_proj = total_expected_poss * home_ppp + hca_full

    # ── First half projection (Q1+Q2) ──
    half_poss = game_pace * HALF_POSS_SHARE
    half_min = 2 * QUARTER_MIN  # 20 minutes for 1H

    if state == "pre":
        # Pre-game: 1H projection uses 47.25% of game possessions
        hca_half = hca_full * HALF_POSS_SHARE
        away_1h_proj = half_poss * away_ppp - hca_half
        home_1h_proj = half_poss * home_ppp + hca_half
    elif period <= 2 and state == "in":
        # Currently in 1H (Q1 or Q2): project remaining 1H possessions
        elapsed_1h_min = time_elapsed  # already accounts for quarters
        if elapsed_1h_min > half_min:
            elapsed_1h_min = half_min
        used_1h_poss = game_pace * (elapsed_1h_min / REGULATION_MIN)
        remaining_1h_poss = max(half_poss - used_1h_poss, 0.0)
        hca_1h_remaining = (hca_pts / 2.0) * (remaining_1h_poss / game_pace) if game_pace > 0 else 0
        away_1h_proj = game["away_score"] + remaining_1h_poss * away_ppp - hca_1h_remaining
        home_1h_proj = game["home_score"] + remaining_1h_poss * home_ppp + hca_1h_remaining
    else:
        # 2H (Q3+), halftime, OT, or post: use actual 1H scores if available
        a1h = game.get("away_1h_score")
        h1h = game.get("home_1h_score")
        if a1h is not None and h1h is not None:
            away_1h_proj = float(a1h)
            home_1h_proj = float(h1h)
        else:
            # Fallback: estimate from pace-based share
            hca_half = hca_full * HALF_POSS_SHARE
            away_1h_proj = half_poss * away_ppp - hca_half
            home_1h_proj = half_poss * home_ppp + hca_half

    proj_1h_total = away_1h_proj + home_1h_proj
    proj_1h_spread = home_1h_proj - away_1h_proj  # positive = home favored
    # Whether to label as "Proj" or "Actual"
    h1_is_actual = (state != "pre" and period >= 3 and
                    game.get("away_1h_score") is not None)

    return {
        **game,
        "away_wn": away_wn,
        "home_wn": home_wn,
        "has_away_data": has_away_data,
        "has_home_data": has_home_data,
        "game_pace": round(game_pace, 1),
        "away_pace": round(away_pace_val, 1),
        "home_pace": round(home_pace_val, 1),
        "away_pace_pct": away_pace_pct,
        "home_pace_pct": home_pace_pct,
        "away_pace_cls": pace_pct_class(away_pace_pct),
        "home_pace_cls": pace_pct_class(home_pace_pct),
        "away_oe": round(away_oe, 1),
        "home_oe": round(home_oe, 1),
        "away_de": round(away_de, 1),
        "home_de": round(home_de, 1),
        "time_elapsed": round(time_elapsed, 1),
        "time_remaining": round(time_remaining, 1),
        "poss_so_far": round(poss_so_far, 1),
        "poss_remaining": round(poss_remaining, 1),
        "total_expected_poss": round(total_expected_poss, 1),
        "away_proj_remaining": round(away_proj_remaining, 1),
        "home_proj_remaining": round(home_proj_remaining, 1),
        "away_final": round(away_final, 1),
        "home_final": round(home_final, 1),
        "away_full_proj": round(away_full_proj, 1),
        "home_full_proj": round(home_full_proj, 1),
        "proj_total": round(proj_total, 1),
        "proj_spread": round(proj_spread, 1),
        "away_1h_proj": round(away_1h_proj, 1),
        "home_1h_proj": round(home_1h_proj, 1),
        "proj_1h_total": round(proj_1h_total, 1),
        "proj_1h_spread": round(proj_1h_spread, 1),
        "h1_is_actual": h1_is_actual,
        "neutral_site": game.get("neutral_site", False),
        "hca_applied": hca_pts,
        "blowout_regress": round(blowout_regress * 100, 1),
    }


def compute_live_pace_stats(g: dict) -> dict:
    """Estimate current pace from the live box score and extrapolate.

    Poss = FGA - ORB + TOV + 0.44 x FTA per team, averaged across both
    teams, normalized to 40 minutes of elapsed game time. If that pace and
    each team's current points-per-possession hold, project the final
    score/total/margin — and, while the first half (Q1+Q2) is still in
    progress, the 1H final score/total/margin.
    """
    out = {
        "live_box_poss": None, "live_pace": None,
        "pace_away_final": None, "pace_home_final": None,
        "pace_proj_total": None, "pace_proj_margin": None,
        "pace_away_1h_final": None, "pace_home_1h_final": None,
        "pace_1h_total": None, "pace_1h_margin": None,
    }
    if g.get("state") != "in":
        return out
    keys = ("away_fga", "away_orb", "away_tov", "away_fta",
            "home_fga", "home_orb", "home_tov", "home_fta")
    if any(g.get(k) is None for k in keys):
        return out
    elapsed = g.get("time_elapsed") or 0.0
    if elapsed < LIVE_PACE_MIN_ELAPSED:
        return out

    away_poss = g["away_fga"] - g["away_orb"] + g["away_tov"] + FT_POSS_COEF * g["away_fta"]
    home_poss = g["home_fga"] - g["home_orb"] + g["home_tov"] + FT_POSS_COEF * g["home_fta"]
    box_poss = (away_poss + home_poss) / 2.0
    if box_poss <= 0:
        return out

    live_pace = REGULATION_MIN * box_poss / elapsed
    remaining = g.get("time_remaining") or 0.0
    rem_poss = live_pace * (remaining / REGULATION_MIN)

    away_ppp = g["away_score"] / box_poss
    home_ppp = g["home_score"] / box_poss
    away_final = g["away_score"] + rem_poss * away_ppp
    home_final = g["home_score"] + rem_poss * home_ppp

    out.update({
        "live_box_poss": round(box_poss, 1),
        "live_pace": round(live_pace, 1),
        "pace_away_final": round(away_final, 1),
        "pace_home_final": round(home_final, 1),
        "pace_proj_total": round(away_final + home_final, 1),
        "pace_proj_margin": round(home_final - away_final, 1),
    })

    # 1H line: extrapolate while the first half (Q1+Q2) is in progress; once it
    # is over, show the ACTUAL first-half result so the 1H stays on the card.
    half_min = 2 * QUARTER_MIN
    if g.get("period", 0) <= 2 and elapsed < half_min:
        rem_1h_poss = live_pace * ((half_min - elapsed) / REGULATION_MIN)
        away_1h = g["away_score"] + rem_1h_poss * away_ppp
        home_1h = g["home_score"] + rem_1h_poss * home_ppp
        out.update({
            "pace_away_1h_final": round(away_1h, 1),
            "pace_home_1h_final": round(home_1h, 1),
            "pace_1h_total": round(away_1h + home_1h, 1),
            "pace_1h_margin": round(home_1h - away_1h, 1),
        })
    elif g.get("away_1h_score") is not None and g.get("home_1h_score") is not None:
        a1h, h1h = float(g["away_1h_score"]), float(g["home_1h_score"])
        out.update({
            "pace_away_1h_final": a1h,
            "pace_home_1h_final": h1h,
            "pace_1h_total": a1h + h1h,
            "pace_1h_margin": h1h - a1h,
        })

    return out


def compute_true_pace_proj(g: dict) -> dict:
    """Extrapolate at tonight's OBSERVED pace with SEASON-LONG scoring rates.

    The Actual row extrapolates live box-score pace x live in-game PPP; the
    Expected (model) row uses season pace x season PPP. This hybrid holds the
    observed box-score pace (tempo stabilizes much faster than shooting) but
    scores the remaining possessions at each team's opponent-adjusted
    season-long rates, with the model's time-scaled HCA and blowout RTM — so
    the only input that differs from the Expected row is the pace.
    """
    out = {
        "tp_pace": None,
        "tp_away_final": None, "tp_home_final": None,
        "tp_total": None, "tp_margin": None,
        "tp_away_1h": None, "tp_home_1h": None,
        "tp_1h_total": None, "tp_1h_margin": None,
    }
    true_pace = g.get("live_pace")   # set by compute_live_pace_stats (>=3 min gate)
    if g.get("state") != "in" or not true_pace or true_pace <= 0:
        return out

    elapsed = g.get("time_elapsed") or 0.0
    remaining = g.get("time_remaining") or 0.0

    # Season-long opponent-adjusted PPP — same construction as project_game.
    nat_avg = RATINGS["national_avg_oe"]
    away_ppp = (g["away_oe"] * g["home_de"]) / nat_avg / 100.0
    home_ppp = (g["home_oe"] * g["away_de"]) / nat_avg / 100.0

    hca_pts = 0.0 if g.get("neutral_site", False) else HCA_POINTS

    # Blowout RTM (mirrors project_game): at a large lead both teams' PPPs
    # regress toward league average, scaled by lead size and remaining time.
    # (project_game's per-side branches reduce to the same algebra.)
    regress = 0.0
    lead = abs(g["home_score"] - g["away_score"])
    if lead >= BLOWOUT_THRESHOLD:
        lead_frac = min((lead - BLOWOUT_THRESHOLD) /
                        (BLOWOUT_LEAD_CAP - BLOWOUT_THRESHOLD), 1.0)
        regress = BLOWOUT_MAX_REGRESS * lead_frac * (remaining / REGULATION_MIN)
    away_ppp_adj = away_ppp + regress * (1.0 - away_ppp)
    home_ppp_adj = home_ppp + regress * (1.0 - home_ppp)

    rem_poss = true_pace * (remaining / REGULATION_MIN)
    hca_rem = (hca_pts / 2.0) * (remaining / REGULATION_MIN)
    away_final = g["away_score"] + rem_poss * away_ppp_adj - hca_rem
    home_final = g["home_score"] + rem_poss * home_ppp_adj + hca_rem

    out.update({
        "tp_pace": round(true_pace, 1),
        "tp_away_final": round(away_final, 1),
        "tp_home_final": round(home_final, 1),
        "tp_total": round(away_final + home_final, 1),
        "tp_margin": round(home_final - away_final, 1),
    })

    # 1H line: extrapolate while the first half (Q1+Q2) is in progress; once it
    # is over, show the ACTUAL first-half result so the 1H stays on the card.
    # RTM is not applied here, matching project_game's in-progress 1H calc.
    half_min = 2 * QUARTER_MIN
    if g.get("period", 0) <= 2 and elapsed < half_min:
        rem_1h_min = half_min - elapsed
        rem_1h_poss = true_pace * (rem_1h_min / REGULATION_MIN)
        hca_1h = (hca_pts / 2.0) * (rem_1h_min / REGULATION_MIN)
        away_1h = g["away_score"] + rem_1h_poss * away_ppp - hca_1h
        home_1h = g["home_score"] + rem_1h_poss * home_ppp + hca_1h
        out.update({
            "tp_away_1h": round(away_1h, 1),
            "tp_home_1h": round(home_1h, 1),
            "tp_1h_total": round(away_1h + home_1h, 1),
            "tp_1h_margin": round(home_1h - away_1h, 1),
        })
    elif g.get("away_1h_score") is not None and g.get("home_1h_score") is not None:
        a1h, h1h = float(g["away_1h_score"]), float(g["home_1h_score"])
        out.update({
            "tp_away_1h": a1h,
            "tp_home_1h": h1h,
            "tp_1h_total": a1h + h1h,
            "tp_1h_margin": h1h - a1h,
        })

    return out


# ── HTML Template ──

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="theme-color" content="#0f1923">
<title>WCBB Live Projections</title>
<style>
  :root {
    --bg: #0f1923;
    --card-bg: #1a2634;
    --card-border: #2a3a4a;
    --text: #e8edf2;
    --text-muted: #8899aa;
    --accent: #e040a0;
    --green: #4caf50;
    --blue: #2196f3;
    --amber: #ffc107;
    --header-bg: #0a1218;
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
    border-bottom: 2px solid var(--accent);
    padding: 16px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
  }
  header h1 {
    font-size: 1.4em;
    font-weight: 700;
    color: var(--text);
  }
  header h1 span { color: var(--accent); }
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
    background: rgba(224,64,160,0.15);
    color: var(--accent);
    padding: 2px 10px;
    border-radius: 4px;
    font-weight: 600;
    font-size: 0.9em;
  }
  .container { max-width: 1100px; margin: 0 auto; padding: 16px; }

  .section-header {
    font-size: 1.1em;
    font-weight: 700;
    padding: 10px 0 8px;
    border-bottom: 1px solid var(--card-border);
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .section-header .count {
    background: var(--accent);
    color: white;
    font-size: 0.75em;
    padding: 1px 8px;
    border-radius: 10px;
  }
  .section-header.upcoming .count { background: var(--blue); }
  .section-header.completed .count { background: var(--text-muted); }

  /* Live game cards */
  .game-card {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-left: 4px solid var(--accent);
    border-radius: 8px;
    padding: 14px 18px;
    margin-bottom: 12px;
    transition: border-color 0.2s;
  }
  .game-card:hover { border-color: var(--accent); }
  .game-card.pre { border-left-color: var(--blue); }
  .game-card.post { border-left-color: var(--text-muted); }

  .teams-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 10px;
  }
  .team {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
  }
  .team.home { justify-content: flex-end; text-align: right; }
  .team img {
    width: 28px;
    height: 28px;
    object-fit: contain;
    flex-shrink: 0;
  }
  .team-name {
    font-weight: 600;
    font-size: 0.95em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 200px;
  }
  .score-block {
    text-align: center;
    min-width: 140px;
    flex-shrink: 0;
  }
  .score-line {
    font-size: 1.8em;
    font-weight: 800;
    letter-spacing: 2px;
    font-variant-numeric: tabular-nums;
  }
  .score-line .dash { color: var(--text-muted); margin: 0 6px; }
  .winning { color: var(--green); }
  .clock-line {
    font-size: 0.8em;
    color: var(--accent);
    font-weight: 600;
    margin-top: 2px;
  }
  .clock-line.final { color: var(--text-muted); }
  .clock-line.pre { color: var(--blue); }

  .proj-row {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid var(--card-border);
  }
  .proj-stat {
    flex: 1;
    min-width: 100px;
    text-align: center;
    padding: 6px 8px;
    background: rgba(255,255,255,0.03);
    border-radius: 6px;
  }
  .proj-stat label {
    display: block;
    font-size: 0.65em;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 3px;
  }
  .proj-stat .val {
    font-size: 1.05em;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .proj-stat .val.spread-home { color: var(--green); }
  .proj-stat .val.spread-away { color: var(--blue); }
  .proj-stat.box-pace { border: 1px dashed rgba(224,64,160,0.45); }
  .proj-stat.box-pace label { color: var(--accent); }

  .proj-row.tpace-row {
    margin-top: 6px;
    padding-top: 6px;
    border-top: 1px dashed var(--card-border);
  }
  .proj-stat.tpace { border: 1px dashed rgba(38,198,218,0.55); }
  .proj-stat.tpace label { color: #26c6da; }

  .detail-row {
    margin-top: 6px;
    font-size: 0.7em;
    color: var(--text-muted);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 4px;
  }
  .detail-row .warn { color: var(--amber); }
  .detail-row .hca { color: var(--accent); font-weight: 600; }

  .pace-pct {
    display: inline-block;
    font-size: 0.85em;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 3px;
    margin-left: 3px;
    min-width: 30px;
    text-align: center;
  }
  .pace-very-fast { background: rgba(255,68,68,0.3); color: #ff6666; }
  .pace-fast { background: rgba(255,140,0,0.3); color: #ffaa33; }
  .pace-avg { background: rgba(255,193,7,0.25); color: #ffc107; }
  .pace-slow { background: rgba(76,175,80,0.3); color: #66bb6a; }
  .pace-very-slow { background: rgba(33,150,243,0.3); color: #64b5f6; }

  .fouls-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 6px;
    padding: 5px 8px;
    background: rgba(255,255,255,0.03);
    border-radius: 6px;
    font-size: 0.78em;
    font-variant-numeric: tabular-nums;
  }
  .fouls-row .foul-team {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .fouls-row .foul-label {
    color: var(--text-muted);
    font-size: 0.85em;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .fouls-row .foul-qtr {
    color: var(--text);
  }
  .fouls-row .foul-qtr .num {
    font-weight: 700;
    min-width: 14px;
    display: inline-block;
    text-align: center;
  }
  .fouls-row .bonus {
    color: var(--amber);
    font-weight: 700;
    font-size: 0.85em;
  }
  .fouls-row .foul-center {
    color: var(--text-muted);
    font-size: 0.85em;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .neutral-badge {
    display: inline-block;
    background: var(--amber);
    color: #000;
    font-size: 0.7em;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 3px;
    vertical-align: middle;
    letter-spacing: 0.5px;
  }

  /* Upcoming / completed tables */
  .table-wrap {
    overflow-x: auto;
    margin-bottom: 20px;
  }
  .compact-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82em;
    min-width: 850px;
  }
  .compact-table th {
    background: var(--card-bg);
    color: var(--text-muted);
    font-weight: 600;
    font-size: 0.8em;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 6px 8px;
    text-align: center;
    border-bottom: 1px solid var(--card-border);
  }
  .compact-table td {
    padding: 6px 8px;
    text-align: center;
    border-bottom: 1px solid rgba(42,58,74,0.5);
    font-variant-numeric: tabular-nums;
  }
  .compact-table td.team-cell { text-align: left; font-weight: 600; white-space: nowrap; }
  .compact-table tr:hover { background: rgba(255,255,255,0.03); }

  .toggle-btn {
    background: none;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    font-size: 0.8em;
    margin-left: 8px;
  }
  .toggle-btn:hover { color: var(--text); }
  .hidden { display: none; }

  .no-games {
    text-align: center;
    color: var(--text-muted);
    padding: 24px;
    font-style: italic;
  }
  .error-banner {
    background: rgba(224,64,160,0.15);
    border: 1px solid var(--accent);
    border-radius: 6px;
    padding: 10px 16px;
    margin-bottom: 16px;
    color: var(--accent);
    font-size: 0.9em;
  }

  @media (max-width: 700px) {
    .teams-row { flex-direction: column; text-align: center; }
    .team, .team.home { justify-content: center; }
    .team-name { max-width: 160px; }
    .proj-stat { min-width: 80px; }
    .detail-row { flex-direction: column; text-align: center; }
  }
</style>
</head>
<body>

<header>
  <h1><span>&#9679;</span> WCBB Live Projections</h1>
  <div class="header-meta">
    <a href="/" style="font-weight:600">&larr; Main Menu</a>
    <span>{{ date_display }}</span>
    <span>{{ total_games }} games</span>
    <span>Ratings: {{ ratings_source }}</span>
    <span>HCA: {{ hca_points }} pts (0 at neutral)</span>
    <span style="color:var(--amber)" title="Regression to the Mean: at {{ blowout_threshold }}+ pt leads, projections regress toward league avg">RTM: {{ blowout_threshold }}+ pts</span>
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
  {% if live %}
    {% for g in live %}
    <div class="game-card">
      <div class="teams-row">
        <div class="team away">
          {% if g.away_logo %}<img src="{{ g.away_logo }}" alt="">{% endif %}
          <span class="team-name">{{ g.away_name }}</span>
        </div>
        <div class="score-block">
          <div class="score-line">
            <span class="{{ 'winning' if g.away_score > g.home_score else '' }}">{{ g.away_score }}</span>
            <span class="dash">-</span>
            <span class="{{ 'winning' if g.home_score > g.away_score else '' }}">{{ g.home_score }}</span>
          </div>
          <div class="clock-line">{{ g.status_detail }}{% if g.neutral_site %} <span class="neutral-badge">NEUTRAL</span>{% endif %}</div>
        </div>
        <div class="team home">
          <span class="team-name">{{ g.home_name }}</span>
          {% if g.home_logo %}<img src="{{ g.home_logo }}" alt="">{% endif %}
        </div>
      </div>
      <div class="proj-row">
        <div class="proj-stat">
          <label>{{ "1H Margin" if g.h1_is_actual else "Exp 1H Margin" }}</label>
          <span class="val {{ 'spread-home' if g.proj_1h_spread > 0 else 'spread-away' }}">
            {{ "Home" if g.proj_1h_spread > 0 else "Away" }} {{ "%.1f"|format(g.proj_1h_spread|abs) }}
          </span>
        </div>
        <div class="proj-stat">
          <label>{{ "1H Total" if g.h1_is_actual else "Expected 1H Total" }}</label>
          <span class="val">{{ g.proj_1h_total }}</span>
        </div>
        <div class="proj-stat">
          <label>Expected Margin</label>
          <span class="val {{ 'spread-home' if g.proj_spread > 0 else 'spread-away' }}">
            {{ "Home" if g.proj_spread > 0 else "Away" }} {{ "%.1f"|format(g.proj_spread|abs) }}
          </span>
        </div>
        <div class="proj-stat">
          <label>Expected Total</label>
          <span class="val">{{ g.proj_total }}</span>
        </div>
        {% if g.pace_1h_total is not none %}
        <div class="proj-stat box-pace">
          <label>Actual 1H Margin</label>
          <span class="val {{ 'spread-home' if g.pace_1h_margin > 0 else 'spread-away' }}">
            {{ "Home" if g.pace_1h_margin > 0 else "Away" }} {{ "%.1f"|format(g.pace_1h_margin|abs) }}
          </span>
        </div>
        <div class="proj-stat box-pace">
          <label>Actual 1H Total</label>
          <span class="val">{{ g.pace_1h_total }}</span>
        </div>
        {% endif %}
        <div class="proj-stat box-pace">
          <label>Actual Margin</label>
          {% if g.pace_proj_margin is not none %}
          <span class="val {{ 'spread-home' if g.pace_proj_margin > 0 else 'spread-away' }}">
            {{ "Home" if g.pace_proj_margin > 0 else "Away" }} {{ "%.1f"|format(g.pace_proj_margin|abs) }}
          </span>
          {% else %}
          <span class="val">&mdash;</span>
          {% endif %}
        </div>
        <div class="proj-stat box-pace">
          <label>Actual Total</label>
          <span class="val">{% if g.pace_proj_total is not none %}{{ g.pace_proj_total }}{% else %}&mdash;{% endif %}</span>
        </div>
      </div>
      {% if g.tp_total is not none %}
      <div class="proj-row tpace-row">
        {% if g.tp_1h_total is not none %}
        <div class="proj-stat tpace">
          <label>T Pace 1H Margin</label>
          <span class="val {{ 'spread-home' if g.tp_1h_margin > 0 else 'spread-away' }}">
            {{ "Home" if g.tp_1h_margin > 0 else "Away" }} {{ "%.1f"|format(g.tp_1h_margin|abs) }}
          </span>
        </div>
        <div class="proj-stat tpace">
          <label>T Pace 1H Total</label>
          <span class="val">{{ g.tp_1h_total }}</span>
        </div>
        {% endif %}
        <div class="proj-stat tpace">
          <label>T Pace Margin</label>
          <span class="val {{ 'spread-home' if g.tp_margin > 0 else 'spread-away' }}">
            {{ "Home" if g.tp_margin > 0 else "Away" }} {{ "%.1f"|format(g.tp_margin|abs) }}
          </span>
        </div>
        <div class="proj-stat tpace">
          <label>T Pace Total</label>
          <span class="val">{{ g.tp_total }}</span>
        </div>
      </div>
      {% endif %}
      <div class="fouls-row">
        <div class="foul-team">
          <span class="foul-label">{{ g.away_abbrev }}</span>
          <span class="foul-qtr">Q: <span class="num">{{ g.away_current_qtr_fouls }}</span></span>
          <span class="foul-qtr">Tot: <span class="num">{{ g.away_total }}</span></span>
          {% if g.away_current_qtr_fouls >= 5 %}<span class="bonus">BONUS</span>{% endif %}
        </div>
        <span class="foul-center">Team Fouls</span>
        <div class="foul-team">
          {% if g.home_current_qtr_fouls >= 5 %}<span class="bonus">BONUS</span>{% endif %}
          <span class="foul-qtr">Q: <span class="num">{{ g.home_current_qtr_fouls }}</span></span>
          <span class="foul-qtr">Tot: <span class="num">{{ g.home_total }}</span></span>
          <span class="foul-label">{{ g.home_abbrev }}</span>
        </div>
      </div>
      <div class="detail-row">
        <span>{{ g.away_abbrev }}: OE {{ g.away_oe }} | DE {{ g.away_de }} | Pace {{ g.away_pace }} <span class="pace-pct {{ g.away_pace_cls }}">{{ g.away_pace_pct }}%</span>{{ "" if g.has_away_data else " &#9888;" }}</span>
        <span>Game Pace: {{ g.game_pace }} | Poss Rem: {{ g.poss_remaining }} | <span class="hca">HCA &plusmn;{{ "%.1f"|format(g.hca_applied / 2) }}</span>{% if g.blowout_regress > 0 %} | <span style="color:var(--amber)">RTM {{ g.blowout_regress }}%</span>{% endif %}{% if g.live_box_poss is not none %} | Actual Poss: {{ g.live_box_poss }} | Actual Final: {{ g.pace_away_final }} - {{ g.pace_home_final }}{% endif %}{% if g.pace_1h_total is not none %} | Actual 1H: {{ g.pace_away_1h_final }} - {{ g.pace_home_1h_final }}{% endif %}</span>
        <span>{{ g.home_abbrev }}: OE {{ g.home_oe }} | DE {{ g.home_de }} | Pace {{ g.home_pace }} <span class="pace-pct {{ g.home_pace_cls }}">{{ g.home_pace_pct }}%</span>{{ "" if g.has_home_data else " &#9888;" }}</span>
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="no-games">No live games right now</div>
  {% endif %}
  </div>

  <!-- UPCOMING GAMES -->
  <div class="section-header upcoming">
    Upcoming <span class="count" id="upcoming-count">{{ upcoming|length }}</span>
    <button class="toggle-btn" onclick="toggleSection('upcoming')">show/hide</button>
  </div>
  <div id="upcoming-container">
  {% if upcoming %}
    <div class="table-wrap">
    <table class="compact-table">
      <tr>
        <th>Time</th>
        <th>Away</th><th>OE</th><th>DE</th><th>Pace</th><th>P%</th>
        <th>Home</th><th>OE</th><th>DE</th><th>Pace</th><th>P%</th>
        <th>G.Pace</th><th>1H Score</th><th>1H Sprd</th><th>1H Tot</th><th>Proj Score</th><th>Spread</th><th>Total</th>
      </tr>
      {% for g in upcoming %}
      <tr>
        <td>{{ g.start_time_str }}{% if g.neutral_site %} <span class="neutral-badge">N</span>{% endif %}</td>
        <td class="team-cell">{{ g.away_name }}{{ "" if g.has_away_data else " &#9888;" }}</td>
        <td>{{ g.away_oe }}</td><td>{{ g.away_de }}</td><td>{{ g.away_pace }}</td>
        <td><span class="pace-pct {{ g.away_pace_cls }}">{{ g.away_pace_pct }}%</span></td>
        <td class="team-cell">{{ g.home_name }}{{ "" if g.has_home_data else " &#9888;" }}</td>
        <td>{{ g.home_oe }}</td><td>{{ g.home_de }}</td><td>{{ g.home_pace }}</td>
        <td><span class="pace-pct {{ g.home_pace_cls }}">{{ g.home_pace_pct }}%</span></td>
        <td>{{ g.game_pace }}</td>
        <td>{{ g.away_1h_proj }} - {{ g.home_1h_proj }}</td>
        <td class="{{ 'spread-home' if g.proj_1h_spread > 0 else 'spread-away' }}">
          {{ "H" if g.proj_1h_spread > 0 else "A" }} {{ "%.1f"|format(g.proj_1h_spread|abs) }}
        </td>
        <td>{{ g.proj_1h_total }}</td>
        <td>{{ g.away_full_proj }} - {{ g.home_full_proj }}</td>
        <td class="{{ 'spread-home' if g.proj_spread > 0 else 'spread-away' }}">
          {{ "H" if g.proj_spread > 0 else "A" }} {{ "%.1f"|format(g.proj_spread|abs) }}
        </td>
        <td>{{ g.proj_total }}</td>
      </tr>
      {% endfor %}
    </table>
    </div>
  {% else %}
    <div class="no-games">No upcoming games</div>
  {% endif %}
  </div>

  <!-- COMPLETED GAMES -->
  <div class="section-header completed">
    Completed <span class="count" id="completed-count">{{ completed|length }}</span>
    <button class="toggle-btn" onclick="toggleSection('completed')">show/hide</button>
  </div>
  <div id="completed-container" class="hidden">
  {% if completed %}
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
        <td>{{ g.status_detail }}</td>
      </tr>
      {% endfor %}
    </table>
    </div>
  {% else %}
    <div class="no-games">No completed games</div>
  {% endif %}
  </div>

</div>

<script>
const LIVE_INTERVAL = 30;
const IDLE_INTERVAL = 300;  // 5 minutes
let interval = {{ 30 if live|length > 0 else 300 }};
let countdown = interval;

function toggleSection(id) {
  const el = document.getElementById(id + '-container');
  el.classList.toggle('hidden');
}

async function refreshGames() {
  try {
    const resp = await fetch('api/games');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    document.getElementById('live-container').innerHTML = data.live_html;
    document.getElementById('upcoming-container').innerHTML = data.upcoming_html;
    // Don't replace completed if user toggled it open
    const compEl = document.getElementById('completed-container');
    const wasHidden = compEl.classList.contains('hidden');
    compEl.innerHTML = data.completed_html;
    if (wasHidden) compEl.classList.add('hidden');
    else compEl.classList.remove('hidden');
    // Update counts
    document.getElementById('live-count').textContent = data.live_count;
    document.getElementById('upcoming-count').textContent = data.upcoming_count;
    document.getElementById('completed-count').textContent = data.completed_count;
    // Switch interval based on live game count
    interval = data.live_count > 0 ? LIVE_INTERVAL : IDLE_INTERVAL;
  } catch (e) {
    console.error('Refresh failed:', e);
  }
  countdown = interval;
}

setInterval(() => {
  countdown--;
  const el = document.getElementById('countdown');
  if (el) el.textContent = Math.max(0, countdown);
  if (countdown <= 0) refreshGames();
}, 1000);
</script>

</body>
</html>
"""

# Partials for AJAX refresh (same markup, no full page wrapper)
LIVE_PARTIAL = r"""{% if games %}
  {% for g in games %}
  <div class="game-card">
    <div class="teams-row">
      <div class="team away">
        {% if g.away_logo %}<img src="{{ g.away_logo }}" alt="">{% endif %}
        <span class="team-name">{{ g.away_name }}</span>
      </div>
      <div class="score-block">
        <div class="score-line">
          <span class="{{ 'winning' if g.away_score > g.home_score else '' }}">{{ g.away_score }}</span>
          <span class="dash">-</span>
          <span class="{{ 'winning' if g.home_score > g.away_score else '' }}">{{ g.home_score }}</span>
        </div>
        <div class="clock-line">{{ g.status_detail }}{% if g.neutral_site %} <span class="neutral-badge">NEUTRAL</span>{% endif %}</div>
      </div>
      <div class="team home">
        <span class="team-name">{{ g.home_name }}</span>
        {% if g.home_logo %}<img src="{{ g.home_logo }}" alt="">{% endif %}
      </div>
    </div>
    <div class="proj-row">
      <div class="proj-stat">
        <label>{{ "1H Margin" if g.h1_is_actual else "Exp 1H Margin" }}</label>
        <span class="val {{ 'spread-home' if g.proj_1h_spread > 0 else 'spread-away' }}">
          {{ "Home" if g.proj_1h_spread > 0 else "Away" }} {{ "%.1f"|format(g.proj_1h_spread|abs) }}
        </span>
      </div>
      <div class="proj-stat">
        <label>{{ "1H Total" if g.h1_is_actual else "Expected 1H Total" }}</label>
        <span class="val">{{ g.proj_1h_total }}</span>
      </div>
      <div class="proj-stat">
        <label>Expected Margin</label>
        <span class="val {{ 'spread-home' if g.proj_spread > 0 else 'spread-away' }}">
          {{ "Home" if g.proj_spread > 0 else "Away" }} {{ "%.1f"|format(g.proj_spread|abs) }}
        </span>
      </div>
      <div class="proj-stat">
        <label>Expected Total</label>
        <span class="val">{{ g.proj_total }}</span>
      </div>
      {% if g.pace_1h_total is not none %}
      <div class="proj-stat box-pace">
        <label>Actual 1H Margin</label>
        <span class="val {{ 'spread-home' if g.pace_1h_margin > 0 else 'spread-away' }}">
          {{ "Home" if g.pace_1h_margin > 0 else "Away" }} {{ "%.1f"|format(g.pace_1h_margin|abs) }}
        </span>
      </div>
      <div class="proj-stat box-pace">
        <label>Actual 1H Total</label>
        <span class="val">{{ g.pace_1h_total }}</span>
      </div>
      {% endif %}
      <div class="proj-stat box-pace">
        <label>Actual Margin</label>
        {% if g.pace_proj_margin is not none %}
        <span class="val {{ 'spread-home' if g.pace_proj_margin > 0 else 'spread-away' }}">
          {{ "Home" if g.pace_proj_margin > 0 else "Away" }} {{ "%.1f"|format(g.pace_proj_margin|abs) }}
        </span>
        {% else %}
        <span class="val">&mdash;</span>
        {% endif %}
      </div>
      <div class="proj-stat box-pace">
        <label>Actual Total</label>
        <span class="val">{% if g.pace_proj_total is not none %}{{ g.pace_proj_total }}{% else %}&mdash;{% endif %}</span>
      </div>
    </div>
    {% if g.tp_total is not none %}
    <div class="proj-row tpace-row">
      {% if g.tp_1h_total is not none %}
      <div class="proj-stat tpace">
        <label>T Pace 1H Margin</label>
        <span class="val {{ 'spread-home' if g.tp_1h_margin > 0 else 'spread-away' }}">
          {{ "Home" if g.tp_1h_margin > 0 else "Away" }} {{ "%.1f"|format(g.tp_1h_margin|abs) }}
        </span>
      </div>
      <div class="proj-stat tpace">
        <label>T Pace 1H Total</label>
        <span class="val">{{ g.tp_1h_total }}</span>
      </div>
      {% endif %}
      <div class="proj-stat tpace">
        <label>T Pace Margin</label>
        <span class="val {{ 'spread-home' if g.tp_margin > 0 else 'spread-away' }}">
          {{ "Home" if g.tp_margin > 0 else "Away" }} {{ "%.1f"|format(g.tp_margin|abs) }}
        </span>
      </div>
      <div class="proj-stat tpace">
        <label>T Pace Total</label>
        <span class="val">{{ g.tp_total }}</span>
      </div>
    </div>
    {% endif %}
    <div class="fouls-row">
      <div class="foul-team">
        <span class="foul-label">{{ g.away_abbrev }}</span>
        <span class="foul-qtr">Q: <span class="num">{{ g.away_current_qtr_fouls }}</span></span>
        <span class="foul-qtr">Tot: <span class="num">{{ g.away_total }}</span></span>
        {% if g.away_current_qtr_fouls >= 5 %}<span class="bonus">BONUS</span>{% endif %}
      </div>
      <span class="foul-center">Team Fouls</span>
      <div class="foul-team">
        {% if g.home_current_qtr_fouls >= 5 %}<span class="bonus">BONUS</span>{% endif %}
        <span class="foul-qtr">Q: <span class="num">{{ g.home_current_qtr_fouls }}</span></span>
        <span class="foul-qtr">Tot: <span class="num">{{ g.home_total }}</span></span>
        <span class="foul-label">{{ g.home_abbrev }}</span>
      </div>
    </div>
    <div class="detail-row">
      <span>{{ g.away_abbrev }}: OE {{ g.away_oe }} | DE {{ g.away_de }} | Pace {{ g.away_pace }} <span class="pace-pct {{ g.away_pace_cls }}">{{ g.away_pace_pct }}%</span>{{ "" if g.has_away_data else " &#9888;" }}</span>
      <span>Game Pace: {{ g.game_pace }} | Poss Rem: {{ g.poss_remaining }} | <span class="hca">HCA &plusmn;{{ "%.1f"|format(g.hca_applied / 2) }}</span>{% if g.blowout_regress > 0 %} | <span style="color:var(--amber)">RTM {{ g.blowout_regress }}%</span>{% endif %}{% if g.live_box_poss is not none %} | Actual Poss: {{ g.live_box_poss }} | Actual Final: {{ g.pace_away_final }} - {{ g.pace_home_final }}{% endif %}{% if g.pace_1h_total is not none %} | Actual 1H: {{ g.pace_away_1h_final }} - {{ g.pace_home_1h_final }}{% endif %}</span>
      <span>{{ g.home_abbrev }}: OE {{ g.home_oe }} | DE {{ g.home_de }} | Pace {{ g.home_pace }} <span class="pace-pct {{ g.home_pace_cls }}">{{ g.home_pace_pct }}%</span>{{ "" if g.has_home_data else " &#9888;" }}</span>
    </div>
  </div>
  {% endfor %}
{% else %}
  <div class="no-games">No live games right now</div>
{% endif %}
"""

UPCOMING_PARTIAL = r"""{% if games %}
  <div class="table-wrap">
  <table class="compact-table">
    <tr>
      <th>Time</th>
      <th>Away</th><th>OE</th><th>DE</th><th>Pace</th><th>P%</th>
      <th>Home</th><th>OE</th><th>DE</th><th>Pace</th><th>P%</th>
      <th>G.Pace</th><th>1H Score</th><th>1H Sprd</th><th>1H Tot</th><th>Proj Score</th><th>Spread</th><th>Total</th>
    </tr>
    {% for g in games %}
    <tr>
      <td>{{ g.start_time_str }}{% if g.neutral_site %} <span class="neutral-badge">N</span>{% endif %}</td>
      <td class="team-cell">{{ g.away_name }}{{ "" if g.has_away_data else " &#9888;" }}</td>
      <td>{{ g.away_oe }}</td><td>{{ g.away_de }}</td><td>{{ g.away_pace }}</td>
      <td><span class="pace-pct {{ g.away_pace_cls }}">{{ g.away_pace_pct }}%</span></td>
      <td class="team-cell">{{ g.home_name }}{{ "" if g.has_home_data else " &#9888;" }}</td>
      <td>{{ g.home_oe }}</td><td>{{ g.home_de }}</td><td>{{ g.home_pace }}</td>
      <td><span class="pace-pct {{ g.home_pace_cls }}">{{ g.home_pace_pct }}%</span></td>
      <td>{{ g.game_pace }}</td>
      <td>{{ g.away_1h_proj }} - {{ g.home_1h_proj }}</td>
      <td class="{{ 'spread-home' if g.proj_1h_spread > 0 else 'spread-away' }}">
        {{ "H" if g.proj_1h_spread > 0 else "A" }} {{ "%.1f"|format(g.proj_1h_spread|abs) }}
      </td>
      <td>{{ g.proj_1h_total }}</td>
      <td>{{ g.away_full_proj }} - {{ g.home_full_proj }}</td>
      <td class="{{ 'spread-home' if g.proj_spread > 0 else 'spread-away' }}">
        {{ "H" if g.proj_spread > 0 else "A" }} {{ "%.1f"|format(g.proj_spread|abs) }}
      </td>
      <td>{{ g.proj_total }}</td>
    </tr>
    {% endfor %}
  </table>
  </div>
{% else %}
  <div class="no-games">No upcoming games</div>
{% endif %}
"""

COMPLETED_PARTIAL = r"""{% if games %}
  <div class="table-wrap">
  <table class="compact-table">
    <tr>
      <th>Away</th><th>Score</th><th>Home</th><th>Score</th><th>Status</th>
    </tr>
    {% for g in games %}
    <tr>
      <td class="team-cell" style="{{ 'font-weight:800' if g.away_score > g.home_score else '' }}">{{ g.away_name }}</td>
      <td style="{{ 'font-weight:800' if g.away_score > g.home_score else '' }}">{{ g.away_score }}</td>
      <td class="team-cell" style="{{ 'font-weight:800' if g.home_score > g.away_score else '' }}">{{ g.home_name }}</td>
      <td style="{{ 'font-weight:800' if g.home_score > g.away_score else '' }}">{{ g.home_score }}</td>
      <td>{{ g.status_detail }}</td>
    </tr>
    {% endfor %}
  </table>
  </div>
{% else %}
  <div class="no-games">No completed games</div>
{% endif %}
"""


# ── Flask app ──

app = Flask(__name__)
DATE_OVERRIDE: str | None = None

# ── Scoreboard cache (avoid hammering ESPN on every request) ──

_cache_lock = threading.Lock()
_cache: dict = {
    "games": [],
    "fetched_at": 0.0,
    "error": None,
}
CACHE_TTL = 20  # seconds — ESPN data refreshed at most every 20s


# Show every game tipping off within this rolling window (not just "today").
WINDOW_HOURS = 48
WINDOW_DAYS = 3          # calendar days to fetch to cover a rolling 48h window


def get_date_str() -> str:
    """Get the target date as YYYYMMDD string."""
    if DATE_OVERRIDE:
        dt = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").replace(tzinfo=ET)
    else:
        dt = datetime.now(ET)
    return dt.strftime("%Y%m%d"), dt


def _fetch_scoreboard_cached() -> tuple[list[dict], str | None]:
    """Return cached scoreboard if fresh, otherwise fetch new data."""
    now = time.monotonic()
    with _cache_lock:
        if now - _cache["fetched_at"] < CACHE_TTL and _cache["games"]:
            return _cache["games"], _cache["error"]

    # Fetch outside the lock so other threads aren't blocked
    _, base_dt = get_date_str()
    error = None
    games, seen = [], set()
    try:
        for _i in range(WINDOW_DAYS):       # fetch enough calendar days to span 48h
            ds = (base_dt + timedelta(days=_i)).strftime("%Y%m%d")
            for g in fetch_live_scoreboard(ds):
                if g["game_id"] not in seen:
                    seen.add(g["game_id"])
                    games.append(g)
    except Exception as e:
        error = f"ESPN API error: {e}. Showing last known data."
        with _cache_lock:
            return _cache["games"], error

    with _cache_lock:
        _cache["games"] = games
        _cache["fetched_at"] = time.monotonic()
        _cache["error"] = None

    return games, error


def _get_current_qtr_fouls(game: dict, side: str) -> int:
    """Get foul count for the current quarter based on game period."""
    period = game.get("period", 0)
    if period == 1:
        return game.get(f"{side}_q1", 0)
    elif period == 2:
        return game.get(f"{side}_q2", 0)
    elif period == 3:
        return game.get(f"{side}_q3", 0)
    else:
        return game.get(f"{side}_q4", 0)


def fetch_and_project() -> tuple[list[dict], list[dict], list[dict], str, str | None]:
    """Fetch live data (cached), project all games, attach fouls for live games."""
    _, dt = get_date_str()
    date_display = dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")

    games, error = _fetch_scoreboard_cached()
    projected = [project_game(g) for g in games]

    # Fetch fouls in parallel for live games
    foul_map = fetch_fouls_for_live_games(projected)
    for g in projected:
        fouls = foul_map.get(g["game_id"], None)
        if fouls:
            g.update(fouls)
        else:
            g.update({"away_q1": 0, "away_q2": 0, "away_q3": 0, "away_q4": 0,
                       "home_q1": 0, "home_q2": 0, "home_q3": 0, "home_q4": 0,
                       "away_total": 0, "home_total": 0})
        # Compute current quarter fouls for bonus display
        g["away_current_qtr_fouls"] = _get_current_qtr_fouls(g, "away")
        g["home_current_qtr_fouls"] = _get_current_qtr_fouls(g, "home")
        g.update(compute_live_pace_stats(g))
        g.update(compute_true_pace_proj(g))

    live = sorted(
        [g for g in projected if g["state"] == "in"],
        key=lambda g: g["time_elapsed"],
        reverse=True,
    )
    _, _base_dt = get_date_str()
    _cutoff = _base_dt.timestamp() + WINDOW_HOURS * 3600
    upcoming = sorted(
        [g for g in projected if g["state"] == "pre"
         and g.get("start_epoch", 0) <= _cutoff],
        key=lambda g: g.get("start_epoch", g["start_time_sort"]),
    )
    completed = sorted(
        [g for g in projected if g["state"] == "post"],
        key=lambda g: g.get("start_epoch", g["start_time_sort"]),
        reverse=True,
    )

    return live, upcoming, completed, date_display, error


@app.route("/")
def index():
    if not RATINGS["loaded_at"]:
        load_all_ratings()

    live, upcoming, completed, date_display, error = fetch_and_project()

    return render_template_string(
        HTML_TEMPLATE,
        live=live,
        upcoming=upcoming,
        completed=completed,
        date_display=date_display,
        total_games=len(live) + len(upcoming) + len(completed),
        ratings_source=RATINGS["source"],
        hca_points=HCA_POINTS,
        blowout_threshold=BLOWOUT_THRESHOLD,
        error=error,
    )


@app.route("/api/games")
def api_games():
    if not RATINGS["loaded_at"]:
        load_all_ratings()

    live, upcoming, completed, _, error = fetch_and_project()

    return jsonify({
        "live_html": render_template_string(LIVE_PARTIAL, games=live),
        "upcoming_html": render_template_string(UPCOMING_PARTIAL, games=upcoming),
        "completed_html": render_template_string(COMPLETED_PARTIAL, games=completed),
        "live_count": len(live),
        "upcoming_count": len(upcoming),
        "completed_count": len(completed),
        "updated_at": datetime.now(ET).strftime("%I:%M:%S %p ET"),
        "error": error,
    })


@app.route("/refresh")
def refresh_ratings():
    load_all_ratings()
    return redirect(url_for("index"))


# ── Entry point ──

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WCBB Live Projections Dashboard")
    parser.add_argument("--port", type=int, default=5002, help="Port (default: 5002)")
    parser.add_argument("--date", type=str, default=None, help="Date override YYYY-MM-DD")
    args = parser.parse_args()

    if args.date:
        DATE_OVERRIDE = args.date

    print("=" * 50)
    print("  WCBB Live Projections Dashboard")
    print("=" * 50)

    load_all_ratings()

    date_str, dt = get_date_str()
    print(f"\nDate: {dt.strftime('%A, %B')} {dt.day}, {dt.year}")
    print(f"Server: http://localhost:{args.port}")
    print(f"Home Court Advantage: {HCA_POINTS} pts (\u00b1{HCA_POINTS / 2:.1f} per side, 0 at neutral sites)")
    print(f"Game Format: 4 \u00d7 10-min quarters")
    print("Press Ctrl+C to stop.\n")

    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
