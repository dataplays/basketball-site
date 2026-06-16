"""
Live International Basketball Projections Dashboard

Flask web app that shows:
  - Live games from ESPN (G League, Australian NBL) with projected final scores
  - European league ratings & pre-game projections from eurobasket.com

Ratings sources:
  - ESPN leagues: Core API records endpoint (PPG/OPPG per team)
  - European leagues: eurobasket.com subscriber standings (PPG/OPPG)

Usage:
    py -3 intl_live_projections.py              # today's games, port 5003
    py -3 intl_live_projections.py --port 8080  # custom port
    py -3 intl_live_projections.py --date 2026-03-01  # specific date
"""

import argparse
import io
import json
import os
import re
import sys
import time
import threading

# Fix Windows console encoding for emoji output
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import requests as _requests

from flask import Flask, jsonify, redirect, render_template_string, url_for

# ── Constants ──

ET = ZoneInfo("America/New_York")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues"

# ── Eurobasket.com credentials & config ──

EUROBASKET_LOGIN_URL = "https://www.eurobasket.com/news_system/ndverifikacijasub.aspx"
EUROBASKET_STANDINGS_URL = "https://www.eurobasket.com/Ajax/fullstandings.aspx"
EUROBASKET_EMAIL = "dataplays@yahoo.com"
EUROBASKET_PWD = "Sports1!"

# ── League definitions ──
# ESPN-powered leagues (live scores + ratings from ESPN Core API)

LEAGUES: dict[str, dict] = {
    "nba-development": {
        "name": "NBA G League",
        "short": "G-League",
        "emoji": "🏀",
        "reg_min": 48.0,
        "qtr_min": 12.0,
        "ot_min": 2.0,
        "hca": 2.5,
        "has_pbp": True,
        "accent": "#00b894",
    },
    "nbl": {
        "name": "Australian NBL",
        "short": "NBL",
        "emoji": "🦘",
        "reg_min": 40.0,
        "qtr_min": 10.0,
        "ot_min": 5.0,
        "hca": 3.5,
        "has_pbp": False,
        "accent": "#fdcb6e",
    },
}

# Eurobasket-powered leagues (ratings only — no live scores from ESPN)
# Format: key -> {name, short, emoji, accent, section_id, league_num, is_cup,
#                 reg_min, qtr_min, ot_min, hca}
# section_id + league_num come from eurobasket AJAX: openFullStandings(SectionId, League, isCup)

