#!/usr/bin/env python3
"""
inplay_props.py — In-Game Player Prop Projections (/inplay).
=============================================================
For every LIVE game: take each player's current stat total + minutes played
from the live box score, and project their FINAL total using the pregame
props-engine forecast (projected minutes + projected total -> the player's
expected per-minute rate). Then pull LIVE prop lines from The Odds API
(the same multi-book source the nightly props engines use) and show the
EV% on both sides of every live line.

Projection model
----------------
* Pregame: the actual props engine's `project_player()` gives ExpMin and
  ExpPts/ExpReb -> pregame rate = ExpStat / ExpMin. (Same engine as the
  nightly PDF and /vacuum — imported, not re-implemented.)
* Live: remaining_min = clamp(ExpMin − min_played, 0, game_min_remaining).
  If a player has already passed their projected minutes and the game is
  still going, their remaining share is extrapolated from the share of
  game time they've played so far.
* Projected final = current + remaining_min × pregame rate.
* P(over) for EV: remaining production ~ Normal(mean = remaining_min × rate,
  SD = full-game prop SD × sqrt(remaining_min / ExpMin)) — the same SD
  shapes as the /median tool (pts max(4.0,.30·m), reb max(1.8,.38·m)).
  Integer lines get a push band; EV is push-conditional at the best
  live odds across books.

Run:
    py -3 inplay_props.py                    # web -> http://localhost:5021
    py -3 inplay_props.py --once             # console print (all leagues)
    py -3 inplay_props.py --once --league wnba
Also mounted at /inplay on the basketball-site (module-level `app`,
GET form + relative action so it works under the mount).
"""

from __future__ import annotations

import argparse
import html
import importlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from math import erf, sqrt

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    ET = None

from flask import Flask, request

# league key -> config (module = that league's props engine; minutes rules)
LEAGUES = {
    "wnba": {"label": "WNBA", "module": "wnba_props_projections",
             "reg_min": 40, "qtr_min": 10, "ot_min": 5},
    "nba":  {"label": "NBA",  "module": "nba_props_projections",
             "reg_min": 48, "qtr_min": 12, "ot_min": 5},
}
DEFAULT_LEAGUE = "wnba"
ACCENT = "#9ccc65"

PROPS = [("pts", "Points"), ("reb", "Rebounds"), ("pr", "Pts+Reb")]
MIN_REM_FOR_SD = 0.25       # below this many remaining min, outcome ~decided
EV_HIGHLIGHT = 3.0          # green highlight at >= this EV%

# EV probability = blend of the model's P(over) with the market's de-vigged
# consensus P(over) (same anchoring philosophy as the props engines' Model
# v2026-07-01). A pure-model live EV runs absurdly hot (+50-70%) because the
# books price live-game context (sit-risk in blowouts, real-time role news)
# that a minutes extrapolation can't see.
W_MODEL = 0.40              # weight on the model; 1-W on market consensus
P_CLAMP = (0.03, 0.97)

# Blowout minutes discount: regulars lose remaining minutes late in lopsided
# games. Kicks in above a 8-pt lead in the 2nd half, up to -45% of remaining
# minutes for rotation players (exp_min >= BLOWOUT_MIN_EXP) as the lead and
# the lateness grow.
BLOWOUT_MIN_EXP = 22.0
BLOWOUT_MAX_CUT = 0.45


def blowout_min_factor(lead: float, rem_game: float, reg_min: float,
                       exp_min: float) -> float:
    if exp_min < BLOWOUT_MIN_EXP:
        return 1.0
    half = reg_min / 2.0
    if lead < 9 or rem_game >= half:
        return 1.0
    severity = min(1.0, (lead - 8.0) / 12.0)
    lateness = 1.0 - rem_game / half
    return 1.0 - BLOWOUT_MAX_CUT * severity * lateness

