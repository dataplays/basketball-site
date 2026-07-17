#!/usr/bin/env python3
"""
inplay_vacuum_props.py — Live Usage-Vacuum Prop Projections.
============================================================
For every LIVE game: detect players who are OUT of the game and NOT coming
back (fouled out, ejected, or ruled out on the play-by-play), re-run the
props engine's redistribution with those players added to the OUT set (the
/vacuum approach), and feed the ADJUSTED expected minutes/rates into the
in-game prop projection (the /inplay approach) to surface +EV live plays
the market may not have re-priced yet.

The chain
---------
1. DETECT — scan the live ESPN box + play-by-play:
     * HARD (auto-applied): fouled out (PF >= 6), ejected, or PBP text like
       "will not return" / "ruled out" / "out for the remainder".
     * SOFT (flagged, not auto-applied): "leaves the game" / "to the locker
       room" — the player may return, so you decide via checkbox.
   Anyone can also be manually marked out (broadcast/social news beats the
   feed) — same checkbox.
2. REDISTRIBUTE — exactly like /vacuum: call the actual props engine's
   project_player() twice per player (baseline = real pregame outs only;
   adjusted = + the ruled-out players) so the minutes / usage / rebound
   redistribution is IDENTICAL to the nightly props model.
3. PROJECT LIVE — like /inplay, but the redistribution boost only applies
   to the REMAINING game time:
     rem_min = baseline_rem + (adjMin − baseMin) × (rem_game / reg_min)
     projected final = current + rem_min × adjusted per-minute rate
   Ruled-out players are projected at their CURRENT stat (they aren't
   coming back) — their UNDERs light up while books still post a line.
4. EV — live multi-book lines via the engine's own Odds API fetcher.
   Standard rows use the /inplay 40/60 model/market blend; rows where the
   vacuum shifted the projection materially (|Δmin| ≥ 1.5) use 60/40
   (the model is carrying information the market may not have priced),
   and ruled-out player rows are pure model (the info is certain).

Run:
    py -3 inplay_vacuum_props.py                 # web -> http://localhost:5022
    py -3 inplay_vacuum_props.py --once          # console print (all leagues)
    py -3 inplay_vacuum_props.py --once --league wnba
Module-level `app` + relative form action, so it is mountable later.
"""

from __future__ import annotations

import argparse
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import sqrt

from flask import Flask, request

import inplay_props as ip          # shared live-props machinery (/inplay)

LEAGUES = ip.LEAGUES               # same league configs / engine modules
DEFAULT_LEAGUE = ip.DEFAULT_LEAGUE
ACCENT = "#ffb74d"

FOUL_OUT_LIMIT = 6                 # WNBA + NBA: 6 personal fouls
MATERIAL_DMIN = 1.5                # |full-game Δmin| >= this -> boosted model weight
W_MODEL_ADJ = 0.60                 # model weight on materially-adjusted rows
TTL_SUMMARY = 25
TTL_CTX = 1800

HARD_PATTERNS = (
    "will not return", "out for the remainder", "out for the game",
    "ruled out", "ejected", "ejection", "fouls out", "fouled out",
)
SOFT_PATTERNS = (
    "leaves the game", "left the game", "to the locker room",
    "taken to the locker", "helped off", "carried off",
)


# ── live summary (box w/ PF + play-by-play) ──────────────────────────────────
def fetch_summary(league: str, event_id: str) -> dict:
    def _fetch():
        mod = ip._mod(league)
        return mod.fetch_json(f"{mod.ESPN_SUMMARY}?event={event_id}") or {}
    return ip._cached(("vsum", league, event_id), TTL_SUMMARY, _fetch)


