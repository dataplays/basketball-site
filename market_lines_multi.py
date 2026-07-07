#!/usr/bin/env python3
"""
market_lines_multi.py — shared, multi-league consensus betting-line engine.
============================================================================
One place to pull the market's consensus **spread** and **total** for a whole
league's slate from The Odds API, so any tool can compare a model projection to
the number the books are actually posting.

Why this module exists
----------------------
The WNBA live board grew a "market row" (projection vs. book line + edge chip)
but the logic lived inside that one file and only ran for WNBA live games. The
cross-league **/edges** aggregator (and the alerts watcher) need the same thing
for every league, pre-game and live. Rather than copy it four times, the fetch /
consensus / matching / edge math lives here, parameterized by sport key.

Design notes
------------
* **One bulk call per league.** The `/sports/{key}/odds` endpoint returns the
  entire slate (pre-game + live) with every book in a single request — 1-2 API
  credits regardless of how many games. We do NOT hit the per-event endpoint
  here (that is only needed for first-half "additional markets"; the aggregator
  is a full-game view, so the cheap bulk feed is enough).
* **Consensus = median across sharp books** (`ALLOWED_BOOKS`), skipping soft /
  sweepstakes books, matching the WNBA board's method.
* **Home-oriented spread.** `spread_home` is the home team's handicap (negative
  == home favored), the same convention the boards' `proj_spread`
  (= home_final - away_final) compares against, so edge = proj_spread +
  spread_home from the home side.
* **Degrades quietly.** No key / quota exhausted / league off-season → empty
  result, never an exception that would take a page down.

Public API
----------
    SPORT_KEYS                       # our board prefix -> Odds API sport key
    fetch_consensus(sport_key)       # -> {(away_norm, home_norm): LineRow}
    match_line(away, home, lines)    # -> LineRow | None  (board names -> line)
    spread_edge(proj_spread, home_line)   # -> (value, side, css_class)
    total_edge(proj_total, mkt_total)     # -> (value, side, css_class)

A LineRow is a plain dict: {"spread_home", "total", "book_count"}.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

# The shared Odds API key (env override; same fallback the props/WNBA tools use).
THE_ODDS_API_KEY = os.environ.get(
    "THE_ODDS_API_KEY", "fdb2de0728216509287d06490355c922"
)
ODDS_API_HOST = "https://api.the-odds-api.com/v4"

# Our dashboard prefix -> The Odds API sport key. Off-season leagues simply
# return no games; keeping them here means /edges lights them up automatically
# once their season (and the books' lines) return.
SPORT_KEYS = {
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "cbb": "basketball_ncaab",
    "wcbb": "basketball_wncaab",
    "summer": "basketball_nba_summer_league",
}

# Books averaged into the consensus (skip soft / sweepstakes books).
ALLOWED_BOOKS = {
    "draftkings", "fanduel", "betmgm", "williamhill_us", "betrivers", "espnbet",
    "caesars", "pointsbetus", "betonlineag",
}

# Edge magnitude (points) at/above which an edge is "strong" (green chip).
STRONG_EDGE = 2.0
# Below this it is not worth surfacing at all.
MILD_EDGE = 1.0

CACHE_TTL = 90          # seconds to reuse a league's consensus slate
_cache_lock = threading.Lock()
_cache: dict[str, tuple[dict, float]] = {}     # sport_key -> (lines, ts)
_quota = {"used": None, "remaining": None}      # last-seen Odds API quota


# ── low-level fetch ───────────────────────────────────────────────────────────
def _http_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # The Odds API reports remaining quota in response headers.
        used = r.headers.get("x-requests-used")
        remaining = r.headers.get("x-requests-remaining")
        if used is not None:
            _quota["used"] = used
        if remaining is not None:
            _quota["remaining"] = remaining
        return json.loads(r.read())


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _median(vals: list):
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


# ── consensus builder ─────────────────────────────────────────────────────────
def _consensus_from_odds(games: list) -> dict:
    """Turn a bulk /odds payload into {(away_norm, home_norm): LineRow}."""
    out: dict = {}
    for g in games:
        home_name = g.get("home_team", "")
        away_name = g.get("away_team", "")
        home_norm = _norm(home_name)
        away_norm = _norm(away_name)
        sp, tot, books = [], [], set()
        for bm in g.get("bookmakers", []):
            if bm.get("key") not in ALLOWED_BOOKS:
                continue
            used = False
            for m in bm.get("markets", []):
                k = m.get("key")
                outs = m.get("outcomes", [])
                if k == "spreads":
                    for o in outs:
                        if _norm(o.get("name", "")) == home_norm and o.get("point") is not None:
                            sp.append(float(o["point"]))
                            used = True
                elif k == "totals":
                    pts = [o.get("point") for o in outs if o.get("point") is not None]
                    if pts:
                        tot.append(float(pts[0]))
                        used = True
            if used:
                books.add(bm["key"])
        if not sp and not tot:
            continue
        out[(away_norm, home_norm)] = {
            "spread_home": _median(sp),
            "total": _median(tot),
            "book_count": len(books),
            "commence": g.get("commence_time"),
            "home_team": home_name,
            "away_team": away_name,
        }
    return out


def fetch_consensus(sport_key: str) -> dict:
    """Consensus spread/total for a whole league slate (pre-game + live).

    Cached per sport key ~CACHE_TTL. Returns {} on any failure or if no key.
    """
    if not THE_ODDS_API_KEY:
        return {}
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(sport_key)
        if hit and now - hit[1] < CACHE_TTL:
            return hit[0]
    url = (f"{ODDS_API_HOST}/sports/{sport_key}/odds"
           f"?apiKey={THE_ODDS_API_KEY}&regions=us"
           f"&markets=spreads,totals&oddsFormat=american")
    try:
        games = _http_json(url)
    except Exception:
        with _cache_lock:                        # serve stale on error if we have it
            hit = _cache.get(sport_key)
            return hit[0] if hit else {}
    if not isinstance(games, list):
        return {}
    lines = _consensus_from_odds(games)
    with _cache_lock:
        _cache[sport_key] = (lines, now)
    return lines


def match_line(away_name: str, home_name: str, lines: dict):
    """Match a board game (ESPN location names) to a consensus LineRow.

    The boards give the location ("New York"); The Odds API gives the full name
    ("New York Liberty"), so a location-substring match on both sides suffices.
    """
    a, h = _norm(away_name), _norm(home_name)
    for (oa, oh), row in lines.items():
        if a and h and a in oa and h in oh:
            return row
    return None


# ── edge math (home-oriented spread; proj_spread = home_final - away_final) ────
def spread_edge(proj_spread, home_line):
    """Edge on the spread. Returns (abs_value, side, css_class).

    Market implies home margin = -home_line; model implies proj_spread, so
    edge = proj_spread + home_line. Positive => back HOME, negative => back AWAY.
    """
    if proj_spread is None or home_line is None:
        return None, "", ""
    e = proj_spread + home_line
    side = "Home" if e > 0 else "Away"
    return abs(e), side, _edge_cls(abs(e))


def total_edge(proj_total, mkt_total):
    """Edge on the total: proj - market. Positive => Over lean."""
    if proj_total is None or mkt_total is None:
        return None, "", ""
    e = proj_total - mkt_total
    side = "Over" if e > 0 else "Under"
    return abs(e), side, _edge_cls(abs(e))


def _edge_cls(mag: float) -> str:
    if mag >= STRONG_EDGE:
        return "edge-strong"
    if mag >= MILD_EDGE:
        return "edge-mild"
    return "edge-none"


def quota() -> dict:
    """Last-seen Odds API quota (used / remaining), or Nones before any call."""
    return dict(_quota)


if __name__ == "__main__":
    # Quick manual check: print the consensus for whatever leagues are live.
    for pfx, key in SPORT_KEYS.items():
        lines = fetch_consensus(key)
        if not lines:
            continue
        print(f"\n=== {pfx.upper()} ({key}) — {len(lines)} games ===")
        for (a, h), row in lines.items():
            print(f"  {a:26s} @ {h:26s}  "
                  f"spread_home {row['spread_home']}  total {row['total']}  "
                  f"books {row['book_count']}")
    print(f"\nOdds API quota: {quota()}")
