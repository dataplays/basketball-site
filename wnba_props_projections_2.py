"""
WNBA Player Props Projection Engine

Mirrors the NBA props projection model but tailored to the WNBA:
  - 40-minute regulation (4 x 10 min quarters)
  - WNBA-specific gamelog label order
  - 2025 + 2026 regular season blending (early-2026 has limited samples)
  - 14-team roster sizes / lower minutes baseline
  - Correlated bootstrap Monte Carlo: points and rebounds are drawn from
    the SAME historical game (recency- and minute-weighted) and replayed
    scaled to projected minutes, with adjustments for pace, opponent
    defense/rebounding, injury redistribution, back-to-back, blowout,
    foul trouble, usage boost.
  - Model win-probabilities are de-vigged and blended with the no-vig
    market price before EV, trimming false edges on thin samples.

Usage:
    py -3 wnba_props_projections.py                       # today's games
    py -3 wnba_props_projections.py --date 2026-05-15     # specific date
    py -3 wnba_props_projections.py --pdf                 # also generate PDF
"""

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# ── Paths & constants ──────────────────────────────────────────────────────

RATINGS_CSV = (Path(os.environ.get("BBALL_DATA_DIR", str(Path(__file__).resolve().parent / "data"))) / "wnba_ratings_2026.csv")
OUTPUT_DIR = Path(os.environ.get("BBALL_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
ET = ZoneInfo("America/New_York")

ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
)
ESPN_SUMMARY = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"
)
ESPN_ROSTER = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/roster"
)
ESPN_GAMELOG = (
    "https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba/"
    "athletes/{player_id}/gamelog?season={season}&seasontype=2"
)
ESPN_PROP_BETS = (
    "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/"
    "events/{event_id}/competitions/{event_id}/odds/100/propBets"
    "?lang=en&region=us&limit=100"
)

# The Odds API — multi-book aggregator. Set env var THE_ODDS_API_KEY to override.
THE_ODDS_API_KEY = os.environ.get(
    "THE_ODDS_API_KEY", "fdb2de0728216509287d06490355c922"
)
THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports/basketball_wnba"

# Short labels for sportsbooks
BOOK_SHORT = {
    "draftkings": "DK", "fanduel": "FD", "betmgm": "MGM",
    "caesars": "CZR", "betrivers": "BR", "pointsbetus": "PB",
    "betonlineag": "BO", "bovada": "BV", "lowvig": "LV",
    "mybookieag": "MB", "williamhill_us": "WH", "espnbet": "TSB",
    "wynnbet": "WY", "unibet_us": "UN", "barstool": "BS",
    "superbook": "SB", "twinspires": "TW", "tipico_us": "TC",
    "sisportsbook": "SI", "fanatics": "FAN", "hardrockbet": "HR",
    # us2-region books (theScore Bet comes through the espnbet key above)
    "ballybet": "BLY", "betanysports": "BAS", "betparx": "PRX",
    "fliff": "FLF", "rebet": "RB", "betus": "BUS",
}

# Only ingest lines from these books (Odds API keys). The request pulls the
# us,us2 regions so theScore Bet (espnbet) is reachable; this allowlist then
# restricts which books actually feed consensus lines / best-price shopping,
# keeping soft/sweepstakes books (Fliff, ReBet, etc.) out of the pool.
ALLOWED_BOOKS = {
    "fanduel", "draftkings", "betmgm", "williamhill_us",  # FD, DK, MGM, Caesars
    "betrivers", "espnbet",                                # BetRivers, theScore Bet
}

# Weights for rolling window: games 1-5 (most recent) = 1.0, 6-10 = 0.7, 11-15 = 0.4
GAME_WEIGHTS = [1.0] * 5 + [0.7] * 5 + [0.4] * 5

# WNBA regulation: 40 minutes
REGULATION_MIN = 40.0

# League averages (will be recomputed from CSV at runtime)
LEAGUE_AVG_DRTG = 105.4
LEAGUE_AVG_PACE = 77.3
LEAGUE_AVG_REB_PG = 33.5

# Minimum minutes threshold — WNBA rotations are tighter (10-11 deep)
# but minutes per game similar; use 8.0 to capture rotation players
MIN_MPG_THRESHOLD = 8.0

# Minimum edge to flag as a bet candidate
PTS_EDGE_THRESHOLD = 1.5
REB_EDGE_THRESHOLD = 1.0

# Hard cap on projected minutes (4 x 10 = 40 + OT). Star WNBA players
# routinely play 32-36; cap at 38 to leave headroom while never exceeding 40.
DEFAULT_MAX_MIN = 38.0

# Seasons to pull for game logs.  Early-2026 has limited samples, so we
# also pull 2025 (regular + post) for context, then weight the resulting
# games by recency. Prior season acts as a prior when 2026 sample is small.
SEASONS_TO_FETCH = [2026, 2025]

# When 2026 sample is small, pad with 2025 games (most-recent first) up to
# a total of 15 games to feed the rolling window.
PRIOR_SEASON_PAD_TARGET = 15

# A projection leaning on fewer than this many CURRENT-season games is flagged
# "thin:N" in the output — the rest of its sample is prior-season padding, so a
# recent form/role change shows up as a loud but fragile edge (see Ionescu,
# Jun 2026: 6 games at ~9 ppg padded with 2025 produced a +56% phantom UNDER).
THIN_SAMPLE_GP = 8

# ESPN team name aliases — ESPN scoreboard uses location, CSV is keyed by location too,
# but a couple of WNBA team locations have alternate forms.
ESPN_TO_CSV = {
    # Currently no aliases needed — ESPN locations match CSV keys.
}


# ── HTTP helper ────────────────────────────────────────────────────────────

def fetch_json(url: str, retries: int = 2, timeout: int = 15) -> dict | None:
    """Fetch JSON from a URL with retries."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(1)
            else:
                print(f"  [WARN] Failed to fetch {url}: {e}")
                return None


# ── 1. Load team ratings ──────────────────────────────────────────────────

def load_team_ratings() -> dict:
    """Load team ratings from CSV. Returns {team_name: {pace, oe, de}}."""
    ratings = {}
    with open(RATINGS_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["Team"].strip()
            if not name:
                continue
            ratings[name] = {
                "pace": float(row["Pace"]),
                "oe": float(row["OE"]),
                "de": float(row["DE"]),
            }
    return ratings


# ── 2. Fetch today's schedule ─────────────────────────────────────────────

def fetch_schedule(date_str: str) -> list[dict]:
    """Fetch today's games from ESPN. Returns list of game dicts."""
    url = f"{ESPN_SCOREBOARD}?dates={date_str}"
    data = fetch_json(url)
    if not data:
        return []

    games = []
    for event in data.get("events", []):
        status = event["status"]["type"]["description"]
        if status not in ("Scheduled", "In Progress", "Halftime",
                          "End of 1st Quarter", "End of 2nd Quarter",
                          "End of 3rd Quarter"):
            continue  # skip completed games

        comp = event["competitions"][0]
        game = {
            "event_id": event["id"],
            "short_name": event["shortName"],
            "status": status,
            "start_time": event.get("date", ""),
            "neutral_site": comp.get("neutralSite", False),
            "home": None,
            "away": None,
            "spread": None,
            "over_under": None,
        }

        # Odds
        odds = comp.get("odds", [])
        if odds:
            game["spread"] = odds[0].get("spread")
            game["over_under"] = odds[0].get("overUnder")
            game["spread_detail"] = odds[0].get("details", "")

        # Teams
        for team_data in comp.get("competitors", []):
            t = team_data["team"]
            side = team_data["homeAway"]
            team_info = {
                "id": t["id"],
                "name": t.get("location", t.get("displayName", "")),
                "display_name": t.get("displayName", ""),
                "abbreviation": t.get("abbreviation", ""),
            }
            game[side] = team_info

        games.append(game)

    return games


# ── 3. Fetch injuries for a game ─────────────────────────────────────────

def fetch_injuries(event_id: str) -> dict:
    """Fetch injuries for a game. Returns {player_id: status}."""
    url = f"{ESPN_SUMMARY}?event={event_id}"
    data = fetch_json(url)
    if not data:
        return {}

    injuries = {}
    for team_inj in data.get("injuries", []):
        for inj in team_inj.get("injuries", []):
            athlete = inj.get("athlete", {})
            pid = str(athlete.get("id", ""))
            status = inj.get("status", "").lower()
            if pid:
                injuries[pid] = status
    return injuries


# ── 4. Fetch team roster ─────────────────────────────────────────────────

def fetch_roster(team_id: str) -> list[dict]:
    """Fetch a team's roster. Returns list of {id, name, position, age}."""
    url = ESPN_ROSTER.format(team_id=team_id)
    data = fetch_json(url)
    if not data:
        return []

    raw_athletes = data.get("athletes", [])
    # WNBA sometimes nests athletes inside position groups (items)
    if raw_athletes and isinstance(raw_athletes[0], dict) and "items" in raw_athletes[0]:
        flat = []
        for grp in raw_athletes:
            flat.extend(grp.get("items", []))
        raw_athletes = flat

    players = []
    for a in raw_athletes:
        players.append({
            "id": str(a["id"]),
            "name": a.get("displayName", ""),
            "position": a.get("position", {}).get("abbreviation", ""),
            "age": a.get("age", 0),
        })
    return players


# ── 5. Fetch player game log ─────────────────────────────────────────────

