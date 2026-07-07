#!/usr/bin/env python3
"""
BIG3 Live Projections Dashboard
================================
A live, rules-aware projection model for Ice Cube's BIG3 (3-on-3) league.

Why BIG3 needs its own model
----------------------------
Unlike the NBA/CBB/WNBA dashboards, a BIG3 game is NOT decided by a clock.
It is a RACE TO A TARGET SCORE:
    * First team to 50 points wins, but must lead by at least 2 (no overtime).
    * Halftime is triggered the instant a team reaches 25 (score is cumulative;
      it does NOT reset at the half).
    * Shots: 2 pts inside the arc, 3 behind it, 4 from the three 30-ft circles.
      Free throws are worth the value of the shot fouled on (1 pt in the bonus).

So a naive time-based extrapolation is meaningless here. Instead we run a
Monte-Carlo "race" simulation that bakes in those scoring limits directly:
possessions alternate, each yields a draw from a BIG3 scoring distribution,
and a game ends exactly when the 50/win-by-2 condition is met. The half (25)
crossing is tracked inside the same simulation to project the first-half line.

Data source
-----------
big3.com's own scoreboard is powered by a public JSON feed (a Genius/AirPLAi
feed) which carries live scores, per-half linescores, status and records:
    https://big3.com/wp-json/big3/v1/schedule/{year}
We poll that and project every live/upcoming game.

Usage
-----
    py -3 big3_live_projections.py                 # dashboard at http://localhost:5006
    py -3 big3_live_projections.py --port 8080
    py -3 big3_live_projections.py --date 2026-06-20   # focus a specific game day
    py -3 big3_live_projections.py --once          # print today's slate to console, no server
    py -3 big3_live_projections.py --selftest      # validate/inspect the projection engine

Dependencies: flask, requests, tzdata (zoneinfo)   -- all already installed.
"""

import argparse
import random
import sys
import threading
import time
from datetime import datetime, date, timedelta
from statistics import median

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    ET = None

import requests

# ----------------------------------------------------------------------------
# League rules / model constants
# ----------------------------------------------------------------------------
TARGET_SCORE = 50          # first to 50 ...
WIN_BY = 2                 # ... and must lead by 2 (no overtime)
HALF_SCORE = 25            # halftime triggers when a team reaches 25 (cumulative)
WINDOW_HOURS = 48          # "upcoming" = any game tipping within this rolling window

# Average points per possession in the BIG3 (tuned so that an even matchup
# yields a realistic ~51-42 final / ~93 total under the game-flow model).
LEAGUE_PPP = 1.16

# Game-flow model (v2, Jul 7 2026) — replaces the old flat MARGIN_CAL=0.75
# compression, which squeezed every matchup to the same ~51-43 / 94 line.
# Calibrated on the 12 real 2025-26 finals (margins 2..17 avg 8.3, totals
# 83..100 avg 92.6, winner ~50.7): competitive games finish tight with HIGH
# totals, mismatches break open with LOW totals. Three mechanisms:
#   1. Competitive rubber band on the gap BEYOND the expected trajectory
#      (RUBBER_BAND / RUBBER_DEPTH): luck-driven runs get pulled back, so even
#      matchups finish close — but a true strength gap is expected and is NOT
#      subsidized away.
#   2. Breaking point on the raw scoreboard (BREAK_PTS / BLOWOUT_FLOW): once a
#      team is down big it capitulates and the rout inflates — the fat tail
#      real blowouts show (e.g. LA's 50-33).
#   3. Per-game form/uncertainty wobble (FORM_SD): each sim draws a game-day
#      strength for both teams. Keeps win% honest given few games of ratings
#      signal (a top-vs-bottom matchup tops out ~85%, not ~99%).
# Result: margins/totals now RESPOND to the matchup — even ~51-42 / t93,
# top-vs-bottom ~51-38 / t89 — while the winner stays at the race-ending ~51.
RUBBER_BAND = 0.45      # competitive-band strength (0 = pure i.i.d. race)
RUBBER_DEPTH = 8.0      # pts beyond expectation at which the band saturates
BREAK_PTS = 14          # scoreboard deficit where a team "breaks"
BLOWOUT_FLOW = 0.12     # scoring-rate swing once broken (leader +, trailer -)
FORM_SD = 0.12          # sd of the per-game team strength multiplier

# Distribution of points scored *when a possession results in a score*.
# Reflects BIG3's 2/3/4-pt shots plus the occasional single bonus free throw.
#   value : probability
VALUE_DIST = ((2, 0.54), (3, 0.30), (4, 0.11), (1, 0.05))
MEAN_VALUE = sum(v * p for v, p in VALUE_DIST)      # ~= 2.46

# In-game rate blending: optionally nudge each team's *future* scoring rate
# toward what they've done so far. OFF by default (0.0): the current score gap
# already captures performance, and ~30 points is far too small a sample to
# re-estimate team strength -- enabling it overreacts to early runs. The pure
# rules-based race (lead + matchup ratings) is well calibrated on its own.
# Raise the cap (e.g. 0.15) only if you want a mild hot-hand effect.
INGAME_BLEND_CAP = 0.0
INGAME_BLEND_FULL_PTS = 90.0    # combined points at which blending would hit its cap

# Home-court edge. BIG3 is a touring, largely neutral-site league (one arena
# per week), so this is intentionally tiny. Expressed as a PPP multiplier.
HCA_PPP_MULT = 1.00             # 1.00 == no home edge

DEFAULT_SIMS = 8000
FEED_URL = "https://big3.com/wp-json/big3/v1/schedule/{year}"
SEASON_YEAR = 2026
PORT = 5006
FEED_CACHE_SECS = 18

# Precompute cumulative thresholds for fast value sampling
_VAL_CUM = []
_acc = 0.0
for _v, _p in VALUE_DIST:
    _acc += _p
    _VAL_CUM.append((_acc, _v))


# ----------------------------------------------------------------------------
# Projection engine -- the race-to-target Monte Carlo
# ----------------------------------------------------------------------------
def _score_prob(ppp):
    """Probability a single possession scores, so that E[points] == ppp."""
    return max(0.05, min(0.95, ppp / MEAN_VALUE))