def box_from_summary(summary: dict) -> dict:
    """{pid: {name, team_abbr, min, pts, reb, pf}} — like /inplay's box but
    with personal fouls (for foul-out detection)."""
    out = {}
    for team in (summary.get("boxscore", {}) or {}).get("players", []):
        abbr = (team.get("team", {}) or {}).get("abbreviation", "")
        for block in team.get("statistics", [])[:1]:
            labels = block.get("labels") or block.get("names") or []
            try:
                i_min = labels.index("MIN")
                i_pts = labels.index("PTS")
                i_reb = labels.index("REB")
            except ValueError:
                continue
            i_pf = labels.index("PF") if "PF" in labels else None
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
                    "pf": _num(stats[i_pf]) if (i_pf is not None
                                                and len(stats) > i_pf) else 0.0,
                }
    return out


def detect_outs(summary: dict, box: dict) -> tuple[dict, dict]:
    """(hard, soft) -> {pid: reason}. HARD = definitely done for the game
    (auto-applied); SOFT = left the game, may return (user decides)."""
    hard, soft = {}, {}
    for pid, b in box.items():
        if b["pf"] >= FOUL_OUT_LIMIT:
            hard[pid] = f"fouled out ({int(b['pf'])} PF)"

    for play in summary.get("plays") or []:
        text = play.get("text") or ""
        tl = text.lower()
        is_hard = any(k in tl for k in HARD_PATTERNS)
        is_soft = any(k in tl for k in SOFT_PATTERNS)
        if not (is_hard or is_soft):
            continue
        pids = []
        for part in play.get("participants") or []:
            aid = str(((part.get("athlete") or {}).get("id")) or "")
            if aid in box:
                pids.append(aid)
        if not pids:                            # fall back to name matching
            for pid, b in box.items():
                if b["name"] and b["name"].lower() in tl:
                    pids.append(pid)
        reason = text if len(text) <= 80 else text[:77] + "..."
        for pid in pids:
            if is_hard:
                hard.setdefault(pid, reason)
            elif pid not in hard:
                soft.setdefault(pid, reason)
    return hard, soft


# ── per-game raw context (both teams, /vacuum-grade data) ────────────────────
def live_context(league: str, game: dict) -> dict:
    """Roster + stats + game logs + ratings for BOTH teams of a live game,
    retaining the raw per-player data so we can re-project with any OUT set.
    Heavy (~15-30s first load), cached 30 min."""
    def _build():
        mod = ip._mod(league)
        ratings = mod.load_team_ratings()
        if ratings:
            mod.LEAGUE_AVG_DRTG = sum(r["de"] for r in ratings.values()) / len(ratings)
            mod.LEAGUE_AVG_PACE = sum(r["pace"] for r in ratings.values()) / len(ratings)
        try:
            spread = float(game.get("spread")) if game.get("spread") is not None else None
        except (TypeError, ValueError):
            spread = None
        injuries = mod.fetch_injuries(game["event_id"])
        gtd_statuses = set(getattr(mod, "GTD_STATUSES", ()))
        date_str = ip._today()

        sides, name_to_pid = {}, {}
        for side, opp_side in (("home", "away"), ("away", "home")):
            team, opp = game[side], game[opp_side]
            team_key = mod.resolve_team_name(team["name"], ratings)
            opp_key = mod.resolve_team_name(opp["name"], ratings)
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

            players = []
            for p in roster:
                pid = p["id"]
                gl = logs.get(pid, [])
                stats = mod.compute_player_stats(gl)
                status = (injuries.get(pid) or "").lower()
                players.append({
                    "player": p, "stats": stats, "status": status,
                    "is_gtd": status in gtd_statuses,
                    "is_out": status in ("out", "doubtful"),
                    "events": {g["event_id"] for g in gl if g.get("event_id")},
                    "recent_events": [g.get("event_id", "") for g in gl[:15]],
                })
                name_to_pid[mod._normalize_name(p["name"])] = pid
            sides[side] = {
                "players": players, "is_home": side == "home",
                "team_r": ratings.get(team_key, {}) if team_key else {},
                "opp_r": ratings.get(opp_key, {}) if opp_key else {},
                "b2b": b2b, "spread": spread,
                "neutral": game.get("neutral_site", False),
            }
        return {"sides": sides, "name_to_pid": name_to_pid}
    return ip._cached(("vctx", league, game["event_id"]), TTL_CTX, _build)