EURO_LEAGUES: dict[str, dict] = {
    "euroleague": {
        "name": "EuroLeague",
        "short": "EuroLge",
        "emoji": "🇪🇺",
        "accent": "#e84118",
        "section_id": 95, "league_num": 1, "is_cup": 0,

        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "eurocup": {
        "name": "EuroCup",
        "short": "EuroCup",
        "emoji": "🇪🇺",
        "accent": "#e67e22",
        "section_id": 134, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "spain-liga-endesa": {
        "name": "Liga Endesa (Spain)",
        "short": "ACB",
        "emoji": "🇪🇸",
        "accent": "#c0392b",
        "section_id": 2, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "france-betclic": {
        "name": "Betclic Elite (France)",
        "short": "LNB",
        "emoji": "🇫🇷",
        "accent": "#2980b9",
        "section_id": 4, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "germany-bbl": {
        "name": "BBL (Germany)",
        "short": "BBL",
        "emoji": "🇩🇪",
        "accent": "#d35400",
        "section_id": 52, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "italy-serie-a": {
        "name": "Serie A (Italy)",
        "short": "LBA",
        "emoji": "🇮🇹",
        "accent": "#27ae60",
        "section_id": 1, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "greece-gbl": {
        "name": "GBL (Greece)",
        "short": "GBL",
        "emoji": "🇬🇷",
        "accent": "#2196f3",
        "section_id": 3, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "turkey-bsl": {
        "name": "BSL (Turkey)",
        "short": "BSL",
        "emoji": "🇹🇷",
        "accent": "#e74c3c",
        "section_id": 5, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "israel-winner": {
        "name": "Winner League (Israel)",
        "short": "ISR",
        "emoji": "🇮🇱",
        "accent": "#3498db",
        "section_id": 7, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "lithuania-lkl": {
        "name": "LKL (Lithuania)",
        "short": "LKL",
        "emoji": "🇱🇹",
        "accent": "#f39c12",
        "section_id": 11, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "serbia-kls": {
        "name": "KLS (Serbia)",
        "short": "KLS",
        "emoji": "🇷🇸",
        "accent": "#8e44ad",
        "section_id": 9, "league_num": 1, "is_cup": 0,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
    "bcl": {
        "name": "Basketball Champions League",
        "short": "BCL",
        "emoji": "🏆",
        "accent": "#f1c40f",
        "section_id": 332, "league_num": 1, "is_cup": 1,
        "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5,
    },
}


# ── Blowout regression to the mean ──
BLOWOUT_THRESHOLD = 15
BLOWOUT_MAX_REGRESS = 0.40
BLOWOUT_LEAD_CAP = 30

# ── Cached ratings per league ──
# Structure: {league_slug: {team_name: {"ppg": X, "oppg": X, "pace": X}}}

RATINGS: dict[str, dict] = {}
EURO_RATINGS: dict[str, dict] = {}  # eurobasket-powered league ratings
RATINGS_LOADED_AT: datetime | None = None

# ── Eurobasket session (reusable) ──

_euro_session: _requests.Session | None = None


def _get_euro_session() -> _requests.Session:
    """Get an authenticated eurobasket.com session, logging in if needed."""
    global _euro_session
    if _euro_session is not None:
        # Check if still valid (has cookies)
        if any(c.name == "PREMIUM" for c in _euro_session.cookies):
            return _euro_session

    s = _requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    s.post(EUROBASKET_LOGIN_URL, data={
        "email": EUROBASKET_EMAIL,
        "pwd": EUROBASKET_PWD,
        "B1": "Login",
        "Referal": "",
    }, timeout=15, allow_redirects=True)

    cookie_count = len(list(s.cookies))
    if cookie_count > 0:
        print(f"    Eurobasket login OK ({cookie_count} cookies)")
    else:
        print("    [WARN] Eurobasket login may have failed (0 cookies)")

    _euro_session = s
    return s


def fetch_euro_standings(section_id: int, league_num: int, is_cup: int) -> list[dict]:
    """
    Fetch standings from eurobasket.com AJAX endpoint.
    Returns list of {team, gp, w, l, ppg, oppg, diff}.
    """
    s = _get_euro_session()
    cookie_str = "; ".join(
        f"{c.name}={c.value}" for c in s.cookies if c.domain == "www.eurobasket.com"
    )
    r = _requests.post(
        EUROBASKET_STANDINGS_URL,
        data=f"SectionId={section_id}&League={league_num}&isCup={is_cup}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Cookie": cookie_str,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )

    teams = []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.DOTALL)
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        # Valid rows: rank(digit), team, GP, W, L, Win%, ... PPG, OPPG, Diff, ...
        if len(clean) >= 12 and clean[0].isdigit():
            try:
                teams.append({
                    "team": clean[1],
                    "gp": int(clean[2]),
                    "w": int(clean[3]),
                    "l": int(clean[4]),
                    "ppg": float(clean[9]),
                    "oppg": float(clean[10]),
                    "diff": float(clean[11]),
                })
            except (ValueError, IndexError):
                continue

    return teams


def load_euro_league_ratings(key: str, cfg: dict) -> dict[str, dict]:
    """Load ratings for one eurobasket league. Returns {team: {ppg, oppg, pace}}."""
    print(f"  Loading {cfg['name']}...")
    try:
        standings = fetch_euro_standings(cfg["section_id"], cfg["league_num"], cfg["is_cup"])
    except Exception as e:
        print(f"    [WARN] Failed: {e}")
        return {}

    # Deduplicate: eurobasket may show overall + home/away splits
    # Take the first occurrence (overall) for each team
    seen = set()
    ratings = {}
    for t in standings:
        name = t["team"]
        if name in seen:
            continue
        seen.add(name)
        ppg = t["ppg"]
        oppg = t["oppg"]
        pace_est = (ppg + oppg) / 2.0
        ratings[name] = {
            "ppg": round(ppg, 1),
            "oppg": round(oppg, 1),
            "pace": round(pace_est, 1),
            "gp": t["gp"],
            "w": t["w"],
            "l": t["l"],
        }

    print(f"    Got {len(ratings)} teams.")
    return ratings


# ── Data loading: ESPN team statistics ──

def _fetch_json(url: str, timeout: int = 15) -> dict:
    """Fetch JSON from URL with standard headers."""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_league_teams(slug: str) -> list[dict]:
    """Fetch all teams for a league from ESPN."""
    url = f"{ESPN_BASE}/{slug}/teams?limit=100"
    try:
        data = _fetch_json(url)
    except Exception as e:
        print(f"  [WARN] Could not fetch teams for {slug}: {e}")
        return []

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


def fetch_team_record(slug: str, team_id: str) -> dict | None:
    """
    Fetch season PPG/OPPG from ESPN core API records endpoint.
    Returns {"ppg": float, "oppg": float, "pace": float} or None.
    """
    url = (
        f"{ESPN_CORE}/{slug}/seasons/2026/types/2/teams/{team_id}/records/0"
    )
    try:
        data = _fetch_json(url, timeout=10)
    except Exception:
        # Try current year -1 as fallback (season numbering varies)
        try:
            url2 = f"{ESPN_CORE}/{slug}/seasons/2025/types/2/teams/{team_id}/records/0"
            data = _fetch_json(url2, timeout=10)
        except Exception:
            return None

    stats = {}
    for s in data.get("stats", []):
        stats[s.get("name", "")] = s.get("value", 0)

    ppg = stats.get("avgPointsFor", 0)
    oppg = stats.get("avgPointsAgainst", 0)

    if not ppg:
        return None

    # Estimate pace from total scoring (proxy)
    league = LEAGUES.get(slug, {})
    reg_min = league.get("reg_min", 48.0)
    raw_pace = (ppg + oppg) / 2.0
    pace_est = raw_pace * (reg_min / 48.0) if reg_min != 48.0 else raw_pace

    return {
        "ppg": round(ppg, 1),
        "oppg": round(oppg, 1) if oppg else round(ppg, 1),
        "pace": round(pace_est, 1),
    }



def load_league_ratings(slug: str) -> dict[str, dict]:
    """
    Load ratings for a league from ESPN core API records.
    Returns {team_name: {"ppg": X, "oppg": X, "pace": X}}.
    """
    print(f"  Loading ratings for {LEAGUES[slug]['name']}...")
    teams = fetch_league_teams(slug)
    if not teams:
        print(f"    No teams found.")
        return {}

    ratings = {}

    def _fetch_one(team: dict) -> tuple[str, dict | None]:
        name = team["location"] or team["displayName"]
        stats = fetch_team_record(slug, team["id"])
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

    print(f"    Got stats for {len(ratings)}/{len(teams)} teams.")
    return ratings


def load_all_ratings() -> None:
    """Load ratings for all configured leagues (ESPN + eurobasket)."""
    global RATINGS, EURO_RATINGS, RATINGS_LOADED_AT

    print("Loading ESPN league ratings...")
    for slug in LEAGUES:
        RATINGS[slug] = load_league_ratings(slug)

    print("\nLoading European league ratings from eurobasket.com...")
    for key, cfg in EURO_LEAGUES.items():
        EURO_RATINGS[key] = load_euro_league_ratings(key, cfg)

    RATINGS_LOADED_AT = datetime.now(ET)
    total_espn = sum(len(v) for v in RATINGS.values())
    total_euro = sum(len(v) for v in EURO_RATINGS.values())
    print(f"\nRatings loaded at {RATINGS_LOADED_AT.strftime('%I:%M %p ET')}.")
    print(f"  ESPN: {total_espn} teams across {len(RATINGS)} leagues")
    print(f"  Eurobasket: {total_euro} teams across {len(EURO_RATINGS)} leagues")


# ── Euroleague API: European league game schedules ──

EUROLEAGUE_API = "https://api-live.euroleague.net/v2"

# Map EURO_LEAGUES keys to Euroleague API competition codes + season codes
# Only leagues available in the Euroleague API are mapped here
EUROLEAGUE_API_LEAGUES: dict[str, dict] = {
    "euroleague": {"comp": "E", "season": "E2025"},
    "eurocup": {"comp": "U", "season": "U2025"},
}

# Cache for Euroleague API full-season game lists (fetched once, reused)
_euroleague_games_cache: dict[str, tuple[list[dict], float]] = {}
_EUROLEAGUE_GAMES_CACHE_TTL = 120  # seconds


def _fetch_euroleague_season_games(comp_code: str, season_code: str) -> list[dict]:
    """Fetch all games for a Euroleague API competition/season."""
    url = f"{EUROLEAGUE_API}/competitions/{comp_code}/seasons/{season_code}/games?seasonMode=All"
    try:
        resp = _requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
        # Response may have trailing data; parse first JSON object
        text = resp.text.strip()
        data = json.loads(text.split("\n")[0])
        return data.get("data", [])
    except Exception as e:
        print(f"  [WARN] Euroleague API fetch failed for {comp_code}: {e}")
        return []


def _get_euroleague_games_cached(comp_code: str, season_code: str) -> list[dict]:
    """Get season games with caching."""
    now = time.monotonic()
    key = f"{comp_code}_{season_code}"
    if key in _euroleague_games_cache:
        games, ts = _euroleague_games_cache[key]
        if now - ts < _EUROLEAGUE_GAMES_CACHE_TTL:
            return games
    games = _fetch_euroleague_season_games(comp_code, season_code)
    _euroleague_games_cache[key] = (games, time.monotonic())
    return games


def _euroleague_game_to_state(game: dict) -> str:
    """Convert Euroleague API game to ESPN-style state."""
    played = game.get("played", False)
    status = game.get("gameStatus", "")
    if played:
        return "post"
    # Check if game is live (has partial scores but not marked played)
    local_score = game.get("local", {}).get("score", 0)
    road_score = game.get("road", {}).get("score", 0)
    if (local_score > 0 or road_score > 0) and not played:
        return "in"
    return "pre"


def fetch_euroleague_api_games(date_str: str) -> list[dict]:
    """
    Fetch games for configured Euroleague API leagues on a given date.
    date_str: YYYYMMDD format (same as ESPN).
    Returns games in the same dict format as ESPN scoreboard games.
    """
    # Convert YYYYMMDD to YYYY-MM-DD for comparison
    target_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    all_games = []

    for euro_key, api_cfg in EUROLEAGUE_API_LEAGUES.items():
        cfg = EURO_LEAGUES[euro_key]
        season_games = _get_euroleague_games_cached(api_cfg["comp"], api_cfg["season"])

        for g in season_games:
            # Match by UTC date
            utc_date = g.get("utcDate", "")
            if not utc_date.startswith(target_date):
                continue

            state = _euroleague_game_to_state(g)
            local = g.get("local", {})
            road = g.get("road", {})
            local_club = local.get("club", {})
            road_club = road.get("club", {})

            home_score = local.get("score", 0) or 0
            away_score = road.get("score", 0) or 0

            # Parse start time
            try:
                utc_dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
                et_dt = utc_dt.astimezone(ET)
                time_str = (
                    et_dt.strftime("%#I:%M %p") if sys.platform == "win32"
                    else et_dt.strftime("%-I:%M %p")
                )
                sort_key = et_dt.hour * 100 + et_dt.minute
            except Exception:
                time_str = "TBD"
                sort_key = 9999

            # 1H scores from partials (Q1 + Q2)
            local_p = local.get("partials", {})
            road_p = road.get("partials", {})
            home_q1 = local_p.get("partials1")
            home_q2 = local_p.get("partials2")
            away_q1 = road_p.get("partials1")
            away_q2 = road_p.get("partials2")
            home_1h = (home_q1 + home_q2) if home_q1 is not None and home_q2 is not None else None
            away_1h = (away_q1 + away_q2) if away_q1 is not None and away_q2 is not None else None

            # Determine period from partials
            period = 0
            if state == "post":
                period = 4
                extra = local_p.get("extraPeriods", {})
                if extra:
                    period += len(extra)
            elif state == "in":
                # Estimate period from which partials exist
                for qi in range(4, 0, -1):
                    if local_p.get(f"partials{qi}") is not None:
                        period = qi
                        break

            neutral = g.get("isNeutralVenue", False)

            # Home team logo from Euroleague API images
            home_logo = local_club.get("images", {}).get("crest", "")
            away_logo = road_club.get("images", {}).get("crest", "")

            game = {
                "league_slug": euro_key,
                "league_name": cfg["name"],
                "league_short": cfg.get("short", euro_key),
                "league_emoji": cfg["emoji"],
                "league_accent": cfg["accent"],
                "game_id": f"el_{g.get('gameCode', 0)}",
                "state": state,
                "clock_seconds": 0,
                "display_clock": "0:00",
                "period": period,
                "status_detail": "Final" if state == "post" else (time_str if state == "pre" else "Live"),
                "away_name": road_club.get("name", "Away"),
                "away_abbrev": road_club.get("code", ""),
                "away_score": int(away_score),
                "away_logo": away_logo,
                "home_name": local_club.get("name", "Home"),
                "home_abbrev": local_club.get("code", ""),
                "home_score": int(home_score),
                "home_logo": home_logo,
                "away_1h_score": away_1h,
                "home_1h_score": home_1h,
                "neutral_site": neutral,
                "start_time_str": time_str,
                "start_time_sort": sort_key,
                "source": "euroleague_api",
            }
            all_games.append(game)

    return all_games


# ── ESPN scoreboard ──

def _get_linescore(competitor: dict, period_index: int) -> int | None:
    ls = competitor.get("linescores", [])
    if period_index < len(ls):
        val = ls[period_index].get("value", None)
        if val is not None:
            return int(val)
    return None


def fetch_league_scoreboard(slug: str, date_str: str) -> list[dict]:
    """Fetch all games for a league on a given date."""
    url = f"{ESPN_BASE}/{slug}/scoreboard?dates={date_str}"
    try:
        data = _fetch_json(url)
    except Exception as e:
        print(f"  [WARN] Scoreboard fetch failed for {slug}: {e}")
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
        time_str = (
            et_dt.strftime("%-I:%M %p") if sys.platform != "win32"
            else et_dt.strftime("%#I:%M %p")
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
            "league_slug": slug,
            "league_name": LEAGUES[slug]["name"],
            "league_short": LEAGUES[slug]["short"],
            "league_emoji": LEAGUES[slug]["emoji"],
            "league_accent": LEAGUES[slug]["accent"],
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
            "start_time_str": time_str,
            "start_time_sort": et_dt.hour * 100 + et_dt.minute,
        }
        games.append(game)

    return games


# ── Foul tracking ──

_foul_cache_lock = threading.Lock()
_foul_cache: dict[str, tuple[dict, float]] = {}
FOUL_CACHE_TTL = 25

EMPTY_FOULS = {
    "away_total_fouls": 0, "away_current_fouls": 0,
    "away_fouls_to_give": 0, "away_bonus": "NONE",
    "home_total_fouls": 0, "home_current_fouls": 0,
    "home_fouls_to_give": 0, "home_bonus": "NONE",
}


def fetch_game_fouls(slug: str, game_id: str) -> dict:
    """Fetch team fouls from ESPN summary API."""
    now = time.monotonic()
    with _foul_cache_lock:
        if game_id in _foul_cache:
            data, ts = _foul_cache[game_id]
            if now - ts < FOUL_CACHE_TTL:
                return data

    try:
        url = f"{ESPN_BASE}/{slug}/summary?event={game_id}"
        raw = _fetch_json(url, timeout=8)
    except Exception:
        return dict(EMPTY_FOULS)

    try:
        comps = raw.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
        result: dict = {}
        for c in comps:
            side = c.get("homeAway", "")
            fouls = c.get("fouls", {})
            if not side or not fouls:
                continue
            result[f"{side}_total_fouls"] = fouls.get("teamFouls", 0)
            result[f"{side}_current_fouls"] = fouls.get("teamFoulsCurrent", 0)
            result[f"{side}_fouls_to_give"] = fouls.get("foulsToGive", 0)
            result[f"{side}_bonus"] = fouls.get("bonusState", "NONE")

        if not result:
            result = _count_fouls_from_plays(raw)

        if result:
            for key in EMPTY_FOULS:
                result.setdefault(key, EMPTY_FOULS[key])
            with _foul_cache_lock:
                _foul_cache[game_id] = (result, time.monotonic())
            return result
    except Exception:
        pass

    return dict(EMPTY_FOULS)


def _count_fouls_from_plays(raw: dict) -> dict:
    """Fallback: count fouls from play-by-play."""
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
        tid = play.get("team", {}).get("id", "")
        period = play.get("period", {}).get("number", 0)
        side = team_ids.get(tid, "")
        if not side:
            continue
        q_fouls[side][period] = q_fouls[side].get(period, 0) + 1

    current_period = plays[-1].get("period", {}).get("number", 0) if plays else 0

    result: dict = {}
    for side in ["away", "home"]:
        total = sum(q_fouls[side].values())
        current = q_fouls[side].get(current_period, 0)
        result[f"{side}_total_fouls"] = total
        result[f"{side}_current_fouls"] = current
        result[f"{side}_fouls_to_give"] = max(0, 4 - current)
        result[f"{side}_bonus"] = "BONUS" if current >= 5 else "NONE"

    return result


def fetch_fouls_for_live_games(games: list[dict]) -> dict[str, dict]:
    """Fetch fouls for all live games with play-by-play support."""
    live = [g for g in games if g["state"] == "in" and LEAGUES.get(g["league_slug"], {}).get("has_pbp")]
    if not live:
        return {}

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(live), 8)) as pool:
        futures = {
            pool.submit(fetch_game_fouls, g["league_slug"], g["game_id"]): g["game_id"]
            for g in live
        }
        for fut in futures:
            gid = futures[fut]
            try:
                results[gid] = fut.result(timeout=10)
            except Exception:
                results[gid] = dict(EMPTY_FOULS)
    return results


# ── Projection engine ──

def _get_league_config(slug: str) -> dict:
    """Get league config from either LEAGUES or EURO_LEAGUES."""
    if slug in LEAGUES:
        return LEAGUES[slug]
    return EURO_LEAGUES.get(slug, {})


def _get_league_ratings(slug: str) -> dict:
    """Get ratings dict from either RATINGS (ESPN) or EURO_RATINGS (eurobasket)."""
    if slug in RATINGS:
        return RATINGS[slug]
    return EURO_RATINGS.get(slug, {})


def resolve_team(slug: str, team_name: str) -> str:
    """Find the best match for a team in our ratings."""
    league_ratings = _get_league_ratings(slug)
    if team_name in league_ratings:
        return team_name
    # Try partial match
    name_lower = team_name.lower()
    for key in league_ratings:
        if key.lower() == name_lower or name_lower in key.lower() or key.lower() in name_lower:
            return key
    return team_name


def pct_class(pct: int) -> str:
    """CSS class for percentile color tier."""
    if pct >= 80:
        return "pct-very-high"
    if pct >= 60:
        return "pct-high"
    if pct >= 40:
        return "pct-avg"
    if pct >= 20:
        return "pct-low"
    return "pct-very-low"


def project_game(game: dict) -> dict:
    """Compute projections for a single game."""
    slug = game["league_slug"]
    league = _get_league_config(slug)
    league_ratings = _get_league_ratings(slug)

    reg_min = league["reg_min"]
    qtr_min = league["qtr_min"]
    ot_min = league["ot_min"]
    hca_pts = league["hca"]

    away_key = resolve_team(slug, game["away_name"])
    home_key = resolve_team(slug, game["home_name"])

    # League averages
    if league_ratings:
        all_ppg = [r["ppg"] for r in league_ratings.values() if r["ppg"] > 0]
        all_oppg = [r["oppg"] for r in league_ratings.values() if r["oppg"] > 0]
        all_pace = [r["pace"] for r in league_ratings.values() if r["pace"] > 0]
        league_avg_ppg = sum(all_ppg) / len(all_ppg) if all_ppg else 100.0
        league_avg_oppg = sum(all_oppg) / len(all_oppg) if all_oppg else 100.0
        league_avg_pace = sum(all_pace) / len(all_pace) if all_pace else 95.0
        num_teams = len(league_ratings)
    else:
        league_avg_ppg = 110.0 if reg_min == 48 else 85.0
        league_avg_oppg = league_avg_ppg
        league_avg_pace = 100.0 if reg_min == 48 else 75.0
        num_teams = 1

    # Team ratings (default to league average)
    default = {"ppg": league_avg_ppg, "oppg": league_avg_oppg, "pace": league_avg_pace}
    away_r = league_ratings.get(away_key, default)
    home_r = league_ratings.get(home_key, default)

    has_away_data = away_key in league_ratings
    has_home_data = home_key in league_ratings

    away_ppg = away_r["ppg"]
    away_oppg = away_r["oppg"]
    away_pace = away_r["pace"]
    home_ppg = home_r["ppg"]
    home_oppg = home_r["oppg"]
    home_pace = home_r["pace"]

    # Compute percentiles
    def compute_pct(val: float, all_vals: list[float], higher_is_better: bool = True) -> int:
        if not all_vals:
            return 50
        sorted_vals = sorted(all_vals)
        rank = sum(1 for v in sorted_vals if v <= val)
        pct = int(rank / len(sorted_vals) * 100)
        return pct if higher_is_better else (100 - pct)

    all_ppg_vals = [r["ppg"] for r in league_ratings.values()]
    all_oppg_vals = [r["oppg"] for r in league_ratings.values()]
    all_pace_vals = [r["pace"] for r in league_ratings.values()]

    away_ppg_pct = compute_pct(away_ppg, all_ppg_vals, True)
    home_ppg_pct = compute_pct(home_ppg, all_ppg_vals, True)
    away_oppg_pct = compute_pct(away_oppg, all_oppg_vals, False)  # lower is better
    home_oppg_pct = compute_pct(home_oppg, all_oppg_vals, False)
    away_pace_pct = compute_pct(away_pace, all_pace_vals, True)
    home_pace_pct = compute_pct(home_pace, all_pace_vals, True)

    # Game pace estimate
    game_pace = (away_pace + home_pace) / 2.0

    # Projected scoring using opponent-adjusted model:
    # away_proj = (away_ppg * home_oppg) / league_avg_ppg
    # This adjusts for strength of opponent defense
    away_proj_full = (away_ppg * home_oppg) / league_avg_ppg if league_avg_ppg > 0 else away_ppg
    home_proj_full = (home_ppg * away_oppg) / league_avg_ppg if league_avg_ppg > 0 else home_ppg

    # HCA adjustment (0 for neutral site)
    hca = 0.0 if game.get("neutral_site") else hca_pts
    away_proj_full -= hca / 2.0
    home_proj_full += hca / 2.0

    # Time calculations
    period = game["period"]
    clock_sec = float(game["clock_seconds"])
    state = game["state"]
    detail = game.get("status_detail", "").lower()

    if state == "pre":
        time_elapsed = 0.0
        total_game_min = reg_min
    elif state == "post":
        if period <= 4:
            total_game_min = reg_min
        else:
            total_game_min = reg_min + ot_min * (period - 4)
        time_elapsed = total_game_min
    else:
        clock_min = clock_sec / 60.0
        if "halftime" in detail or ("half" in detail and clock_min < 0.1):
            time_elapsed = reg_min / 2.0
            total_game_min = reg_min
        elif "end" in detail and period <= 4:
            time_elapsed = period * qtr_min
            total_game_min = reg_min
        elif period <= 4:
            time_elapsed = (period - 1) * qtr_min + (qtr_min - clock_min)
            total_game_min = reg_min
        else:
            ot_num = period - 4
            time_elapsed = reg_min + (ot_num - 1) * ot_min + (ot_min - clock_min)
            total_game_min = reg_min + ot_min * ot_num

    time_remaining = max(0.0, total_game_min - time_elapsed)

    # Points per minute rates
    away_ppm = away_proj_full / reg_min if reg_min > 0 else 0
    home_ppm = home_proj_full / reg_min if reg_min > 0 else 0

    # Blowout regression to the mean
    blowout_regress = 0.0
    if state == "in" and time_elapsed > 0:
        lead = abs(game["home_score"] - game["away_score"])
        if lead >= BLOWOUT_THRESHOLD:
            lead_frac = min((lead - BLOWOUT_THRESHOLD) / (BLOWOUT_LEAD_CAP - BLOWOUT_THRESHOLD), 1.0)
            time_frac = time_remaining / reg_min
            blowout_regress = BLOWOUT_MAX_REGRESS * lead_frac * time_frac

    # Apply regression: pull toward league-average PPM
    league_avg_ppm = league_avg_ppg / reg_min if reg_min > 0 else 2.0
    if game["away_score"] < game["home_score"]:
        # Away is trailing → boost toward avg; Home is leading → pull toward avg
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

    away_final = game["away_score"] + away_proj_remaining
    home_final = game["home_score"] + home_proj_remaining

    proj_total = away_final + home_final
    proj_spread = home_final - away_final

    # 1H projection
    half_min = reg_min / 2.0
    if state == "pre":
        away_1h_proj = half_min * (away_proj_full / reg_min)
        home_1h_proj = half_min * (home_proj_full / reg_min)
    elif period <= 2 and state == "in":
        if period == 1:
            elapsed_1h = qtr_min - (clock_sec / 60.0)
        else:
            elapsed_1h = qtr_min + (qtr_min - (clock_sec / 60.0))
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
            away_1h_proj = half_min * (away_proj_full / reg_min)
            home_1h_proj = half_min * (home_proj_full / reg_min)

    proj_1h_total = away_1h_proj + home_1h_proj
    proj_1h_spread = home_1h_proj - away_1h_proj
    h1_is_actual = (state != "pre" and period >= 3 and game.get("away_1h_score") is not None)

    # Estimated possessions (using game_pace as proxy)
    poss_per_min = game_pace / reg_min if reg_min > 0 else 2.0
    poss_so_far = poss_per_min * time_elapsed
    poss_remaining = poss_per_min * time_remaining
    total_poss = poss_per_min * total_game_min

    return {
        **game,
        "has_away_data": has_away_data,
        "has_home_data": has_home_data,
        "away_ppg": round(away_ppg, 1),
        "away_oppg": round(away_oppg, 1),
        "away_pace": round(away_pace, 1),
        "home_ppg": round(home_ppg, 1),
        "home_oppg": round(home_oppg, 1),
        "home_pace": round(home_pace, 1),
        "away_ppg_pct": away_ppg_pct,
        "home_ppg_pct": home_ppg_pct,
        "away_oppg_pct": away_oppg_pct,
        "home_oppg_pct": home_oppg_pct,
        "away_pace_pct": away_pace_pct,
        "home_pace_pct": home_pace_pct,
        "away_ppg_cls": pct_class(away_ppg_pct),
        "home_ppg_cls": pct_class(home_ppg_pct),
        "away_oppg_cls": pct_class(away_oppg_pct),
        "home_oppg_cls": pct_class(home_oppg_pct),
        "away_pace_cls": pct_class(away_pace_pct),
        "home_pace_cls": pct_class(home_pace_pct),
        "game_pace": round(game_pace, 1),
        "time_elapsed": round(time_elapsed, 1),
        "time_remaining": round(time_remaining, 1),
        "poss_so_far": round(poss_so_far, 1),
        "poss_remaining": round(poss_remaining, 1),
        "total_expected_poss": round(total_poss, 1),
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


# ── HTML Template ──

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>International Basketball Live Projections</title>
<style>
  :root {
    --bg: #0f1923;
    --card-bg: #1a2634;
    --card-border: #2a3a4a;
    --text: #e8edf2;
    --text-muted: #8899aa;
    --accent: #00b894;
    --green: #4caf50;
    --blue: #2196f3;
    --amber: #ffc107;
    --header-bg: #0a1218;
    --teal: #00b894;
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
    border-bottom: 2px solid var(--teal);
    padding: 16px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
  }
  header h1 { font-size: 1.4em; font-weight: 700; color: var(--text); }
  header h1 span { color: var(--teal); }
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
    background: rgba(0,184,148,0.15);
    color: var(--teal);
    padding: 2px 10px;
    border-radius: 4px;
    font-weight: 600;
    font-size: 0.9em;
  }
  .container { max-width: 1100px; margin: 0 auto; padding: 16px; }

  .league-header {
    font-size: 1.0em;
    font-weight: 700;
    padding: 8px 12px;
    margin: 16px 0 8px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .league-badge {
    font-size: 0.7em;
    padding: 2px 8px;
    border-radius: 10px;
    color: white;
    font-weight: 600;
  }

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
    background: var(--teal);
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
    border-left: 4px solid var(--teal);
    border-radius: 8px;
    padding: 14px 18px;
    margin-bottom: 12px;
    transition: border-color 0.2s;
  }
  .game-card:hover { border-color: var(--teal); }
  .game-card.pre { border-left-color: var(--blue); }
  .game-card.post { border-left-color: var(--text-muted); }

  .card-league-tag {
    font-size: 0.7em;
    padding: 1px 8px;
    border-radius: 8px;
    color: white;
    font-weight: 600;
    margin-bottom: 8px;
    display: inline-block;
  }

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
  .team img { width: 28px; height: 28px; object-fit: contain; flex-shrink: 0; }
  .team-name {
    font-weight: 600;
    font-size: 0.95em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 200px;
  }
  .score-block { text-align: center; min-width: 140px; flex-shrink: 0; }
  .score-line {
    font-size: 1.8em;
    font-weight: 800;
    letter-spacing: 2px;
    font-variant-numeric: tabular-nums;
  }
  .score-line .dash { color: var(--text-muted); margin: 0 6px; }
  .winning { color: var(--green); }
  .clock-line { font-size: 0.8em; color: var(--teal); font-weight: 600; margin-top: 2px; }
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

  .detail-row {
    margin-top: 6px;
    font-size: 0.7em;
    color: var(--text-muted);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 4px;
  }
  .detail-row .hca { color: var(--teal); font-weight: 600; }

  .pct-badge {
    display: inline-block;
    font-size: 0.85em;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 3px;
    margin-left: 3px;
    min-width: 30px;
    text-align: center;
  }
  .pct-very-high { background: rgba(255,68,68,0.3); color: #ff6666; }
  .pct-high { background: rgba(255,140,0,0.3); color: #ffaa33; }
  .pct-avg { background: rgba(255,193,7,0.25); color: #ffc107; }
  .pct-low { background: rgba(76,175,80,0.3); color: #66bb6a; }
  .pct-very-low { background: rgba(33,150,243,0.3); color: #64b5f6; }

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
  .fouls-row .foul-team { display: flex; align-items: center; gap: 10px; }
  .fouls-row .foul-label { color: var(--text-muted); font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.5px; }
  .fouls-row .foul-half { color: var(--text); }
  .fouls-row .foul-half .num { font-weight: 700; min-width: 14px; display: inline-block; text-align: center; }
  .fouls-row .bonus { color: var(--amber); font-weight: 700; font-size: 0.85em; }
  .fouls-row .dbl-bonus { color: #ff4444; font-weight: 700; font-size: 0.85em; }
  .fouls-row .foul-center { color: var(--text-muted); font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.5px; }

  .neutral-badge {
    font-size: 0.65em;
    background: rgba(33,150,243,0.2);
    color: var(--blue);
    padding: 1px 6px;
    border-radius: 4px;
    font-weight: 600;
    margin-left: 4px;
  }

  .table-wrap { overflow-x: auto; margin-bottom: 20px; }
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
    background: rgba(255,68,68,0.15);
    border: 1px solid #ff4444;
    border-radius: 6px;
    padding: 10px 16px;
    margin-bottom: 16px;
    color: #ff4444;
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
  <h1><span>&#127760;</span> International Basketball Live Projections</h1>
  <div class="header-meta">
    <span>{{ date_display }}</span>
    <span>{{ total_games }} games across {{ league_count }} leagues</span>
    {% for lname, lcount in league_summary %}
      <span>{{ lname }}: {{ lcount }}</span>
    {% endfor %}
    <a href="/refresh">Refresh Ratings</a>
    <span id="countdown-wrap">Next update: <span id="countdown">30</span>s</span>
  </div>
</header>

<div class="container">

  {% if error %}
  <div class="error-banner">{{ error }}</div>
  {% endif %}

  {% if no_games_at_all %}
  <div class="no-games" style="padding:60px 24px; font-size:1.1em;">
    No international basketball games found for today.<br>
    <span style="font-size:0.85em; color:var(--text-muted);">
      Leagues checked: {{ leagues_checked }}
    </span>
  </div>
  {% endif %}

  <!-- LIVE GAMES -->
  <div class="section-header">
    Live Games <span class="count" id="live-count">{{ live|length }}</span>
  </div>
  <div id="live-container">
  {% include "live_partial" %}
  </div>

  <!-- UPCOMING GAMES -->
  <div class="section-header upcoming">
    Upcoming <span class="count" id="upcoming-count">{{ upcoming|length }}</span>
    <button class="toggle-btn" onclick="toggleSection('upcoming')">show/hide</button>
  </div>
  <div id="upcoming-container">
  {% include "upcoming_partial" %}
  </div>

  <!-- COMPLETED GAMES -->
  <div class="section-header completed">
    Completed <span class="count" id="completed-count">{{ completed|length }}</span>
    <button class="toggle-btn" onclick="toggleSection('completed')">show/hide</button>
  </div>
  <div id="completed-container" class="hidden">
  {% include "completed_partial" %}
  </div>

  <!-- EUROPEAN LEAGUE STANDINGS -->
  {% if euro_leagues %}
  <div class="section-header" style="margin-top:24px; border-bottom-color:var(--accent);">
    European League Standings <span class="count" style="background:#e84118;">{{ euro_leagues|length }} leagues</span>
    <button class="toggle-btn" onclick="toggleSection('euro')">show/hide</button>
  </div>
  <div id="euro-container">
    {% for league in euro_leagues %}
    <div style="margin-bottom:16px;">
      <div class="league-header" style="background:rgba(255,255,255,0.03); border-left:3px solid {{ league.accent }};">
        <span>{{ league.emoji }} {{ league.name }}</span>
        <span class="league-badge" style="background:{{ league.accent }};">{{ league.team_count }} teams</span>
      </div>
      <div class="table-wrap">
      <table class="compact-table" style="min-width:700px;">
        <tr>
          <th>#</th><th style="text-align:left;">Team</th><th>GP</th><th>W</th><th>L</th>
          <th>PPG</th><th>OPPG</th><th>Diff</th><th>Pace Est</th>
        </tr>
        {% for t in league.teams %}
        <tr>
          <td>{{ loop.index }}</td>
          <td class="team-cell">{{ t.team }}</td>
          <td>{{ t.gp }}</td>
          <td style="color:var(--green);">{{ t.w }}</td>
          <td style="color:#ff4444;">{{ t.l }}</td>
          <td><span class="pct-badge {{ t.ppg_cls }}">{{ t.ppg }}</span></td>
          <td><span class="pct-badge {{ t.oppg_cls }}">{{ t.oppg }}</span></td>
          <td style="color:{{ 'var(--green)' if t.diff > 0 else '#ff4444' if t.diff < 0 else 'var(--text-muted)' }};">
            {{ "%+.1f"|format(t.diff) }}
          </td>
          <td>{{ t.pace }}</td>
        </tr>
        {% endfor %}
      </table>
      </div>
    </div>
    {% endfor %}
    <div style="text-align:center; font-size:0.75em; color:var(--text-muted); padding:8px;">
      Ratings source: eurobasket.com &bull; Updated at {{ ratings_time }}
    </div>
  </div>
  {% endif %}

</div>

<script>
const LIVE_INTERVAL = 30;
const IDLE_INTERVAL = 300;
let interval = {{ 30 if live|length > 0 else 300 }};
let countdown = interval;

function toggleSection(id) {
  document.getElementById(id + '-container').classList.toggle('hidden');
}

async function refreshGames() {
  try {
    const resp = await fetch('api/games');
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

LIVE_PARTIAL = r"""{% if games %}
  {% for g in games %}
  <div class="game-card">
    <span class="card-league-tag" style="background:{{ g.league_accent }}">{{ g.league_emoji }} {{ g.league_short }}</span>
    {% if g.neutral_site %}<span class="neutral-badge">NEUTRAL</span>{% endif %}
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
        <div class="clock-line">{{ g.status_detail }}</div>
      </div>
      <div class="team home">
        <span class="team-name">{{ g.home_name }}</span>
        {% if g.home_logo %}<img src="{{ g.home_logo }}" alt="">{% endif %}
      </div>
    </div>
    <div class="proj-row">
      <div class="proj-stat">
        <label>Est. Possessions</label>
        <span class="val">{{ g.poss_so_far }} / {{ g.total_expected_poss }}</span>
      </div>
      <div class="proj-stat">
        <label>{{ "1H Final" if g.h1_is_actual else "1H Proj" }}</label>
        <span class="val">{{ g.away_1h_proj }} - {{ g.home_1h_proj }}</span>
      </div>
      <div class="proj-stat">
        <label>{{ "1H Total" if g.h1_is_actual else "1H Proj Total" }}</label>
        <span class="val">{{ g.proj_1h_total }}</span>
      </div>
      <div class="proj-stat">
        <label>Projected Final</label>
        <span class="val">{{ g.away_final }} - {{ g.home_final }}</span>
      </div>
      <div class="proj-stat">
        <label>Proj Spread</label>
        <span class="val {{ 'spread-home' if g.proj_spread > 0 else 'spread-away' }}">
          {{ "Home" if g.proj_spread > 0 else "Away" }} {{ "%.1f"|format(g.proj_spread|abs) }}
        </span>
      </div>
      <div class="proj-stat">
        <label>Proj Total</label>
        <span class="val">{{ g.proj_total }}</span>
      </div>
    </div>
    {% if g.has_fouls %}
    <div class="fouls-row">
      <div class="foul-team">
        <span class="foul-label">{{ g.away_abbrev }}</span>
        <span class="foul-half">Total: <span class="num">{{ g.away_total_fouls }}</span></span>
        <span class="foul-half">Qtr: <span class="num">{{ g.away_current_fouls }}</span></span>
        {% if g.away_bonus == "BONUS" %}<span class="bonus">BONUS</span>
        {% elif g.away_bonus == "DOUBLE" %}<span class="dbl-bonus">PENALTY</span>{% endif %}
      </div>
      <span class="foul-center">Team Fouls</span>
      <div class="foul-team">
        {% if g.home_bonus == "BONUS" %}<span class="bonus">BONUS</span>
        {% elif g.home_bonus == "DOUBLE" %}<span class="dbl-bonus">PENALTY</span>{% endif %}
        <span class="foul-half">Qtr: <span class="num">{{ g.home_current_fouls }}</span></span>
        <span class="foul-half">Total: <span class="num">{{ g.home_total_fouls }}</span></span>
        <span class="foul-label">{{ g.home_abbrev }}</span>
      </div>
    </div>
    {% endif %}
    <div class="detail-row">
      <span>{{ g.away_abbrev }}: PPG {{ g.away_ppg }} <span class="pct-badge {{ g.away_ppg_cls }}">{{ g.away_ppg_pct }}%</span> | OPPG {{ g.away_oppg }} <span class="pct-badge {{ g.away_oppg_cls }}">{{ g.away_oppg_pct }}%</span>{{ "" if g.has_away_data else " &#9888;" }}</span>
      <span>G.Pace: {{ g.game_pace }} | Rem: {{ g.poss_remaining }} | <span class="hca">HCA &plusmn;{{ g.hca_display }}</span>{% if g.blowout_regress > 0 %} | <span style="color:var(--amber)">RTM {{ g.blowout_regress }}%</span>{% endif %}</span>
      <span>{{ g.home_abbrev }}: PPG {{ g.home_ppg }} <span class="pct-badge {{ g.home_ppg_cls }}">{{ g.home_ppg_pct }}%</span> | OPPG {{ g.home_oppg }} <span class="pct-badge {{ g.home_oppg_cls }}">{{ g.home_oppg_pct }}%</span>{{ "" if g.has_home_data else " &#9888;" }}</span>
    </div>
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
      <th>League</th><th>Time</th>
      <th>Away</th><th>PPG</th><th>OPPG</th><th>Pace</th>
      <th>Home</th><th>PPG</th><th>OPPG</th><th>Pace</th>
      <th>1H Proj</th><th>1H Tot</th><th>Proj Score</th><th>Spread</th><th>Total</th>
    </tr>
    {% for g in upcoming %}
    <tr>
      <td><span class="card-league-tag" style="background:{{ g.league_accent }};font-size:0.85em;padding:1px 6px;">{{ g.league_short }}</span></td>
      <td>{{ g.start_time_str }}</td>
      <td class="team-cell">{{ g.away_name }}{{ "" if g.has_away_data else " &#9888;" }}</td>
      <td>{{ g.away_ppg }}</td><td>{{ g.away_oppg }}</td>
      <td>{{ g.away_pace }}</td>
      <td class="team-cell">{{ g.home_name }}{{ "" if g.has_home_data else " &#9888;" }}</td>
      <td>{{ g.home_ppg }}</td><td>{{ g.home_oppg }}</td>
      <td>{{ g.home_pace }}</td>
      <td>{{ g.away_1h_proj }} - {{ g.home_1h_proj }}</td>
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

COMPLETED_PARTIAL = r"""{% if completed %}
  <div class="table-wrap">
  <table class="compact-table">
    <tr>
      <th>League</th><th>Away</th><th>Score</th><th>Home</th><th>Score</th><th>Status</th>
    </tr>
    {% for g in completed %}
    <tr>
      <td><span class="card-league-tag" style="background:{{ g.league_accent }};font-size:0.85em;padding:1px 6px;">{{ g.league_short }}</span></td>
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
_cache: dict = {"games": [], "fetched_at": 0.0, "error": None}
CACHE_TTL = 20


def get_date_str() -> tuple[str, datetime]:
    if DATE_OVERRIDE:
        dt = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d")
    else:
        dt = datetime.now(ET)
    return dt.strftime("%Y%m%d"), dt


def _fetch_all_scoreboards_cached() -> tuple[list[dict], str | None]:
    """Fetch scoreboards for all leagues (ESPN + Euroleague API), with caching."""
    now = time.monotonic()
    with _cache_lock:
        if now - _cache["fetched_at"] < CACHE_TTL and _cache["games"]:
            return _cache["games"], _cache["error"]

    date_str, dt = get_date_str()
    all_games = []
    errors = []

    # ESPN leagues (G League, NBL)
    for slug in LEAGUES:
        try:
            games = fetch_league_scoreboard(slug, date_str)
            all_games.extend(games)
        except Exception as e:
            errors.append(f"{LEAGUES[slug]['name']}: {e}")

    # Euroleague API (EuroLeague, EuroCup)
    try:
        euro_games = fetch_euroleague_api_games(date_str)
        all_games.extend(euro_games)
    except Exception as e:
        errors.append(f"Euroleague API: {e}")

    error = "; ".join(errors) if errors else None

    with _cache_lock:
        _cache["games"] = all_games
        _cache["fetched_at"] = time.monotonic()
        _cache["error"] = error

    return all_games, error


def fetch_and_project() -> tuple[list[dict], list[dict], list[dict], str, str | None, list[tuple], int]:
    """Fetch, project, attach fouls, split into live/upcoming/completed."""
    _, dt = get_date_str()
    date_display = dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")

    games, error = _fetch_all_scoreboards_cached()
    projected = [project_game(g) for g in games]

    # Fetch fouls for live games that support play-by-play
    foul_map = fetch_fouls_for_live_games(projected)
    for g in projected:
        fouls = foul_map.get(g["game_id"], None)
        has_pbp = LEAGUES.get(g["league_slug"], {}).get("has_pbp", False)
        if fouls and has_pbp:
            g.update(fouls)
            g["has_fouls"] = True
        else:
            g.update(EMPTY_FOULS)
            g["has_fouls"] = False

    live = sorted([g for g in projected if g["state"] == "in"], key=lambda g: g["time_elapsed"], reverse=True)
    upcoming = sorted([g for g in projected if g["state"] == "pre"], key=lambda g: g["start_time_sort"])
    completed = sorted([g for g in projected if g["state"] == "post"], key=lambda g: g["start_time_sort"], reverse=True)

    # League summary
    league_counts: dict[str, int] = {}
    for g in projected:
        ln = g["league_name"]
        league_counts[ln] = league_counts.get(ln, 0) + 1
    league_summary = sorted(league_counts.items())
    league_count = len(league_counts)

    return live, upcoming, completed, date_display, error, league_summary, league_count


def _render_with_partials(template: str, **kwargs) -> str:
    """Render main template with partials inlined."""
    rendered = template.replace(
        '{% include "live_partial" %}',
        LIVE_PARTIAL
    ).replace(
        '{% include "upcoming_partial" %}',
        UPCOMING_PARTIAL
    ).replace(
        '{% include "completed_partial" %}',
        COMPLETED_PARTIAL
    )
    return render_template_string(rendered, **kwargs)


def _build_euro_league_display() -> list[dict]:
    """Build euro league standings data for template rendering."""
    result = []
    for key, cfg in EURO_LEAGUES.items():
        ratings = EURO_RATINGS.get(key, {})
        if not ratings:
            continue

        all_ppg = [r["ppg"] for r in ratings.values()]
        all_oppg = [r["oppg"] for r in ratings.values()]

        teams = []
        for name, r in ratings.items():
            # Compute percentiles within this league
            ppg_pct = int(sum(1 for v in all_ppg if v <= r["ppg"]) / len(all_ppg) * 100) if all_ppg else 50
            oppg_pct = 100 - int(sum(1 for v in all_oppg if v <= r["oppg"]) / len(all_oppg) * 100) if all_oppg else 50

            teams.append({
                "team": name,
                "gp": r.get("gp", 0),
                "w": r.get("w", 0),
                "l": r.get("l", 0),
                "ppg": r["ppg"],
                "oppg": r["oppg"],
                "diff": round(r["ppg"] - r["oppg"], 1),
                "pace": r["pace"],
                "ppg_cls": pct_class(ppg_pct),
                "oppg_cls": pct_class(oppg_pct),
            })

        # Sort by differential descending
        teams.sort(key=lambda t: t["diff"], reverse=True)

        result.append({
            "name": cfg["name"],
            "short": cfg["short"],
            "emoji": cfg["emoji"],
            "accent": cfg["accent"],
            "team_count": len(teams),
            "teams": teams,
        })

    return result


@app.route("/")
def index():
    if not RATINGS_LOADED_AT:
        load_all_ratings()

    live, upcoming, completed, date_display, error, league_summary, league_count = fetch_and_project()
    euro_leagues = _build_euro_league_display()

    return _render_with_partials(
        HTML_TEMPLATE,
        live=live,
        upcoming=upcoming,
        completed=completed,
        games=live,   # for live_partial
        date_display=date_display,
        total_games=len(live) + len(upcoming) + len(completed),
        league_summary=league_summary,
        league_count=league_count,
        leagues_checked=", ".join(
            [LEAGUES[s]["name"] for s in LEAGUES] +
            [EURO_LEAGUES[k]["name"] for k in EURO_LEAGUES]
        ),
        no_games_at_all=(len(live) + len(upcoming) + len(completed) == 0 and not euro_leagues),
        euro_leagues=euro_leagues,
        ratings_time=RATINGS_LOADED_AT.strftime("%I:%M %p ET") if RATINGS_LOADED_AT else "N/A",
        error=error,
    )


@app.route("/api/games")
def api_games():
    if not RATINGS_LOADED_AT:
        load_all_ratings()

    live, upcoming, completed, _, error, _, _ = fetch_and_project()

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
    """Re-fetch ratings for all leagues."""
    try:
        load_all_ratings()
    except Exception as e:
        print(f"  Refresh failed: {e}")
    return redirect(url_for("index"))


# ── Entry point ──

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="International Basketball Live Projections")
    parser.add_argument("--port", type=int, default=5003, help="Port (default: 5003)")
    parser.add_argument("--date", type=str, default=None, help="Date override YYYY-MM-DD")
    args = parser.parse_args()

    if args.date:
        DATE_OVERRIDE = args.date

    print("=" * 58)
    print("  International Basketball Live Projections Dashboard")
    print("=" * 58)
    print()
    print("ESPN Leagues (live scores + projections):")
    for slug, cfg in LEAGUES.items():
        print(f"  {cfg['emoji']} {cfg['name']} ({slug})")
        print(f"     Format: 4 x {cfg['qtr_min']:.0f}-min quarters, {cfg['ot_min']:.0f}-min OT")
        print(f"     HCA: {cfg['hca']} pts | Play-by-play: {'Yes' if cfg['has_pbp'] else 'No'}")
    print()
    print(f"European Leagues (games: Euroleague API, ratings: eurobasket.com): {len(EURO_LEAGUES)} leagues")
    for key, cfg in EURO_LEAGUES.items():
        print(f"  {cfg['emoji']} {cfg['name']}")
    print()

    load_all_ratings()

    date_str, dt = get_date_str()
    print(f"\nDate: {dt.strftime('%A, %B')} {dt.day}, {dt.year}")
    print(f"Server: http://localhost:{args.port}")
    print(f"Blowout RTM: {BLOWOUT_THRESHOLD}+ pt lead, up to {BLOWOUT_MAX_REGRESS*100:.0f}% at {BLOWOUT_LEAD_CAP}+ pts")
    print("Press Ctrl+C to stop.\n")

    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