# ── caches ────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_cache: dict = {}           # key -> (value, monotonic_ts)
TTL_LIVE = 20               # live scoreboard
TTL_BOX = 20                # live box scores
TTL_PRE = 1800              # pregame projections (heavy: rosters + game logs)
TTL_LINES = 60              # live Odds API prop lines


def _cached(key, ttl, fn):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[1] < ttl:
            return hit[0]
    val = fn()
    with _lock:
        _cache[key] = (val, time.monotonic())
    return val


def _mod(league: str):
    return importlib.import_module(LEAGUES[league]["module"])


def _today() -> str:
    d = datetime.now(ET).date() if ET is not None else datetime.now().date()
    return d.strftime("%Y%m%d")


# ── live games (scoreboard with clock/period) ─────────────────────────────────
def fetch_live_games(league: str) -> list[dict]:
    def _fetch():
        mod = _mod(league)
        data = mod.fetch_json(f"{mod.ESPN_SCOREBOARD}?dates={_today()}") or {}
        games = []
        for event in data.get("events", []):
            st = event.get("status", {})
            if (st.get("type", {}) or {}).get("state") != "in":
                continue
            comp = event["competitions"][0]
            g = {
                "event_id": event["id"],
                "short_name": event.get("shortName", ""),
                "detail": (st.get("type", {}) or {}).get("shortDetail", ""),
                "period": int(st.get("period") or 1),
                "clock": float(st.get("clock") or 0.0),
                "neutral_site": comp.get("neutralSite", False),
                "spread": None, "home": None, "away": None,
            }
            odds = comp.get("odds", [])
            if odds:
                g["spread"] = odds[0].get("spread")
            for td in comp.get("competitors", []):
                t = td["team"]
                g[td["homeAway"]] = {
                    "id": t["id"],
                    "name": t.get("location", t.get("displayName", "")),
                    "display_name": t.get("displayName", ""),
                    "abbreviation": t.get("abbreviation", ""),
                    "score": td.get("score", ""),
                }
            if g["home"] and g["away"]:
                games.append(g)
        return games
    return _cached(("live", league), TTL_LIVE, _fetch)


def game_time(league: str, period: int, clock_sec: float) -> tuple[float, float]:
    """(elapsed_min, remaining_min) — remaining includes only the current
    OT period once past regulation (further OTs unknowable)."""
    cfg = LEAGUES[league]
    reg, qtr, ot = cfg["reg_min"], cfg["qtr_min"], cfg["ot_min"]
    n_reg_periods = reg // qtr
    clock_min = max(0.0, clock_sec) / 60.0
    if period <= n_reg_periods:
        elapsed = (period - 1) * qtr + (qtr - min(clock_min, qtr))
        remaining = reg - elapsed
    else:
        done_ots = period - n_reg_periods - 1
        elapsed = reg + done_ots * ot + (ot - min(clock_min, ot))
        remaining = min(clock_min, ot)
    return round(elapsed, 2), round(max(0.0, remaining), 2)


# ── live box score (per-player MIN / PTS / REB) ───────────────────────────────
def fetch_box(league: str, event_id: str) -> dict:
    """{player_id: {name, team_abbr, min, pts, reb}} from the live box."""
    def _fetch():
        mod = _mod(league)
        data = mod.fetch_json(f"{mod.ESPN_SUMMARY}?event={event_id}") or {}
        out = {}
        for team in (data.get("boxscore", {}) or {}).get("players", []):
            abbr = (team.get("team", {}) or {}).get("abbreviation", "")
            for block in team.get("statistics", [])[:1]:
                labels = block.get("labels") or block.get("names") or []
                try:
                    i_min = labels.index("MIN")
                    i_pts = labels.index("PTS")
                    i_reb = labels.index("REB")
                except ValueError:
                    continue
                for ath in block.get("athletes", []):
                    if ath.get("didNotPlay"):
                        continue
                    a = ath.get("athlete", {}) or {}
                    pid = str(a.get("id", ""))
                    stats = ath.get("stats") or []
                    if not pid or len(stats) <= max(i_min, i_pts, i_reb):
                        continue
                    def _num(s):
                        try:
                            return float(str(s).replace("+", ""))
                        except (TypeError, ValueError):
                            return 0.0
                    out[pid] = {
                        "name": a.get("displayName", ""),
                        "team_abbr": abbr,
                        "min": _num(stats[i_min]),
                        "pts": _num(stats[i_pts]),
                        "reb": _num(stats[i_reb]),
                    }
        return out
    return _cached(("box", league, event_id), TTL_BOX, _fetch)