def _project_side(mod, sd: dict, out_ids: set) -> dict:
    """{pid: proj} for one team given an OUT set — same call the nightly
    props engine and /vacuum make."""
    injured = [{"id": p["player"]["id"], "name": p["player"]["name"],
                "position": p["player"].get("position", ""),
                "stats": p["stats"], "p_out": 1.0, "events": p["events"]}
               for p in sd["players"]
               if p["player"]["id"] in out_ids and p["stats"]]
    projs = {}
    for p in sd["players"]:
        pid = p["player"]["id"]
        if pid in out_ids or not p["stats"]:
            continue
        try:
            proj = mod.project_player(
                player=p["player"], stats=p["stats"],
                team_ratings=sd["team_r"], opp_ratings=sd["opp_r"],
                is_home=sd["is_home"], spread=sd["spread"], team_b2b=sd["b2b"],
                injured_teammates=[tm for tm in injured if tm["id"] != pid],
                player_recent_events=p["recent_events"],
                neutral_site=sd["neutral"],
            )
        except Exception:
            proj = None
        if proj is None:
            s = p["stats"]
            proj = {"expected_min": round(s["weighted_mpg"], 1),
                    "expected_pts": round(s["weighted_mpg"] * s["ppm"], 1),
                    "expected_reb": round(s["weighted_mpg"] * s["rpm"], 1)}
        projs[pid] = proj
    return projs