def _parse_gamelog_payload(data: dict) -> list[dict]:
    """
    Parse one ESPN gamelog response into a list of game dicts.
    WNBA labels: ['MIN','PTS','REB','AST','STL','BLK','TO','FG','FG%','3PT','3P%','FT','FT%','PF']
    """
    if not data:
        return []
    labels = data.get("labels", [])
    if not labels:
        return []
    idx = {lbl: i for i, lbl in enumerate(labels)}

    games = []
    for st in data.get("seasonTypes", []):
        display = st.get("displayName", "")
        if "Regular" not in display and "regular" not in display.lower():
            continue
        for cat in st.get("categories", []):
            for evt in cat.get("events", []):
                stats = evt.get("stats", [])
                if not stats:
                    continue
                try:
                    def get(label, default="0"):
                        i = idx.get(label, -1)
                        if i < 0 or i >= len(stats):
                            return default
                        v = stats[i]
                        return v if v not in (None, "", "--") else default

                    mins_raw = get("MIN", "0")
                    pts_raw = get("PTS", "0")
                    reb_raw = get("REB", "0")
                    pf_raw = get("PF", "0")
                    fg_raw = get("FG", "0-0")
                    ft_raw = get("FT", "0-0")
                    tpt_raw = get("3PT", "0-0")

                    mins = int(mins_raw) if str(mins_raw).strip().isdigit() else 0
                    pts = int(pts_raw) if str(pts_raw).strip().isdigit() else 0
                    reb = int(reb_raw) if str(reb_raw).strip().isdigit() else 0
                    pf = int(pf_raw) if str(pf_raw).strip().isdigit() else 0

                    def parse_ma(s):
                        if not s or s in ("--", ""):
                            return 0, 0
                        parts = s.split("-")
                        if len(parts) != 2:
                            return 0, 0
                        try:
                            return int(parts[0]), int(parts[1])
                        except ValueError:
                            return 0, 0

                    fg_m, fg_a = parse_ma(fg_raw)
                    ft_m, ft_a = parse_ma(ft_raw)
                    fg3_m, fg3_a = parse_ma(tpt_raw)

                    if mins > 0:
                        games.append({
                            "event_id": evt.get("eventId", ""),
                            "min": mins,
                            "pts": pts,
                            "reb": reb,
                            "pf": pf,
                            "fg_m": fg_m,
                            "fg_a": fg_a,
                            "ft_m": ft_m,
                            "ft_a": ft_a,
                            "fg3_m": fg3_m,
                            "fg3_a": fg3_a,
                        })
                except (ValueError, IndexError):
                    continue

    return games


def fetch_player_gamelog(player_id: str) -> list[dict]:
    """
    Fetch a player's regular-season game log for SEASONS_TO_FETCH and
    return them concatenated, most-recent-season first. ESPN orders
    months reverse-chronologically within a season, so within each
    season the most-recent game is first.

    For early-season 2026 with sparse games, the pool is padded with
    2025 games up to PRIOR_SEASON_PAD_TARGET total.
    """
    season_games: dict[int, list[dict]] = {}
    for season in SEASONS_TO_FETCH:
        url = ESPN_GAMELOG.format(player_id=player_id, season=season)
        data = fetch_json(url)
        parsed = _parse_gamelog_payload(data) if data else []
        for g in parsed:
            g["season"] = season          # tag so we can count current-season games
        season_games[season] = parsed

    # Newest season first
    games: list[dict] = []
    for season in SEASONS_TO_FETCH:
        games.extend(season_games.get(season, []))
        if len(games) >= PRIOR_SEASON_PAD_TARGET:
            break

    return games


# ── 5b. Fetch prop lines for a game ───────────────────────────────────────

def fetch_prop_lines(event_id: str) -> dict:
    """
    Fetch DraftKings player prop lines from ESPN for a game.
    Returns {player_id: {pts_line, pts_over_odds, pts_under_odds,
                         reb_line, reb_over_odds, reb_under_odds,
                         pr_line,  pr_over_odds,  pr_under_odds}}
    """
    url = ESPN_PROP_BETS.format(event_id=event_id)
    data = fetch_json(url)
    if not data:
        return {}

    items = data.get("items", [])
    props: dict[str, dict] = {}

    i = 0
    while i < len(items) - 1:
        item1 = items[i]
        item2 = items[i + 1]

        ref1 = item1.get("athlete", {}).get("$ref", "")
        ref2 = item2.get("athlete", {}).get("$ref", "")
        type1 = item1.get("type", {}).get("name", "")
        type2 = item2.get("type", {}).get("name", "")
        total1 = item1.get("odds", {}).get("total", {}).get("value", "")
        total2 = item2.get("odds", {}).get("total", {}).get("value", "")

        if ref1 == ref2 and type1 == type2 and total1 == total2:
            if not total1:
                i += 2
                continue
            aid = ref1.split("/")[-1].split("?")[0]
            try:
                total = float(total1)
                over_odds = int(item1["odds"]["american"]["value"])
                under_odds = int(item2["odds"]["american"]["value"])
            except (ValueError, KeyError):
                i += 2
                continue

            if aid not in props:
                props[aid] = {}

            if type1 == "Total Points":
                props[aid]["pts_line"] = total
                props[aid]["pts_over_odds"] = over_odds
                props[aid]["pts_under_odds"] = under_odds
            elif type1 == "Total Rebounds":
                props[aid]["reb_line"] = total
                props[aid]["reb_over_odds"] = over_odds
                props[aid]["reb_under_odds"] = under_odds
            elif type1 == "Total Points and Rebounds":
                props[aid]["pr_line"] = total
                props[aid]["pr_over_odds"] = over_odds
                props[aid]["pr_under_odds"] = under_odds

            i += 2
        else:
            i += 1

    return props


def _normalize_name(name: str) -> str:
    """Normalize player names for cross-source matching."""
    s = name.lower().strip()
    for ch in ("'", "’", ".", ","):
        s = s.replace(ch, "")
    s = s.replace("-", " ")
    return " ".join(s.split())


def _normalize_team(name: str) -> str:
    """Normalize team display names (e.g. 'Atlanta Dream') for matching."""
    return " ".join(name.lower().strip().split())


def fetch_odds_api_props(
    games: list[dict],
    name_to_pid_by_event: dict[str, dict[str, str]],
    api_key: str = THE_ODDS_API_KEY,
) -> dict:
    """
    Fetch player prop lines from The Odds API across multiple US sportsbooks.

    For each player x market, picks the consensus line (mode across books) and
    the best over/under American odds available at that consensus line.

    Returns {espn_event_id: {espn_player_id: {pts_line, pts_over_odds,
            pts_under_odds, pts_over_book, pts_under_book, reb_*, pr_*}}}.
    """
    # 1. Get all WNBA events on The Odds API
    events_url = f"{THE_ODDS_API_BASE}/events?apiKey={api_key}"
    events = fetch_json(events_url)
    if not events or not isinstance(events, list):
        print("  [WARN] Could not fetch events from The Odds API.")
        return {}

    # 2. Map team-pair -> Odds API event id
    pair_to_oaid = {}
    for e in events:
        away = _normalize_team(e.get("away_team", ""))
        home = _normalize_team(e.get("home_team", ""))
        pair_to_oaid[(away, home)] = e["id"]

    # 3. Map ESPN event_id -> Odds API event_id by team-pair match
    espn_to_oa = {}
    for g in games:
        away_full = _normalize_team(g["away"].get("display_name", ""))
        home_full = _normalize_team(g["home"].get("display_name", ""))
        oaid = pair_to_oaid.get((away_full, home_full))
        if oaid:
            espn_to_oa[g["event_id"]] = oaid

    # 4. For each matched event, fetch player props from all US books
    markets = "player_points,player_rebounds,player_points_rebounds"

    def fetch_one(espn_eid, oa_eid):
        url = (
            f"{THE_ODDS_API_BASE}/events/{oa_eid}/odds"
            f"?apiKey={api_key}&regions=us,us2&markets={markets}"
            f"&oddsFormat=american"
        )
        return espn_eid, fetch_json(url)

    raw_by_event = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(fetch_one, espn_eid, oa_eid)
            for espn_eid, oa_eid in espn_to_oa.items()
        ]
        for fut in as_completed(futures):
            espn_eid, data = fut.result()
            if data:
                raw_by_event[espn_eid] = data

    # 5. Process each event's props into best-line-by-player
    result: dict[str, dict] = {}
    market_map = {
        "player_points": "pts",
        "player_rebounds": "reb",
        "player_points_rebounds": "pr",
    }

    for espn_eid, data in raw_by_event.items():
        name_to_pid = name_to_pid_by_event.get(espn_eid, {})
        # collected[player_name_norm][market_prefix][line] = {
        #     "over": [(odds, book_key)], "under": [(odds, book_key)]
        # }
        collected: dict[str, dict] = {}

        for bm in data.get("bookmakers", []):
            book_key = bm.get("key", "")
            if book_key not in ALLOWED_BOOKS:
                continue
            for mk in bm.get("markets", []):
                mkey = mk.get("key", "")
                prefix = market_map.get(mkey)
                if not prefix:
                    continue
                for o in mk.get("outcomes", []):
                    pname = o.get("description", "")
                    side = o.get("name", "").lower()  # "over" / "under"
                    try:
                        line = float(o.get("point"))
                        odds = int(o.get("price"))
                    except (TypeError, ValueError):
                        continue
                    if side not in ("over", "under"):
                        continue
                    nkey = _normalize_name(pname)
                    if not nkey:
                        continue
                    pl = collected.setdefault(nkey, {}).setdefault(prefix, {})
                    pl_l = pl.setdefault(line, {"over": [], "under": []})
                    pl_l[side].append((odds, book_key))

        # 6. Reduce to best line + best odds for each player x market
        event_out: dict[str, dict] = {}
        for nkey, mkts in collected.items():
            pid = name_to_pid.get(nkey)
            if not pid:
                continue
            for prefix, lines in mkts.items():
                # Pick consensus line: line offered by the most distinct books
                def line_score(item):
                    line, sides = item
                    books = set()
                    for odds, bk in sides["over"]:
                        books.add(bk)
                    for odds, bk in sides["under"]:
                        books.add(bk)
                    # Tie-breaker: prefer lower line for over-side EV neutrality
                    return (len(books), -line)

                line, sides = max(lines.items(), key=line_score)
                if not sides["over"] or not sides["under"]:
                    # Fall back: pick the line that has both sides
                    candidates = [
                        (ln, s) for ln, s in lines.items() if s["over"] and s["under"]
                    ]
                    if not candidates:
                        continue
                    line, sides = max(candidates, key=line_score)

                # Best American odds = highest value (i.e. +150 beats -110)
                best_over = max(sides["over"], key=lambda t: t[0])
                best_under = max(sides["under"], key=lambda t: t[0])

                entry = event_out.setdefault(pid, {})
                entry[f"{prefix}_line"] = line
                entry[f"{prefix}_over_odds"] = best_over[0]
                entry[f"{prefix}_under_odds"] = best_under[0]
                entry[f"{prefix}_over_book"] = BOOK_SHORT.get(best_over[1], best_over[1][:3].upper())
                entry[f"{prefix}_under_book"] = BOOK_SHORT.get(best_under[1], best_under[1][:3].upper())
                entry[f"{prefix}_book_count"] = len({bk for _, bk in sides["over"]} | {bk for _, bk in sides["under"]})

                # Retain every book's line + over/under odds (across ALL lines
                # offered, not just the consensus one) so the report can list
                # the full board and compute per-book EV. One row per book,
                # pairing that book's over/under at the line where it posted both.
                per_book: dict[str, dict] = {}
                for ln, s in lines.items():
                    over_by_bk = {bk: od for od, bk in s["over"]}
                    under_by_bk = {bk: od for od, bk in s["under"]}
                    for bk in set(over_by_bk) | set(under_by_bk):
                        # Prefer a line where this book posted BOTH sides; if a
                        # book straddles lines, keep its most complete entry.
                        existing = per_book.get(bk)
                        has_both = bk in over_by_bk and bk in under_by_bk
                        if existing and existing.get("both") and not has_both:
                            continue
                        per_book[bk] = {
                            "book": BOOK_SHORT.get(bk, bk[:3].upper()),
                            "line": ln,
                            "over_odds": over_by_bk.get(bk),
                            "under_odds": under_by_bk.get(bk),
                            "both": has_both,
                        }
                entry[f"{prefix}_all_books"] = sorted(
                    per_book.values(), key=lambda d: d["book"]
                )

        if event_out:
            result[espn_eid] = event_out

    return result


