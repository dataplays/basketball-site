"""
Live WNBA Projections Dashboard

Flask web app that shows live WNBA games with current scores,
estimated possessions, and projected final scores based on each team's
pace, offensive efficiency (OE), and defensive efficiency (DE),
including a home court advantage adjustment.

Mirrors the NBA live projections engine, with WNBA-specific
adjustments:
  - 40-minute regulation (4 x 10-min quarters)
  - 5-minute OT periods
  - Foul bonus at 5 team fouls per quarter (2 FTs, no 1-and-1)
  - Lower HCA (~2.7 pts vs 3.0 NBA)
  - Tighter blowout RTM thresholds (12 pt floor, 25 pt cap)
  - Neutral site detection (HCA = 0)
  - Ratings sourced from Basketball-Reference WNBA advanced team table
  - Live box-score pace estimate (FGA - ORB + TOV + 0.44*FTA, averaged
    across both teams) with at-current-pace total/margin extrapolation
    for the full game and, while the 1H is in progress, the first half

Usage:
    py -3 wnba_live_projections.py              # today's games, port 5005
    py -3 wnba_live_projections.py --port 8080  # custom port
    py -3 wnba_live_projections.py --date 2026-05-15  # specific date
"""

import argparse
import csv
import html as htmlmod
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

RATINGS_CSV = Path(r"C:\Users\User\Documents\wnba_ratings_2026.csv")
ET = ZoneInfo("America/New_York")

ESPN_API = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
)
ESPN_SUMMARY = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"
)
# B-Ref WNBA: try 2026 first, fall back to 2025 if 2026 has no data yet
BREF_RATINGS_URLS = [
    "https://www.basketball-reference.com/wnba/years/2026.html",
    "https://www.basketball-reference.com/wnba/years/2025.html",
]

# ── WNBA game constants ──

REGULATION_MIN = 40.0        # 4 × 10-minute quarters
QUARTER_MIN = 10.0
OT_MIN = 5.0
HALF_POSS_SHARE = 0.49       # ~49% of total possessions in 1H (Q1+Q2)
HCA_POINTS = 2.7             # WNBA HCA: ~2.5-3.0 pts (research consensus)

# Regression to the mean (blowout adjustment)
# WNBA games are 40 min, scoring is lower — blowouts kick in at smaller leads
BLOWOUT_THRESHOLD = 12       # point lead where regression starts
BLOWOUT_MAX_REGRESS = 0.40
BLOWOUT_LEAD_CAP = 25

# Live box-score pace estimate (basketball_pace_formula.pdf):
#   Poss = FGA - ORB + TOV + 0.44 x FTA  (per team, averaged across both)
# 0.44 = share of free throw attempts that end a possession
FT_POSS_COEF = 0.44
LIVE_PACE_MIN_ELAPSED = 3.0  # min game-minutes before extrapolating

# ── Team name mappings ──
# ESPN scoreboard returns team.location which already matches the
# wnba_ratings_2026.csv keys. Aliases only needed if a name diverges.
ESPN_TO_CSV: dict[str, str] = {}

# Basketball-Reference team names (full "City Name") mapped to ESPN locations
BREF_TO_ESPN = {
    "Atlanta Dream": "Atlanta",
    "Chicago Sky": "Chicago",
    "Connecticut Sun": "Connecticut",
    "Dallas Wings": "Dallas",
    "Golden State Valkyries": "Golden State",
    "Indiana Fever": "Indiana",
    "Las Vegas Aces": "Las Vegas",
    "Los Angeles Sparks": "Los Angeles",
    "Minnesota Lynx": "Minnesota",
    "New York Liberty": "New York",
    "Phoenix Mercury": "Phoenix",
    "Portland Fire": "Portland",
    "Seattle Storm": "Seattle",
    "Toronto Tempo": "Toronto",
    "Washington Mystics": "Washington",
}

# Cached ratings (loaded once, refreshable)

RATINGS: dict = {
    "pace": {},
    "oe": {},
    "de": {},
    "national_avg_oe": 105.4,
    "source": "",
    "loaded_at": None,
}


# ── Data loading functions ──