# ── the live vacuum-adjusted projection ──────────────────────────────────────
def build_game(league: str, game: dict, user_out: set | None = None) -> dict:
    """One live game: detection + baseline/adjusted projections + EV rows.
    user_out=None -> auto (hard detections applied); a set (possibly empty)
    -> the user's checkbox state governs exactly who is out."""
    mod = ip._mod(league)
    eid = game["event_id"]
    elapsed, rem_game = ip.game_time(league, game["period"], game["clock"])
    reg_min = LEAGUES[league]["reg_min"]
    summary = fetch_summary(league, eid)
    box = box_from_summary(summary)
    hard, soft = detect_outs(summary, box)
    ctx = live_context(league, game)

    extra_out = set(user_out) if user_out is not None else set(hard)
    extra_out &= set(box)                      # only players in this game

    base_projs, adj_projs = {}, {}
    side_pids = {}
    for side, sd in ctx["sides"].items():
        pids = {p["player"]["id"] for p in sd["players"]}
        side_pids[side] = pids
        base_out = {p["player"]["id"] for p in sd["players"] if p["is_out"]}
        adj_out = base_out | (extra_out & pids)
        base_projs.update(_project_side(mod, sd, base_out))
        # skip the second (heavier) pass when nothing changed for this team
        if adj_out == base_out:
            adj_projs.update({pid: base_projs[pid] for pid in pids
                              if pid in base_projs})
        else:
            adj_projs.update(_project_side(mod, sd, adj_out))

    lines = ip.fetch_live_lines(league, [game], {eid: ctx["name_to_pid"]}
                                ).get(eid, {})
    try:
        lead = abs(float(game["away"]["score"]) - float(game["home"]["score"]))
    except (TypeError, ValueError):
        lead = 0.0

    rows = []
    for pid, b in box.items():
        pl_lines = lines.get(pid, {})
        cur = {"pts": b["pts"], "reb": b["reb"]}

        if pid in extra_out:
            # Ruled out mid-game: final = current. Pure model (the info is
            # certain), no market blend.
            for prefix, label in ip.PROPS:
                line = pl_lines.get(f"{prefix}_line")
                if line is None:
                    continue
                p_model, p_push = ip.prob_over(cur[prefix], 0.05, line)
                p_over = max(ip.P_CLAMP[0], min(ip.P_CLAMP[1], p_model))
                over_odds = pl_lines.get(f"{prefix}_over_odds")
                under_odds = pl_lines.get(f"{prefix}_under_odds")
                rows.append({
                    "pid": pid, "name": b["name"], "team": b["team_abbr"],
                    "prop": label, "prefix": prefix, "min": b["min"],
                    "cur": cur[prefix], "exp_min": 0.0, "exp_stat": cur[prefix],
                    "rem_min": 0.0, "proj_final": float(cur[prefix]),
                    "d_min": None, "flag": "OUT",
                    "line": line, "over_odds": over_odds,
                    "under_odds": under_odds,
                    "over_book": pl_lines.get(f"{prefix}_over_book", ""),
                    "under_book": pl_lines.get(f"{prefix}_under_book", ""),
                    "books": pl_lines.get(f"{prefix}_book_count", 0),
                    "p_model": p_model * 100.0, "p_mkt": None,
                    "p_over": p_over * 100.0,
                    "ev_over": ip.ev_pct(p_over, p_push, over_odds),
                    "ev_under": ip.ev_pct(max(0.0, 1.0 - p_over - p_push),
                                          p_push, under_odds),
                })
            continue

        adj = adj_projs.get(pid)
        if not adj:
            continue
        base = base_projs.get(pid, adj)
        base_min = base.get("expected_min") or 0.0
        adj_min = adj.get("expected_min") or 0.0
        if adj_min <= 0:
            continue

        # Boost applies only to the REMAINING game time: the full-game Δmin
        # assumes the teammate was out all game, so scale by rem/reg.
        rem_base = ip.remaining_minutes(base_min, b["min"], elapsed, rem_game)
        boost = (adj_min - base_min) * (rem_game / reg_min) if reg_min else 0.0
        rem_min = max(0.0, min(rem_base + boost, rem_game * 0.98))
        rem_min *= ip.blowout_min_factor(lead, rem_game, reg_min, adj_min)
        d_min = adj_min - base_min

        exp = {"pts": adj.get("expected_pts") or 0.0,
               "reb": adj.get("expected_reb") or 0.0}
        for prefix, label in ip.PROPS:
            line = pl_lines.get(f"{prefix}_line")
            exp_stat = exp[prefix]
            if line is None or exp_stat <= 0:
                continue
            rate = exp_stat / adj_min
            mean_final = cur[prefix] + rem_min * rate
            sd = ip._sd_full(prefix, exp_stat) * sqrt(min(1.0, rem_min / adj_min))
            if rem_min < ip.MIN_REM_FOR_SD:
                sd = 0.05
            p_model, p_push = ip.prob_over(mean_final, sd, line)

            w = W_MODEL_ADJ if abs(d_min) >= MATERIAL_DMIN else ip.W_MODEL
            mkt_p = pl_lines.get(f"{prefix}_mkt_p_over")
            if mkt_p is not None:
                p_over = w * p_model + (1.0 - w) * mkt_p * (1.0 - p_push)
            else:
                p_over = p_model
            p_over = max(ip.P_CLAMP[0], min(ip.P_CLAMP[1], p_over))

            over_odds = pl_lines.get(f"{prefix}_over_odds")
            under_odds = pl_lines.get(f"{prefix}_under_odds")
            rows.append({
                "pid": pid, "name": b["name"], "team": b["team_abbr"],
                "prop": label, "prefix": prefix, "min": b["min"],
                "cur": cur[prefix], "exp_min": adj_min, "exp_stat": exp_stat,
                "rem_min": rem_min, "proj_final": mean_final,
                "d_min": d_min, "flag": "",
                "line": line, "over_odds": over_odds, "under_odds": under_odds,
                "over_book": pl_lines.get(f"{prefix}_over_book", ""),
                "under_book": pl_lines.get(f"{prefix}_under_book", ""),
                "books": pl_lines.get(f"{prefix}_book_count", 0),
                "p_model": p_model * 100.0,
                "p_mkt": (mkt_p * 100.0) if mkt_p is not None else None,
                "p_over": p_over * 100.0,
                "ev_over": ip.ev_pct(p_over, p_push, over_odds),
                "ev_under": ip.ev_pct(max(0.0, 1.0 - p_over - p_push),
                                      p_push, under_odds),
            })
    rows.sort(key=lambda r: max(r["ev_over"] or -99, r["ev_under"] or -99),
              reverse=True)

    # checkbox roster: everyone who has appeared, sorted by minutes played
    roster_rows = sorted(box.items(), key=lambda kv: -kv[1]["min"])
    return {
        "event_id": eid, "short_name": game["short_name"],
        "detail": game["detail"], "away": game["away"], "home": game["home"],
        "elapsed": elapsed, "rem_game": rem_game,
        "rows": rows, "n_lines": len(lines),
        "hard": hard, "soft": soft, "extra_out": extra_out,
        "roster_rows": roster_rows,
    }