# ── pregame projections (the props engine, same path as /vacuum) ─────────────
def pregame_context(league: str, game: dict) -> dict:
    """{pid: {proj, name, position, team_abbr}}, plus name->pid map for odds.
    Baseline = actually Out/Doubtful players excluded (engine handles the
    redistribution). Cached 30 min per game."""
    def _build():
        mod = _mod(league)
        ratings = mod.load_team_ratings()
        if ratings:
            mod.LEAGUE_AVG_DRTG = sum(r["de"] for r in ratings.values()) / len(ratings)
            mod.LEAGUE_AVG_PACE = sum(r["pace"] for r in ratings.values()) / len(ratings)
        try:
            spread = float(game.get("spread")) if game.get("spread") is not None else None
        except (TypeError, ValueError):
            spread = None
        injuries = mod.fetch_injuries(game["event_id"])
        date_str = _today()

        projs: dict = {}
        name_to_pid: dict = {}
        for side, opp_side in (("home", "away"), ("away", "home")):
            team, opp = game[side], game[opp_side]
            team_key = mod.resolve_team_name(team["name"], ratings)
            opp_key = mod.resolve_team_name(opp["name"], ratings)
            team_r = ratings.get(team_key, {}) if team_key else {}
            opp_r = ratings.get(opp_key, {}) if opp_key else {}
            b2b = mod.fetch_team_schedule_b2b(team["id"], date_str)
            roster = mod.fetch_roster(team["id"])

            logs: dict = {}
            with ThreadPoolExecutor(max_workers=10) as pool:
                futs = {pool.submit(mod.fetch_player_gamelog, p["id"]): p["id"]
                        for p in roster}
                for fut in as_completed(futs):
                    pid = futs[fut]
                    try:
                        logs[pid] = fut.result() or []
                    except Exception:
                        logs[pid] = []

            players, injured = [], []
            for p in roster:
                pid = p["id"]
                gl = logs.get(pid, [])
                stats = mod.compute_player_stats(gl)
                status = (injuries.get(pid) or "").lower()
                rec = {"player": p, "stats": stats, "status": status,
                       "recent_events": [g.get("event_id", "") for g in gl[:15]],
                       "events": {g["event_id"] for g in gl if g.get("event_id")}}
                players.append(rec)
                name_to_pid[mod._normalize_name(p["name"])] = pid
                if status in ("out", "doubtful") and stats:
                    injured.append({"id": pid, "name": p["name"],
                                    "position": p.get("position", ""),
                                    "stats": stats, "p_out": 1.0,
                                    "events": rec["events"]})
            for rec in players:
                p, stats = rec["player"], rec["stats"]
                if not stats:
                    continue
                info = {"name": p["name"], "position": p.get("position", ""),
                        "team_abbr": team.get("abbreviation", ""), "proj": None}
                if rec["status"] not in ("out", "doubtful"):
                    try:
                        info["proj"] = mod.project_player(
                            player=p, stats=stats,
                            team_ratings=team_r, opp_ratings=opp_r,
                            is_home=(side == "home"), spread=spread, team_b2b=b2b,
                            injured_teammates=[tm for tm in injured if tm["id"] != p["id"]],
                            player_recent_events=rec["recent_events"],
                            neutral_site=game.get("neutral_site", False),
                        )
                    except Exception:
                        info["proj"] = None
                if info["proj"] is None:
                    # ruled-out player who is actually playing, or projection
                    # failure: fall back to season per-minute rates.
                    info["proj"] = {
                        "expected_min": round(stats["weighted_mpg"], 1),
                        "expected_pts": round(stats["weighted_mpg"] * stats["ppm"], 1),
                        "expected_reb": round(stats["weighted_mpg"] * stats["rpm"], 1),
                    }
                projs[p["id"]] = info
        return {"projs": projs, "name_to_pid": name_to_pid}
    return _cached(("pre", league, game["event_id"]), TTL_PRE, _build)