def load_ratings_from_csv() -> tuple[dict, dict, dict]:
    """Load Pace, OE, DE from CSV. Returns (pace_dict, oe_dict, de_dict).
    Each dict: {team_name: (value, rank)}."""
    rows: list[tuple[str, float, float, float]] = []
    with open(RATINGS_CSV, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append((
                row["Team"].strip(),
                float(row["Pace"]),
                float(row["OE"]),
                float(row["DE"]),
            ))

    pace_sorted = sorted(rows, key=lambda x: x[1], reverse=True)
    pace = {name: (p, rank) for rank, (name, p, _, _) in enumerate(pace_sorted, 1)}

    oe_sorted = sorted(rows, key=lambda x: x[2], reverse=True)
    oe = {name: (o, rank) for rank, (name, _, o, _) in enumerate(oe_sorted, 1)}

    de_sorted = sorted(rows, key=lambda x: x[3])
    de = {name: (d, rank) for rank, (name, _, _, d) in enumerate(de_sorted, 1)}

    return pace, oe, de


def scrape_bref_ratings() -> list[tuple[str, float, float, float]]:
    """Scrape Basketball-Reference WNBA advanced-team table for Pace, ORtg, DRtg.
    Returns list of (espn_team_name, pace, oe, de).

    Tries 2026 first, falls back to 2025 if no advanced data published yet.
    """
    last_err = None
    for url in BREF_RATINGS_URLS:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError) as e:
            last_err = e
            continue

        idx = html.find('id="advanced-team"')
        if idx < 0:
            continue
        end = html.find("</table>", idx)
        if end < 0:
            continue
        table_html = html[idx:end + 8]

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.DOTALL)
        teams: list[tuple[str, float, float, float]] = []
        for row in rows:
            tname = re.search(r'data-stat="team"[^>]*>(?:<a[^>]*>)?([^<]+)', row)
            ortg = re.search(r'data-stat="off_rtg"[^>]*>([\d.]+)', row)
            drtg = re.search(r'data-stat="def_rtg"[^>]*>([\d.]+)', row)
            pace = re.search(r'data-stat="pace"[^>]*>([\d.]+)', row)
            if tname and ortg and drtg and pace:
                full_name = htmlmod.unescape(tname.group(1)).strip()
                if full_name in ("Team", "League Average", ""):
                    continue
                espn_name = BREF_TO_ESPN.get(full_name, full_name)
                teams.append((
                    espn_name,
                    float(pace.group(1)),
                    float(ortg.group(1)),
                    float(drtg.group(1)),
                ))
        if teams:
            return teams

    if last_err:
        print(f"  [WARN] B-Ref scrape failed: {last_err}")
    return []