def _draw(p_score, rnd):
    """Points from one possession: 0 (empty) or a 1/2/3/4 made-shot value."""
    if rnd() >= p_score:
        return 0
    r = rnd()
    for cum, v in _VAL_CUM:
        if r <= cum:
            return v
    return 2


def _adjusted_ppp(off_rtg, def_rtg, home_mult=1.0):
    """Matchup-adjusted points per possession (1.0 ratings == league average)."""
    return LEAGUE_PPP * off_rtg * def_rtg * home_mult


def simulate(score_a, score_b, ppp_a, ppp_b, sims=DEFAULT_SIMS, seed=None):
    """
    Simulate the remainder of a BIG3 game as a race to 50 (win by 2), with
    game-flow dynamics (competitive rubber band, breaking point, per-game
    form wobble — see the constants block above).

    a == home, b == away (by our convention). Scores are cumulative and may
    already be non-zero for a live game. Returns a dict of projections.
    """
    rng = random.Random(seed)
    rnd = rng.random
    gauss = rng.gauss

    already_half = (score_a >= HALF_SCORE or score_b >= HALF_SCORE)

    a_wins = 0
    sum_win = 0.0             # winner's final score (always reaches >= 50)
    sum_lose = 0.0            # loser's final score
    h1_n = 0
    h1_home_first = 0         # count of sims where home reached 25 first
    h1_sum_win = 0.0          # first-half winner (first-to-25) score
    h1_sum_lose = 0.0

    for _ in range(sims):
        # Game-day form / ratings-uncertainty wobble (one draw per team per game)
        fa = ppp_a * max(0.6, 1.0 + gauss(0.0, FORM_SD))
        fb = ppp_b * max(0.6, 1.0 + gauss(0.0, FORM_SD))
        base_a = fa / MEAN_VALUE
        base_b = fb / MEAN_VALUE
        a, b = score_a, score_b
        ea, eb = float(a), float(b)     # expected-points trajectories
        rec_half = already_half
        ha = hb = None
        turn_a = rnd() < 0.5   # unknown who has the ball live -> randomize
        guard = 0
        while not ((a >= TARGET_SCORE or b >= TARGET_SCORE) and abs(a - b) >= WIN_BY):
            if turn_a:
                d = a - b
                if d >= BREAK_PTS:          # rout: broken opponent
                    f = BLOWOUT_FLOW
                elif d <= -BREAK_PTS:       # broken: scoring dries up
                    f = -BLOWOUT_FLOW
                else:                       # competitive: revert luck, not talent
                    excess = d - (ea - eb)
                    f = -RUBBER_BAND * max(-1.0, min(1.0, excess / RUBBER_DEPTH))
                a += _draw(max(0.05, min(0.95, base_a * (1.0 + f))), rnd)
                ea += fa
            else:
                d = b - a
                if d >= BREAK_PTS:
                    f = BLOWOUT_FLOW
                elif d <= -BREAK_PTS:
                    f = -BLOWOUT_FLOW
                else:
                    excess = d - (eb - ea)
                    f = -RUBBER_BAND * max(-1.0, min(1.0, excess / RUBBER_DEPTH))
                b += _draw(max(0.05, min(0.95, base_b * (1.0 + f))), rnd)
                eb += fb
            turn_a = not turn_a
            if not rec_half and (a >= HALF_SCORE or b >= HALF_SCORE):
                ha, hb, rec_half = a, b, True
            guard += 1
            if guard > 1200:   # numerical safety; should never trigger
                break

        if a > b:
            a_wins += 1
            sum_win += a
            sum_lose += b
        else:
            sum_win += b
            sum_lose += a
        if ha is not None:
            h1_n += 1
            if ha >= HALF_SCORE:        # home scored the basket that hit 25
                h1_home_first += 1
                h1_sum_win += ha
                h1_sum_lose += hb
            else:                       # away reached 25 first
                h1_sum_win += hb
                h1_sum_lose += ha

    p_a = 100.0 * a_wins / sims
    mean_win = sum_win / sims          # winner ~50-51 (race-to-50 floor)
    mean_lose = sum_lose / sims
    # BIG3 ends only when a team reaches 50 (by >=2), so the projected winner is
    # shown at the race-ending ~51. The game-flow dynamics (rubber band /
    # breaking point / form wobble) are calibrated on real finals, so the raw
    # sim gap is used directly — no post-hoc compression. Spread & total derive
    # FROM this final so the three always agree; the spread is the projected
    # WINNING margin (now matchup-responsive: ~9 even, ~13+ top-vs-bottom) and
    # win% is the separate likelihood (a near-even game still shows a ~9-pt
    # winning margin at ~50% — the win% bar carries the real closeness).
    win_score, lose_score = mean_win, mean_lose
    if p_a >= 50.0:
        proj_home, proj_away = int(round(win_score)), int(round(lose_score))
    else:
        proj_home, proj_away = int(round(lose_score)), int(round(win_score))
    out = {
        "p_home": round(p_a, 1),
        "p_away": round(100.0 - p_a, 1),
        "proj_home": proj_home,
        "proj_away": proj_away,
        "spread": float(proj_home - proj_away),        # home - away == score gap
        "total": proj_home + proj_away,
        "needed_home": max(0, TARGET_SCORE - score_a),
        "needed_away": max(0, TARGET_SCORE - score_b),
        "half_actual": already_half,
    }
    if h1_n:                                            # projected first half
        h1w = h1_sum_win / h1_n                          # team first to 25 (~25)
        h1l = h1_sum_lose / h1_n
        p_h1_home = 100.0 * h1_home_first / h1_n
        if p_h1_home >= 50.0:
            out["h1_home"], out["h1_away"] = int(round(h1w)), int(round(h1l))
        else:
            out["h1_home"], out["h1_away"] = int(round(h1l)), int(round(h1w))
        out["h1_total"] = out["h1_home"] + out["h1_away"]
        out["p_h1_home"] = round(p_h1_home, 1)
    return out


_PROJ_CACHE = {}        # (home,away,score_h,score_a,status,sims) -> proj dict


