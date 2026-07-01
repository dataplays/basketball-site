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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
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


_euro_session_lock = threading.Lock()


def _get_euro_session() -> _requests.Session:
    """Get an authenticated eurobasket.com session, logging in if needed.

    Thread-safe: a single login is shared across the parallel league loaders,
    so concurrent workers don't each trigger their own login round-trip."""
    global _euro_session
    with _euro_session_lock:
        if _euro_session is not None and any(
            c.name == "PREMIUM" for c in _euro_session.cookies
        ):
            return _euro_session

        s = _requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        try:
            s.post(EUROBASKET_LOGIN_URL, data={
                "email": EUROBASKET_EMAIL,
                "pwd": EUROBASKET_PWD,
                "B1": "Login",
                "Referal": "",
            }, timeout=15, allow_redirects=True)
        except Exception as e:
            print(f"    [WARN] Eurobasket login error: {e}")

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
    """Load ratings for all configured leagues (ESPN + eurobasket), in parallel.

    Each league is independent, so they're fetched concurrently — one slow or
    unreachable source no longer serializes behind the others (this is what
    used to make the dashboard take minutes to load). Runs off the request
    path; see ensure_ratings_loading()."""
    global RATINGS, EURO_RATINGS, RATINGS_LOADED_AT

    print("Loading ESPN league ratings (parallel)...")
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(load_league_ratings, slug): slug for slug in LEAGUES}
        for fut in as_completed(futs):
            slug = futs[fut]
            try:
                RATINGS[slug] = fut.result()
            except Exception as e:
                RATINGS[slug] = {}
                print(f"  [WARN] ESPN ratings for {slug} failed: {e}")

    print("Loading European league ratings from eurobasket.com (parallel)...")
    try:
        _get_euro_session()      # prime the shared login once before fan-out
    except Exception as e:
        print(f"  [WARN] eurobasket session prime failed: {e}")
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(load_euro_league_ratings, key, cfg): key
                for key, cfg in EURO_LEAGUES.items()}
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                EURO_RATINGS[key] = fut.result()
            except Exception as e:
                EURO_RATINGS[key] = {}
                print(f"  [WARN] eurobasket ratings for {key} failed: {e}")

    RATINGS_LOADED_AT = datetime.now(ET)
    total_espn = sum(len(v) for v in RATINGS.values())
    total_euro = sum(len(v) for v in EURO_RATINGS.values())
    print(f"\nRatings loaded at {RATINGS_LOADED_AT.strftime('%I:%M %p ET')}.")
    print(f"  ESPN: {total_espn} teams across {len(RATINGS)} leagues")
    print(f"  Eurobasket: {total_euro} teams across {len(EURO_RATINGS)} leagues")


# Background ratings loading so the dashboard never blocks a page load on the
# (multi-second) league scrape. The page renders immediately; projections fall
# back to league-average defaults until the load finishes, then sharpen on the
# next 30s AJAX refresh.
_ratings_thread = None
_ratings_thread_lock = threading.Lock()


def ensure_ratings_loading() -> None:
    """Start the ratings load in the background once, without blocking."""
    global _ratings_thread
    if RATINGS_LOADED_AT is not None:
        return
    with _ratings_thread_lock:
        if RATINGS_LOADED_AT is not None:
            return
        if _ratings_thread is not None and _ratings_thread.is_alive():
            return

        def _runner():
            try:
                load_all_ratings()
            except Exception as e:
                print(f"[ratings] background load failed: {e}")

        _ratings_thread = threading.Thread(target=_runner, name="intl-ratings",
                                           daemon=True)
        _ratings_thread.start()


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
            start_epoch = None
            try:
                utc_dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
                et_dt = utc_dt.astimezone(ET)
                _t = (et_dt.strftime("%#I:%M %p") if sys.platform == "win32"
                      else et_dt.strftime("%-I:%M %p"))
                _base_date = (datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").date()
                              if DATE_OVERRIDE else datetime.now(ET).date())
                time_str = _t if et_dt.date() == _base_date else (et_dt.strftime("%a ") + _t)
                sort_key = et_dt.hour * 100 + et_dt.minute
                start_epoch = et_dt.timestamp()
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
                "el_game_code": g.get("gameCode"),
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
                "start_epoch": start_epoch,
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


# ── German BBL: official-site live data (domestic league, not on Euroleague API) ──
# easycredit-bbl.de server-renders full game data (teams, live + per-quarter
# scores, status, scheduledTime) into its page JSON — no API token needed.
BBL_HOME_URL = "https://www.easycredit-bbl.de/"
_bbl_cache: dict = {"games": [], "ts": 0.0}
_BBL_CACHE_TTL = 25  # seconds