# ── live prop lines (The Odds API via the engine's own fetcher) ───────────────
def fetch_live_lines(league: str, games: list[dict],
                     name_maps: dict[str, dict]) -> dict:
    """{espn_event_id: {pid: {pts_line, pts_over_odds, ...}}} — live odds."""
    if not games:
        return {}
    key = ("lines", league, tuple(sorted(g["event_id"] for g in games)))
    def _fetch():
        mod = _mod(league)
        try:
            return mod.fetch_odds_api_props(games, name_maps) or {}
        except Exception:
            return {}
    return _cached(key, TTL_LINES, _fetch)


# ── projection + EV math ──────────────────────────────────────────────────────
def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + erf((x - mu) / (sigma * sqrt(2.0))))


def _sd_full(prefix: str, exp_stat: float) -> float:
    """Full-game prop SD, same shapes as the /median tool."""
    if prefix == "pts":
        return max(4.0, 0.30 * exp_stat)
    if prefix == "reb":
        return max(1.8, 0.38 * exp_stat)
    # pts+reb: independent-ish sum
    return sqrt(_sd_full("pts", exp_stat * 0.75) ** 2
                + _sd_full("reb", exp_stat * 0.25) ** 2)


def remaining_minutes(exp_min: float, min_played: float,
                      elapsed: float, rem_game: float) -> float:
    """Expected minutes still to be played."""
    if rem_game <= 0:
        return 0.0
    base = exp_min - min_played
    if base <= 0:
        # already past the pregame minutes projection but the game is still
        # going: extrapolate from the share of game time played so far.
        share = min(1.0, min_played / elapsed) if elapsed > 1 else 0.0
        base = rem_game * share
    return max(0.0, min(base, rem_game * 0.98))


def prob_over(mean_final: float, sd: float, line: float) -> tuple[float, float]:
    """(p_over, p_push), push band on integer lines (continuity correction)."""
    sd = max(sd, 0.05)
    if abs(line - round(line)) < 1e-9:
        hi = _norm_cdf(line + 0.5, mean_final, sd)
        lo = _norm_cdf(line - 0.5, mean_final, sd)
        return 1.0 - hi, hi - lo
    return 1.0 - _norm_cdf(line, mean_final, sd), 0.0


def ev_pct(p_win: float, p_push: float, american) -> float | None:
    """EV% of a 1u bet at American odds, push-conditional."""
    if american is None:
        return None
    try:
        american = int(american)
    except (TypeError, ValueError):
        return None
    dec = 1.0 + (american / 100.0 if american > 0 else 100.0 / abs(american))
    denom = 1.0 - p_push
    if denom <= 0:
        return None
    p = p_win / denom
    return (p * (dec - 1.0) - (1.0 - p)) * 100.0