def build_league(league: str, form: dict | None = None) -> list[dict]:
    """form: {event_id: set(pids)} for games whose checkbox form was
    submitted; games absent from the map use auto-detection."""
    form = form or {}
    return [build_game(league, g, form.get(g["event_id"]))
            for g in ip.fetch_live_games(league)]


def top_plus_ev(games: list[dict]) -> list[dict]:
    picks = []
    for g in games:
        for r in g["rows"]:
            for side, ev, odds, book in (
                    ("OVER", r["ev_over"], r["over_odds"], r["over_book"]),
                    ("UNDER", r["ev_under"], r["under_odds"], r["under_book"])):
                if ev is not None and ev > 0:
                    picks.append({**r, "side": side, "ev": ev, "odds": odds,
                                  "book": book, "game": g["short_name"],
                                  "event_id": g["event_id"]})
    picks.sort(key=lambda p: p["ev"], reverse=True)
    return picks


# ── console ──────────────────────────────────────────────────────────────────
def print_console(league: str) -> None:
    cfg = LEAGUES[league]
    games = build_league(league)
    print("=" * 100)
    print(f"  Live Vacuum Props — {cfg['label']} — {ip._today()}")
    print("=" * 100)
    if not games:
        print("  No live games right now.")
        return
    for g in games:
        print(f"\n  {g['short_name']}  |  {g['away']['abbreviation']} "
              f"{g['away']['score']} - {g['home']['score']} "
              f"{g['home']['abbreviation']}  |  {g['detail']}  "
              f"|  {g['rem_game']:.1f} min left")
        if g["hard"]:
            for pid, reason in g["hard"].items():
                nm = next((b["name"] for p, b in g["roster_rows"] if p == pid), pid)
                print(f"    OUT (auto): {nm} — {reason}")
        if g["soft"]:
            for pid, reason in g["soft"].items():
                if pid in g["extra_out"]:
                    continue
                nm = next((b["name"] for p, b in g["roster_rows"] if p == pid), pid)
                print(f"    left game? (not applied): {nm} — {reason}")
        if not (g["hard"] or g["soft"]):
            print("    no in-game exits detected")
        for r in g["rows"][:10]:
            dm = (f"  adjMin {r['d_min']:+.1f}" if r["d_min"] else
                  ("  [OUT]" if r["flag"] else ""))
            print(f"    {r['name'][:22]:<22} {r['prop']:<9} now {r['cur']:>3.0f} "
                  f"proj {r['proj_final']:>5.1f} line {r['line']:>5.1f} "
                  f"O {str(r['over_odds']):>5} "
                  f"{(('%+.1f%%' % r['ev_over']) if r['ev_over'] is not None else '-'):>7} "
                  f"U {str(r['under_odds']):>5} "
                  f"{(('%+.1f%%' % r['ev_under']) if r['ev_under'] is not None else '-'):>7}"
                  f"{dm}")
    picks = top_plus_ev(games)
    print("\n  TOP +EV (vacuum-adjusted)")
    if not picks:
        print("    (none at current lines)")
    for p in picks[:15]:
        tag = " [OUT]" if p["flag"] else (
            f" [Δ{p['d_min']:+.1f}m]" if p["d_min"] and abs(p["d_min"]) >= 1 else "")
        print(f"    {p['name']:<22} {p['prop']:<9} {p['side']:<5} {p['line']:>5.1f} "
              f"@ {str(p['odds']):>5} ({p['book']})  EV {p['ev']:+.1f}%  "
              f"proj {p['proj_final']:.1f}{tag}  [{p['game']}]")
    print()