def project_game(game, ratings, sims=DEFAULT_SIMS):
    """Attach a projection to a normalized game dict (in place)."""
    home, away = game["home"], game["away"]
    hr = ratings.get(home["key"], {"off": 1.0, "def": 1.0})
    ar = ratings.get(away["key"], {"off": 1.0, "def": 1.0})

    # pre-game matchup-adjusted scoring rates
    ppp_home = _adjusted_ppp(hr["off"], ar["def"], HCA_PPP_MULT)
    ppp_away = _adjusted_ppp(ar["off"], hr["def"], 1.0)

    sh, sa = home["score"], away["score"]

    # In-game blending: shift future scoring rate toward observed performance.
    if game["status"] == "live" and (sh + sa) >= 6 and INGAME_BLEND_CAP > 0:
        w = min(INGAME_BLEND_CAP, (sh + sa) / INGAME_BLEND_FULL_PTS)
        base_sum = ppp_home + ppp_away
        obs_share_home = sh / (sh + sa)
        pre_share_home = ppp_home / base_sum
        blended = (1 - w) * pre_share_home + w * obs_share_home
        ppp_home = base_sum * blended
        ppp_away = base_sum * (1 - blended)

    if game["status"] == "final":
        game["proj"] = None
        return game

    # Cache by game state so repeated 30s polls don't re-run the Monte Carlo
    # when nothing has changed. The matchup ppps are part of the key so a
    # ratings update (new finals landing) invalidates stale projections.
    ckey = (home["key"], away["key"], sh, sa, game["status"], sims,
            round(ppp_home, 4), round(ppp_away, 4))
    cached = _PROJ_CACHE.get(ckey)
    if cached is not None:
        game["proj"] = cached
        return game

    seed = hash((home["key"], away["key"], sh, sa, game["status"])) & 0xFFFFFFFF
    proj = simulate(sh, sa, ppp_home, ppp_away, sims=sims, seed=seed)

    # If we're already past the half, replace the projected 1H with the real one.
    if proj["half_actual"]:
        h_home = _linescore_half(home)
        h_away = _linescore_half(away)
        if h_home is not None and h_away is not None:
            proj["h1_home"], proj["h1_away"] = h_home, h_away
            proj["h1_total"] = h_home + h_away
            proj["h1_is_real"] = True

    if len(_PROJ_CACHE) > 4000:        # keep the cache from growing unbounded
        _PROJ_CACHE.clear()
    _PROJ_CACHE[ckey] = proj
    game["proj"] = proj
    return game


# ----------------------------------------------------------------------------
# Feed fetching + normalization
# ----------------------------------------------------------------------------
_feed_lock = threading.Lock()
_feed_cache = {"ts": 0.0, "year": None, "raw": None}


def fetch_feed(year=SEASON_YEAR, force=False):
    """Fetch (and briefly cache) the BIG3 schedule/scoreboard JSON feed."""
    now = time.time()
    with _feed_lock:
        fresh = (_feed_cache["raw"] is not None
                 and _feed_cache["year"] == year
                 and (now - _feed_cache["ts"]) < FEED_CACHE_SECS)
        if fresh and not force:
            return _feed_cache["raw"]
    try:
        r = requests.get(
            FEED_URL.format(year=year),
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (BIG3-live-projections)"},
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"[feed] fetch error: {e}", file=sys.stderr)
        with _feed_lock:
            return _feed_cache["raw"]      # serve stale on error if we have it
    with _feed_lock:
        _feed_cache.update(ts=now, year=year, raw=raw)
    return raw


def _parse_dt(start_date):
    """startDate is a list of {dateType: Local|UTC, full: ISO}. Return ET dt."""
    utc_full = local_full = None
    for d in start_date or []:
        if d.get("dateType") == "UTC":
            utc_full = d.get("full")
        elif d.get("dateType") == "Local":
            local_full = d.get("full")
    try:
        if utc_full and ET is not None:
            from datetime import timezone
            dt = datetime.fromisoformat(utc_full).replace(tzinfo=timezone.utc)
            return dt.astimezone(ET)
        if local_full:
            return datetime.fromisoformat(local_full)
    except Exception:
        pass
    return None


def _team_block(t):
    loc = (t.get("location") or "").strip()
    nick = (t.get("nickname") or "").strip()
    rec = t.get("record") or {}
    return {
        "key": str(t.get("teamId") or (loc + nick)),
        "loc": loc,
        "nick": nick,
        "name": (loc + " " + nick).strip(),
        "abbr": (t.get("abbreviation") or loc[:3]).upper(),
        "score": int(t.get("score") or 0),
        "linescores": t.get("linescores") or [],
        "wins": int(rec.get("wins") or 0),
        "losses": int(rec.get("losses") or 0),
    }


def _linescore_half(team_block):
    """Actual first-half points from the linescores (period 1), if present."""
    ls = team_block.get("linescores") or []
    if not ls:
        return None
    first = ls[0]
    if isinstance(first, dict):
        for k in ("score", "points", "value"):
            if k in first:
                try:
                    return int(first[k])
                except (TypeError, ValueError):
                    return None
        return None
    try:
        return int(first)
    except (TypeError, ValueError):
        return None


def _status_of(ev):
    es = ev.get("eventStatus") or {}
    sid = es.get("eventStatusId")
    name = (es.get("name") or "").lower()
    active = bool(es.get("isActive"))
    if "final" in name or sid == 3:
        return "final", es
    if active or sid == 2 or any(k in name for k in
                                 ("progress", "live", "half", "period", "quarter")):
        return "live", es
    return "pre", es


def _is_incomplete_final(game):
    """True for a game the feed calls 'Final' that never actually finished.

    A completed BIG3 game is a race to TARGET_SCORE, so the winner's score is
    always >= TARGET_SCORE (50). A "final" whose top score is below that was
    suspended/abandoned mid-game -- its score is not a real result. Single source
    of truth: used both to exclude such games from auto-ratings and to flag them
    in the UI instead of showing them as an official final.
    """
    if game.get("status") != "final":
        return False
    hs = game["home"].get("score") or 0
    as_ = game["away"].get("score") or 0
    if hs <= 0 and as_ <= 0:        # nothing recorded yet (0-0) -- not "suspended"
        return False
    return max(hs, as_) < TARGET_SCORE