def build_game_rows(league: str, game: dict) -> dict:
    """All prop rows for one live game."""
    elapsed, rem_game = game_time(league, game["period"], game["clock"])
    box = fetch_box(league, game["event_id"])
    pre = pregame_context(league, game)
    lines = fetch_live_lines(league, [game],
                             {game["event_id"]: pre["name_to_pid"]}
                             ).get(game["event_id"], {})
    try:
        lead = abs(float(game["away"]["score"]) - float(game["home"]["score"]))
    except (TypeError, ValueError):
        lead = 0.0
    reg_min = LEAGUES[league]["reg_min"]

    rows = []
    for pid, b in box.items():
        info = pre["projs"].get(pid)
        if not info:
            continue
        pj = info["proj"]
        exp_min = pj.get("expected_min") or 0.0
        pl_lines = lines.get(pid, {})
        cur = {"pts": b["pts"], "reb": b["reb"], "pr": b["pts"] + b["reb"]}
        exp = {"pts": pj.get("expected_pts") or 0.0,
               "reb": pj.get("expected_reb") or 0.0}
        exp["pr"] = exp["pts"] + exp["reb"]
        rem_min = remaining_minutes(exp_min, b["min"], elapsed, rem_game)
        rem_min *= blowout_min_factor(lead, rem_game, reg_min, exp_min)

        for prefix, label in PROPS:
            line = pl_lines.get(f"{prefix}_line")
            exp_stat = exp[prefix]
            if exp_min <= 0 or exp_stat <= 0:
                continue
            rate = exp_stat / exp_min
            mean_final = cur[prefix] + rem_min * rate
            if line is None:
                continue                      # only show props with a live line
            sd = _sd_full(prefix, exp_stat) * sqrt(
                min(1.0, rem_min / exp_min)) if exp_min > 0 else 0.0
            if rem_min < MIN_REM_FOR_SD:
                sd = 0.05                     # outcome essentially decided
            p_model, p_push = prob_over(mean_final, sd, line)

            # Blend with the market's de-vigged consensus P(over) when the
            # engine's odds fetch produced one; pure model otherwise.
            mkt_p = pl_lines.get(f"{prefix}_mkt_p_over")
            if mkt_p is not None:
                p_over = W_MODEL * p_model + (1.0 - W_MODEL) * mkt_p * (1.0 - p_push)
            else:
                p_over = p_model
            p_over = max(P_CLAMP[0], min(P_CLAMP[1], p_over))

            over_odds = pl_lines.get(f"{prefix}_over_odds")
            under_odds = pl_lines.get(f"{prefix}_under_odds")
            ev_o = ev_pct(p_over, p_push, over_odds)
            ev_u = ev_pct(max(0.0, 1.0 - p_over - p_push), p_push, under_odds)
            rows.append({
                "pid": pid, "name": b["name"], "team": b["team_abbr"],
                "prop": label, "prefix": prefix,
                "min": b["min"], "cur": cur[prefix],
                "exp_min": exp_min, "exp_stat": exp_stat,
                "rem_min": rem_min, "proj_final": mean_final,
                "line": line,
                "over_odds": over_odds, "under_odds": under_odds,
                "over_book": pl_lines.get(f"{prefix}_over_book", ""),
                "under_book": pl_lines.get(f"{prefix}_under_book", ""),
                "books": pl_lines.get(f"{prefix}_book_count", 0),
                "p_model": p_model * 100.0, "p_mkt": (mkt_p * 100.0) if mkt_p is not None else None,
                "p_over": p_over * 100.0, "ev_over": ev_o, "ev_under": ev_u,
            })
    rows.sort(key=lambda r: max(r["ev_over"] or -99, r["ev_under"] or -99),
              reverse=True)
    return {
        "event_id": game["event_id"], "short_name": game["short_name"],
        "detail": game["detail"],
        "away": game["away"], "home": game["home"],
        "elapsed": elapsed, "rem_game": rem_game,
        "rows": rows, "n_lines": len(lines),
    }


def build_league(league: str) -> list[dict]:
    return [build_game_rows(league, g) for g in fetch_live_games(league)]


def top_plus_ev(games: list[dict]) -> list[dict]:
    """Flatten to the plus-EV sides across all live games, best first."""
    picks = []
    for g in games:
        for r in g["rows"]:
            for side, ev, odds, book in (("OVER", r["ev_over"], r["over_odds"], r["over_book"]),
                                         ("UNDER", r["ev_under"], r["under_odds"], r["under_book"])):
                if ev is not None and ev > 0:
                    picks.append({**r, "side": side, "ev": ev,
                                  "odds": odds, "book": book,
                                  "game": g["short_name"]})
    picks.sort(key=lambda p: p["ev"], reverse=True)
    return picks