# ── web ──────────────────────────────────────────────────────────────────────
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
    cls = "pos" if ev >= ip.EV_HIGHLIGHT else ("up" if ev > 0 else "dn")
    return f'<td class="{cls}">{ev:+.1f}%</td>'


def _topbar(picks: list[dict]) -> str:
    if not picks:
        return ""
    chips = []
    for p in picks[:8]:
        hot = " hot" if p["ev"] >= ip.EV_HIGHLIGHT else ""
        tag = " OUT" if p["flag"] else ""
        chips.append(
            f'<a class="chip{hot}" href="#g{_esc(p["event_id"])}">'
            f'<b>{_esc(ip._short_name(p["name"]))}</b>{tag} '
            f'{ip._PROP_SHORT.get(p["prefix"], p["prop"])} '
            f'{"O" if p["side"] == "OVER" else "U"} {p["line"]:g} '
            f'<span class="odds">{_odds_txt(p["odds"])} {_esc(p["book"])}</span> '
            f'<span class="ev">{p["ev"]:+.1f}%</span></a>')
    return ('<div class="topbar"><span class="tb-label">TOP PLAYS</span>'
            + "".join(chips) + "</div>")


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
            f'<tr><td class="nm">{_esc(p["name"])}'
            f'{"<span class=outchip>OUT</span>" if p["flag"] else ""}</td>'
            f'<td>{p["prop"]}</td>'
            f'<td class="side">{p["side"]}</td><td>{p["line"]:g}</td>'
            f'<td>{_odds_txt(p["odds"])} <span class="mut">{_esc(p["book"])}</span></td>'
            f'<td class="{"pos" if p["ev"] >= ip.EV_HIGHLIGHT else "up"}">{p["ev"]:+.1f}%</td>'
            f'<td>{p["proj_final"]:.1f}</td>'
            f'<td>{(("%+.1f" % p["d_min"]) if p["d_min"] else "·")}</td>'
            f'<td class="mut">{_esc(p["game"])}</td></tr>'
            for p in picks[:15])
        body += ('<div class="panel"><h2>Top +EV live props (vacuum-adjusted)</h2>'
                 '<table><tr><th>Player</th><th>Prop</th><th>Side</th><th>Line</th>'
                 '<th>Best odds</th><th>EV%</th><th>Proj final</th><th>ΔMin</th>'
                 '<th>Game</th></tr>'
                 f'{trs}</table>'
                 '<div class="note">ΔMin = full-game expected-minutes change from the '
                 'redistribution (applied to remaining time only). OUT rows project the '
                 'player\'s CURRENT stat as final.</div></div>')

    if not games and not err:
        body += ('<div class="panel note">No live games right now. This page only '
                 'projects games that are in progress — check back at tip-off.</div>')

    for g in games:
        hdr = (f'{_esc(g["away"]["abbreviation"])} {_esc(g["away"]["score"])} — '
               f'{_esc(g["home"]["score"])} {_esc(g["home"]["abbreviation"])}'
               f'<span class="mut"> · {_esc(g["detail"])} · '
               f'{g["rem_game"]:.1f} min left</span>')

        # out-not-returning checkbox panel
        checks = []
        for pid, b in g["roster_rows"]:
            checked = " checked" if pid in g["extra_out"] else ""
            chip = ""
            if pid in g["hard"]:
                chip = f' <span class="hardchip" title="{_esc(g["hard"][pid])}">auto</span>'
            elif pid in g["soft"]:
                chip = f' <span class="softchip" title="{_esc(g["soft"][pid])}">left game?</span>'
            checks.append(
                f'<label class="pchk"><input type="checkbox" name="out" '
                f'value="{_esc(pid)}"{checked}>'
                f'{_esc(b["name"])} <span class="mut">{_esc(b["team_abbr"])} · '
                f'{b["min"]:.0f}m</span>{chip}</label>')
        det = []
        for pid, reason in g["hard"].items():
            nm = next((b["name"] for p, b in g["roster_rows"] if p == pid), pid)
            det.append(f'<b>{_esc(nm)}</b> — {_esc(reason)}')
        det_note = (f'<div class="note">Auto-detected out: {"; ".join(det)}</div>'
                    if det else "")

        body += (f'<div class="panel" id="g{_esc(g["event_id"])}"><h2>{hdr}</h2>'
                 f'<div class="lbl">Out — not returning (check to apply the '
                 f'minutes redistribution)</div>'
                 f'<input type="hidden" name="f" value="{_esc(g["event_id"])}">'
                 f'<div class="checks">{"".join(checks)}</div>{det_note}'
                 f'<button type="submit" class="btn">Recompute</button>')

        if not g["rows"]:
            body += ('<div class="note">No live prop lines posted for this game '
                     '(books pause props during play — refresh soon).</div></div>')
            continue
        trs = []
        for r in g["rows"]:
            nm_extra = ('<span class="outchip">OUT</span>' if r["flag"] else "")
            trs.append(
                f'<tr><td class="nm">{_esc(r["name"])}{nm_extra} '
                f'<span class="mut">{_esc(r["team"])}</span></td>'
                f'<td>{r["prop"]}</td>'
                f'<td>{r["min"]:.0f}</td><td>{r["cur"]:g}</td>'
                f'<td class="mut">{r["exp_stat"]:.1f} @ {r["exp_min"]:.0f}m</td>'
                f'<td>{(("%+.1f" % r["d_min"]) if r["d_min"] else "·")}</td>'
                f'<td>{r["rem_min"]:.1f}</td>'
                f'<td class="proj">{r["proj_final"]:.1f}</td>'
                f'<td>{r["line"]:g}</td>'
                f'<td class="mut">{r["p_model"]:.0f}·'
                f'{(("%.0f" % r["p_mkt"]) if r["p_mkt"] is not None else "—")}</td>'
                f'<td>{_odds_txt(r["over_odds"])} <span class="mut">{_esc(r["over_book"])}</span></td>'
                f'{_ev_cell(r["ev_over"])}'
                f'<td>{_odds_txt(r["under_odds"])} <span class="mut">{_esc(r["under_book"])}</span></td>'
                f'{_ev_cell(r["ev_under"])}</tr>')
        body += ('<table><tr><th>Player</th><th>Prop</th><th>Min</th><th>Now</th>'
                 '<th>Adj pregame</th><th>ΔMin</th><th>Rem min</th><th>Proj final</th>'
                 '<th>Live line</th><th>P(O) mod·mkt</th>'
                 '<th>Over</th><th>EV%</th><th>Under</th><th>EV%</th></tr>'
                 f'{"".join(trs)}</table></div>')

    return (PAGE.replace("{{LG_OPTS}}", lg_opts)
                .replace("{{TOPBAR}}", _topbar(picks))
                .replace("{{BODY}}", body)
                .replace("{{ACCENT}}", ACCENT))


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<meta name="theme-color" content="#0f1419">
<title>Live Vacuum Props</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--mut:#8a95a5;
--accent:{{ACCENT}};--up:#4caf50;--dn:#e57373;--gtd:#f0b429;}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);margin:0;padding:22px}
.wrap{max-width:1120px;margin:0 auto}
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
font-size:14px;font-weight:600;cursor:pointer;margin:10px 0 14px}
.btn:hover{filter:brightness(1.12)}
.lbl{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em;
margin-bottom:10px}
.checks{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:7px 14px}
.pchk{display:flex;align-items:center;gap:7px;font-size:13.5px;cursor:pointer;
white-space:nowrap}
.pchk input{width:15px;height:15px;accent-color:var(--accent)}
.hardchip{color:#e57373;font-size:10px;font-weight:700;border:1px solid #e57373;
border-radius:4px;padding:1px 4px}
.softchip{color:var(--gtd);font-size:10px;font-weight:700;border:1px solid var(--gtd);
border-radius:4px;padding:1px 4px}
.outchip{color:#e57373;font-size:10px;font-weight:800;border:1px solid #e57373;
border-radius:4px;padding:1px 4px;margin-left:5px;vertical-align:middle}
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
.topbar{position:sticky;top:0;z-index:20;display:flex;gap:8px;align-items:center;
overflow-x:auto;background:rgba(15,20,25,.97);border:1px solid var(--border);
border-radius:10px;padding:10px 12px;margin-bottom:14px}
.tb-label{color:var(--accent);font-size:11px;font-weight:800;letter-spacing:.08em;
white-space:nowrap;flex:0 0 auto}
.chip{display:inline-flex;gap:6px;align-items:center;white-space:nowrap;background:#151b23;
border:1px solid var(--border);border-radius:999px;padding:6px 12px;font-size:12.5px;
color:var(--text);text-decoration:none;flex:0 0 auto}
.chip:hover{border-color:var(--accent)}
.chip .ev{color:var(--up);font-weight:700}
.chip .odds{color:var(--mut)}
.chip.hot{border-color:rgba(76,175,80,.55);background:rgba(76,175,80,.10)}
</style></head><body><div class=wrap>
{{TOPBAR}}
<h1>Live <span>Vacuum</span> Props</h1>
<div class=sub>Detect in-game exits (foul-outs, ejections, ruled-out injuries), re-run
the props engine's minutes redistribution for everyone still playing, and project each
live prop at the ADJUSTED remaining minutes — versus the live multi-book market.
Auto-refreshes every 60s. First load of a live game takes ~15-30s while the pregame
context builds (then cached).</div>
<form method=get>
<div class=toprow>
  <div><label class=f>League</label><select name=league onchange="this.form.submit()">{{LG_OPTS}}</select></div>
  <button type=submit class=btn style="margin:0">Refresh</button>