def normalize_games(raw):
    """Walk the nested feed and return a flat list of normalized game dicts."""
    games = []
    if not isinstance(raw, dict):
        return games
    for result in raw.get("apiResults", []):
        league = result.get("league", {})
        season = league.get("season", {})
        for et in season.get("eventType", []) or []:
            et_name = et.get("name", "")
            for ev in et.get("events", []) or []:
                teams = ev.get("teams", []) or []
                home = away = None
                for t in teams:
                    side = ((t.get("teamLocationType") or {}).get("name") or "").lower()
                    if side == "home":
                        home = _team_block(t)
                    elif side == "away":
                        away = _team_block(t)
                if home is None or away is None:        # fallback by order
                    if len(teams) == 2:
                        away = away or _team_block(teams[0])
                        home = home or _team_block(teams[1])
                    else:
                        continue
                status, es = _status_of(ev)
                dt = _parse_dt(ev.get("startDate"))
                g = {
                    "id": ev.get("eventId"),
                    "status": status,
                    "period": es.get("period", 0),
                    "status_name": es.get("name", ""),
                    "event_type": et_name,
                    "tip_et": dt,
                    "date_et": dt.date().isoformat() if dt else None,
                    "tip_str": dt.strftime("%-I:%M %p ET") if dt and hasattr(dt, "strftime")
                               and _safe_strftime(dt) else (_fmt_time(dt) if dt else "TBD"),
                    "venue": _venue_str(ev.get("venue")),
                    "home": home,
                    "away": away,
                    "proj": None,
                }
                g["incomplete"] = _is_incomplete_final(g)
                games.append(g)
    return games


def _safe_strftime(dt):
    try:
        dt.strftime("%-I:%M %p")
        return True
    except ValueError:
        return False


def _fmt_time(dt):
    """Cross-platform 12-hour time (Windows lacks %-I)."""
    try:
        h = dt.hour % 12 or 12
        return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'} ET"
    except Exception:
        return "TBD"


def _venue_str(v):
    if not v:
        return ""
    parts = [v.get("name")]
    city = v.get("city")
    st = (v.get("state") or {}).get("abbreviation")
    loc = ", ".join(p for p in (city, st) if p)
    if loc:
        parts.append(loc)
    return " · ".join(p for p in parts if p)


# ----------------------------------------------------------------------------
# Ratings (offense/defense multipliers, 1.0 == league average)
# ----------------------------------------------------------------------------
import csv
import os


def _resolve_ratings_csv():
    """Locate big3_ratings_2026.csv.

    * BBALL_DATA_DIR env set (combined basketball-site / Render) -> that dir.
    * else a sibling data/ dir if present (repo checkout) -> data/.
    * else next to this script (standalone use from Documents).
    """
    env = os.environ.get("BBALL_DATA_DIR")
    if env:
        return os.path.join(env, "big3_ratings_2026.csv")
    here = os.path.dirname(os.path.abspath(__file__))
    data_sub = os.path.join(here, "data")
    base = data_sub if os.path.isdir(data_sub) else here
    return os.path.join(base, "big3_ratings_2026.csv")


RATINGS_CSV = _resolve_ratings_csv()