# ── console ───────────────────────────────────────────────────────────────────
def print_console(league: str) -> None:
    cfg = LEAGUES[league]
    games = build_league(league)
    print("=" * 100)
    print(f"  In-Game Player Props — {cfg['label']} — {_today()}")
    print("=" * 100)
    if not games:
        print("  No live games right now.")
        return
    for g in games:
        print(f"\n  {g['short_name']}  |  {g['away']['abbreviation']} "
              f"{g['away']['score']} - {g['home']['score']} "
              f"{g['home']['abbreviation']}  |  {g['detail']}  "
              f"|  {g['rem_game']:.1f} min left")
        if not g["rows"]:
            print("    (no live prop lines found for this game)")
            continue
        print(f"    {'Player':<22} {'Prop':<9} {'Min':>4} {'Now':>4} "
              f"{'ExpM':>5} {'Proj':>5} {'Line':>5} {'Pmod':>5} {'Pmkt':>5} "
              f"{'Over':>6} {'EV%':>6} {'Under':>6} {'EV%':>6}")
        for r in g["rows"]:
            pmkt = f"{r['p_mkt']:.0f}" if r["p_mkt"] is not None else "-"
            print(f"    {r['name'][:22]:<22} {r['prop']:<9} {r['min']:>4.0f} "
                  f"{r['cur']:>4.0f} {r['exp_min']:>5.1f} {r['proj_final']:>5.1f} "
                  f"{r['line']:>5.1f} {r['p_model']:>5.0f} {pmkt:>5} "
                  f"{str(r['over_odds']):>6} "
                  f"{(('%+.1f' % r['ev_over']) if r['ev_over'] is not None else '-'):>6} "
                  f"{str(r['under_odds']):>6} "
                  f"{(('%+.1f' % r['ev_under']) if r['ev_under'] is not None else '-'):>6}")
    picks = top_plus_ev(games)
    print("\n  TOP +EV LIVE PROPS")
    if not picks:
        print("    (none at current lines)")
    for p in picks[:15]:
        print(f"    {p['name']:<22} {p['prop']:<9} {p['side']:<5} {p['line']:>5.1f} "
              f"@ {str(p['odds']):>5} ({p['book']})  EV {p['ev']:+.1f}%  "
              f"proj {p['proj_final']:.1f}  [{p['game']}]")
    print()


# ── web ───────────────────────────────────────────────────────────────────────
def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _odds_txt(odds) -> str:
    if odds is None:
        return "—"
    try:
        o = int(odds)
        return f"+{o}" if o > 0 else str(o)
    except (TypeError, ValueError):
        return str(odds)


def _ev_cell(ev) -> str:
    if ev is None:
        return '<td class="mut">—</td>'
    cls = "pos" if ev >= EV_HIGHLIGHT else ("up" if ev > 0 else "dn")
    return f'<td class="{cls}">{ev:+.1f}%</td>'