</div>
{{BODY}}
</form>
<div class=note style="text-align:center;margin-top:20px">
Baseline = the pregame props-engine projection with real injury outs. Checking a player
re-projects both teams with them added to the OUT set (same redistribution as /vacuum);
the full-game Δmin is applied to the remaining game time only:
rem = baseRem + Δmin × (remGame/regMin), production at the adjusted per-minute rate.
Foul-outs (6 PF) and ejections auto-apply; "left game?" flags are yours to judge.
EV blend: 40/60 model/market normally, 60/40 when |Δmin| ≥ 1.5 (the model carries
un-priced info), pure model for ruled-out players. Same Odds API live lines as /inplay.
</div>
</div></body></html>"""

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="6" fill="#0f1419"/>'
           '<path d="M9 6 h14 l-5 8 v7 l-4 3 v-10 z" fill="#ffb74d"/>'
           '<circle cx="24" cy="24" r="5" fill="none" stroke="#ffb74d" '
           'stroke-width="2"/></svg>')


def create_app():
    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def index():
        league = request.values.get("league", DEFAULT_LEAGUE)
        if league not in LEAGUES:
            league = DEFAULT_LEAGUE
        # games whose form was submitted (hidden "f" inputs) use the exact
        # checkbox state; others use auto-detection.
        submitted = set(request.values.getlist("f"))
        checked = set(request.values.getlist("out"))
        form = {eid: checked for eid in submitted}
        err, games = None, []
        try:
            games = build_league(league, form)
        except Exception as e:
            err = f"Could not build projections: {e}"
        return render_page(league, games, err)

    @app.route("/favicon.svg")
    def favicon():
        return (FAVICON, 200, {"Content-Type": "image/svg+xml"})

    return app


app = create_app()


def main():
    ap = argparse.ArgumentParser(
        description="Live vacuum-adjusted in-game prop projections")
    ap.add_argument("--once", action="store_true", help="print to console and exit")
    ap.add_argument("--league", choices=sorted(LEAGUES), default=None,
                    help="league for --once (default: all)")
    ap.add_argument("--port", type=int, default=5022)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.once:
        for lg in ([args.league] if args.league else list(LEAGUES)):
            print_console(lg)
        return
    print(f"Live Vacuum Props -> http://{args.host}:{args.port}")
    create_app().run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