def _bbl_find_games(obj) -> list[dict]:
    """Recursively collect game objects (have homeTeam + guestTeam + scheduledTime)."""
    out = []
    if isinstance(obj, dict):
        if "homeTeam" in obj and "guestTeam" in obj and obj.get("scheduledTime"):
            out.append(obj)
        else:
            for v in obj.values():
                out += _bbl_find_games(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _bbl_find_games(v)
    return out


def _bbl_state(status, progress) -> str:
    s = (status or "").upper()
    if s in ("OFFICIAL", "FINAL", "ENDED", "FINISHED") or progress == "E":
        return "post"
    if s in ("PRE", "SCHEDULED", "UPCOMING", "PLANNED", "PREVIEW", ""):
        return "pre"
    return "in"   # LIVE / running


def _bbl_to_game(g: dict) -> dict:
    cfg = EURO_LEAGUES.get("germany-bbl", {})
    home = g.get("homeTeam", {}) or {}
    away = g.get("guestTeam", {}) or {}
    res = g.get("result") or {}
    state = _bbl_state(g.get("status"), g.get("gameProgress"))

    start_epoch, time_str, sort_key = None, "TBD", 9999
    st = g.get("scheduledTime")
    if st:
        try:
            et_dt = datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone(ET)
            _t = (et_dt.strftime("%#I:%M %p") if sys.platform == "win32"
                  else et_dt.strftime("%-I:%M %p"))
            base = (datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").date()
                    if DATE_OVERRIDE else datetime.now(ET).date())
            time_str = _t if et_dt.date() == base else (et_dt.strftime("%a ") + _t)
            sort_key = et_dt.hour * 100 + et_dt.minute
            start_epoch = et_dt.timestamp()
        except Exception:
            pass

    def _i(x):
        try:
            return int(x or 0)
        except (TypeError, ValueError):
            return 0
    hs, as_ = _i(res.get("homeTeamFinalScore")), _i(res.get("guestTeamFinalScore"))
    h1 = (_i(res.get("homeTeamQ1Score")) + _i(res.get("homeTeamQ2Score"))) if res else None
    a1 = (_i(res.get("guestTeamQ1Score")) + _i(res.get("guestTeamQ2Score"))) if res else None
    prog = (g.get("gameProgress") or "").strip().upper()
    _qm = re.match(r"Q(\d)", prog)          # handles "Q2" and "Q2_BREAK"
    if _qm:
        period = int(_qm.group(1))
    elif prog == "HT":
        period = 2
    elif prog.startswith("OT"):
        period = 5
    elif prog == "E":
        period = 4
    else:
        period = 0
    # Live clock: the per-game page's gameTime is "HH:MM:SS" remaining in the
    # current period (counts down). The homepage summary omits it.
    clock_seconds, clock_str = 0, ""
    gt = g.get("gameTime")
    if gt and state == "in":
        try:
            parts = [int(p) for p in str(gt).split(":")]
            while len(parts) < 3:
                parts.insert(0, 0)
            clock_seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
            cm, cs = divmod(clock_seconds, 60)
            clock_str = f"{cm}:{cs:02d}"
        except (ValueError, TypeError):
            clock_seconds, clock_str = 0, ""
    # Live clock-line: quarter + running clock ("Q2 0:46"); never the playoff round.
    if state == "post":
        detail = "Final"
    elif state == "in":
        if prog == "HT":
            base = "Halftime"
        elif "BREAK" in prog:
            base = "Break"
        else:
            base = prog or "Live"
        detail = f"{base} {clock_str}" if (clock_seconds > 0 and clock_str) else base
    else:
        detail = time_str

    return {
        "league_slug": "germany-bbl",
        "league_name": cfg.get("name", "BBL (Germany)"),
        "league_short": cfg.get("short", "BBL"),
        "league_emoji": cfg.get("emoji", "\U0001F1E9\U0001F1EA"),
        "league_accent": cfg.get("accent", "#d35400"),
        "game_id": f"bbl_{g.get('id') or g.get('sourceId')}",
        "state": state,
        "clock_seconds": clock_seconds,
        "display_clock": clock_str,
        "period": period,
        "status_detail": detail,
        "away_name": away.get("name", "Away"),
        "away_abbrev": away.get("tlc", ""),
        "away_score": as_,
        "away_logo": away.get("logoUrl", ""),
        "home_name": home.get("name", "Home"),
        "home_abbrev": home.get("tlc", ""),
        "home_score": hs,
        "home_logo": home.get("logoUrl", ""),
        "away_1h_score": a1,
        "home_1h_score": h1,
        "neutral_site": False,
        "start_time_str": time_str,
        "start_time_sort": sort_key,
        "start_epoch": start_epoch,
        "source": "bbl_official",
    }


def _bbl_window_games() -> list[dict]:
    """German BBL games from the official site's embedded SSR data (no token).

    Keeps only games tipping within roughly [now-18h, now+WINDOW_HOURS] so the
    completed list isn't flooded with weeks-old results. Live clocks are added
    by fetch_bbl_games() via each game's per-game page."""
    now = time.monotonic()
    if _bbl_cache["games"] and (now - _bbl_cache["ts"]) < _BBL_CACHE_TTL:
        return _bbl_cache["games"]
    games = []
    try:
        r = _requests.get(BBL_HOME_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if m:
            pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
            _, base_dt = get_date_str()
            now_ts = base_dt.timestamp()
            lo, hi = now_ts - 18 * 3600, now_ts + WINDOW_HOURS * 3600
            seen = set()
            for raw in _bbl_find_games(pp):
                gid = str(raw.get("id") or raw.get("sourceId") or "")
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                gm = _bbl_to_game(raw)
                se = gm.get("start_epoch")
                if se is None or lo <= se <= hi:
                    games.append(gm)
    except Exception as e:
        print(f"  [WARN] BBL fetch failed: {e}", file=sys.stderr)
        return _bbl_cache["games"]
    _bbl_cache["games"] = games
    _bbl_cache["ts"] = now
    return games


_bbl_game_cache: dict = {}     # game_id -> (dash_game, ts) for live per-game clocks
_BBL_GAME_TTL = 20


def _bbl_live_detail(game_id: str):
    """Fetch one BBL game's page for the live clock (gameTime). Per-game cached."""
    gid = str(game_id).replace("bbl_", "")
    now = time.monotonic()
    c = _bbl_game_cache.get(gid)
    if c and (now - c[1]) < _BBL_GAME_TTL:
        return c[0]
    dash = None
    try:
        r = _requests.get(f"https://www.easycredit-bbl.de/spiele/{gid}",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if m:
            igd = (json.loads(m.group(1)).get("props", {})
                   .get("pageProps", {}).get("initialGameData"))
            if igd:
                dash = _bbl_to_game(igd)
    except Exception as e:
        print(f"  [WARN] BBL game {gid} refresh failed: {e}", file=sys.stderr)
        return c[0] if c else None
    _bbl_game_cache[gid] = (dash, now)
    return dash


def fetch_bbl_games() -> list[dict]:
    """Windowed BBL games; live games are refreshed from their per-game page so
    the clock-line shows the running quarter clock (e.g. "Q2 0:46")."""
    out = []
    for gm in _bbl_window_games():
        if gm["state"] == "in":
            out.append(_bbl_live_detail(gm["game_id"]) or gm)
        else:
            out.append(gm)
    return out


# ── Domestic European leagues via api-sports (live games + computed ratings) ──
# Clean JSON feed with live score/quarter/status for leagues eurobasket/ESPN
# don't cover live. Key comes from the APISPORTS_KEY env var (never committed).
APISPORTS_KEY = os.environ.get("APISPORTS_KEY", "").strip()
APISPORTS_BASE = "https://v1.basketball.api-sports.io"
APISPORTS_SEASON = os.environ.get("APISPORTS_SEASON", "2025-2026")
APISPORTS_LEAGUES = {                 # dashboard slug -> api-sports league id
    "turkey-bsl": 104, "greece-gbl": 45, "italy-serie-a": 52,
    "spain-liga-endesa": 117, "france-betclic": 2, "israel-winner": 51,
    "lithuania-lkl": 60, "serbia-kls": 85,
    # added Jun 2026 — other pro leagues active now (configs in EXTRA_APISPORTS_LEAGUES)
    "pr-bsn": 76, "ph-mpbl": 426, "ca-cebl": 222, "nz-nbl": 66,
    "ar-liga-a": 18, "pl-ebl": 72, "id-ibl": 139, "do-lnb": 380,
}
# Non-eurobasket leagues: games + ratings come only from api-sports (so they
# show as games but not in the eurobasket standings section). "season" overrides
# the default for leagues that run on a calendar year ("2026") vs a split season.
EXTRA_APISPORTS_LEAGUES = {
    "pr-bsn":    {"name": "BSN (Puerto Rico)",           "short": "BSN",    "emoji": "\U0001F1F5\U0001F1F7", "accent": "#d62828", "season": "2026",      "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5},
    "ph-mpbl":   {"name": "MPBL (Philippines)",          "short": "MPBL",   "emoji": "\U0001F1F5\U0001F1ED", "accent": "#0353a4", "season": "2026",      "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5},
    "ca-cebl":   {"name": "CEBL (Canada)",               "short": "CEBL",   "emoji": "\U0001F1E8\U0001F1E6", "accent": "#e01e37", "season": "2026",      "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5},
    "nz-nbl":    {"name": "NBL (New Zealand)",           "short": "NZ NBL", "emoji": "\U0001F1F3\U0001F1FF", "accent": "#118ab2", "season": "2026",      "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5},
    "ar-liga-a": {"name": "Liga A (Argentina)",          "short": "ARG",    "emoji": "\U0001F1E6\U0001F1F7", "accent": "#4ea8de", "season": "2025-2026", "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5},
    "pl-ebl":    {"name": "Energa Basket Liga (Poland)", "short": "PLK",    "emoji": "\U0001F1F5\U0001F1F1", "accent": "#e63946", "season": "2025-2026", "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5},
    "id-ibl":    {"name": "IBL (Indonesia)",             "short": "IBL",    "emoji": "\U0001F1EE\U0001F1E9", "accent": "#ef476f", "season": "2025-2026", "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5},
    "do-lnb":    {"name": "LNB (Dominican Rep.)",        "short": "DOM",    "emoji": "\U0001F1E9\U0001F1F4", "accent": "#168aad", "season": "2026",      "reg_min": 40.0, "qtr_min": 10.0, "ot_min": 5.0, "hca": 3.5},
}
APISPORTS_LIVE = {"Q1", "Q2", "Q3", "Q4", "OT", "HT", "BT", "ET"}
APISPORTS_FINAL = {"FT", "AOT", "AET"}
APISPORTS_RATINGS: dict = {}          # slug -> {team_name: {ppg, oppg, pace, gp}}
_apisports_cache: dict = {}           # slug -> (dash_games, ts, ttl)


def _apisports_get(path: str) -> dict:
    # api-sports signals a rate-limited/transient request as a 200 with a
    # non-empty "errors" field (not an HTTP error). When many leagues fetch at
    # once a burst can trip it, so back off briefly and retry.
    d = {}
    for _attempt in range(3):
        req = Request(APISPORTS_BASE + path,
                      headers={"x-apisports-key": APISPORTS_KEY, "Accept": "application/json"})
        with urlopen(req, timeout=25) as r:
            d = json.loads(r.read())
        if not d.get("errors"):
            return d
        time.sleep(1.0)
    return d


def _apisports_state(short: str) -> str:
    if short in APISPORTS_FINAL:
        return "post"
    if short in APISPORTS_LIVE:
        return "in"
    return "pre"


def _apisports_ratings_from(games: list) -> dict:
    """Per-team ppg/oppg/pace from completed games (names match the games exactly)."""
    agg = {}
    for g in games:
        if (g.get("status") or {}).get("short") not in APISPORTS_FINAL:
            continue
        sc = g.get("scores") or {}
        hs = (sc.get("home") or {}).get("total")
        as_ = (sc.get("away") or {}).get("total")
        if hs is None or as_ is None:
            continue
        h = ((g.get("teams") or {}).get("home") or {}).get("name")
        a = ((g.get("teams") or {}).get("away") or {}).get("name")
        if not h or not a:
            continue
        agg.setdefault(h, [0, 0, 0]); agg.setdefault(a, [0, 0, 0])
        agg[h][0] += hs; agg[h][1] += as_; agg[h][2] += 1
        agg[a][0] += as_; agg[a][1] += hs; agg[a][2] += 1
    out = {}
    for name, (pf, pa, gp) in agg.items():
        if gp:
            ppg, oppg = pf / gp, pa / gp
            out[name] = {"ppg": round(ppg, 1), "oppg": round(oppg, 1),
                         "pace": round((ppg + oppg) / 2, 1), "gp": gp, "w": 0, "l": 0}
    return out


def _apisports_to_game(slug: str, g: dict) -> dict:
    cfg = _get_league_config(slug)
    st = g.get("status") or {}
    short = st.get("short", "")
    state = _apisports_state(short)
    teams = g.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    sc = g.get("scores") or {}
    hs, as_ = sc.get("home") or {}, sc.get("away") or {}

    def _i(x):
        try:
            return int(x or 0)
        except (TypeError, ValueError):
            return 0
    h1 = (_i(hs.get("quarter_1")) + _i(hs.get("quarter_2"))) if state != "pre" else None
    a1 = (_i(as_.get("quarter_1")) + _i(as_.get("quarter_2"))) if state != "pre" else None

    start_epoch, time_str, sort_key = None, "TBD", 9999
    ts = g.get("timestamp")
    if ts:
        try:
            et_dt = datetime.fromtimestamp(ts, ET) if ET else datetime.fromtimestamp(ts)
            _t = (et_dt.strftime("%#I:%M %p") if sys.platform == "win32"
                  else et_dt.strftime("%-I:%M %p"))
            base = (datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").date()
                    if DATE_OVERRIDE else datetime.now(ET).date())
            time_str = _t if et_dt.date() == base else (et_dt.strftime("%a ") + _t)
            sort_key = et_dt.hour * 100 + et_dt.minute
            start_epoch = float(ts)
        except Exception:
            pass
    period = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "HT": 2, "BT": 2,
              "FT": 4, "AOT": 4}.get(short, 0)
    # api-sports "timer" is the ELAPSED clock in the period, counting UP -- verified
    # live (it ticked 3 -> 4 during Q1 with the score rising). project_game expects
    # clock_seconds = seconds REMAINING in the period, so convert. Defensive against
    # feeds that report whole-game elapsed minutes instead of per-period.
    clock_seconds = 0
    timer = st.get("timer")
    if state == "in" and timer not in (None, ""):
        ts_s = str(timer).strip()
        try:
            if ":" in ts_s:
                mm, ss = (ts_s.split(":") + ["0"])[:2]
                elapsed_sec = int(float(mm)) * 60 + int(float(ss))
            else:
                elapsed_sec = int(float(ts_s)) * 60
        except (ValueError, TypeError):
            elapsed_sec = 0
        qtr_sec = float(cfg.get("qtr_min", 10.0)) * 60.0
        per = period if (period and period >= 1) else 1
        elapsed_in_period = elapsed_sec
        if elapsed_in_period > qtr_sec:      # looks like whole-game elapsed -> reduce to this period
            elapsed_in_period = elapsed_sec - (per - 1) * qtr_sec
        elapsed_in_period = min(max(elapsed_in_period, 0.0), qtr_sec)
        clock_seconds = int(qtr_sec - elapsed_in_period)
    detail = ("Final" if state == "post"
              else st.get("long") if state == "in"
              else (g.get("week") or g.get("stage") or time_str))

    def _abbr(nm):
        return (nm or "")[:3].upper()
    return {
        "league_slug": slug,
        "league_name": cfg.get("name", slug),
        "league_short": cfg.get("short", ""),
        "league_emoji": cfg.get("emoji", "\U0001F3C0"),
        "league_accent": cfg.get("accent", "#888"),
        "game_id": f"as_{g.get('id')}",
        "state": state,
        "clock_seconds": clock_seconds,
        "display_clock": str(timer) if timer else "",
        "period": period,
        "status_detail": detail,
        "away_name": away.get("name", "Away"),
        "away_abbrev": _abbr(away.get("name")),
        "away_score": _i(as_.get("total")),
        "away_logo": away.get("logo", ""),
        "home_name": home.get("name", "Home"),
        "home_abbrev": _abbr(home.get("name")),
        "home_score": _i(hs.get("total")),
        "home_logo": home.get("logo", ""),
        "away_1h_score": a1,
        "home_1h_score": h1,
        "neutral_site": False,
        "start_time_str": time_str,
        "start_time_sort": sort_key,
        "start_epoch": start_epoch,
        "source": "apisports",
    }


def fetch_apisports_league(slug: str, league_id: int) -> list[dict]:
    """One league's windowed games + refreshed ratings. Adaptive cache: refresh
    active leagues every 90s, idle (off-season) leagues hourly to save bandwidth."""
    now = time.monotonic()
    c = _apisports_cache.get(slug)
    if c and (now - c[1]) < c[2]:
        return c[0]
    try:
        season = (_get_league_config(slug) or {}).get("season") or APISPORTS_SEASON
        allg = (_apisports_get(f"/games?league={league_id}&season={season}")
                .get("response") or [])
    except Exception as e:
        print(f"  [WARN] api-sports {slug} failed: {e}", file=sys.stderr)
        return c[0] if c else []

    APISPORTS_RATINGS[slug] = _apisports_ratings_from(allg)
    _, base_dt = get_date_str()
    now_ts = base_dt.timestamp()
    lo, hi = now_ts - 18 * 3600, now_ts + WINDOW_HOURS * 3600
    out = [_apisports_to_game(slug, g) for g in allg
           if g.get("timestamp") and lo <= g["timestamp"] <= hi]
    # active if a game falls in a wider [-1d, +3d] band; else treat as off-season
    active = any(g.get("timestamp") and (now_ts - 86400) <= g["timestamp"] <= (now_ts + 3 * 86400)
                 for g in allg)
    _apisports_cache[slug] = (out, now, 90 if active else 3600)
    return out


def fetch_apisports_games() -> list[dict]:
    """All windowed api-sports domestic-league games (parallel across leagues)."""
    if not APISPORTS_KEY:
        return []
    games = []
    with ThreadPoolExecutor(max_workers=4) as ex:   # gentle concurrency to avoid rate bursts
        futs = {ex.submit(fetch_apisports_league, slug, lid): slug
                for slug, lid in APISPORTS_LEAGUES.items()}
        for fut in as_completed(futs):
            try:
                games += fut.result() or []
            except Exception as e:
                print(f"  [WARN] api-sports {futs[fut]}: {e}", file=sys.stderr)
    return games


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
        _t = (et_dt.strftime("%-I:%M %p") if sys.platform != "win32"
              else et_dt.strftime("%#I:%M %p"))
        _base_date = (datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").date()
                      if DATE_OVERRIDE else datetime.now(ET).date())
        time_str = _t if et_dt.date() == _base_date else (et_dt.strftime("%a ") + _t)

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
            "start_epoch": et_dt.timestamp(),
        }
        games.append(game)

    return games


# ── Foul tracking ──

_foul_cache_lock = threading.Lock()
_foul_cache: dict[str, tuple[dict, float]] = {}
FOUL_CACHE_TTL = 25

# Shared ESPN summary cache so fouls + box stats reuse one fetch per game.
_summary_cache_lock = threading.Lock()
_summary_cache: dict[str, tuple] = {}
SUMMARY_CACHE_TTL = 20

# Live box-score pace ("Actual" columns): Poss = FGA - ORB + TOV + 0.44*FTA per
# team, averaged, normalized to a league's regulation minutes. ESPN feeds only
# (G League / NBL) — other sources (api-sports, Euroleague, BBL) carry no box
# stats, so the Actual columns simply don't render for them.
FT_POSS_COEF = 0.44
LIVE_PACE_MIN_ELAPSED = 3.0     # min elapsed game-minutes before extrapolating


def _get_summary(slug: str, game_id: str):
    """Fetch (and briefly cache) an ESPN summary payload, shared by fouls + box."""
    now = time.monotonic()
    with _summary_cache_lock:
        hit = _summary_cache.get(game_id)
        if hit and now - hit[1] < SUMMARY_CACHE_TTL:
            return hit[0]
    try:
        raw = _fetch_json(f"{ESPN_BASE}/{slug}/summary?event={game_id}", timeout=8)
    except Exception:
        raw = None
    with _summary_cache_lock:
        _summary_cache[game_id] = (raw, time.monotonic())
    return raw


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

    raw = _get_summary(slug, game_id)
    if raw is None:
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


# ── Live box-score pace ("Actual" columns, ESPN feeds only) ──

def _extract_box_stats(raw: dict) -> dict:
    """Pull per-team FGA, ORB, TOV, FTA from an ESPN summary boxscore.

    Returns {away_fga, away_orb, away_tov, away_fta, home_...} or {} unless all
    four stats are present for both teams.
    """
    teams = raw.get("boxscore", {}).get("teams", [])
    if len(teams) != 2:
        return {}
    comps = raw.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
    side_by_id = {c.get("id", ""): c.get("homeAway", "") for c in comps}

    def attempts(val: str):
        try:
            return int(val.split("-")[1])
        except (IndexError, ValueError, AttributeError):
            return None

    def whole(val: str):
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


def fetch_game_box(slug: str, game_id: str) -> dict:
    """Per-team box stats for an ESPN game (shares the summary fetch with fouls)."""
    raw = _get_summary(slug, game_id)
    if not raw:
        return {}
    try:
        return _extract_box_stats(raw)
    except Exception:
        return {}


# EuroLeague / EuroCup classic boxscore feed (separate host, carries team totals).
EUROLEAGUE_BOX_URL = "https://live.euroleague.net/api/Boxscore"
_el_box_cache_lock = threading.Lock()
_el_box_cache: dict[str, tuple] = {}
EL_BOX_CACHE_TTL = 20


def _get_euroleague_boxscore(game_code, season_code: str):
    """Fetch (and briefly cache) a EuroLeague/EuroCup classic boxscore payload."""
    key = f"{season_code}_{game_code}"
    now = time.monotonic()
    with _el_box_cache_lock:
        hit = _el_box_cache.get(key)
        if hit and now - hit[1] < EL_BOX_CACHE_TTL:
            return hit[0]
    raw = None
    try:
        resp = _requests.get(
            EUROLEAGUE_BOX_URL,
            params={"gamecode": game_code, "seasoncode": season_code},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        resp.raise_for_status()
        text = resp.text.strip()
        try:
            raw = resp.json()
        except Exception:
            raw = json.loads(text.split("\n")[0])    # tolerate trailing data
    except Exception:
        raw = None
    with _el_box_cache_lock:
        _el_box_cache[key] = (raw, time.monotonic())
    return raw


def _extract_euroleague_box(raw: dict, home_name: str, away_name: str) -> dict:
    """Pull per-team FGA/ORB/TOV/FTA from a EuroLeague boxscore (`Stats[].totr`).

    Stats[0] is the home (local) team, Stats[1] the away (road) team; we still
    match on team name when possible and fall back to that positional order.
    """
    stats = raw.get("Stats") or []
    if len(stats) != 2:
        return {}

    def nm(s):
        return (s.get("Team") or "").strip().upper()

    hn, an = (home_name or "").strip().upper(), (away_name or "").strip().upper()
    home_stat = next((s for s in stats if nm(s) == hn), None)
    away_stat = next((s for s in stats if nm(s) == an), None)
    if home_stat is None or away_stat is None:
        home_stat, away_stat = stats[0], stats[1]      # local-first convention

    out: dict = {}
    for side, s in (("home", home_stat), ("away", away_stat)):
        tot = s.get("totr") or {}
        try:
            fga = int(tot["FieldGoalsAttempted2"]) + int(tot["FieldGoalsAttempted3"])
            fta = int(tot["FreeThrowsAttempted"])
            orb = int(tot["OffensiveRebounds"])
            tov = int(tot["Turnovers"])
        except (KeyError, TypeError, ValueError):
            return {}
        out[f"{side}_fga"] = fga
        out[f"{side}_orb"] = orb
        out[f"{side}_tov"] = tov
        out[f"{side}_fta"] = fta
    return out if len(out) == 8 else {}


def fetch_euroleague_box(game: dict) -> dict:
    """Box stats for a live EuroLeague/EuroCup game via its classic boxscore feed."""
    code = game.get("el_game_code")
    season = EUROLEAGUE_API_LEAGUES.get(game["league_slug"], {}).get("season")
    if code is None or not season:
        return {}
    raw = _get_euroleague_boxscore(code, season)
    if not raw:
        return {}
    try:
        return _extract_euroleague_box(raw, game.get("home_name", ""), game.get("away_name", ""))
    except Exception:
        return {}


def fetch_box_for_live_games(games: list[dict]) -> dict[str, dict]:
    """Box stats for live games that expose them: ESPN feeds (LEAGUES) via the
    summary boxscore, and EuroLeague/EuroCup via the classic boxscore feed."""
    tasks = []   # (game, kind)
    for g in games:
        if g["state"] != "in":
            continue
        if g["league_slug"] in LEAGUES:
            tasks.append((g, "espn"))
        elif g["league_slug"] in EUROLEAGUE_API_LEAGUES:
            tasks.append((g, "euro"))
    if not tasks:
        return {}

    def _one(g, kind):
        return (fetch_game_box(g["league_slug"], g["game_id"]) if kind == "espn"
                else fetch_euroleague_box(g))

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as pool:
        futures = {pool.submit(_one, g, kind): g["game_id"] for g, kind in tasks}
        for fut in futures:
            gid = futures[fut]
            try:
                results[gid] = fut.result(timeout=10)
            except Exception:
                results[gid] = {}
    return results


# Default (all-None) so the template's `is not none` checks are always safe.
EMPTY_PACE = {
    "live_box_poss": None, "live_pace": None,
    "pace_away_final": None, "pace_home_final": None,
    "pace_proj_total": None, "pace_proj_margin": None,
    "pace_away_1h_final": None, "pace_home_1h_final": None,
    "pace_1h_total": None, "pace_1h_margin": None,
}


def compute_live_pace_stats(g: dict) -> dict:
    """Estimate current pace from the live box score and extrapolate.

    Poss = FGA - ORB + TOV + 0.44*FTA per team, averaged, normalized to the
    league's regulation minutes. If that pace and each team's current PPP hold,
    project the final score, total, and margin — plus the 1H line while the
    first half is still in progress. Needs box stats, so it only produces values
    for ESPN-fed games; returns all-None otherwise.
    """
    out = dict(EMPTY_PACE)
    if g.get("state") != "in":
        return out
    keys = ("away_fga", "away_orb", "away_tov", "away_fta",
            "home_fga", "home_orb", "home_tov", "home_fta")
    if any(g.get(k) is None for k in keys):
        return out
    elapsed = g.get("time_elapsed") or 0.0
    if elapsed < LIVE_PACE_MIN_ELAPSED:
        return out

    reg_min = _get_league_config(g["league_slug"]).get("reg_min", 40.0)

    away_poss = g["away_fga"] - g["away_orb"] + g["away_tov"] + FT_POSS_COEF * g["away_fta"]
    home_poss = g["home_fga"] - g["home_orb"] + g["home_tov"] + FT_POSS_COEF * g["home_fta"]
    box_poss = (away_poss + home_poss) / 2.0
    if box_poss <= 0:
        return out

    live_pace = reg_min * box_poss / elapsed
    remaining = g.get("time_remaining") or 0.0
    rem_poss = live_pace * (remaining / reg_min)

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
        "pace_proj_margin": round(home_final - away_final, 1),   # home - away
    })

    # 1H extrapolation only while the first half is still in progress.
    half_min = reg_min / 2.0
    if g.get("period", 0) <= 2 and elapsed < half_min:
        rem_1h_poss = live_pace * ((half_min - elapsed) / reg_min)
        away_1h = g["away_score"] + rem_1h_poss * away_ppp
        home_1h = g["home_score"] + rem_1h_poss * home_ppp
        out.update({
            "pace_away_1h_final": round(away_1h, 1),
            "pace_home_1h_final": round(home_1h, 1),
            "pace_1h_total": round(away_1h + home_1h, 1),
            "pace_1h_margin": round(home_1h - away_1h, 1),
        })
    return out


# ── Projection engine ──

def _get_league_config(slug: str) -> dict:
    """Get league config from either LEAGUES or EURO_LEAGUES."""
    if slug in LEAGUES:
        return LEAGUES[slug]
    if slug in EURO_LEAGUES:
        return EURO_LEAGUES[slug]
    return EXTRA_APISPORTS_LEAGUES.get(slug, {})


def _get_league_ratings(slug: str) -> dict:
    """Ratings dict, preferring api-sports (matches its game team names exactly),
    then ESPN, then eurobasket standings."""
    if APISPORTS_RATINGS.get(slug):
        return APISPORTS_RATINGS[slug]
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


def _fmt_clock(mins_left: float, estimated: bool) -> str:
    """Format minutes-left as M:SS; prefix '~' and suffix '(est)' when inferred."""
    mins_left = max(0.0, mins_left)
    m = int(mins_left)
    s = int(round((mins_left - m) * 60))
    if s == 60:
        m, s = m + 1, 0
    return f"~{m}:{s:02d} (est)" if estimated else f"{m}:{s:02d}"


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

    clock_display = ""        # period + time-left, shown for live games
    clock_estimated = False   # True when the feed gave no clock and we inferred it

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
            total_game_min = reg_min
            if clock_sec > 0:
                qtr_left = clock_min                       # real game clock
            else:
                # The feed gave no game clock (common on some api-sports
                # leagues). Estimate progress through the current quarter from
                # score vs the projected total, constrained to this quarter
                # (fallback: mid-quarter). Without this, a missing clock made
                # the model assume the whole quarter was already over.
                exp_total = away_proj_full + home_proj_full
                cur_total = game["away_score"] + game["home_score"]
                frac = 0.5
                if exp_total > 0 and cur_total > 0:
                    implied_min = (cur_total / exp_total) * reg_min
                    frac = (implied_min - (period - 1) * qtr_min) / qtr_min
                    frac = min(max(frac, 0.05), 0.95)
                qtr_left = qtr_min * (1.0 - frac)
                clock_estimated = True
            time_elapsed = (period - 1) * qtr_min + (qtr_min - qtr_left)
            clock_display = f"Q{period} {_fmt_clock(qtr_left, clock_estimated)}"
        else:
            ot_num = period - 4
            total_game_min = reg_min + ot_min * ot_num
            if clock_sec > 0:
                ot_left = clock_min
            else:
                ot_left = ot_min * 0.5
                clock_estimated = True
            time_elapsed = reg_min + (ot_num - 1) * ot_min + (ot_min - ot_left)
            ot_lbl = "OT" if ot_num == 1 else f"OT{ot_num}"
            clock_display = f"{ot_lbl} {_fmt_clock(ot_left, clock_estimated)}"

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
        # time_elapsed already reflects the real-or-estimated clock for 1H periods
        remaining_1h = max(half_min - time_elapsed, 0.0)
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
        "clock_display": clock_display,
        "clock_estimated": clock_estimated,
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
    <a href="refresh">Refresh Ratings</a>
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
        <div class="clock-line">{{ g.clock_display if g.clock_display else g.status_detail }}</div>
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
        <label>{{ "1H Total" if g.h1_is_actual else "1H Proj Total" }}</label>
        <span class="val">{{ g.proj_1h_total }}</span>
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
    {% if g.live_pace is not none %}
    <div class="proj-row" style="border-top:1px dashed rgba(255,255,255,.12);margin-top:4px;padding-top:8px">
      {% if g.pace_away_1h_final is not none %}
      <div class="proj-stat">
        <label>Actual 1H</label>
        <span class="val">{{ g.pace_away_1h_final }} - {{ g.pace_home_1h_final }}</span>
      </div>
      <div class="proj-stat">
        <label>Actual 1H Total</label>
        <span class="val">{{ g.pace_1h_total }}</span>
      </div>
      {% endif %}
      <div class="proj-stat">
        <label>Actual Final</label>
        <span class="val">{{ g.pace_away_final }} - {{ g.pace_home_final }}</span>
      </div>
      <div class="proj-stat">
        <label>Actual Margin</label>
        <span class="val {{ 'spread-home' if g.pace_proj_margin > 0 else 'spread-away' }}">
          {{ "Home" if g.pace_proj_margin > 0 else "Away" }} {{ "%.1f"|format(g.pace_proj_margin|abs) }}
        </span>
      </div>
      <div class="proj-stat">
        <label>Actual Total</label>
        <span class="val">{{ g.pace_proj_total }}</span>
      </div>
    </div>
    {% endif %}
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


# Show every game tipping off within this rolling window (not just "today").
WINDOW_HOURS = 48
WINDOW_DAYS = 3          # calendar days to fetch to cover a rolling 48h window


def get_date_str() -> tuple[str, datetime]:
    if DATE_OVERRIDE:
        dt = datetime.strptime(DATE_OVERRIDE, "%Y-%m-%d").replace(tzinfo=ET)
    else:
        dt = datetime.now(ET)
    return dt.strftime("%Y%m%d"), dt


def _fetch_all_scoreboards_cached() -> tuple[list[dict], str | None]:
    """Fetch scoreboards for all leagues (ESPN + Euroleague API), with caching."""
    now = time.monotonic()
    with _cache_lock:
        if now - _cache["fetched_at"] < CACHE_TTL and _cache["games"]:
            return _cache["games"], _cache["error"]

    _, base_dt = get_date_str()
    date_strs = [(base_dt + timedelta(days=_i)).strftime("%Y%m%d")
                 for _i in range(WINDOW_DAYS)]
    all_games = []
    errors = []
    seen = set()

    # ESPN leagues × the date window — fetched concurrently so one slow league
    # can't stall the poll; deduped by game_id across days.
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_league_scoreboard, slug, ds): (slug, ds)
                for slug in LEAGUES for ds in date_strs}
        for fut in as_completed(futs):
            slug, ds = futs[fut]
            try:
                for g in fut.result():
                    if g["game_id"] not in seen:
                        seen.add(g["game_id"])
                        all_games.append(g)
            except Exception as e:
                errors.append(f"{LEAGUES[slug]['name']}: {e}")

    # Euroleague API (EuroLeague, EuroCup) across the same window. The season
    # game list is cached, so the per-day calls just re-filter it cheaply.
    for ds in date_strs:
        try:
            for g in fetch_euroleague_api_games(ds):
                if g["game_id"] not in seen:
                    seen.add(g["game_id"])
                    all_games.append(g)
        except Exception as e:
            errors.append(f"Euroleague API: {e}")

    # German BBL — official-site live data (domestic league, not on Euroleague API)
    try:
        for g in fetch_bbl_games():
            if g["game_id"] not in seen:
                seen.add(g["game_id"])
                all_games.append(g)
    except Exception as e:
        errors.append(f"BBL: {e}")

    # Domestic European leagues (Turkey, Greece, Italy, Spain, France, Israel,
    # Lithuania, Serbia) — live games + ratings via api-sports
    try:
        for g in fetch_apisports_games():
            if g["game_id"] not in seen:
                seen.add(g["game_id"])
                all_games.append(g)
    except Exception as e:
        errors.append(f"api-sports: {e}")

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

    # Fetch fouls (play-by-play leagues) + box stats (all ESPN feeds) for live
    # games; box stats drive the "Actual" live-pace columns.
    foul_map = fetch_fouls_for_live_games(projected)
    box_map = fetch_box_for_live_games(projected)
    for g in projected:
        fouls = foul_map.get(g["game_id"], None)
        has_pbp = LEAGUES.get(g["league_slug"], {}).get("has_pbp", False)
        if fouls and has_pbp:
            g.update(fouls)
            g["has_fouls"] = True
        else:
            g.update(EMPTY_FOULS)
            g["has_fouls"] = False
        # Actual box-score pace (ESPN feeds only; all-None otherwise)
        g.update(box_map.get(g["game_id"]) or {})
        g.update(compute_live_pace_stats(g))

    _, _base_dt = get_date_str()
    _cutoff = _base_dt.timestamp() + WINDOW_HOURS * 3600
    live = sorted([g for g in projected if g["state"] == "in"], key=lambda g: g["time_elapsed"], reverse=True)
    upcoming = sorted([g for g in projected if g["state"] == "pre"
                       and (g.get("start_epoch") is None or g["start_epoch"] <= _cutoff)],
                      key=lambda g: g.get("start_epoch") or g["start_time_sort"])
    completed = sorted([g for g in projected if g["state"] == "post"],
                       key=lambda g: g.get("start_epoch") or g["start_time_sort"], reverse=True)

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
    ensure_ratings_loading()

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
    ensure_ratings_loading()

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
        # diagnostic (no secret value): is APISPORTS_KEY present, and which
        # leagues have api-sports ratings loaded (non-empty == calls succeeded)
        "apisports_key_set": bool(APISPORTS_KEY),
        "apisports_ratings": sorted(k for k, v in APISPORTS_RATINGS.items() if v),
    })


@app.route("/refresh")
def refresh_ratings():
    """Trigger a fresh ratings reload in the background (non-blocking)."""
    global RATINGS_LOADED_AT, _ratings_thread
    with _ratings_thread_lock:
        RATINGS_LOADED_AT = None
        _ratings_thread = None
    ensure_ratings_loading()
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