def render_page(league: str, games: list[dict], err: str | None) -> str:
    lg_opts = "".join(
        f'<option value="{k}"{" selected" if k == league else ""}>{v["label"]}</option>'
        for k, v in LEAGUES.items())

    body = ""
    if err:
        body += f'<div class="note err">{_esc(err)}</div>'

    picks = top_plus_ev(games)
    if picks:
        trs = "".join(
            f'<tr><td class="nm">{_esc(p["name"])}</td><td>{p["prop"]}</td>'
            f'<td class="side">{p["side"]}</td><td>{p["line"]:g}</td>'
            f'<td>{_odds_txt(p["odds"])} <span class="mut">{_esc(p["book"])}</span></td>'
            f'<td class="{"pos" if p["ev"] >= EV_HIGHLIGHT else "up"}">{p["ev"]:+.1f}%</td>'
            f'<td>{p["proj_final"]:.1f}</td><td class="mut">{_esc(p["game"])}</td></tr>'
            for p in picks[:15])
        body += ('<div class="panel"><h2>Top +EV live props</h2>'
                 '<table><tr><th>Player</th><th>Prop</th><th>Side</th><th>Line</th>'
                 '<th>Best odds</th><th>EV%</th><th>Proj final</th><th>Game</th></tr>'
                 f'{trs}</table>'
                 '<div class="note">EV at the best live price across books at the '
                 'consensus line; projection = current + remaining minutes × the '
                 'pregame props-engine rate.</div></div>')

    if not games and not err:
        body += ('<div class="panel note">No live games right now. This page only '
                 'projects games that are in progress — check back at tip-off.</div>')

    for g in games:
        hdr = (f'{_esc(g["away"]["abbreviation"])} {_esc(g["away"]["score"])} — '
               f'{_esc(g["home"]["score"])} {_esc(g["home"]["abbreviation"])}'
               f'<span class="mut"> · {_esc(g["detail"])} · '
               f'{g["rem_game"]:.1f} min left</span>')
        if not g["rows"]:
            body += (f'<div class="panel"><h2>{hdr}</h2>'
                     '<div class="note">No live prop lines posted for this game '
                     '(books pause props during play — refresh soon).</div></div>')
            continue
        trs = []
        for r in g["rows"]:
            trs.append(
                f'<tr><td class="nm">{_esc(r["name"])} '
                f'<span class="mut">{_esc(r["team"])}</span></td>'
                f'<td>{r["prop"]}</td>'
                f'<td>{r["min"]:.0f}</td><td>{r["cur"]:g}</td>'
                f'<td class="mut">{r["exp_stat"]:.1f} @ {r["exp_min"]:.0f}m</td>'
                f'<td>{r["rem_min"]:.1f}</td>'
                f'<td class="proj">{r["proj_final"]:.1f}</td>'
                f'<td>{r["line"]:g}</td>'
                f'<td class="mut">{r["p_model"]:.0f}·'
                f'{(("%.0f" % r["p_mkt"]) if r["p_mkt"] is not None else "—")}</td>'
                f'<td>{_odds_txt(r["over_odds"])} <span class="mut">{_esc(r["over_book"])}</span></td>'
                f'{_ev_cell(r["ev_over"])}'
                f'<td>{_odds_txt(r["under_odds"])} <span class="mut">{_esc(r["under_book"])}</span></td>'
                f'{_ev_cell(r["ev_under"])}</tr>')
        body += (f'<div class="panel"><h2>{hdr}</h2>'
                 '<table><tr><th>Player</th><th>Prop</th><th>Min</th><th>Now</th>'
                 '<th>Pregame</th><th>Rem min</th><th>Proj final</th><th>Live line</th>'
                 '<th>P(O) mod·mkt</th>'
                 '<th>Over</th><th>EV%</th><th>Under</th><th>EV%</th></tr>'
                 f'{"".join(trs)}</table></div>')

    return (PAGE.replace("{{LG_OPTS}}", lg_opts)
                .replace("{{BODY}}", body)
                .replace("{{ACCENT}}", ACCENT))


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<meta name="theme-color" content="#0f1419">
<title>In-Game Prop Projections</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--mut:#8a95a5;
--accent:{{ACCENT}};--up:#4caf50;--dn:#e57373;}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);margin:0;padding:22px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:23px;margin:0 0 3px}h1 span{color:var(--accent)}
h2{font-size:16px;font-weight:600;margin:0 0 12px}
.sub{color:var(--mut);font-size:13.5px;margin-bottom:18px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;
padding:16px 18px;margin-bottom:16px;overflow-x:auto}
.toprow{display:flex;gap:12px;flex-wrap:wrap;align-items:end;background:var(--panel);
border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:16px}
label.f{display:block;color:var(--mut);font-size:11px;text-transform:uppercase;
letter-spacing:.06em;margin-bottom:5px}
select{padding:9px 11px;background:#0f1419;color:var(--text);
border:1px solid var(--border);border-radius:6px;font-size:14px;min-width:120px}
select:focus{outline:none;border-color:var(--accent)}
.btn{padding:9px 18px;background:var(--accent);color:#0f1419;border:0;border-radius:6px;
font-size:14px;font-weight:600;cursor:pointer}
.btn:hover{filter:brightness(1.12)}
table{width:100%;border-collapse:collapse;font-size:13.5px;white-space:nowrap}
th,td{padding:7px 8px;text-align:right;border-bottom:1px solid var(--border)}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
td.nm{font-weight:600}
td.proj{color:var(--accent);font-weight:600}
td.side{font-weight:700}
.pos{color:var(--up);font-weight:700;background:rgba(76,175,80,.10)}
.up{color:var(--up)}.dn{color:var(--dn)}
.mut{color:var(--mut)}
.note{color:var(--mut);font-size:12.5px;margin-top:10px;line-height:1.5}
.note.err{color:#ffb4a2}
</style></head><body><div class=wrap>
<h1>In-Game <span>Prop Projections</span></h1>
<div class=sub>Live box score + the pregame props-engine forecast → each player's projected final line, versus the live multi-book prop market. Auto-refreshes every 60s. First load of a live game takes ~15-30s while pregame projections build (then cached).</div>
<form method=get>
<div class=toprow>
  <div><label class=f>League</label><select name=league onchange="this.form.submit()">{{LG_OPTS}}</select></div>
  <button type=submit class=btn>Refresh</button>
</div>
</form>
{{BODY}}
<div class=note style="text-align:center;margin-top:20px">
Projected final = current + remaining minutes × pregame rate (rate = engine ExpStat / ExpMin;
remaining minutes = ExpMin − played, capped by game clock, discounted for regulars late in
blowouts). P(over) from a Normal on the remaining production with the /median SD shapes,
scaled by √(remaining share). EV uses a 40/60 blend of the model's P(over) with the market's
de-vigged consensus (P(O) mod·mkt column) — a pure-model live EV runs unrealistically hot.
Lines/odds: The Odds API consensus line, best price per side across DK/FD/MGM/Caesars/BR/
ESPN/Fanatics; books pause live props during play, so lines can briefly disappear.
</div>
</div></body></html>"""

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="6" fill="#0f1419"/>'
           '<circle cx="12" cy="20" r="6" fill="none" stroke="#9ccc65" stroke-width="2.5"/>'
           '<path d="M12 20 L24 8 M24 8 h-6 M24 8 v6" stroke="#9ccc65" '
           'stroke-width="2.5" fill="none"/></svg>')


def create_app():
    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def index():
        league = request.values.get("league", DEFAULT_LEAGUE)
        if league not in LEAGUES:
            league = DEFAULT_LEAGUE
        err, games = None, []
        try:
            games = build_league(league)
        except Exception as e:
            err = f"Could not build projections: {e}"
        return render_page(league, games, err)

    @app.route("/favicon.svg")
    def favicon():
        return (FAVICON, 200, {"Content-Type": "image/svg+xml"})

    return app


app = create_app()


def main():
    ap = argparse.ArgumentParser(description="In-game player prop projections + live EV")
    ap.add_argument("--once", action="store_true", help="print to console and exit")
    ap.add_argument("--league", choices=sorted(LEAGUES), default=None,
                    help="league for --once (default: all)")
    ap.add_argument("--port", type=int, default=5021)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.once:
        for lg in ([args.league] if args.league else list(LEAGUES)):
            print_console(lg)
        return
    print(f"In-Game Props -> http://{args.host}:{args.port}")
    create_app().run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