def american_to_decimal(american: int) -> float:
    if american >= 100:
        return 1 + american / 100
    else:
        return 1 + 100 / abs(american)


def implied_probability(american: int) -> float:
    if american >= 100:
        return 100 / (american + 100)
    else:
        return abs(american) / (abs(american) + 100)


# Blend weight for combining the model's simulated probability with the
# market's no-vig probability before computing EV.
#   1.0 = trust the model entirely, 0.0 = trust the market entirely.
# The closing line is the single sharpest estimator available, so anchoring
# partially to it trims false edges — especially on thin-sample players
# (rookies, injury returns, expansion teams) where the sim is overconfident.
MODEL_BLEND_WEIGHT = 0.70


def calc_ev_sim(sim_outcomes: list[float], line: float, over_odds: int,
                under_odds: int) -> dict:
    """Compute EV from Monte-Carlo outcomes vs sportsbook odds.

    The model's simulated win probability is de-vigged against the market's
    no-vig implied probability and the two are blended (MODEL_BLEND_WEIGHT)
    before EV is computed, so the recommendation leans on the market where
    the model is least certain.
    """
    n = len(sim_outcomes)
    if n == 0:
        return {"edge": 0, "p_over": 0.5, "p_under": 0.5,
                "ev_over": 0, "ev_under": 0, "rec": "", "median": 0}

    sorted_out = sorted(sim_outcomes)
    median = sorted_out[n // 2]

    count_over = sum(1 for v in sim_outcomes if v > line)
    count_push = sum(1 for v in sim_outcomes if v == line)
    count_under = sum(1 for v in sim_outcomes if v < line)

    # Model probabilities (pushes split evenly).
    p_over_model = (count_over + count_push * 0.5) / n
    p_under_model = (count_under + count_push * 0.5) / n

    # Market no-vig probabilities — strip the bookmaker hold so we blend
    # against a fair price rather than the vigged one.
    imp_over = implied_probability(over_odds)
    imp_under = implied_probability(under_odds)
    vig_total = imp_over + imp_under
    if vig_total > 0:
        mkt_p_over = imp_over / vig_total
        mkt_p_under = imp_under / vig_total
    else:
        mkt_p_over, mkt_p_under = p_over_model, p_under_model

    # Blend model with market (complementary, so they still sum to 1).
    w = MODEL_BLEND_WEIGHT
    p_over = w * p_over_model + (1 - w) * mkt_p_over
    p_under = w * p_under_model + (1 - w) * mkt_p_under

    edge = median - line

    dec_over = american_to_decimal(over_odds)
    dec_under = american_to_decimal(under_odds)

    ev_over = p_over * (dec_over - 1) - (1 - p_over) * 1
    ev_under = p_under * (dec_under - 1) - (1 - p_under) * 1

    ev_over_pct = ev_over * 100
    ev_under_pct = ev_under * 100

    if ev_over_pct > ev_under_pct and ev_over_pct > 0:
        rec = "OVER"
    elif ev_under_pct > ev_over_pct and ev_under_pct > 0:
        rec = "UNDER"
    else:
        rec = ""

    return {
        "median": round(median, 1),
        "edge": round(edge, 1),
        "p_over": round(p_over, 3),
        "p_under": round(p_under, 3),
        "p_over_model": round(p_over_model, 3),
        "p_over_mkt": round(mkt_p_over, 3),
        "ev_over": round(ev_over_pct, 1),
        "ev_under": round(ev_under_pct, 1),
        "rec": rec,
    }


# ── Monte Carlo simulation engine ────────────────────────────────────────

NUM_SIMS = 10000


def simulate_player(
    gamelog: list[dict],
    expected_min: float,
    ppm: float,
    rpm: float,
    opp_def_adj: float,
    reb_opp_adj: float,
    pace_adj: float,
    usage_boost: float,
    max_min_cap: float = DEFAULT_MAX_MIN,
) -> dict:
    """Run a correlated bootstrap Monte Carlo for points and rebounds.

    Each iteration draws ONE historical game (weighted by recency and
    minutes played) and replays it scaled to tonight's projected minutes,
    then applies today's matchup adjustments. Drawing a single game per
    iteration preserves the within-game points<->rebounds correlation that
    the points+rebounds combo prop depends on.
    """
    recent = gamelog[:15]
    n_games = len(recent)

    if n_games < 3 or expected_min < 1:
        det_pts = round(expected_min * ppm * (1 + usage_boost) * opp_def_adj * pace_adj)
        det_reb = round(expected_min * rpm * reb_opp_adj * pace_adj)
        return {
            "sim_pts": [det_pts] * NUM_SIMS,
            "sim_reb": [det_reb] * NUM_SIMS,
            "sim_pr": [det_pts + det_reb] * NUM_SIMS,
            "median_pts": det_pts,
            "median_reb": det_reb,
            "median_pr": det_pts + det_reb,
        }

    # Recency weights for the same 15-game window the point estimate uses,
    # so the simulation centers on the weighted projection rather than a flat
    # average of the last 15.
    weights = GAME_WEIGHTS[:n_games]

    game_minutes = [g["min"] for g in recent]
    avg_min = sum(game_minutes) / n_games
    if avg_min > 0:
        min_std = max(
            (sum((m - avg_min) ** 2 for m in game_minutes) / n_games) ** 0.5,
            1.5,
        )
    else:
        min_std = 3.0

    # Draw probability per game = recency weight x minutes played. Minute-
    # weighting downweights noisy low-minute games and makes the expected
    # sampled rate equal the minute-weighted ppm/rpm of the point estimate.
    # (All logged games have min > 0, so every weight is positive.)
    draw_weights = [w * g["min"] for w, g in zip(weights, recent)]

    sim_pts = []
    sim_reb = []
    sim_pr = []

    # One game per simulation keeps points and rebounds correlated; draw the
    # whole batch up front (random.choices builds its cumulative weights once).
    drawn = random.choices(recent, weights=draw_weights, k=NUM_SIMS)
    for g in drawn:
        sim_min = random.gauss(expected_min, min_std)
        sim_min = max(0.0, min(sim_min, max_min_cap))

        # Replay the drawn game scaled to tonight's projected minutes. This
        # ties efficiency to minutes — a 9-minute game's line is no longer
        # stretched across 32 projected minutes as if the rate were stable.
        scale = sim_min / g["min"]
        pts = max(round(g["pts"] * scale * (1 + usage_boost) * opp_def_adj * pace_adj), 0)
        reb = max(round(g["reb"] * scale * reb_opp_adj * pace_adj), 0)

        sim_pts.append(pts)
        sim_reb.append(reb)
        sim_pr.append(pts + reb)

    sorted_pts = sorted(sim_pts)
    sorted_reb = sorted(sim_reb)
    sorted_pr = sorted(sim_pr)
    mid = NUM_SIMS // 2

    return {
        "sim_pts": sim_pts,
        "sim_reb": sim_reb,
        "sim_pr": sim_pr,
        "median_pts": sorted_pts[mid],
        "median_reb": sorted_reb[mid],
        "median_pr": sorted_pr[mid],
    }


# ── 6. Compute weighted stats from game log ──────────────────────────────

def compute_player_stats(games: list[dict]) -> dict | None:
    """Compute weighted rolling averages from game log."""
    if not games:
        return None

    recent = games[:15]
    n = len(recent)
    weights = GAME_WEIGHTS[:n]

    w_min = sum(w * g["min"] for w, g in zip(weights, recent))
    w_sum = sum(weights)
    total_w_min = w_min

    weighted_mpg = w_min / w_sum

    if weighted_mpg < MIN_MPG_THRESHOLD:
        return None

    w_pts = sum(w * g["pts"] for w, g in zip(weights, recent))
    ppm = w_pts / total_w_min if total_w_min > 0 else 0

    w_reb = sum(w * g["reb"] for w, g in zip(weights, recent))
    rpm = w_reb / total_w_min if total_w_min > 0 else 0

    w_pf = sum(w * g["pf"] for w, g in zip(weights, recent))
    avg_pf = w_pf / w_sum

    total_fta = sum(g["ft_a"] for g in recent)
    total_fga = sum(g["fg_a"] for g in recent)
    ftr = total_fta / total_fga if total_fga > 0 else 0

    total_fg3a = sum(g["fg3_a"] for g in recent)
    fg3_rate = total_fg3a / total_fga if total_fga > 0 else 0

    all_games = games
    season_mpg = sum(g["min"] for g in all_games) / len(all_games) if all_games else 0
    season_ppg = sum(g["pts"] for g in all_games) / len(all_games) if all_games else 0
    season_rpg = sum(g["reb"] for g in all_games) / len(all_games) if all_games else 0
    games_played = len(all_games)
    cur_season_games = sum(1 for g in all_games if g.get("season") == SEASONS_TO_FETCH[0])

    last_40 = games[:40]
    max_min_last_40 = max((g["min"] for g in last_40), default=0.0)

    return {
        "weighted_mpg": weighted_mpg,
        "ppm": ppm,
        "rpm": rpm,
        "avg_pf": avg_pf,
        "ftr": ftr,
        "fg3_rate": fg3_rate,
        "season_mpg": season_mpg,
        "season_ppg": season_ppg,
        "season_rpg": season_rpg,
        "games_played": games_played,
        "cur_season_games": cur_season_games,
        "max_min_last_40": max_min_last_40,
    }


# ── 7. Detect back-to-back ───────────────────────────────────────────────

def fetch_team_schedule_b2b(team_id: str, target_date: str) -> bool:
    """Check if a team played yesterday (B2B 2nd night)."""
    dt = datetime.strptime(target_date, "%Y%m%d")
    yesterday = (dt - timedelta(days=1)).strftime("%Y%m%d")
    url = f"{ESPN_SCOREBOARD}?dates={yesterday}"
    data = fetch_json(url)
    if not data:
        return False

    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            for team in comp.get("competitors", []):
                if team["team"]["id"] == team_id:
                    return True
    return False


# ── 8. Projection engine ─────────────────────────────────────────────────

def project_player(
    player: dict,
    stats: dict,
    team_ratings: dict,
    opp_ratings: dict,
    is_home: bool,
    spread: float | None,
    team_b2b: bool,
    injured_teammates: list[dict],
    player_recent_events: list | None = None,
) -> dict:
    """Project expected minutes, points, and rebounds for a player."""

    base_min = stats["weighted_mpg"]

    # Presence gate: the weighted fraction of THIS player's recent game sample
    # that each OUT teammate actually played in (matched by ESPN event id). A
    # teammate who's been out for the whole sample (e.g. a season-long injury)
    # is ALREADY reflected in base_min / ppm / rpm, so their redistribution
    # boost must not be applied again -> presence ~0. A just-injured teammate
    # who played the whole sample scores ~1.0 and gets the full boost (the
    # original behaviour). Recent games are weighted like the rolling averages.
    _recent_events = player_recent_events or []

    def _presence(tm: dict) -> float:
        tm_events = tm.get("events") or set()
        num = den = 0.0
        for i, ev in enumerate(_recent_events):
            if not ev:
                continue
            w = GAME_WEIGHTS[i] if i < len(GAME_WEIGHTS) else GAME_WEIGHTS[-1]
            den += w
            if ev in tm_events:
                num += w
        return (num / den) if den else 1.0

    presence = {tm["id"]: _presence(tm) for tm in injured_teammates}

    # B2B adjustment (by age) — WNBA effect smaller than NBA per published research
    b2b_adj = 0.0
    if team_b2b:
        age = player.get("age", 25)
        if age >= 33:
            b2b_adj = -2.5
        elif age >= 28:
            b2b_adj = -1.5
        else:
            b2b_adj = -0.7

    # Blowout adjustment — WNBA games are 40 min so threshold tighter
    blowout_adj = 0.0
    if spread is not None:
        abs_spread = abs(spread)
        if abs_spread > 7:
            blowout_adj = -0.5 * (abs_spread - 7)

    # Foul trouble — WNBA personal foul threshold lower (5 disqualifies vs 6 in NBA)
    foul_adj = 0.0
    if stats["avg_pf"] >= 3.2:
        foul_adj = -1.0
    elif stats["avg_pf"] >= 2.7:
        foul_adj = -0.5

    # Injury redistribution
    injury_min_boost = 0.0
    pos = player.get("position", "")
    is_big = pos in ("C", "PF", "F-C", "C-F")
    is_guard = pos in ("PG", "SG", "G", "G-F")
    is_wing = pos in ("SF", "F", "F-G")

    for tm in injured_teammates:
        tm_stats = tm.get("stats")
        if not tm_stats:
            continue
        pf = presence.get(tm["id"], 1.0)
        if pf <= 0:
            continue  # absence already baked into this player's baseline
        absent_mpg = tm_stats["weighted_mpg"]
        tm_pos = tm.get("position", "")
        tm_is_big = tm_pos in ("C", "PF", "F-C", "C-F")
        tm_is_guard = tm_pos in ("PG", "SG", "G", "G-F")
        tm_is_wing = tm_pos in ("SF", "F", "F-G")

        if (is_big and tm_is_big) or (is_guard and tm_is_guard) or (is_wing and tm_is_wing):
            if base_min >= 22:
                injury_min_boost += absent_mpg * 0.20 * pf
            elif base_min >= 12:
                injury_min_boost += absent_mpg * 0.12 * pf
        else:
            if base_min >= 24:
                injury_min_boost += absent_mpg * 0.06 * pf

    expected_min = base_min + b2b_adj + blowout_adj + foul_adj + injury_min_boost
    expected_min = max(expected_min, 0.0)
    max_min_cap = stats.get("max_min_last_40") or DEFAULT_MAX_MIN
    expected_min = min(expected_min, max_min_cap, REGULATION_MIN - 2)

    # Expected Points
    ppm = stats["ppm"]

    usage_boost = 0.0
    for tm in injured_teammates:
        tm_stats = tm.get("stats")
        if not tm_stats:
            continue
        pf = presence.get(tm["id"], 1.0)
        if tm_stats["season_ppg"] >= 16:
            usage_boost += 0.07 * pf
        elif tm_stats["season_ppg"] >= 12:
            usage_boost += 0.035 * pf
    adjusted_ppm = ppm * (1 + usage_boost)

    opp_de = opp_ratings.get("de", LEAGUE_AVG_DRTG)
    opp_def_adj = opp_de / LEAGUE_AVG_DRTG

    team_pace = team_ratings.get("pace", LEAGUE_AVG_PACE)
    opp_pace = opp_ratings.get("pace", LEAGUE_AVG_PACE)
    game_pace = (team_pace + opp_pace) / 2
    pace_adj = game_pace / LEAGUE_AVG_PACE

    expected_pts = expected_min * adjusted_ppm * opp_def_adj * pace_adj

    # Expected Rebounds
    rpm_val = stats["rpm"]

    opp_oe = opp_ratings.get("oe", LEAGUE_AVG_DRTG)
    reb_opp_adj = (2 * LEAGUE_AVG_DRTG - opp_oe) / LEAGUE_AVG_DRTG
    reb_opp_adj = max(0.90, min(reb_opp_adj, 1.15))

    reb_teammate_boost = 0.0
    for tm in injured_teammates:
        tm_stats = tm.get("stats")
        if not tm_stats:
            continue
        pf = presence.get(tm["id"], 1.0)
        tm_pos = tm.get("position", "")
        tm_is_big = tm_pos in ("C", "PF", "F-C", "C-F")
        if tm_stats["season_rpg"] >= 6:
            if is_big and tm_is_big:
                reb_teammate_boost += tm_stats["season_rpg"] * 0.35 * pf
            elif is_big:
                reb_teammate_boost += tm_stats["season_rpg"] * 0.15 * pf
            elif (is_wing or is_guard) and tm_is_big:
                reb_teammate_boost += tm_stats["season_rpg"] * 0.08 * pf

    expected_reb = expected_min * rpm_val * reb_opp_adj * pace_adj + reb_teammate_boost

    return {
        "expected_min": round(expected_min, 1),
        "expected_pts": round(expected_pts, 1),
        "expected_reb": round(expected_reb, 1),
        "base_min": round(base_min, 1),
        "b2b_adj": round(b2b_adj, 1),
        "blowout_adj": round(blowout_adj, 1),
        "foul_adj": round(foul_adj, 1),
        "injury_min_boost": round(injury_min_boost, 1),
        "max_min_cap": round(max_min_cap, 1),
        "ppm": round(ppm, 3),
        "rpm": round(rpm_val, 3),
        "opp_def_adj": round(opp_def_adj, 3),
        "reb_opp_adj": round(reb_opp_adj, 3),
        "pace_adj": round(pace_adj, 3),
        "usage_boost": round(usage_boost, 3),
    }


# ── 9. Resolve team name to CSV key ──────────────────────────────────────

def resolve_team_name(espn_name: str, all_ratings: dict) -> str | None:
    """Try to match an ESPN team name to a ratings CSV key."""
    if espn_name in all_ratings:
        return espn_name
    mapped = ESPN_TO_CSV.get(espn_name)
    if mapped and mapped in all_ratings:
        return mapped
    espn_lower = espn_name.lower()
    for key in all_ratings:
        if key.lower() in espn_lower or espn_lower in key.lower():
            return key
    return None


# ── 10. Main pipeline ─────────────────────────────────────────────────────

def run_projections(date_str: str, generate_pdf: bool = False):
    """Main entry point: scrape data and project all players."""

    print(f"\n{'='*70}")
    print(f"  WNBA Player Props Projections 2 (pre-Jul-1 model) — {date_str[:4]}-{date_str[4:6]}-{date_str[6:]}")
    print(f"{'='*70}\n")

    print("[1/7] Loading team ratings...")
    all_ratings = load_team_ratings()
    print(f"       Loaded {len(all_ratings)} teams from CSV.\n")

    if all_ratings:
        global LEAGUE_AVG_DRTG, LEAGUE_AVG_PACE
        LEAGUE_AVG_DRTG = sum(r["de"] for r in all_ratings.values()) / len(all_ratings)
        LEAGUE_AVG_PACE = sum(r["pace"] for r in all_ratings.values()) / len(all_ratings)
        print(f"       League Avg DRtg: {LEAGUE_AVG_DRTG:.1f}, Avg Pace: {LEAGUE_AVG_PACE:.1f}\n")

    print("[2/7] Fetching schedule...")
    games = fetch_schedule(date_str)
    if not games:
        print("       No upcoming/in-progress games found.")
        return
    print(f"       Found {len(games)} games to project.\n")
    for g in games:
        sp = g.get("spread_detail", "N/A")
        ou = g.get("over_under", "N/A")
        neutral = " (NEUTRAL)" if g.get("neutral_site") else ""
        print(f"       {g['short_name']:18s}  Spread: {str(sp):15s}  O/U: {ou}{neutral}")
    print()

    print("[3/7] Checking back-to-back status...")
    team_ids = set()
    for g in games:
        team_ids.add(g["home"]["id"])
        team_ids.add(g["away"]["id"])

    b2b_status = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(fetch_team_schedule_b2b, tid, date_str): tid
            for tid in team_ids
        }
        for future in as_completed(futures):
            tid = futures[future]
            b2b_status[tid] = future.result()

    b2b_teams = [tid for tid, is_b2b in b2b_status.items() if is_b2b]
    if b2b_teams:
        print(f"       B2B teams detected: {len(b2b_teams)} teams\n")
    else:
        print("       No B2B situations.\n")

    print("[4/7] Fetching rosters and injuries...")
    game_data = []

    injury_map = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_injuries, g["event_id"]): g["event_id"]
            for g in games
        }
        for future in as_completed(futures):
            eid = futures[future]
            injury_map[eid] = future.result()

    roster_cache = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(fetch_roster, tid): tid
            for tid in team_ids
        }
        for future in as_completed(futures):
            tid = futures[future]
            roster_cache[tid] = future.result()

    for g in games:
        home_roster = roster_cache.get(g["home"]["id"], [])
        away_roster = roster_cache.get(g["away"]["id"], [])
        injuries = injury_map.get(g["event_id"], {})
        game_data.append({
            "game": g,
            "home_players": home_roster,
            "away_players": away_roster,
            "injuries": injuries,
        })

    total_players = sum(len(gd["home_players"]) + len(gd["away_players"]) for gd in game_data)
    print(f"       {total_players} total players across {len(games)} games.\n")

    print("[5/7] Fetching multi-book prop lines (The Odds API)...")
    # Build {espn_event_id: {normalized_player_name: espn_player_id}} for matching
    name_to_pid_by_event: dict[str, dict[str, str]] = {}
    for gd in game_data:
        eid = gd["game"]["event_id"]
        m = name_to_pid_by_event.setdefault(eid, {})
        for p in gd["home_players"] + gd["away_players"]:
            m[_normalize_name(p["name"])] = p["id"]

    prop_lines_map = fetch_odds_api_props(games, name_to_pid_by_event)

    total_props = sum(len(v) for v in prop_lines_map.values())
    books_used = set()
    for evprops in prop_lines_map.values():
        for pl in evprops.values():
            for k, v in pl.items():
                if k.endswith("_book") and isinstance(v, str):
                    books_used.add(v)
    book_str = ", ".join(sorted(books_used)) if books_used else "n/a"
    print(f"       {total_props} players with prop lines across {len(games)} games. Books: {book_str}\n")

    print("[6/7] Fetching player game logs (this may take 30-60 seconds)...")
    all_player_ids = set()
    for gd in game_data:
        for p in gd["home_players"] + gd["away_players"]:
            all_player_ids.add(p["id"])

    gamelog_cache = {}
    stats_cache = {}

    completed = 0
    total = len(all_player_ids)

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(fetch_player_gamelog, pid): pid
            for pid in all_player_ids
        }
        for future in as_completed(futures):
            pid = futures[future]
            games_list = future.result()
            gamelog_cache[pid] = games_list
            stats_cache[pid] = compute_player_stats(games_list)
            completed += 1
            if completed % 25 == 0 or completed == total:
                print(f"       {completed}/{total} players loaded...")

    players_with_stats = sum(1 for s in stats_cache.values() if s is not None)
    print(f"       {players_with_stats} players with sufficient data (>= {MIN_MPG_THRESHOLD} MPG).\n")

    print(f"[7/7] Running projections + Monte Carlo simulation ({NUM_SIMS:,} iterations per player)...\n")

    all_projections = []

    for gd in game_data:
        game = gd["game"]
        injuries = gd["injuries"]

        home_name = game["home"]["name"]
        away_name = game["away"]["name"]
        home_key = resolve_team_name(home_name, all_ratings)
        away_key = resolve_team_name(away_name, all_ratings)

        home_ratings = all_ratings.get(home_key, {}) if home_key else {}
        away_ratings = all_ratings.get(away_key, {}) if away_key else {}

        spread = game.get("spread")
        if spread is not None:
            try:
                spread = float(spread)
            except (ValueError, TypeError):
                spread = None

        def get_injured_teammates(roster, injuries_dict):
            injured = []
            for p in roster:
                pid = p["id"]
                if injuries_dict.get(pid) in ("out", "doubtful"):
                    s = stats_cache.get(pid)
                    tm_log = gamelog_cache.get(pid, [])
                    injured.append({
                        "id": pid,
                        "name": p["name"],
                        "position": p["position"],
                        "stats": s,
                        "events": {g["event_id"] for g in tm_log if g.get("event_id")},
                    })
            return injured

        for side, roster, team_name, team_key, opp_key, is_home in [
            ("home", gd["home_players"], home_name, home_key, away_key, True),
            ("away", gd["away_players"], away_name, away_key, home_key, False),
        ]:
            team_r = all_ratings.get(team_key, {}) if team_key else {}
            opp_r = all_ratings.get(opp_key, {}) if opp_key else {}
            team_b2b = b2b_status.get(game[side]["id"], False)
            injured_tms = get_injured_teammates(roster, injuries)

            for player in roster:
                pid = player["id"]
                if injuries.get(pid) in ("out", "doubtful"):
                    continue

                pstats = stats_cache.get(pid)
                if not pstats:
                    continue

                proj = project_player(
                    player=player,
                    stats=pstats,
                    team_ratings=team_r,
                    opp_ratings=opp_r,
                    is_home=is_home,
                    spread=spread,
                    team_b2b=team_b2b,
                    injured_teammates=injured_tms,
                    player_recent_events=[g.get("event_id", "")
                                          for g in gamelog_cache.get(pid, [])[:15]],
                )

                player_gamelog = gamelog_cache.get(pid, [])
                sim = simulate_player(
                    gamelog=player_gamelog,
                    expected_min=proj["expected_min"],
                    ppm=proj["ppm"],
                    rpm=proj["rpm"],
                    opp_def_adj=proj["opp_def_adj"],
                    reb_opp_adj=proj["reb_opp_adj"],
                    pace_adj=proj["pace_adj"],
                    usage_boost=proj["usage_boost"],
                    max_min_cap=proj["max_min_cap"],
                )

                proj["sim_median_pts"] = sim["median_pts"]
                proj["sim_median_reb"] = sim["median_reb"]
                proj["sim_median_pr"] = sim["median_pr"]

                game_props = prop_lines_map.get(game["event_id"], {})
                player_props = game_props.get(pid, {})

                entry = {
                    "game": game["short_name"],
                    "event_id": game["event_id"],
                    "team": game[side]["abbreviation"],
                    "player": player["name"],
                    "position": player["position"],
                    "is_home": is_home,
                    "b2b": team_b2b,
                    "spread": spread,
                    "injury_status": injuries.get(pid, "active"),
                    "season_gp": pstats["games_played"],
                    "cur_season_gp": pstats["cur_season_games"],
                    "season_ppg": round(pstats["season_ppg"], 1),
                    "season_rpg": round(pstats["season_rpg"], 1),
                    "season_mpg": round(pstats["season_mpg"], 1),
                    "avg_pf": round(pstats["avg_pf"], 1),
                    **proj,
                    "pts_line": None, "pts_over_odds": None, "pts_under_odds": None,
                    "pts_over_book": None, "pts_under_book": None, "pts_book_count": 0,
                    "reb_line": None, "reb_over_odds": None, "reb_under_odds": None,
                    "reb_over_book": None, "reb_under_book": None, "reb_book_count": 0,
                    "pr_line": None, "pr_over_odds": None, "pr_under_odds": None,
                    "pr_over_book": None, "pr_under_book": None, "pr_book_count": 0,
                    "pts_ev": {}, "reb_ev": {}, "pr_ev": {},
                    "pts_all_books": [], "reb_all_books": [], "pr_all_books": [],
                }

                for prefix, sim_key in (("pts", "sim_pts"), ("reb", "sim_reb"), ("pr", "sim_pr")):
                    if f"{prefix}_line" in player_props:
                        entry[f"{prefix}_line"] = player_props[f"{prefix}_line"]
                        entry[f"{prefix}_over_odds"] = player_props[f"{prefix}_over_odds"]
                        entry[f"{prefix}_under_odds"] = player_props[f"{prefix}_under_odds"]
                        entry[f"{prefix}_over_book"] = player_props.get(f"{prefix}_over_book")
                        entry[f"{prefix}_under_book"] = player_props.get(f"{prefix}_under_book")
                        entry[f"{prefix}_book_count"] = player_props.get(f"{prefix}_book_count", 0)
                        entry[f"{prefix}_ev"] = calc_ev_sim(
                            sim[sim_key], player_props[f"{prefix}_line"],
                            player_props[f"{prefix}_over_odds"],
                            player_props[f"{prefix}_under_odds"],
                        )

                        # Per-book EV: judge each book at ITS OWN line + odds
                        # against the same sim distribution, so the report can
                        # show where the best EV spot is (often not the book
                        # with the best raw price, if it sits on a worse line).
                        all_books = player_props.get(f"{prefix}_all_books", [])
                        rows = []
                        for bk in all_books:
                            if bk["over_odds"] is None or bk["under_odds"] is None:
                                continue
                            bk_ev = calc_ev_sim(
                                sim[sim_key], bk["line"],
                                bk["over_odds"], bk["under_odds"],
                            )
                            rows.append({**bk, "ev": bk_ev})

                        # Order books best -> worst by the EV of the model's
                        # recommended side (the side you'd actually bet), so the
                        # report lists the best line first. Falls back to
                        # over-EV when there's no positive-EV rec. Ties keep the
                        # alphabetical pre-sort from fetch_odds_api_props.
                        rec = entry[f"{prefix}_ev"].get("rec", "")
                        side_key = "ev_under" if rec == "UNDER" else "ev_over"
                        rows.sort(key=lambda r: r["ev"].get(side_key, -999.0),
                                  reverse=True)
                        entry[f"{prefix}_all_books"] = rows

                all_projections.append(entry)

    all_projections.sort(key=lambda x: x.get("sim_median_pts", x["expected_pts"]), reverse=True)

    games_seen = {}
    for p in all_projections:
        gname = p["game"]
        if gname not in games_seen:
            games_seen[gname] = []
        games_seen[gname].append(p)

    for gname, players in games_seen.items():
        g_info = next((g for g in games if g["short_name"] == gname), None)
        sp = g_info.get("spread_detail", "N/A") if g_info else "N/A"
        ou = g_info.get("over_under", "N/A") if g_info else "N/A"

        gd_match = next((gd for gd in game_data if gd["game"]["short_name"] == gname), None)
        injured_names = []
        if gd_match:
            for pid, status in gd_match["injuries"].items():
                if status in ("out", "doubtful"):
                    for pp in gd_match["home_players"] + gd_match["away_players"]:
                        if pp["id"] == pid:
                            injured_names.append(f"{pp['name']} ({status.upper()})")
                            break

        print(f"\n{'='*140}")
        print(f"  {gname}  |  Spread: {sp}  |  O/U: {ou}")
        if injured_names:
            print(f"  Injuries: {', '.join(injured_names)}")
        print(f"{'='*140}")

        with_props = [p for p in players if p["pts_line"] is not None]
        without_props = [p for p in players if p["pts_line"] is None]
        with_props.sort(key=lambda x: x["expected_pts"], reverse=True)
        without_props.sort(key=lambda x: x["expected_pts"], reverse=True)

        if with_props:
            print(f"\n  {'Player':<22} {'Pos':>3} {'Team':>4} {'ExpM':>5} "
                  f"{'SimPt':>5} {'PtLn':>5} {'PtEdg':>5} {'O EV%':>6} {'U EV%':>6} {'Rec':>5} {'Bk':>4} "
                  f"{'SimRb':>5} {'RbLn':>5} {'RbEdg':>5} {'O EV%':>6} {'U EV%':>6} {'Rec':>5} {'Bk':>4} "
                  f"{'Notes'}")
            print(f"  {'-'*146}")

            def best_book(p, prefix):
                ev = p.get(f"{prefix}_ev", {})
                rec = ev.get("rec", "")
                if rec == "OVER":
                    return p.get(f"{prefix}_over_book") or ""
                if rec == "UNDER":
                    return p.get(f"{prefix}_under_book") or ""
                return ""

            for p in with_props:
                notes = []
                if p["b2b"]:
                    notes.append("B2B")
                if p["injury_min_boost"] > 0.5:
                    notes.append(f"+{p['injury_min_boost']:.0f}m")
                if p["blowout_adj"] < -0.5:
                    notes.append(f"{p['blowout_adj']:.0f}m")
                if p["usage_boost"] > 0.02:
                    notes.append(f"+{p['usage_boost']*100:.0f}%u")
                if p.get("cur_season_gp", 99) < THIN_SAMPLE_GP:
                    notes.append(f"thin:{p['cur_season_gp']}")

                sim_pts = p.get("sim_median_pts", p["expected_pts"])
                pts_ln = f"{p['pts_line']:.1f}" if p["pts_line"] is not None else "  -  "
                pts_ev = p.get("pts_ev", {})
                pts_edge = f"{pts_ev.get('edge', 0):+.1f}" if pts_ev else "  -  "
                pts_o_ev = f"{pts_ev.get('ev_over', 0):+.1f}" if pts_ev else "  -  "
                pts_u_ev = f"{pts_ev.get('ev_under', 0):+.1f}" if pts_ev else "  -  "
                pts_rec = pts_ev.get("rec", "") if pts_ev else ""
                pts_bk = best_book(p, "pts")

                sim_reb = p.get("sim_median_reb", p["expected_reb"])
                reb_ln = f"{p['reb_line']:.1f}" if p["reb_line"] is not None else "  -  "
                reb_ev = p.get("reb_ev", {})
                reb_edge = f"{reb_ev.get('edge', 0):+.1f}" if reb_ev else "  -  "
                reb_o_ev = f"{reb_ev.get('ev_over', 0):+.1f}" if reb_ev else "  -  "
                reb_u_ev = f"{reb_ev.get('ev_under', 0):+.1f}" if reb_ev else "  -  "
                reb_rec = reb_ev.get("rec", "") if reb_ev else ""
                reb_bk = best_book(p, "reb")

                note_str = ", ".join(notes)
                print(
                    f"  {p['player']:<22} {p['position']:>3} {p['team']:>4} {p['expected_min']:>5.1f} "
                    f"{sim_pts:>5} {pts_ln:>5} {pts_edge:>5} {pts_o_ev:>6} {pts_u_ev:>6} {pts_rec:>5} {pts_bk:>4} "
                    f"{sim_reb:>5} {reb_ln:>5} {reb_edge:>5} {reb_o_ev:>6} {reb_u_ev:>6} {reb_rec:>5} {reb_bk:>4} "
                    f"{note_str}"
                )

        if without_props:
            print(f"\n  Other players (no prop lines):")
            print(f"  {'Player':<22} {'Pos':>3} {'Team':>4} {'ExpMin':>6} {'SimPts':>6} {'SimReb':>6} {'Notes'}")
            print(f"  {'-'*60}")
            for p in without_props[:8]:
                notes = []
                if p["b2b"]:
                    notes.append("B2B")
                if p["injury_min_boost"] > 0.5:
                    notes.append(f"+{p['injury_min_boost']:.0f}m")
                note_str = ", ".join(notes)
                sim_pts = p.get("sim_median_pts", p["expected_pts"])
                sim_reb = p.get("sim_median_reb", p["expected_reb"])
                print(
                    f"  {p['player']:<22} {p['position']:>3} {p['team']:>4} "
                    f"{p['expected_min']:>6.1f} {sim_pts:>6} "
                    f"{sim_reb:>6} {note_str}"
                )

    # Top Bets summary
    top_bets = []
    for p in all_projections:
        for prefix, prop_label, sim_key, exp_key in (
            ("pts", "Points", "sim_median_pts", "expected_pts"),
            ("reb", "Rebounds", "sim_median_reb", "expected_reb"),
        ):
            ev = p.get(f"{prefix}_ev", {})
            if not ev.get("rec"):
                continue
            book = (
                p.get(f"{prefix}_over_book") if ev["rec"] == "OVER"
                else p.get(f"{prefix}_under_book")
            ) or ""
            best_ev = ev["ev_over"] if ev["rec"] == "OVER" else ev["ev_under"]
            top_bets.append({
                "player": p["player"], "team": p["team"], "game": p["game"],
                "prop": prop_label, "line": p[f"{prefix}_line"],
                "sim": p.get(sim_key, p[exp_key]),
                "edge": ev.get("edge", 0), "rec": ev["rec"],
                "ev_pct": best_ev, "book": book,
                "n_books": p.get(f"{prefix}_book_count", 0),
                "cur_season_gp": p.get("cur_season_gp", 99),
            })

    top_bets.sort(key=lambda x: x["ev_pct"], reverse=True)

    if top_bets:
        print(f"\n{'='*112}")
        print(f"  TOP BETS BY EV%")
        print(f"{'='*112}")
        print(f"  {'#':>2} {'Player':<22} {'Tm':>3} {'Game':>10} {'Prop':>8} "
              f"{'Line':>5} {'Sim':>5} {'Edge':>6} {'Rec':>5} {'EV%':>7} {'Book':>5} {'#Bk':>4}  {'Flag'}")
        print(f"  {'-'*120}")
        thin_any = False
        for i, b in enumerate(top_bets[:10], 1):
            flag = f"thin:{b['cur_season_gp']}" if b["cur_season_gp"] < THIN_SAMPLE_GP else ""
            thin_any = thin_any or bool(flag)
            print(
                f"  {i:>2} {b['player']:<22} {b['team']:>3} {b['game']:>10} {b['prop']:>8} "
                f"{b['line']:>5.1f} {b['sim']:>5} {b['edge']:>+6.1f} {b['rec']:>5} {b['ev_pct']:>+6.1f}% "
                f"{b['book']:>5} {b['n_books']:>4}  {flag}"
            )
        if thin_any:
            print(f"\n  Flag thin:N = projection leans on only N current-season games "
                  f"(< {THIN_SAMPLE_GP}); small sample padded with last year - treat with caution.")

    if generate_pdf:
        pdf_path = generate_pdf_report(all_projections, games, game_data, date_str)
        print(f"\n  PDF saved to: {pdf_path}")

    print(f"\n{'='*70}")
    print(f"  Done. {len(all_projections)} players projected.")
    print(f"{'='*70}\n")

    # _update_tracker() DISABLED for props-2 (pre-Jul-1 model): must not write
    # the main props tracker CSV, which grades the CURRENT model.

    return all_projections