def save_ratings_csv(teams: list[tuple[str, float, float, float]]) -> None:
    """Write scraped ratings to CSV.

    Adds 2026 expansion teams (Toronto Tempo, Portland Fire) at league
    average if they're not in the scraped data.
    """
    seen = {t[0] for t in teams}
    if teams:
        avg_pace = sum(t[1] for t in teams) / len(teams)
        avg_oe = sum(t[2] for t in teams) / len(teams)
        avg_de = sum(t[3] for t in teams) / len(teams)
    else:
        avg_pace, avg_oe, avg_de = 77.3, 105.4, 105.4

    extended = list(teams)
    for expansion in ("Toronto", "Portland"):
        if expansion not in seen:
            extended.append((expansion, round(avg_pace, 1), round(avg_oe, 1), round(avg_de, 1)))

    with open(RATINGS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Team", "Pace", "OE", "DE"])
        for name, pace, oe, de in sorted(extended, key=lambda x: x[1], reverse=True):
            writer.writerow([name, pace, oe, de])


def load_all_ratings() -> None:
    """Load pace, OE, DE into the global RATINGS cache."""
    global RATINGS

    if RATINGS_CSV.exists():
        print("Loading ratings from CSV...")
        pace, oe, de = load_ratings_from_csv()
        source = "CSV"
    else:
        print("CSV not found. Scraping Basketball-Reference WNBA...")
        teams = scrape_bref_ratings()
        if teams:
            save_ratings_csv(teams)
            source = "Basketball-Reference"
        else:
            print("  [ERROR] Could not scrape B-Ref. Cannot proceed without ratings.")
            source = "EMPTY"
        if RATINGS_CSV.exists():
            pace, oe, de = load_ratings_from_csv()
        else:
            pace, oe, de = {}, {}, {}

    print(f"  Loaded {len(pace)} teams.")

    if oe:
        nat_avg = sum(v[0] for v in oe.values()) / len(oe)
    else:
        nat_avg = 105.4

    RATINGS = {
        "pace": pace,
        "oe": oe,
        "de": de,
        "national_avg_oe": round(nat_avg, 2),
        "source": source,
        "loaded_at": datetime.now(ET),
    }
    print(f"Ratings loaded. League avg OE: {RATINGS['national_avg_oe']}")


def resolve_team(espn_name: str) -> str:
    """Map ESPN location name to CSV team name."""
    mapped = ESPN_TO_CSV.get(espn_name)
    if mapped:
        return mapped
    if espn_name in RATINGS["pace"]:
        return espn_name
    if espn_name in RATINGS["oe"]:
        return espn_name
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
    """Extract a period score from ESPN competitor linescores."""
    ls = competitor.get("linescores", [])
    if period_index < len(ls):
        val = ls[period_index].get("value", None)
        if val is not None:
            return int(val)
    return None


def fetch_live_scoreboard(date_str: str) -> list[dict]:
    """Fetch all WNBA games with live scores from ESPN API. date_str: YYYYMMDD."""
    url = f"{ESPN_API}?dates={date_str}"
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

        # 1H score = Q1 + Q2 (WNBA uses 4 × 10-min quarters)
        away_q1 = _get_linescore(away, 0)
        away_q2 = _get_linescore(away, 1)
        home_q1 = _get_linescore(home, 0)
        home_q2 = _get_linescore(home, 1)
        away_1h = (away_q1 + away_q2) if away_q1 is not None and away_q2 is not None else None
        home_1h = (home_q1 + home_q2) if home_q1 is not None and home_q2 is not None else None

        game = {
            "game_id": event["id"],
            "state": status["type"]["state"],
            "clock_seconds": status.get("clock", 0) or 0,
            "display_clock": status.get("displayClock", "0:00"),
            "period": status.get("period", 0),
            "status_detail": status["type"].get("shortDetail", ""),
            "neutral_site": comp.get("neutralSite", False),
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
            "start_time_str": time_str,
            "start_time_sort": et_dt.hour * 100 + et_dt.minute,
            "start_epoch": et_dt.timestamp(),
        }
        games.append(game)

    return games


# ── Game detail / foul tracking ──

_foul_cache_lock = threading.Lock()
_foul_cache: dict[str, tuple[dict, float]] = {}
FOUL_CACHE_TTL = 25


def fetch_game_fouls(game_id: str) -> dict:
    """
    Fetch team foul counts and box-score stats from ESPN summary API.
    WNBA bonus rule: 5 team fouls in a quarter -> opponent shoots 2 FTs.
    Box stats (FGA, ORB, TOV, FTA per team) feed the live pace estimate.
    """
    now = time.monotonic()
    with _foul_cache_lock:
        if game_id in _foul_cache:
            data, ts = _foul_cache[game_id]
            if now - ts < FOUL_CACHE_TTL:
                return data

    empty = {
        "away_total_fouls": 0, "away_current_qtr_fouls": 0,
        "home_total_fouls": 0, "home_current_qtr_fouls": 0,
        "current_period": 0,
    }

    try:
        url = f"{ESPN_SUMMARY}?event={game_id}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read())
    except Exception:
        return empty

    result: dict = dict(empty)
    got_data = False

    try:
        # First try header.competitors[].fouls (works during in-progress games)
        comps = raw.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
        for c in comps:
            side = c.get("homeAway", "")
            fouls = c.get("fouls", {})
            if side and fouls:
                result[f"{side}_total_fouls"] = fouls.get("teamFouls", 0)
                result[f"{side}_current_qtr_fouls"] = fouls.get("teamFoulsCurrent", 0)
                got_data = True

        # If header didn't expose fouls, fall back to play-by-play counting
        if not got_data:
            pbp = _count_fouls_from_plays(raw)
            if pbp:
                result.update(pbp)
                got_data = True
    except Exception:
        pass

    try:
        box = _extract_box_stats(raw)
        if box:
            result.update(box)
            got_data = True
    except Exception:
        pass

    if got_data:
        with _foul_cache_lock:
            _foul_cache[game_id] = (result, time.monotonic())

    return result


def _count_fouls_from_plays(raw: dict) -> dict:
    """Fallback: count fouls per team per quarter from play-by-play."""
    header = raw.get("header", {})
    comps = header.get("competitions", [{}])[0].get("competitors", [])
    team_ids: dict[str, str] = {}
    for c in comps:
        side = c.get("homeAway", "")
        tid = c.get("id", "")
        if side and tid:
            team_ids[tid] = side

    if not team_ids:
        return {}

    q_fouls: dict[str, dict[int, int]] = {"away": {}, "home": {}}
    plays = raw.get("plays", [])
    for play in plays:
        ptype = play.get("type", {}).get("text", "")
        if "Foul" not in ptype or "Technical" in ptype:
            continue
        team_obj = play.get("team", {})
        tid = team_obj.get("id", "")
        period = play.get("period", {}).get("number", 0)
        side = team_ids.get(tid, "")
        if not side:
            continue
        q_fouls[side][period] = q_fouls[side].get(period, 0) + 1

    current_period = 0
    if plays:
        current_period = plays[-1].get("period", {}).get("number", 0)

    return {
        "away_total_fouls": sum(q_fouls["away"].values()),
        "away_current_qtr_fouls": q_fouls["away"].get(current_period, 0),
        "home_total_fouls": sum(q_fouls["home"].values()),
        "home_current_qtr_fouls": q_fouls["home"].get(current_period, 0),
        "current_period": current_period,
    }


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
        # "made-attempted" string, e.g. "31-70" -> 70
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
        # totalTurnovers includes team turnovers; plain turnovers is the fallback
        tov = whole(stats.get("totalTurnovers", "") or stats.get("turnovers", ""))
        if None in (fga, fta, orb, tov):
            continue
        out[f"{side}_fga"] = fga
        out[f"{side}_orb"] = orb
        out[f"{side}_tov"] = tov
        out[f"{side}_fta"] = fta

    return out if len(out) == 8 else {}


def fetch_fouls_for_live_games(games: list[dict]) -> dict[str, dict]:
    """Fetch fouls for all live games in parallel."""
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
                results[gid] = {
                    "away_total_fouls": 0, "away_current_qtr_fouls": 0,
                    "home_total_fouls": 0, "home_current_qtr_fouls": 0,
                    "current_period": 0,
                }
    return results


# ── Projection engine ──

def project_game(game: dict) -> dict:
    """Compute projections for a single game (40-min regulation, 5-min OT)."""
    r = RATINGS
    away_csv = resolve_team(game["away_name"])
    home_csv = resolve_team(game["home_name"])

    median_pace = 77.3
    median_eff = 105.4
    total_pace_teams = len(r["pace"]) or 15
    median_rank = total_pace_teams // 2

    away_pace_data = r["pace"].get(away_csv, (median_pace, median_rank))
    home_pace_data = r["pace"].get(home_csv, (median_pace, median_rank))
    away_pace_val, away_pace_rank = away_pace_data
    home_pace_val, home_pace_rank = home_pace_data

    away_pace_pct = round((total_pace_teams - away_pace_rank + 1) / total_pace_teams * 100)
    home_pace_pct = round((total_pace_teams - home_pace_rank + 1) / total_pace_teams * 100)

    away_oe = r["oe"].get(away_csv, (median_eff, median_rank))[0]
    home_oe = r["oe"].get(home_csv, (median_eff, median_rank))[0]
    away_de = r["de"].get(away_csv, (median_eff, median_rank))[0]
    home_de = r["de"].get(home_csv, (median_eff, median_rank))[0]

    has_away_data = away_csv in r["pace"] or away_csv in r["oe"]
    has_home_data = home_csv in r["pace"] or home_csv in r["oe"]

    game_pace = (away_pace_val + home_pace_val) / 2

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
            time_elapsed = 2 * QUARTER_MIN
            total_game_min = REGULATION_MIN
        elif "end" in detail and period <= 4:
            time_elapsed = period * QUARTER_MIN
            total_game_min = REGULATION_MIN
        elif period <= 4:
            time_elapsed = (period - 1) * QUARTER_MIN + (QUARTER_MIN - clock_min)
            total_game_min = REGULATION_MIN
        else:
            ot_num = period - 4
            time_elapsed = REGULATION_MIN + OT_MIN * (ot_num - 1) + (OT_MIN - clock_min)
            total_game_min = REGULATION_MIN + OT_MIN * ot_num

    time_remaining = max(0.0, total_game_min - time_elapsed)

    poss_so_far = game_pace * (time_elapsed / REGULATION_MIN)
    poss_remaining = game_pace * (time_remaining / REGULATION_MIN)
    total_expected_poss = game_pace * (total_game_min / REGULATION_MIN)

    nat_avg = r["national_avg_oe"]
    away_ppp = (away_oe * home_de) / nat_avg / 100.0
    home_ppp = (home_oe * away_de) / nat_avg / 100.0

    # Home court advantage — disabled at neutral sites
    hca_pts = 0.0 if game.get("neutral_site", False) else HCA_POINTS
    hca_remaining = (hca_pts / 2.0) * (time_remaining / REGULATION_MIN)

    # Blowout RTM: leading team's PPP regresses toward league avg
    blowout_regress = 0.0
    if state == "in" and time_elapsed > 0:
        lead = abs(game["home_score"] - game["away_score"])
        if lead >= BLOWOUT_THRESHOLD:
            lead_frac = min((lead - BLOWOUT_THRESHOLD) /
                            (BLOWOUT_LEAD_CAP - BLOWOUT_THRESHOLD), 1.0)
            time_frac = time_remaining / REGULATION_MIN
            blowout_regress = BLOWOUT_MAX_REGRESS * lead_frac * time_frac

    league_avg_ppp = 1.0
    away_ppp_adj = (
        away_ppp + blowout_regress * (league_avg_ppp - away_ppp)
        if game["away_score"] < game["home_score"]
        else away_ppp - blowout_regress * (away_ppp - league_avg_ppp)
        if game["away_score"] > game["home_score"]
        else away_ppp
    )
    home_ppp_adj = (
        home_ppp + blowout_regress * (league_avg_ppp - home_ppp)
        if game["home_score"] < game["away_score"]
        else home_ppp - blowout_regress * (home_ppp - league_avg_ppp)
        if game["home_score"] > game["away_score"]
        else home_ppp
    )

    away_proj_remaining = poss_remaining * away_ppp_adj - hca_remaining
    home_proj_remaining = poss_remaining * home_ppp_adj + hca_remaining

    away_final = game["away_score"] + away_proj_remaining
    home_final = game["home_score"] + home_proj_remaining

    proj_total = away_final + home_final
    proj_spread = home_final - away_final

    hca_full = hca_pts / 2.0
    away_full_proj = total_expected_poss * away_ppp - hca_full
    home_full_proj = total_expected_poss * home_ppp + hca_full

    # First half projection
    half_poss = game_pace * HALF_POSS_SHARE

    if state == "pre":
        hca_half = hca_full * HALF_POSS_SHARE
        away_1h_proj = half_poss * away_ppp - hca_half
        home_1h_proj = half_poss * home_ppp + hca_half
    elif period <= 2 and state == "in":
        if period == 1:
            elapsed_1h_min = QUARTER_MIN - (clock_sec / 60.0)
        else:
            elapsed_1h_min = QUARTER_MIN + (QUARTER_MIN - (clock_sec / 60.0))
        used_1h_poss = game_pace * (elapsed_1h_min / REGULATION_MIN)
        remaining_1h_poss = max(half_poss - used_1h_poss, 0.0)
        hca_1h_remaining = (hca_pts / 2.0) * (remaining_1h_poss / game_pace) if game_pace > 0 else 0
        away_1h_proj = game["away_score"] + remaining_1h_poss * away_ppp - hca_1h_remaining
        home_1h_proj = game["home_score"] + remaining_1h_poss * home_ppp + hca_1h_remaining
    else:
        a1h = game.get("away_1h_score")
        h1h = game.get("home_1h_score")
        if a1h is not None and h1h is not None:
            away_1h_proj = float(a1h)
            home_1h_proj = float(h1h)
        else:
            hca_half = hca_full * HALF_POSS_SHARE
            away_1h_proj = half_poss * away_ppp - hca_half
            home_1h_proj = half_poss * home_ppp + hca_half

    proj_1h_total = away_1h_proj + home_1h_proj
    proj_1h_spread = home_1h_proj - away_1h_proj
    h1_is_actual = (state != "pre" and period >= 3 and
                    game.get("away_1h_score") is not None)

    return {
        **game,
        "away_csv": away_csv,
        "home_csv": home_csv,
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
        "blowout_regress": round(blowout_regress * 100, 1),
        "hca_pts_used": hca_pts,
    }


def compute_live_pace_stats(g: dict) -> dict:
    """Estimate current pace from the live box score and extrapolate.

    Poss = FGA - ORB + TOV + 0.44 x FTA per team, averaged across both
    teams, normalized to 40 minutes of elapsed game time. If that pace
    and each team's current points-per-possession hold, project the
    final score, total, and margin — and, while the first half is still
    in progress, the 1H final score, total, and margin.
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

    # 1H extrapolation — only while the first half is still in progress
    # (after halftime the actual 1H score is known and shown instead)
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

    return out


# ── HTML Template ──

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WNBA Live Projections</title>
<style>
  :root {
    --bg: #1a0e1a;
    --card-bg: #261626;
    --card-border: #3d2540;
    --text: #f3e7f0;
    --text-muted: #9a8590;
    --accent: #e040a0;
    --green: #4caf50;
    --blue: #2196f3;
    --amber: #ffc107;
    --header-bg: #100610;
    --pink: #c2185b;
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
    border-bottom: 2px solid var(--pink);
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
  header h1 span { color: var(--pink); }
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
    background: rgba(194,24,91,0.18);
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
    background: var(--pink);
    color: white;
    font-size: 0.75em;
    padding: 1px 8px;
    border-radius: 10px;
  }
  .section-header.upcoming .count { background: var(--blue); }
  .section-header.completed .count { background: var(--text-muted); }

  .game-card {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-left: 4px solid var(--pink);
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
  .neutral-badge {
    display: inline-block;
    background: rgba(255,193,7,0.25);
    color: var(--amber);
    font-size: 0.65em;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 3px;
    margin-left: 6px;
    text-transform: uppercase;
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
  .fouls-row .foul-half {
    color: var(--text);
  }
  .fouls-row .foul-half .num {
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

  .table-wrap {
    overflow-x: auto;
    margin-bottom: 20px;
  }
  .compact-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82em;
    min-width: 900px;
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
    border-bottom: 1px solid rgba(61,37,64,0.5);
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

  .spread-home { color: var(--green); }
  .spread-away { color: var(--blue); }

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
  <h1><span>&#9794;</span> WNBA Live Projections</h1>
  <div class="header-meta">
    <span>{{ date_display }}</span>
    <span>{{ total_games }} games</span>
    <span>Ratings: {{ ratings_source }}</span>
    <span class="hca">HCA: &plusmn;{{ hca_half }}</span>
    <span style="color:var(--amber)" title="Regression to the Mean: at {{ blowout_threshold }}+ pt leads, projections regress toward league avg">RTM: {{ blowout_threshold }}+ pts</span>
    <a href="/refresh">Refresh Ratings</a>
    <span id="countdown-wrap">Next update: <span id="countdown">30</span>s</span>
  </div>
</header>

<div class="container">

  {% if error %}
  <div class="error-banner">{{ error }}</div>
  {% endif %}

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
          {% if g.neutral_site %}<span class="neutral-badge">Neutral</span>{% endif %}
        </div>
        <div class="score-block">
          <div class="score-line">
            <span class="{{ 'winning' if g.away_score > g.home_score else '' }}">{{ g.away_score }}</span>
            <span class="dash">-</span>
            <span class="{{ 'winning' if g.home_score > g.away_score else '' }}">{{ g.home_score }}</span>
          </div>
          <div class="clock-line">{{ g.status_detail }}</div>
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
      <div class="fouls-row">
        <div class="foul-team">
          <span class="foul-label">{{ g.away_abbrev }}</span>
          <span class="foul-half">Total: <span class="num">{{ g.away_total_fouls }}</span></span>
          <span class="foul-half">Qtr: <span class="num">{{ g.away_current_qtr_fouls }}</span></span>
          {% if g.away_current_qtr_fouls >= 5 %}<span class="bonus">BONUS</span>{% endif %}
        </div>
        <span class="foul-center">Team Fouls</span>
        <div class="foul-team">
          {% if g.home_current_qtr_fouls >= 5 %}<span class="bonus">BONUS</span>{% endif %}
          <span class="foul-half">Qtr: <span class="num">{{ g.home_current_qtr_fouls }}</span></span>
          <span class="foul-half">Total: <span class="num">{{ g.home_total_fouls }}</span></span>
          <span class="foul-label">{{ g.home_abbrev }}</span>
        </div>
      </div>
      <div class="detail-row">
        <span>{{ g.away_abbrev }}: OE {{ g.away_oe }} | DE {{ g.away_de }} | Pace {{ g.away_pace }} <span class="pace-pct {{ g.away_pace_cls }}">{{ g.away_pace_pct }}%</span>{{ "" if g.has_away_data else " &#9888;" }}</span>
        <span>Exp Pace: {{ g.game_pace }} | Poss Rem: {{ g.poss_remaining }} | <span class="hca">HCA &plusmn;{{ "%.1f"|format(g.hca_pts_used / 2) }}</span>{% if g.blowout_regress > 0 %} | <span style="color:var(--amber)">RTM {{ g.blowout_regress }}%</span>{% endif %}{% if g.live_box_poss is not none %} | Actual Poss: {{ g.live_box_poss }} | Actual Final: {{ g.pace_away_final }} - {{ g.pace_home_final }}{% endif %}{% if g.pace_1h_total is not none %} | Actual 1H: {{ g.pace_away_1h_final }} - {{ g.pace_home_1h_final }}{% endif %}</span>
        <span>{{ g.home_abbrev }}: OE {{ g.home_oe }} | DE {{ g.home_de }} | Pace {{ g.home_pace }} <span class="pace-pct {{ g.home_pace_cls }}">{{ g.home_pace_pct }}%</span>{{ "" if g.has_home_data else " &#9888;" }}</span>
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="no-games">No live games right now</div>
  {% endif %}
  </div>

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
        <td>{{ g.start_time_str }}</td>
        <td class="team-cell">{{ g.away_name }}{% if g.neutral_site %} <span class="neutral-badge">N</span>{% endif %}{{ "" if g.has_away_data else " &#9888;" }}</td>
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
const IDLE_INTERVAL = 300;
let interval = {{ 30 if live|length > 0 else 300 }};
let countdown = interval;

function toggleSection(id) {
  const el = document.getElementById(id + '-container');
  el.classList.toggle('hidden');
}

async function refreshGames() {
  try {
    const resp = await fetch('/api/games');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    document.getElementById('live-container').innerHTML = data.live_html;
    document.getElementById('upcoming-container').innerHTML = data.upcoming_html;
    const compEl = document.getElementById('completed-container');
    const wasHidden = compEl.classList.contains('hidden');
    compEl.innerHTML = data.completed_html;
    if (wasHidden) compEl.classList.add('hidden');
    else compEl.classList.remove('hidden');
    document.getElementById('live-count').textContent = data.live_count;
    document.getElementById('upcoming-count').textContent = data.upcoming_count;
    document.getElementById('completed-count').textContent = data.completed_count;
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

# Partials for AJAX refresh
LIVE_PARTIAL = r"""{% if games %}
  {% for g in games %}
  <div class="game-card">
    <div class="teams-row">
      <div class="team away">
        {% if g.away_logo %}<img src="{{ g.away_logo }}" alt="">{% endif %}
        <span class="team-name">{{ g.away_name }}</span>
        {% if g.neutral_site %}<span class="neutral-badge">Neutral</span>{% endif %}
      </div>
      <div class="score-block">
        <div class="score-line">
          <span class="{{ 'winning' if g.away_score > g.home_score else '' }}">{{ g.away_score }}</span>
          <span class="dash">-</span>
          <span class="{{ 'winning' if g.home_score > g.away_score else '' }}">{{ g.home_score }}</span>
        </div>
        <div class="clock-line">{{ g.status_detail }}</div>
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
    <div class="fouls-row">
      <div class="foul-team">
        <span class="foul-label">{{ g.away_abbrev }}</span>
        <span class="foul-half">Total: <span class="num">{{ g.away_total_fouls }}</span></span>
        <span class="foul-half">Qtr: <span class="num">{{ g.away_current_qtr_fouls }}</span></span>
        {% if g.away_current_qtr_fouls >= 5 %}<span class="bonus">BONUS</span>{% endif %}
      </div>
      <span class="foul-center">Team Fouls</span>
      <div class="foul-team">
        {% if g.home_current_qtr_fouls >= 5 %}<span class="bonus">BONUS</span>{% endif %}
        <span class="foul-half">Qtr: <span class="num">{{ g.home_current_qtr_fouls }}</span></span>
        <span class="foul-half">Total: <span class="num">{{ g.home_total_fouls }}</span></span>
        <span class="foul-label">{{ g.home_abbrev }}</span>
      </div>
    </div>
    <div class="detail-row">
      <span>{{ g.away_abbrev }}: OE {{ g.away_oe }} | DE {{ g.away_de }} | Pace {{ g.away_pace }} <span class="pace-pct {{ g.away_pace_cls }}">{{ g.away_pace_pct }}%</span>{{ "" if g.has_away_data else " &#9888;" }}</span>
      <span>Exp Pace: {{ g.game_pace }} | Poss Rem: {{ g.poss_remaining }} | <span class="hca">HCA &plusmn;{{ "%.1f"|format(g.hca_pts_used / 2) }}</span>{% if g.blowout_regress > 0 %} | <span style="color:var(--amber)">RTM {{ g.blowout_regress }}%</span>{% endif %}{% if g.live_box_poss is not none %} | Actual Poss: {{ g.live_box_poss }} | Actual Final: {{ g.pace_away_final }} - {{ g.pace_home_final }}{% endif %}{% if g.pace_1h_total is not none %} | Actual 1H: {{ g.pace_away_1h_final }} - {{ g.pace_home_1h_final }}{% endif %}</span>
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
      <td>{{ g.start_time_str }}</td>
      <td class="team-cell">{{ g.away_name }}{% if g.neutral_site %} <span class="neutral-badge">N</span>{% endif %}{{ "" if g.has_away_data else " &#9888;" }}</td>
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

_cache_lock = threading.Lock()
_cache: dict = {
    "games": [],
    "fetched_at": 0.0,
    "error": None,
}
CACHE_TTL = 20


# Show every game tipping off within this rolling window (not just "today").
WINDOW_HOURS = 48
WINDOW_DAYS = 3          # calendar days to fetch to cover a rolling 48h window


def get_date_str() -> tuple[str, datetime]:
    """Get the base date as YYYYMMDD string and an ET-aware datetime."""
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


def fetch_and_project() -> tuple[list[dict], list[dict], list[dict], str, str | None]:
    _, dt = get_date_str()
    date_display = dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")

    games, error = _fetch_scoreboard_cached()
    projected = [project_game(g) for g in games]

    foul_map = fetch_fouls_for_live_games(projected)
    for g in projected:
        fouls = foul_map.get(g["game_id"], None)
        if fouls:
            g.update(fouls)
        else:
            g.update({
                "away_total_fouls": 0, "away_current_qtr_fouls": 0,
                "home_total_fouls": 0, "home_current_qtr_fouls": 0,
                "current_period": 0,
            })
        g.update(compute_live_pace_stats(g))

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
        hca_half=round(HCA_POINTS / 2.0, 2),
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
    """Re-scrape Basketball-Reference WNBA and reload ratings."""
    try:
        print("Scraping Basketball-Reference WNBA...")
        teams = scrape_bref_ratings()
        if teams:
            save_ratings_csv(teams)
            print(f"  Saved {len(teams)} teams to CSV.")
        else:
            print("  Scrape returned no teams. Reloading existing CSV.")
        load_all_ratings()
    except Exception as e:
        print(f"  Scrape failed: {e}. Reloading from existing CSV.")
        load_all_ratings()
    return redirect(url_for("index"))


# ── Entry point ──

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WNBA Live Projections Dashboard")
    parser.add_argument("--port", type=int, default=5005, help="Port (default: 5005)")
    parser.add_argument("--date", type=str, default=None, help="Date override YYYY-MM-DD")
    args = parser.parse_args()

    if args.date:
        DATE_OVERRIDE = args.date

    print("=" * 50)
    print("  WNBA Live Projections Dashboard")
    print("=" * 50)

    load_all_ratings()

    date_str, dt = get_date_str()
    print(f"\nDate: {dt.strftime('%A, %B')} {dt.day}, {dt.year}")
    print(f"Server: http://localhost:{args.port}")
    print(f"Home Court Advantage: {HCA_POINTS} pts (±{HCA_POINTS / 2:.2f} per side, 0 at neutral sites)")
    print(f"Blowout RTM: kicks in at {BLOWOUT_THRESHOLD}+ pt lead, up to {BLOWOUT_MAX_REGRESS*100:.0f}% regression at {BLOWOUT_LEAD_CAP}+ pts")
    print("Press Ctrl+C to stop.\n")

    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