def load_ratings(games=None):
    """
    Load per-team offensive/defensive multipliers from big3_ratings_2026.csv.
    Missing file or teams default to 1.0 (league average -> pick'em).
    A template is auto-written (once) listing the current teams at 1.0.
    """
    ratings = {}
    if os.path.exists(RATINGS_CSV):
        try:
            with open(RATINGS_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    key = (row.get("team_id") or row.get("team") or "").strip()
                    if not key:
                        continue
                    ratings[key] = {
                        "off": _f(row.get("off_rating"), 1.0),
                        "def": _f(row.get("def_rating"), 1.0),
                    }
        except Exception as e:
            print(f"[ratings] read error: {e}", file=sys.stderr)
    elif games:
        _write_ratings_template(games)
    return ratings


def _f(x, default):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _write_ratings_template(games):
    seen = {}
    for g in games:
        for side in ("home", "away"):
            t = g[side]
            seen[t["key"]] = t["name"]
    try:
        with open(RATINGS_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["team_id", "team", "off_rating", "def_rating"])
            for key, name in sorted(seen.items(), key=lambda kv: kv[1]):
                w.writerow([key, name, "1.00", "1.00"])
        print(f"[ratings] wrote template {RATINGS_CSV} "
              f"({len(seen)} teams at league average). Edit to tune.",
              file=sys.stderr)
    except Exception as e:
        print(f"[ratings] could not write template: {e}", file=sys.stderr)


def _clamp_rating(x, lo=0.80, hi=1.25):
    return max(lo, min(hi, x))


def compute_ratings_from_games(games, shrink_k=3.0, iters=4):
    """Derive OPPONENT-ADJUSTED per-team off/def multipliers from COMPLETED games.

    A team's offense = its points scored vs the league per-game average,
    discounted by the opponent's defense; its defense = points allowed vs that
    average, discounted by the opponent's offense (def multiplies the opponent's
    offense in project_game, so >1 = leaky, <1 = stingy). Iterated a few times
    so an 0-3 team that only faced the top of the league isn't over-punished —
    with 8 teams and ~3 games each, schedule imbalance is real signal.
    Ratings are shrunk toward 1.0 by games played (factor = n/(n+K): 2 games ->
    40%, 3 -> 50%, 6 -> 67%) and the league means are renormalized to 1.0.
    Returns {team_key: {"off":.., "def":..}} for teams that have played.
    """
    rows, n, pts = [], {}, []
    for g in games:
        if g.get("status") != "final":
            continue
        h, a = g["home"], g["away"]
        hs, as_ = float(h.get("score") or 0), float(a.get("score") or 0)
        if hs <= 0 and as_ <= 0:        # no real result (0-0)
            continue
        if _is_incomplete_final(g):     # suspended/abandoned (winner < 50): its
            continue                    # low score isn't a real result, skip it
        rows.append((h["key"], a["key"], hs, as_))
        rows.append((a["key"], h["key"], as_, hs))
        n[h["key"]] = n.get(h["key"], 0) + 1
        n[a["key"]] = n.get(a["key"], 0) + 1
        pts += [hs, as_]
    if not pts:
        return {}
    league_avg = sum(pts) / len(pts)
    if league_avg <= 0:
        return {}
    r = {k: {"off": 1.0, "def": 1.0} for k in n}
    for _ in range(iters):
        acc_off = {k: 0.0 for k in n}
        acc_def = {k: 0.0 for k in n}
        for team, opp, sf, sag in rows:
            acc_off[team] += (sf / league_avg) / max(0.5, r[opp]["def"])
            acc_def[team] += (sag / league_avg) / max(0.5, r[opp]["off"])
        new = {}
        for k, gp in n.items():
            f = gp / (gp + shrink_k)
            new[k] = {
                "off": _clamp_rating(1.0 + f * (acc_off[k] / gp - 1.0)),
                "def": _clamp_rating(1.0 + f * (acc_def[k] / gp - 1.0)),
            }
        mo = sum(v["off"] for v in new.values()) / len(new)
        md = sum(v["def"] for v in new.values()) / len(new)
        for v in new.values():
            v["off"] /= mo
            v["def"] /= md
        r = new
    return {k: {"off": round(v["off"], 4), "def": round(v["def"], 4)}
            for k, v in r.items()}


def _persist_ratings(ratings, games):
    """Best-effort write of the current (auto) ratings to the CSV for visibility
    and persistence. Caller decides when something changed; this just writes."""
    teams = {}
    for g in games:
        for side in ("home", "away"):
            t = g[side]
            teams[t["key"]] = t["name"]
    if not teams:
        return
    try:
        with open(RATINGS_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["team_id", "team", "off_rating", "def_rating"])
            for key, name in sorted(teams.items(), key=lambda kv: kv[1]):
                rt = ratings.get(key, {"off": 1.0, "def": 1.0})
                w.writerow([key, name, f"{rt['off']:.4f}", f"{rt['def']:.4f}"])
    except Exception as e:
        print(f"[ratings] persist failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Slate selection + the payload the dashboard consumes
# ----------------------------------------------------------------------------
def _today_et(override=None):
    if override:
        return date.fromisoformat(override)
    if ET is not None:
        return datetime.now(ET).date()
    return datetime.now().date()


def build_payload(year=SEASON_YEAR, date_override=None, sims=DEFAULT_SIMS):
    raw = fetch_feed(year)
    games = normalize_games(raw)
    ratings = load_ratings(games)

    # Auto-update team strength from completed games (shrunk toward league avg
    # for small samples), so projections reflect results as the season unfolds.
    auto = compute_ratings_from_games(games)
    if auto:
        changed = any(ratings.get(k) != v for k, v in auto.items())
        ratings.update(auto)
        if changed:
            _persist_ratings(ratings, games)

    today = _today_et(date_override)
    live = [g for g in games if g["status"] == "live"]

    # Pick the focus day: explicit date, else today if it has games,
    # else the nearest upcoming day, else the most recent past day.
    if date_override:
        focus = today.isoformat()
    else:
        days = sorted({g["date_et"] for g in games if g["date_et"]})
        tstr = today.isoformat()
        if tstr in days or live:
            focus = tstr
        else:
            future = [d for d in days if d > tstr]
            past = [d for d in days if d < tstr]
            focus = future[0] if future else (past[-1] if past else tstr)

    focus_games = [g for g in games if g["date_et"] == focus]
    # de-dupe live games already in the focus list
    extra_live = [g for g in live if g["date_et"] != focus]

    # Upcoming = every pre-game tipping within the next 48h (across game days).
    # BIG3 plays ~weekly, so if nothing is within the window fall back to the
    # focus day's slate — the page should never show fewer games than before.
    if ET is not None:
        base_dt = (datetime.now(ET) if not date_override
                   else datetime(today.year, today.month, today.day, tzinfo=ET))
    else:
        base_dt = datetime.now()
    cutoff = base_dt + timedelta(hours=WINDOW_HOURS)
    window_pre = [g for g in games if g["status"] == "pre" and g["tip_et"]
                  and base_dt <= g["tip_et"] <= cutoff]
    if window_pre:
        upcoming = sorted(window_pre, key=lambda g: g["tip_et"])
    else:
        upcoming = sorted([g for g in focus_games if g["status"] == "pre"],
                          key=lambda g: g["tip_et"] or base_dt)

    # Day-prefix the tip label for upcoming games that aren't on the focus day.
    for g in upcoming:
        if g["tip_et"] and g["date_et"] != today.isoformat():
            g["tip_str"] = g["tip_et"].strftime("%a ") + g["tip_str"]

    live_focus = [g for g in focus_games if g["status"] == "live"] + extra_live
    final = [g for g in focus_games if g["status"] == "final"]

    for g in live_focus + upcoming:
        project_game(g, ratings, sims=sims)

    return {
        "focus_date": focus,
        "focus_pretty": _pretty_date(focus),
        "is_today": (focus == today.isoformat()),
        "live": [_slim(g) for g in live_focus],
        "upcoming": [_slim(g) for g in upcoming],
        "final": [_slim(g) for g in final],
        "season": year,
        "updated": _now_str(),
        "rules": {
            "target": TARGET_SCORE, "win_by": WIN_BY, "half": HALF_SCORE,
        },
    }


def _pretty_date(iso):
    try:
        d = date.fromisoformat(iso)
        return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")
    except Exception:
        return iso


def _now_str():
    if ET is not None:
        return datetime.now(ET).strftime("%b ") + str(datetime.now(ET).day) + \
               datetime.now(ET).strftime(" · ") + _fmt_time(datetime.now(ET))
    return datetime.now().strftime("%b %d %H:%M")


def _slim(g):
    """Strip a game down to what the front-end needs."""
    return {
        "id": g["id"],
        "status": g["status"],
        "status_name": g["status_name"],
        "period": g["period"],
        "tip": g["tip_str"],
        "venue": g["venue"],
        "event_type": g["event_type"],
        "incomplete": g.get("incomplete", False),
        "home": _slim_team(g["home"]),
        "away": _slim_team(g["away"]),
        "proj": g["proj"],
    }


def _slim_team(t):
    return {
        "name": t["name"], "loc": t["loc"], "nick": t["nick"], "abbr": t["abbr"],
        "score": t["score"], "wins": t["wins"], "losses": t["losses"],
        "h1": _linescore_half(t),
    }


# ----------------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------------
def create_app(date_override=None, sims=DEFAULT_SIMS):
    from flask import Flask, jsonify

    app = Flask(__name__)

    @app.route("/")
    def index():
        return PAGE_HTML

    @app.route("/api/games")
    def api_games():
        return jsonify(build_payload(SEASON_YEAR, date_override, sims))

    @app.route("/refresh")
    def refresh():
        fetch_feed(SEASON_YEAR, force=True)
        return jsonify({"ok": True})

    @app.route("/favicon.svg")
    def favicon():
        return (FAVICON_SVG, 200, {"Content-Type": "image/svg+xml"})

    return app


# ----------------------------------------------------------------------------
# Front-end (single page; polls /api/games every 30s)
# ----------------------------------------------------------------------------
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#0d0d0f"/>'
    '<text x="16" y="22" font-family="Arial Black,Arial" font-size="15" '
    'font-weight="900" fill="#e8112d" text-anchor="middle">B3</text></svg>'
)

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>BIG3 Live Projections</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml"/>
<style>
  :root{
    --bg:#0b0b0d; --panel:#15161a; --panel2:#1c1d23; --line:#2a2c34;
    --txt:#f2f3f5; --mut:#9aa0ad; --red:#e8112d; --red2:#ff3b54;
    --grn:#27c08a; --yel:#f2c14e; --blu:#4ea3ff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;}
  header{position:sticky;top:0;z-index:10;background:linear-gradient(180deg,#141519,#0b0b0d);
    border-bottom:2px solid var(--red);padding:14px 18px;}
  .hrow{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .logo{font-family:Arial Black,Arial;font-weight:900;font-size:26px;letter-spacing:-1px}
  .logo .b3{color:var(--red)}
  .sub{color:var(--mut);font-size:13px}
  .legend{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--mut)}
  .pill{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
    padding:4px 10px;white-space:nowrap}
  .pill b{color:var(--txt)}
  .wrap{max-width:1180px;margin:0 auto;padding:18px}
  .daybar{display:flex;align-items:center;gap:10px;margin:4px 0 16px;flex-wrap:wrap}
  .daybar h2{font-size:16px;margin:0;font-weight:700}
  .updated{color:var(--mut);font-size:12px}
  .sec{margin:22px 0 8px;display:flex;align-items:center;gap:10px}
  .sec h3{margin:0;font-size:13px;letter-spacing:1.5px;text-transform:uppercase;color:var(--mut)}
  .sec .ln{flex:1;height:1px;background:var(--line)}
  .dot{width:9px;height:9px;border-radius:50%;display:inline-block}
  .dot.live{background:var(--red);box-shadow:0 0 0 0 rgba(232,17,45,.7);animation:pulse 1.6s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(232,17,45,.6)}70%{box-shadow:0 0 0 8px rgba(232,17,45,0)}100%{box-shadow:0 0 0 0 rgba(232,17,45,0)}}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
  .card .top{display:flex;justify-content:space-between;align-items:center;
    padding:9px 13px;background:var(--panel2);border-bottom:1px solid var(--line);
    font-size:12px;color:var(--mut)}
  .badge{font-weight:700;padding:2px 8px;border-radius:6px;font-size:11px;letter-spacing:.5px}
  .badge.live{background:rgba(232,17,45,.15);color:var(--red2);border:1px solid rgba(232,17,45,.4)}
  .badge.pre{background:rgba(78,163,255,.12);color:var(--blu);border:1px solid rgba(78,163,255,.35)}
  .badge.final{background:#2a2c34;color:var(--mut)}
  .badge.susp{background:rgba(232,160,17,.15);color:#f0b429;border:1px solid rgba(232,160,17,.4)}
  .suspnote{font-size:11px;color:#f0b429;margin-top:6px}
  .teams{padding:6px 13px 2px}
  .team{display:flex;align-items:center;gap:10px;padding:8px 0}
  .team + .team{border-top:1px dashed var(--line)}
  .tname{flex:1}
  .tname .nm{font-weight:700;font-size:16px}
  .tname .rec{color:var(--mut);font-size:11px;margin-top:1px}
  .score{font-family:Arial Black,Arial;font-weight:900;font-size:30px;min-width:48px;text-align:right}
  .winner .score{color:var(--red2)}
  .fav{font-size:11px;color:var(--yel);margin-left:6px}
  .wpwrap{padding:4px 13px 10px}
  .wpbar{height:8px;border-radius:6px;background:var(--blu);overflow:hidden;display:flex}
  .wpbar .h{background:var(--red)}
  .wprow{display:flex;justify-content:space-between;font-size:11px;color:var(--mut);margin-top:3px}
  .proj{padding:10px 13px;border-top:1px solid var(--line);background:#101116}
  .pgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center}
  .pbox{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:7px 4px}
  .pbox .k{font-size:10px;letter-spacing:.5px;color:var(--mut);text-transform:uppercase}
  .pbox .v{font-size:17px;font-weight:800;margin-top:2px}
  .pbox .v.sm{font-size:14px}
  .race{display:flex;gap:8px;margin-top:9px;font-size:11px;color:var(--mut);
    align-items:center;flex-wrap:wrap}
  .needpill{background:#000;border:1px solid var(--line);border-radius:7px;padding:3px 8px}
  .needpill b{color:var(--red2)}
  .h1line{margin-top:9px;font-size:12px;color:var(--mut);
    border-top:1px dashed var(--line);padding-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .h1line .tag{background:rgba(242,193,78,.12);color:var(--yel);border:1px solid rgba(242,193,78,.35);
    border-radius:6px;padding:2px 7px;font-weight:700}
  .h1line .real{background:rgba(39,192,138,.12);color:var(--grn);border-color:rgba(39,192,138,.35)}
  .meta{padding:7px 13px;color:var(--mut);font-size:11px;border-top:1px solid var(--line)}
  .empty{color:var(--mut);padding:30px;text-align:center;border:1px dashed var(--line);border-radius:12px}
  .foot{color:var(--mut);font-size:11px;text-align:center;margin:26px 0 10px;line-height:1.6}
  details summary{cursor:pointer;color:var(--mut);font-size:13px}
  a{color:var(--red2);text-decoration:none}
</style>
</head>
<body>
<header>
  <div class="hrow">
    <div class="logo"><span class="b3">BIG3</span> LIVE PROJECTIONS</div>
    <div class="sub" id="season"></div>
    <div class="legend">
      <span class="pill">Race to <b>50</b> · win by <b>2</b> · no OT</span>
      <span class="pill">Halftime at <b>25</b></span>
      <span class="pill">Shots <b>2/3/4</b> pts</span>
      <span class="pill" style="border-color:var(--yel);color:var(--yel)">Read <b>win%</b> + <b>total</b> first</span>
    </div>
  </div>
</header>
<div class="wrap">
  <div class="daybar">
    <h2 id="dayttl">Loading…</h2>
    <span class="updated" id="updated"></span>
  </div>
  <div id="live-sec"></div>
  <div id="upc-sec"></div>
  <div id="fin-sec"></div>
  <div class="foot">
    Model: Monte-Carlo race-to-target simulation honoring BIG3 scoring limits
    (first to 50 win-by-2, halftime at 25, cumulative 2/3/4-pt scoring), with
    game-flow dynamics calibrated on actual BIG3 finals — close matchups finish
    tight with high totals, mismatches break open with low totals.
    Win % &amp; projected finals come from <span id="nsim"></span> simulated games per matchup.
    <b>Read the win % and total first</b> — in a race to 50 the winner is always ~51, so the
    scoreline varies by margin: ~51-42 / total ~93 for an even matchup down to ~51-38 /
    total ~89 for the league's widest gap. Team strength is opponent-adjusted from this
    season's completed games and sharpens weekly.
    Data: big3.com feed · Auto-refreshes every 30s.
  </div>
</div>
<script>
const NSIM_NOTE = "thousands of";
function pct(x){return (x==null?'–':x.toFixed(0)+'%');}
function teamRow(t, isWinner, favTxt){
  return `<div class="team ${isWinner?'winner':''}">
    <div class="tname"><div class="nm">${t.name}${favTxt||''}</div>
      <div class="rec">${t.wins}-${t.losses}</div></div>
    <div class="score">${t.score}</div></div>`;
}
function projBlock(g){
  const p=g.proj; if(!p) return '';
  const home=g.home, away=g.away;
  const favHome = p.p_home>=p.p_away;
  const favName = favHome?home.abbr:away.abbr;
  const spreadVal = Math.abs(p.spread);
  const spreadStr = Number.isInteger(spreadVal) ? spreadVal.toString() : spreadVal.toFixed(1);
  const spreadTxt = spreadVal<1 ? 'PK' : (favName+' -'+spreadStr);
  const wpH=p.p_home, wpA=p.p_away;
  let h1='';
  if(p.h1_home!=null){
    const cls = p.h1_is_real?'tag real':'tag';
    const label = p.h1_is_real?'1H (actual)':'1H proj';
    let firstTxt;
    if(p.p_h1_home!=null){
      const f25=p.p_h1_home>=50?home.abbr:away.abbr;
      const f25p=Math.max(p.p_h1_home,100-p.p_h1_home).toFixed(0);
      firstTxt=`· 1st to 25: <b>${f25} ${f25p}%</b>`;
    } else {
      const lead=p.h1_home===p.h1_away?'even':(p.h1_home>p.h1_away?home.abbr:away.abbr);
      firstTxt=`· to 25: <b>${lead}</b>`;
    }
    h1=`<div class="h1line"><span class="${cls}">${label}</span>
        <span>${away.abbr} ${p.h1_away} – ${p.h1_home} ${home.abbr}</span>
        <span>${firstTxt}</span>
        <span>· 1H total <b>${p.h1_total}</b></span></div>`;
  }
  let race='';
  if(g.status==='live'){
    race=`<div class="race">
      <span>Points to 50:</span>
      <span class="needpill">${away.abbr} <b>${p.needed_away}</b></span>
      <span class="needpill">${home.abbr} <b>${p.needed_home}</b></span></div>`;
  }
  return `<div class="wpwrap">
      <div class="wpbar"><div class="h" style="width:${wpH}%"></div>
        <div style="width:${wpA}%"></div></div>
      <div class="wprow"><span>${home.abbr} win ${pct(wpH)}</span>
        <span>${away.abbr} win ${pct(wpA)}</span></div>
    </div>
    <div class="proj">
      <div class="pgrid">
        <div class="pbox"><div class="k">Proj Final</div>
          <div class="v sm">${away.abbr} ${p.proj_away}–${p.proj_home} ${home.abbr}</div></div>
        <div class="pbox"><div class="k">Spread</div>
          <div class="v">${spreadTxt}</div></div>
        <div class="pbox"><div class="k">Total</div>
          <div class="v">${p.total}</div></div>
      </div>
      ${race}${h1}
    </div>`;
}
function card(g){
  const home=g.home, away=g.away;
  const winH = g.status==='final' && home.score>away.score;
  const winA = g.status==='final' && away.score>home.score;
  let favH='',favA='';
  if(g.proj && Math.abs(g.proj.spread)>=1){
    if(g.proj.p_home>g.proj.p_away) favH=' <span class="fav">▲ fav</span>';
    else if(g.proj.p_away>g.proj.p_home) favA=' <span class="fav">▲ fav</span>'; }
  let badge, period='';
  if(g.status==='live'){ badge=`<span class="badge live">● LIVE</span>`;
    period = g.status_name? `<span>${g.status_name}</span>`:''; }
  else if(g.status==='final'){ badge = g.incomplete
      ? `<span class="badge susp">SUSPENDED</span>`
      : `<span class="badge final">FINAL</span>`; }
  else { badge=`<span class="badge pre">${g.tip}</span>`; }
  const suspNote = (g.status==='final' && g.incomplete)
    ? `<div class="suspnote">⚠ Suspended before the finish — score is unofficial and excluded from team ratings.</div>`
    : '';
  return `<div class="card">
    <div class="top">${badge}<span>${g.event_type||''}</span></div>
    <div class="teams">
      ${teamRow(away, winA, favA)}
      ${teamRow(home, winH, favH)}
    </div>
    ${suspNote}
    ${projBlock(g)}
    ${g.venue?`<div class="meta">${g.venue}</div>`:''}
  </div>`;
}
function section(id,title,games,live){
  const el=document.getElementById(id);
  if(!games||!games.length){ el.innerHTML=''; return; }
  const dot = live?`<span class="dot live"></span>`:'';
  el.innerHTML=`<div class="sec">${dot}<h3>${title}</h3><div class="ln"></div></div>
    <div class="grid">${games.map(card).join('')}</div>`;
}
async function load(){
  try{
    const r=await fetch('api/games',{cache:'no-store'});
    const d=await r.json();
    document.getElementById('season').textContent='Season '+ (d.season-2017) +' · '+d.season;
    document.getElementById('dayttl').textContent =
      (d.is_today?'Today — ':'') + d.focus_pretty;
    document.getElementById('updated').textContent='Updated '+d.updated;
    document.getElementById('nsim').textContent=NSIM_NOTE;
    section('live-sec','Live',d.live,true);
    section('upc-sec', d.live.length?'Upcoming':'Upcoming Games',d.upcoming,false);
    section('fin-sec','Final',d.final,false);
    if(!d.live.length && !d.upcoming.length && !d.final.length){
      document.getElementById('upc-sec').innerHTML=
        '<div class="empty">No BIG3 games found in the feed yet.</div>';
    }
  }catch(e){
    document.getElementById('dayttl').textContent='Feed unavailable — retrying…';
  }
}
load(); setInterval(load, 30000);
</script>
</body>
</html>"""


# ----------------------------------------------------------------------------
# Module-level WSGI app. The combined basketball-site (wsgi.py) discovers each
# dashboard by importing it and grabbing this `app`. Standalone use goes through
# main() below, which builds its own app honoring CLI flags.
# ----------------------------------------------------------------------------
app = create_app()


# ----------------------------------------------------------------------------
# CLI: --selftest, --once, or run the server
# ----------------------------------------------------------------------------
def run_selftest():
    print("BIG3 projection-engine self-test")
    print(f"  TARGET={TARGET_SCORE} WIN_BY={WIN_BY} HALF={HALF_SCORE} "
          f"LEAGUE_PPP={LEAGUE_PPP} mean_value={MEAN_VALUE:.3f} "
          f"p_score={_score_prob(LEAGUE_PPP):.3f}")
    cases = [
        ("Even matchup, tip-off (0-0)", 0, 0, 1.0, 1.0),
        ("Even, home up 30-20 (2nd half)", 30, 20, 1.0, 1.0),
        ("Even, away up 24-12 (late 1st)", 12, 24, 1.0, 1.0),
        ("Mid gap (home +5%/-5%)", 0, 0, 1.05, 0.95),
        ("Top-vs-bottom (~1.24 ppp ratio)", 0, 0, 1.10, 0.89),
        ("Home up 48-45, win-by-2 race", 48, 45, 1.0, 1.0),
        ("Away up 25-19 at half", 19, 25, 1.0, 1.0),
    ]
    for label, a, b, oh, oa in cases:
        ppp_h = _adjusted_ppp(oh, 1.0)
        ppp_a = _adjusted_ppp(oa, 1.0)
        # def ratings folded into the other team's offense; here keep simple
        r = simulate(a, b, ppp_h, ppp_a, sims=20000, seed=7)
        h1 = (f"  |  1H proj H{r['h1_home']}-A{r['h1_away']} "
              f"(tot {r['h1_total']}, home-first-to-25 {r.get('p_h1_home','?')}%)"
              if "h1_home" in r else "  |  (past half)")
        print(f"\n{label}")
        print(f"  start  H {a} - {b} A")
        print(f"  win%   H {r['p_home']:.1f}  /  A {r['p_away']:.1f}")
        print(f"  final  H {r['proj_home']} - {r['proj_away']} A  "
              f"(spread {r['spread']:+.1f}, total {r['total']}){h1}")
    print("\nSanity targets: even 0-0 -> ~50/50, winner ~51, margin ~9 / total ~93;"
          "\nmid gap -> ~70%, margin ~10; top-vs-bottom -> ~85%, margin ~13 / total ~89;"
          "\na leader in a live race should carry a clear win%.")


def run_once(date_override=None):
    payload = build_payload(SEASON_YEAR, date_override, sims=20000)
    print(f"\nBIG3 — {payload['focus_pretty']}"
          f"{'  (today)' if payload['is_today'] else ''}   "
          f"[updated {payload['updated']}]")
    for bucket in ("live", "upcoming", "final"):
        rows = payload[bucket]
        if not rows:
            continue
        print(f"\n=== {bucket.upper()} ===")
        for g in rows:
            h, a = g["home"], g["away"]
            line = f"{a['name']} ({a['score']}) @ {h['name']} ({h['score']})"
            if g["status"] == "pre":
                line = f"{a['name']} @ {h['name']}  [{g['tip']}]"
            print(f"\n  {line}")
            p = g["proj"]
            if p:
                fav = h["abbr"] if p["p_home"] >= p["p_away"] else a["abbr"]
                sp = abs(p["spread"])
                sp_txt = "PK" if sp < 1 else f"{fav} -{sp:.1f}"
                print(f"    win%:  {h['abbr']} {p['p_home']:.0f}%  |  "
                      f"{a['abbr']} {p['p_away']:.0f}%")
                print(f"    proj:  {a['abbr']} {p['proj_away']} - "
                      f"{p['proj_home']} {h['abbr']}   "
                      f"({sp_txt}, total {p['total']})")
                if "h1_home" in p:
                    tag = "1H actual" if p.get("h1_is_real") else "1H proj"
                    print(f"    {tag}: {a['abbr']} {p['h1_away']} - "
                          f"{p['h1_home']} {h['abbr']} (total {p['h1_total']})")
                if g["status"] == "live":
                    print(f"    to 50: {a['abbr']} needs {p['needed_away']}, "
                          f"{h['abbr']} needs {p['needed_home']}")


def main():
    global SEASON_YEAR, DEFAULT_SIMS
    try:                                    # make console output UTF-8 safe on Windows
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="BIG3 live projections dashboard")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--date", help="focus a game day YYYY-MM-DD")
    ap.add_argument("--year", type=int, default=SEASON_YEAR)
    ap.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    ap.add_argument("--selftest", action="store_true",
                    help="run engine sanity checks and exit")
    ap.add_argument("--once", action="store_true",
                    help="print the slate to console and exit (no server)")
    args = ap.parse_args()

    SEASON_YEAR = args.year
    DEFAULT_SIMS = args.sims

    if args.selftest:
        run_selftest()
        return
    if args.once:
        run_once(args.date)
        return

    app = create_app(date_override=args.date, sims=args.sims)
    print(f"BIG3 Live Projections → http://localhost:{args.port}  "
          f"(season {SEASON_YEAR}, {args.sims} sims/game)")
    app.run(host="0.0.0.0", port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