def _update_tracker():
    """Grade any now-settled prior days into the forward performance tracker.

    Runs after the day's projections so the win/loss tracker self-updates
    (e.g. yesterday's slate, now final) every time projections are generated.
    Idempotent and fully guarded — never interrupts the projections run.
    """
    try:
        import wnba_props_track as T
    except Exception:
        return
    try:
        have = T.tracked_dates()
        targets = [d for d in T.gradeable_dates() if d not in have]
        if not targets:
            print("  Tracker: up to date (no newly-settled slates).")
            return
        added_days = added_rows = 0
        for ymd in targets:
            rows = T.grade_date(ymd)
            if rows:
                T.append_rows(rows)
                added_days += 1
                added_rows += len(rows)
        if added_days:
            print(f"  Tracker: graded {added_days} newly-settled day(s), "
                  f"+{added_rows} picks -> {T.TRACKER.name}")
        else:
            print("  Tracker: up to date (prior slates not final yet).")
    except Exception as e:
        print(f"  Tracker update skipped: {e}")


# ── 11. PDF report generation ─────────────────────────────────────────────

def generate_pdf_report(projections, games, game_data, date_str):
    """Generate a PDF report of all projections."""
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
        KeepTogether,
    )
    from reportlab.lib import colors

    dt_label = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    dt_file = datetime.strptime(date_str, "%Y%m%d").strftime("%b%d_%Y")
    pdf_path = OUTPUT_DIR / f"WNBA_Props_Projections_2_{dt_file}.pdf"

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=landscape(letter),
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title2", parent=styles["Title"], fontSize=16, leading=20, spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "Sub", parent=styles["Normal"], fontSize=9, alignment=TA_CENTER,
        textColor=HexColor("#666666"), spaceAfter=8,
    )
    game_header_style = ParagraphStyle(
        "GameH", parent=styles["Heading2"], fontSize=11, leading=14,
        spaceBefore=10, spaceAfter=4, textColor=HexColor("#7d1f5e"),
    )

    elements = []
    elements.append(Paragraph(f"WNBA Player Props Projections 2 (pre-Jul-1 model) — {dt_label}", title_style))
    elements.append(Paragraph(
        f"Generated {datetime.now(ET).strftime('%Y-%m-%d %I:%M %p ET')} | "
        f"Monte Carlo ({NUM_SIMS:,} sims) | "
        f"Lg Avg DRtg: {LEAGUE_AVG_DRTG:.1f} | Lg Avg Pace: {LEAGUE_AVG_PACE:.1f}",
        subtitle_style
    ))

    # Summary page: Top props by EV%
    summary_header_style = ParagraphStyle(
        "SumH", parent=styles["Heading2"], fontSize=13, leading=16,
        spaceBefore=6, spaceAfter=6, textColor=HexColor("#7d1f5e"),
    )
    elements.append(Paragraph("Top Props by EV%", summary_header_style))

    top_props = []
    for p in projections:
        for prefix, prop_label, sim_key, exp_key in (
            ("pts", "Points", "sim_median_pts", "expected_pts"),
            ("reb", "Rebounds", "sim_median_reb", "expected_reb"),
        ):
            ev = p.get(f"{prefix}_ev", {})
            if not ev.get("rec"):
                continue
            book = (
                p.get(f"{prefix}_over_book") if ev["rec"] == "OVER"
                else p.get(f"{prefix}_under_book")
            ) or ""
            best_ev = ev["ev_over"] if ev["rec"] == "OVER" else ev["ev_under"]
            top_props.append({
                "player": p["player"], "pos": p["position"], "team": p["team"],
                "game": p["game"], "prop": prop_label, "line": p[f"{prefix}_line"],
                "sim": p.get(sim_key, p[exp_key]),
                "edge": ev.get("edge", 0), "rec": ev["rec"],
                "ev_pct": best_ev, "ev_over": ev.get("ev_over", 0),
                "ev_under": ev.get("ev_under", 0),
                "book": book, "n_books": p.get(f"{prefix}_book_count", 0),
                "cur_season_gp": p.get("cur_season_gp", 99),
            })

    top_props.sort(key=lambda x: x["ev_pct"], reverse=True)

    sum_data = [["#", "Player", "Tm", "Game", "Prop", "Line", "Sim", "Edge", "Rec", "EV%", "Book", "#Bk"]]
    for idx, tp in enumerate(top_props[:50], 1):
        pname = tp["player"]
        if tp.get("cur_season_gp", 99) < THIN_SAMPLE_GP:
            pname += f" *{tp['cur_season_gp']}"
        sum_data.append([
            str(idx),
            pname,
            tp["team"],
            tp["game"],
            tp["prop"],
            f"{tp['line']:.1f}" if tp["line"] is not None else "-",
            f"{tp['sim']}",
            f"{tp['edge']:+.1f}",
            tp["rec"],
            f"{tp['ev_pct']:+.1f}%",
            tp["book"],
            str(tp["n_books"]) if tp["n_books"] else "",
        ])

    if len(sum_data) > 1:
        sum_col_w = [0.28*inch, 1.3*inch, 0.32*inch, 0.95*inch, 0.6*inch,
                     0.42*inch, 0.38*inch, 0.42*inch, 0.48*inch, 0.52*inch,
                     0.4*inch, 0.32*inch]
        sum_t = Table(sum_data, colWidths=sum_col_w, repeatRows=1)
        sum_style = [
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("LEADING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#7d1f5e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HexColor("#f5e9f0")]),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (2, 0), (-1, -1), "CENTER"),
            ("ALIGN", (1, 0), (1, -1), "LEFT"),
        ]
        green_sum = HexColor("#c8e6c9")
        green_lt = HexColor("#e8f5e9")
        for i, row in enumerate(sum_data[1:], 1):
            try:
                ev_val = float(row[9].replace("%", "").replace("+", ""))
                if ev_val >= 20.0:
                    sum_style.append(("BACKGROUND", (9, i), (9, i), green_sum))
                elif ev_val >= 5.0:
                    sum_style.append(("BACKGROUND", (9, i), (9, i), green_sum))
                elif ev_val > 0:
                    sum_style.append(("BACKGROUND", (9, i), (9, i), green_lt))
            except (ValueError, TypeError):
                pass
            if row[8] in ("OVER", "UNDER"):
                sum_style.append(("FONTNAME", (8, i), (8, i), "Helvetica-Bold"))
                sum_style.append(("BACKGROUND", (8, i), (8, i), green_sum))

        sum_t.setStyle(TableStyle(sum_style))
        elements.append(sum_t)
        elements.append(Paragraph(
            f"* N after a player = projection leans on only N current-season games "
            f"(&lt; {THIN_SAMPLE_GP}); small sample padded with last year — treat the "
            f"edge with caution.", subtitle_style))
    else:
        elements.append(Paragraph("No props with positive EV found.", subtitle_style))

    # Per-game detail pages
    games_map = {}
    for p in projections:
        gname = p["game"]
        if gname not in games_map:
            games_map[gname] = []
        games_map[gname].append(p)

    for game_idx, (gname, players) in enumerate(games_map.items()):
        elements.append(PageBreak())

        g_info = next((g for g in games if g["short_name"] == gname), None)
        sp = g_info.get("spread_detail", "N/A") if g_info else "N/A"
        ou = g_info.get("over_under", "N/A") if g_info else "N/A"

        gd_match = next((gd for gd in game_data if gd["game"]["short_name"] == gname), None)
        injured = []
        if gd_match:
            for pid, status in gd_match["injuries"].items():
                if status in ("out", "doubtful"):
                    for pp in gd_match["home_players"] + gd_match["away_players"]:
                        if pp["id"] == pid:
                            injured.append(f"{pp['name']} ({status.upper()})")
                            break

        header_text = f"{gname}  —  Spread: {sp}  |  O/U: {ou}"
        if injured:
            header_text += f"<br/><font size='8'>OUT: {', '.join(injured)}</font>"
        elements.append(Paragraph(header_text, game_header_style))

        table_data = [
            ["Player", "Pos", "Tm", "ExpM",
             "SimPt", "PtLn", "Edge", "OvEV%", "UnEV%", "Rec", "Bk",
             "SimRb", "RbLn", "Edge", "OvEV%", "UnEV%", "Rec", "Bk",
             "Notes"],
        ]

        all_sorted = sorted(players, key=lambda x: (
            0 if x["pts_line"] is not None else 1,
            -x.get("sim_median_pts", x["expected_pts"])
        ))
        for p in all_sorted:
            sim_pts = p.get("sim_median_pts", p["expected_pts"])
            if p["pts_line"] is None and sim_pts < 4:
                continue

            notes = []
            if p["b2b"]:
                notes.append("B2B")
            if p["injury_min_boost"] > 0.5:
                notes.append(f"+{p['injury_min_boost']:.0f}m")
            if p["blowout_adj"] < -0.5:
                notes.append(f"{p['blowout_adj']:.0f}m")
            if p["usage_boost"] > 0.02:
                notes.append(f"+{p['usage_boost']*100:.0f}%u")
            if p.get("cur_season_gp", 99) < THIN_SAMPLE_GP:
                notes.append(f"thin:{p['cur_season_gp']}")

            pts_ev = p.get("pts_ev", {})
            reb_ev = p.get("reb_ev", {})
            sim_reb = p.get("sim_median_reb", p["expected_reb"])

            def fmt_line(val):
                return f"{val:.1f}" if val is not None else "-"
            def fmt_ev(val):
                return f"{val:+.1f}" if val else "-"
            def fmt_rec(val):
                return val if val else ""

            def book_for(player_dict, prefix):
                ev = player_dict.get(f"{prefix}_ev", {})
                rec = ev.get("rec", "")
                if rec == "OVER":
                    return player_dict.get(f"{prefix}_over_book") or ""
                if rec == "UNDER":
                    return player_dict.get(f"{prefix}_under_book") or ""
                return ""

            table_data.append([
                p["player"],
                p["position"],
                p["team"],
                f"{p['expected_min']:.1f}",
                f"{sim_pts}",
                fmt_line(p["pts_line"]),
                fmt_ev(pts_ev.get("edge")) if pts_ev else "-",
                fmt_ev(pts_ev.get("ev_over")) if pts_ev else "-",
                fmt_ev(pts_ev.get("ev_under")) if pts_ev else "-",
                fmt_rec(pts_ev.get("rec")) if pts_ev else "",
                book_for(p, "pts"),
                f"{sim_reb}",
                fmt_line(p["reb_line"]),
                fmt_ev(reb_ev.get("edge")) if reb_ev else "-",
                fmt_ev(reb_ev.get("ev_over")) if reb_ev else "-",
                fmt_ev(reb_ev.get("ev_under")) if reb_ev else "-",
                fmt_rec(reb_ev.get("rec")) if reb_ev else "",
                book_for(p, "reb"),
                ", ".join(notes),
            ])

        col_w = [1.30*inch, 0.22*inch, 0.25*inch, 0.38*inch,
                 0.38*inch, 0.35*inch, 0.38*inch, 0.42*inch, 0.42*inch, 0.42*inch, 0.30*inch,
                 0.38*inch, 0.35*inch, 0.38*inch, 0.42*inch, 0.42*inch, 0.42*inch, 0.30*inch,
                 0.70*inch]

        t = Table(table_data, colWidths=col_w, repeatRows=1)
        style_cmds = [
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("LEADING", (0, 0), (-1, -1), 9.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#7d1f5e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HexColor("#f5e9f0")]),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("LINEAFTER", (10, 0), (10, -1), 1.0, HexColor("#7d1f5e")),
            ("LINEAFTER", (3, 0), (3, -1), 1.0, HexColor("#7d1f5e")),
        ]

        green_strong = HexColor("#c8e6c9")
        green_light = HexColor("#e8f5e9")

        for i, row in enumerate(table_data[1:], 1):
            for ev_col in (7, 8):
                try:
                    val = float(row[ev_col])
                    if val >= 5.0:
                        style_cmds.append(("BACKGROUND", (ev_col, i), (ev_col, i), green_strong))
                    elif val > 0:
                        style_cmds.append(("BACKGROUND", (ev_col, i), (ev_col, i), green_light))
                except (ValueError, TypeError):
                    pass
            for ev_col in (14, 15):
                try:
                    val = float(row[ev_col])
                    if val >= 5.0:
                        style_cmds.append(("BACKGROUND", (ev_col, i), (ev_col, i), green_strong))
                    elif val > 0:
                        style_cmds.append(("BACKGROUND", (ev_col, i), (ev_col, i), green_light))
                except (ValueError, TypeError):
                    pass
            for rec_col in (9, 16):
                if row[rec_col] in ("OVER", "UNDER"):
                    style_cmds.append(("FONTNAME", (rec_col, i), (rec_col, i), "Helvetica-Bold"))
                    style_cmds.append(("BACKGROUND", (rec_col, i), (rec_col, i), green_strong))

        t.setStyle(TableStyle(style_cmds))
        elements.append(t)
        elements.append(Spacer(1, 6))

    # ── All-books line shopping pages ──
    # One block per prop (player x market) that has >= 2 books, listing every
    # book's line + over/under odds + per-book EV. The single best-EV cell
    # across all books for that prop is highlighted, so the line-shopping
    # spot is obvious at a glance.
    book_label_style = ParagraphStyle(
        "BookLbl", parent=styles["Normal"], fontSize=8.5, leading=11,
        spaceBefore=6, spaceAfter=1, textColor=HexColor("#7d1f5e"),
        fontName="Helvetica-Bold",
    )
    book_note_style = ParagraphStyle(
        "BookNote", parent=styles["Normal"], fontSize=7.5, leading=9,
        textColor=HexColor("#666666"),
    )

    book_blocks = []
    for p in projections:
        for prefix, prop_label, sim_key in (
            ("pts", "Points", "sim_median_pts"),
            ("reb", "Rebounds", "sim_median_reb"),
            ("pr", "Pts+Reb", "sim_median_pr"),
        ):
            rows = p.get(f"{prefix}_all_books", [])
            if len(rows) < 2:
                continue  # nothing to shop with a single book

            sim_val = p.get(sim_key)
            header = (
                f"{p['player']} ({p['team']}) — {prop_label}"
                f"  ·  Sim {sim_val}  ·  {p['game']}"
            )

            # Find the single best EV cell (side+book) across this prop's books.
            best = None  # (ev_value, row_index, side)
            for ri, r in enumerate(rows):
                ev = r["ev"]
                for side_key, side in (("ev_over", "over"), ("ev_under", "under")):
                    val = ev.get(side_key)
                    if val is None:
                        continue
                    if best is None or val > best[0]:
                        best = (val, ri, side)

            tbl = [["Book", "Line", "Over", "EV", "Under", "EV"]]
            for r in rows:
                ev = r["ev"]
                tbl.append([
                    r["book"],
                    f"{r['line']:.1f}",
                    str(r["over_odds"]) if r["over_odds"] is not None else "-",
                    f"{ev.get('ev_over', 0):+.1f}" if r["over_odds"] is not None else "-",
                    str(r["under_odds"]) if r["under_odds"] is not None else "-",
                    f"{ev.get('ev_under', 0):+.1f}" if r["under_odds"] is not None else "-",
                ])

            bt = Table(tbl, colWidths=[0.55*inch, 0.5*inch, 0.55*inch,
                                       0.55*inch, 0.55*inch, 0.55*inch],
                       repeatRows=1)
            bstyle = [
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("LEADING", (0, 0), (-1, -1), 9.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BACKGROUND", (0, 0), (-1, 0), HexColor("#7d1f5e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HexColor("#f5e9f0")]),
            ]
            # Highlight the best-EV cell (its EV column) + its book name.
            if best is not None:
                _, ri, side = best
                ev_col = 3 if side == "over" else 5
                trow = ri + 1  # +1 for header
                bstyle.append(("BACKGROUND", (ev_col, trow), (ev_col, trow), HexColor("#c8e6c9")))
                bstyle.append(("FONTNAME", (ev_col, trow), (ev_col, trow), "Helvetica-Bold"))
                bstyle.append(("FONTNAME", (0, trow), (0, trow), "Helvetica-Bold"))
            bt.setStyle(TableStyle(bstyle))

            block = [Paragraph(header, book_label_style)]
            if best is not None:
                best_row = rows[best[1]]
                block.append(Paragraph(
                    f"Best EV: {best_row['book']} {best[2].upper()} "
                    f"{best_row['line']:.1f} ({best[0]:+.1f}%)",
                    book_note_style,
                ))
            block.append(bt)
            book_blocks.append(KeepTogether(block))

    if book_blocks:
        elements.append(PageBreak())
        allbooks_header_style = ParagraphStyle(
            "AllBk", parent=styles["Heading2"], fontSize=13, leading=16,
            spaceBefore=4, spaceAfter=6, textColor=HexColor("#7d1f5e"),
        )
        elements.append(Paragraph("All-Book Lines (Line Shopping)", allbooks_header_style))
        elements.append(Paragraph(
            "Every book's line, over/under odds, and per-book EV, ordered "
            "best-to-worst by the recommended side's EV (top row = best line). "
            "The highlighted cell is the best-EV spot across books for that prop.",
            subtitle_style,
        ))
        for blk in book_blocks:
            elements.append(blk)

    doc.build(elements)
    return pdf_path


# ── CLI entry point ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WNBA Player Props Projections")
    parser.add_argument(
        "--date", type=str, default=None,
        help="Date to project (YYYY-MM-DD). Defaults to today ET."
    )
    parser.add_argument(
        "--pdf", action="store_true",
        help="Also generate a PDF report."
    )
    args = parser.parse_args()

    if args.date:
        dt = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        dt = datetime.now(ET)

    date_str = dt.strftime("%Y%m%d")
    run_projections(date_str, generate_pdf=args.pdf)


if __name__ == "__main__":
    main()
