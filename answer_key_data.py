#!/usr/bin/env python3
r"""
answer_key_data.py — historical dataset builder for the Answer Key tool.

Walks past ESPN scoreboards for a league, and for every completed game
records the CLOSING spread & total (ESPN Core API odds feed — the same
open/close source the line-movement tool uses) plus the final score and
per-period linescores. Output is one CSV per league that answer_key.py
samples from to price derivatives empirically (Feustel's "Answer Key").

Usage:
    py -3 answer_key_data.py --league wnba              # all configured seasons
    py -3 answer_key_data.py --league nba --season 2025 # one season
    py -3 answer_key_data.py --league cbb               # big: ~5k games/season

Incremental + resumable: already-recorded event_ids are skipped, games with
no odds are remembered in a sidecar json so they aren't refetched every run.
NOTE: ESPN host rule — site.web.api.espn.com, never site.api.espn.com.
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

DOCS = r"C:\Users\User\Documents"
DATA_DIR = os.environ.get("BBALL_DATA_DIR") or (
    DOCS if os.path.isdir(DOCS) else os.path.join(os.path.dirname(__file__), "data"))

SCOREBOARD = ("https://site.web.api.espn.com/apis/site/v2/sports/basketball/"
              "{path}/scoreboard?dates={d}{extra}")
ODDS = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/"
        "{path}/events/{eid}/competitions/{eid}/odds")

# Season windows are (start, end) inclusive; label = the season's ending year
# for winter leagues, the calendar year for summer leagues (WNBA).
LEAGUES = {
    "wnba": {
        "path": "wnba", "periods": 4, "extra": "",
        "seasons": {
            2023: ("2023-05-01", "2023-10-31"),
            2024: ("2024-05-01", "2024-10-31"),
            2025: ("2025-05-01", "2025-10-31"),
            2026: ("2026-05-01", None),           # None = through yesterday
        },
    },
    "nba": {
        "path": "nba", "periods": 4, "extra": "",
        "seasons": {
            2024: ("2023-10-15", "2024-06-30"),
            2025: ("2024-10-15", "2025-06-30"),
            2026: ("2025-10-15", "2026-06-30"),
        },
    },
    "cbb": {
        "path": "mens-college-basketball", "periods": 2,
        "extra": "&groups=50&limit=500",
        "seasons": {
            2024: ("2023-11-01", "2024-04-15"),
            2025: ("2024-11-01", "2025-04-15"),
            2026: ("2025-11-01", "2026-04-15"),
        },
    },
    "wcbb": {
        "path": "womens-college-basketball", "periods": 4,
        "extra": "&groups=50&limit=500",
        "seasons": {
            2024: ("2023-11-01", "2024-04-15"),
            2025: ("2024-11-01", "2025-04-15"),
            2026: ("2025-11-01", "2026-04-15"),
        },
    },
}

PROVIDER_PREF = ["draftkings", "espn bet"]   # then anything with a close line

# Plausible closing-line ranges per league. ESPN's odds docs are inconsistent
# across eras — some store an ODDS PRICE (e.g. -125) where the total belongs,
# which poisoned season 2023 on the first build (mean "total" 118, min -125).
# A candidate value outside these ranges is rejected and the next phase /
# provider / field is tried; no plausible pair -> the game is dropped.
TOTAL_RANGE = {"wnba": (110, 220), "nba": (160, 290),
               "cbb": (80, 220), "wcbb": (80, 220)}
SPREAD_MAX = 45.0
MAX_P = 4                                    # p1..p4 columns (CBB uses p1/p2)
FIELDS = (["league", "season", "season_type", "date", "event_id", "away",
           "home", "neutral", "spread_home", "total", "away_score",
           "home_score"]
          + [f"away_p{i}" for i in range(1, MAX_P + 1)]
          + [f"home_p{i}" for i in range(1, MAX_P + 1)]
          + ["ot_periods", "provider"])


def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def csv_path(league):
    return os.path.join(DATA_DIR, f"answer_key_{league}.csv")


def noodds_path(league):
    return os.path.join(DATA_DIR, f"answer_key_{league}_noodds.json")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _side_num(node):
    """pointSpread/total nodes: {'american': '-11.5', ...}"""
    if not isinstance(node, dict):
        return _num(node)
    return _num(node.get("american") or node.get("alternateDisplayValue"))


def extract_close(odds_doc, league):
    """-> (spread_home, total, provider_name) or (None, None, None).
    Prefers DK, then ESPN BET, then any provider carrying a close (falls
    back to current, then the item's top-level spread/overUnder). Every
    candidate is range-validated — see TOTAL_RANGE note above."""
    lo, hi = TOTAL_RANGE[league]
    items = (odds_doc or {}).get("items") or []

    def ok_spread(v):
        return v is not None and abs(v) <= SPREAD_MAX

    def ok_total(v):
        return v is not None and lo <= v <= hi

    def rank(it):
        name = ((it.get("provider") or {}).get("name") or "").lower()
        for i, p in enumerate(PROVIDER_PREF):
            if p in name:
                return i
        return len(PROVIDER_PREF)

    for it in sorted(items, key=rank):
        name = ((it.get("provider") or {}).get("name") or "")
        if "live" in name.lower():
            continue
        home = it.get("homeTeamOdds") or {}
        spread_cands = [_side_num((home.get(ph) or {}).get("pointSpread"))
                        for ph in ("close", "current", "open")]
        spread_cands.append(_num(it.get("spread")))
        total_cands = [_side_num(((it.get(ph) or {}) or {}).get("total"))
                       for ph in ("close", "current", "open")]
        total_cands.append(_num(it.get("overUnder")))
        spread = next((v for v in spread_cands if ok_spread(v)), None)
        total = next((v for v in total_cands if ok_total(v)), None)
        if spread is not None and total is not None:
            return spread, total, name
    return None, None, None


def scoreboard_games(league_cfg, d):
    """Completed, line-able games on a date -> list of row dicts (no odds yet)."""
    url = SCOREBOARD.format(path=league_cfg["path"], d=d.strftime("%Y%m%d"),
                            extra=league_cfg["extra"])
    try:
        doc = fetch_json(url)
    except Exception:
        return None                                   # date fetch failed
    rows = []
    for ev in doc.get("events", []) or []:
        try:
            st = ((ev.get("status") or {}).get("type") or {})
            if not st.get("completed") or st.get("state") != "post":
                continue
            season_type = ((ev.get("season") or {}).get("type"))
            if season_type not in (2, 3):             # regular + postseason only
                continue
            name = (ev.get("name") or "").lower()
            if "all-star" in name or "rising stars" in name:
                continue
            comp = ev["competitions"][0]
            home = away = None
            for t in comp.get("competitors", []):
                if t.get("homeAway") == "home":
                    home = t
                elif t.get("homeAway") == "away":
                    away = t
            if not home or not away:
                continue
            hs, as_ = _num(home.get("score")), _num(away.get("score"))
            if hs is None or as_ is None:
                continue
            row = {
                "league": None, "season": None,
                "season_type": season_type,
                "date": d.strftime("%Y-%m-%d"),
                "event_id": ev["id"],
                "away": (away.get("team") or {}).get("displayName", ""),
                "home": (home.get("team") or {}).get("displayName", ""),
                "neutral": 1 if comp.get("neutralSite") else 0,
                "away_score": int(as_), "home_score": int(hs),
            }
            reg = league_cfg["periods"]
            for side, t in (("away", away), ("home", home)):
                ls = [(_num(x.get("value")) or 0) for x in (t.get("linescores") or [])]
                for i in range(1, MAX_P + 1):
                    row[f"{side}_p{i}"] = int(ls[i - 1]) if i <= min(len(ls), reg) else ""
            n_ls = len(home.get("linescores") or [])
            row["ot_periods"] = max(0, n_ls - reg) if n_ls else 0
            # a game with no linescores is still usable for FG markets
            rows.append(row)
        except Exception:
            continue
    return rows


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def load_existing(league):
    path = csv_path(league)
    ids = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                ids.add(r["event_id"])
    return ids


def load_noodds(league):
    try:
        with open(noodds_path(league), encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_noodds(league, ids):
    tmp = noodds_path(league) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)
    os.replace(tmp, noodds_path(league))


def build(league, only_season=None):
    cfg = LEAGUES[league]
    have = load_existing(league)
    noodds = load_noodds(league)
    path = csv_path(league)
    new_file = not os.path.exists(path)
    yesterday = date.today() - timedelta(days=1)

    out = open(path, "a", encoding="utf-8", newline="")
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    if new_file:
        writer.writeheader()

    grand_added = grand_no = 0
    for season, (s, e) in sorted(cfg["seasons"].items()):
        if only_season and season != only_season:
            continue
        start = datetime.strptime(s, "%Y-%m-%d").date()
        end = min(datetime.strptime(e, "%Y-%m-%d").date() if e else yesterday,
                  yesterday)
        if start > end:
            continue
        # 1) walk scoreboards for the season's candidate games
        cand = []
        bad_dates = 0
        dates = list(daterange(start, end))
        with ThreadPoolExecutor(max_workers=8) as ex:
            for res in ex.map(lambda d: scoreboard_games(cfg, d), dates):
                if res is None:
                    bad_dates += 1
                    continue
                for row in res:
                    if row["event_id"] in have or row["event_id"] in noodds:
                        continue
                    row["league"] = league
                    row["season"] = season
                    cand.append(row)
        # 2) fetch closing odds for the new games
        added = no_line = 0

        def get_odds(row):
            try:
                doc = fetch_json(ODDS.format(path=cfg["path"], eid=row["event_id"]))
            except Exception:
                return row, None
            return row, extract_close(doc, league)

        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(get_odds, r) for r in cand]
            for fut in as_completed(futs):
                row, got = fut.result()
                if got is None:                       # fetch error: retry next run
                    continue
                spread, total, prov = got
                if spread is None:
                    noodds.add(row["event_id"])
                    no_line += 1
                    continue
                row["spread_home"] = spread
                row["total"] = total
                row["provider"] = prov
                writer.writerow(row)
                have.add(row["event_id"])
                added += 1
        out.flush()
        save_noodds(league, noodds)
        grand_added += added
        grand_no += no_line
        print(f"  {league} {season}: +{added} games "
              f"({no_line} without a line, {bad_dates} dates unfetchable)")
    out.close()
    print(f"{league}: {grand_added} added this run, {grand_no} skipped w/o lines "
          f"-> {csv_path(league)}")


def main():
    ap = argparse.ArgumentParser(description="Build Answer Key historical CSVs")
    ap.add_argument("--league", required=True, choices=sorted(LEAGUES))
    ap.add_argument("--season", type=int, help="only this season label")
    a = ap.parse_args()
    build(a.league, a.season)


if __name__ == "__main__":
    main()
