#!/usr/bin/env python3
"""
usage_vacuum.py — "Usage Vacuum" explorer (/vacuum).
====================================================
React to injury news in seconds: pick a team playing tonight, sit one or more
players, and see who absorbs the vacated minutes / usage / rebounds and what
their projected lines become — WITHOUT re-running the whole props-PDF pipeline.

Why it's faithful (not a parallel model)
----------------------------------------
It imports the actual props engine (`wnba_props_projections` /
`nba_props_projections`) and calls its real `project_player()` with a
hypothetical OUT set. So the minutes redistribution, usage boost, and rebound
reallocation are IDENTICAL to what the nightly props projections would produce
— this is just an interactive front-end on that same engine, with no Odds API
call (it needs ratings + schedule + rosters + game logs only, all from ESPN).

How it works
------------
* Baseline = the team as-is with actually-OUT/doubtful players removed.
* Check any player(s) to "sit" them; the tool rebuilds the injured-teammate set
  and re-projects everyone, showing baseline -> hypothetical with the delta,
  sorted by minutes gained. Day-to-day players are flagged so you can simulate
  them sitting too.
* The heavy per-team data (roster + game logs) is cached ~15 min, so toggling
  who sits is instant after the first load (~10-20s while logs load).

Standalone:
    py -3 usage_vacuum.py                 # http://localhost:5017
    py -3 usage_vacuum.py --port 8080 --host 0.0.0.0

Also mounted at /vacuum on the basketball-site (module-level `app`, relative
form action so it works under the mount).
"""

from __future__ import annotations

import argparse
import html
import importlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    ET = None

from flask import Flask, request

# league key -> (display label, props-engine module name, accent)
LEAGUES = {
    "wnba": ("WNBA", "wnba_props_projections"),
    "nba": ("NBA", "nba_props_projections"),
}
DEFAULT_LEAGUE = "wnba"
ACCENT = "#5c6bc0"

# caches
_sched_lock = threading.Lock()
_sched_cache: dict = {}          # (league, date) -> (games, ratings, ts)
_ctx_lock = threading.Lock()
_ctx_cache: dict = {}            # (league, date, team_id) -> (ctx, ts)
SCHED_TTL = 300
CTX_TTL = 900


def _mod(league: str):
    return importlib.import_module(LEAGUES[league][1])


def _today() -> str:
    d = datetime.now(ET).date() if ET is not None else datetime.now().date()
    return d.strftime("%Y%m%d")


def _fmt_date(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y%m%d").strftime("%A, %B %-d, %Y")
    except Exception:
        try:
            return datetime.strptime(date_str, "%Y%m%d").strftime("%A, %B %#d, %Y")
        except Exception:
            return date_str


def get_slate(league: str, date_str: str):
    """(games, ratings) for a date, with the module's league averages set. Cached."""
    key = (league, date_str)
    now = time.monotonic()
    with _sched_lock:
        hit = _sched_cache.get(key)
        if hit and now - hit[2] < SCHED_TTL:
            return hit[0], hit[1]
    mod = _mod(league)
    ratings = mod.load_team_ratings()
    if ratings:
        mod.LEAGUE_AVG_DRTG = sum(r["de"] for r in ratings.values()) / len(ratings)
        mod.LEAGUE_AVG_PACE = sum(r["pace"] for r in ratings.values()) / len(ratings)
    games = mod.fetch_schedule(date_str) or []
    with _sched_lock:
        _sched_cache[key] = (games, ratings, now)
    return games, ratings


def team_context(league: str, date_str: str, team_id: str):
    """Assemble roster + stats + game logs + game context for one team. Cached."""
    key = (league, date_str, team_id)
    now = time.monotonic()
    with _ctx_lock:
        hit = _ctx_cache.get(key)
        if hit and now - hit[1] < CTX_TTL:
            return hit[0]

    mod = _mod(league)
    games, ratings = get_slate(league, date_str)
    game = next((g for g in games
                 if (g.get("home") or {}).get("id") == team_id
                 or (g.get("away") or {}).get("id") == team_id), None)
    if not game:
        return None

    side = "home" if game["home"]["id"] == team_id else "away"
    opp_side = "away" if side == "home" else "home"
    is_home = side == "home"
    team, opp = game[side], game[opp_side]

    team_key = mod.resolve_team_name(team["name"], ratings)
    opp_key = mod.resolve_team_name(opp["name"], ratings)
    team_r = ratings.get(team_key, {}) if team_key else {}
    opp_r = ratings.get(opp_key, {}) if opp_key else {}

    spread = game.get("spread")
    try:
        spread = float(spread) if spread is not None else None
    except (TypeError, ValueError):
        spread = None

    b2b = mod.fetch_team_schedule_b2b(team_id, date_str)
    injuries = mod.fetch_injuries(game["event_id"])
    roster = mod.fetch_roster(team_id)

    gtd_statuses = set(getattr(mod, "GTD_STATUSES", ()))

    # game logs + stats in parallel
    logs: dict = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(mod.fetch_player_gamelog, p["id"]): p["id"] for p in roster}
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
            "player": p,
            "stats": stats,
            "events": {g["event_id"] for g in gl if g.get("event_id")},
            "recent_events": [g.get("event_id", "") for g in gl[:15]],
            "status": status,
            "is_gtd": status in gtd_statuses,
            "is_out": status in ("out", "doubtful"),
        })

    ctx = {
        "league": league, "date": date_str, "team_id": team_id,
        "team_name": team.get("display_name") or team["name"],
        "opp_name": opp.get("display_name") or opp["name"],
        "is_home": is_home, "spread": spread, "b2b": b2b,
        "neutral": game.get("neutral_site", False),
        "team_r": team_r, "opp_r": opp_r,
        "players": players,
        "mod_name": LEAGUES[league][1],
    }
    with _ctx_lock:
        _ctx_cache[key] = (ctx, now)
    return ctx


def _project(mod, ctx, out_ids: set) -> dict:
    """Project every active (not-out) player given an OUT set. {pid: proj}."""
    injured = []
    for p in ctx["players"]:
        pid = p["player"]["id"]
        if pid in out_ids and p["stats"]:
            injured.append({
                "id": pid, "name": p["player"]["name"],
                "position": p["player"].get("position", ""),
                "stats": p["stats"], "p_out": 1.0, "events": p["events"],
            })
    projs = {}
    for p in ctx["players"]:
        pid = p["player"]["id"]
        if pid in out_ids or not p["stats"]:
            continue
        projs[pid] = mod.project_player(
            player=p["player"], stats=p["stats"],
            team_ratings=ctx["team_r"], opp_ratings=ctx["opp_r"],
            is_home=ctx["is_home"], spread=ctx["spread"], team_b2b=ctx["b2b"],
            injured_teammates=[tm for tm in injured if tm["id"] != pid],
            player_recent_events=p["recent_events"],
            neutral_site=ctx["neutral"],
        )
    return projs


def build_scenario(ctx, user_out: set):
    """Baseline (real injuries out) vs hypothetical (+ user_out). Returns rows."""
    mod = importlib.import_module(ctx["mod_name"])
    base_out = {p["player"]["id"] for p in ctx["players"] if p["is_out"]}
    hypo_out = base_out | user_out

    base = _project(mod, ctx, base_out)
    hypo = _project(mod, ctx, hypo_out)

    rows = []
    for p in ctx["players"]:
        pid = p["player"]["id"]
        if pid in hypo_out or not p["stats"]:
            continue
        b = base.get(pid)
        h = hypo.get(pid)
        if not h:
            continue
        d_min = (h["expected_min"] - (b["expected_min"] if b else h["expected_min"]))
        d_pts = (h["expected_pts"] - (b["expected_pts"] if b else h["expected_pts"]))
        d_reb = (h["expected_reb"] - (b["expected_reb"] if b else h["expected_reb"]))
        rows.append({
            "name": p["player"]["name"], "pos": p["player"].get("position", ""),
            "status": p["status"], "is_gtd": p["is_gtd"],
            "base_min": b["expected_min"] if b else None,
            "min": h["expected_min"], "d_min": d_min,
            "base_pts": b["expected_pts"] if b else None,
            "pts": h["expected_pts"], "d_pts": d_pts,
            "base_reb": b["expected_reb"] if b else None,
            "reb": h["expected_reb"], "d_reb": d_reb,
            "usage": h["usage_boost"] * 100.0,
        })
    rows.sort(key=lambda r: r["d_min"], reverse=True)

    sitting = []
    for p in ctx["players"]:
        pid = p["player"]["id"]
        if pid in user_out:
            s = p["stats"]
            sitting.append({
                "name": p["player"]["name"], "pos": p["player"].get("position", ""),
                "mpg": round(s["weighted_mpg"], 1) if s else None,
                "ppg": round(s.get("season_ppg", 0), 1) if s else None,
                "rpg": round(s.get("season_rpg", 0), 1) if s else None,
            })
    vacated = sum(s["mpg"] or 0 for s in sitting)
    return rows, sitting, round(vacated, 1)


# ── HTML ──────────────────────────────────────────────────────────────────────
def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _delta(x, digits=1):
    if x is None:
        return '<span class="mut">—</span>'
    if abs(x) < 0.05:
        return '<span class="mut">·</span>'
    cls = "up" if x > 0 else "dn"
    sign = "+" if x > 0 else ""
    return f'<span class="{cls}">{sign}{x:.{digits}f}</span>'


def render_page(league, date_str, teams, team_id, ctx, rows, sitting, vacated, err):
    opts = "".join(
        f'<option value="{_esc(tid)}"{" selected" if tid == team_id else ""}>{_esc(nm)}</option>'
        for tid, nm in teams)
    lg_opts = "".join(
        f'<option value="{k}"{" selected" if k == league else ""}>{v[0]}</option>'
        for k, v in LEAGUES.items())

    body = ""
    if err:
        body += f'<div class="note err">{_esc(err)}</div>'

    if ctx:
        loc = "vs" if ctx["is_home"] else "@"
        sp = ctx["spread"]
        sp_txt = (f'{sp:+.1f}' if sp is not None else "—")
        body += (f'<div class="ctx"><b>{_esc(ctx["team_name"])}</b> {loc} '
                 f'{_esc(ctx["opp_name"])}'
                 f'<span class="mut"> · spread {sp_txt}'
                 f'{" · B2B" if ctx["b2b"] else ""}'
                 f'{" · neutral" if ctx["neutral"] else ""}</span></div>')

        # player checkbox list
        checks = []
        user_out = set(request.values.getlist("out"))
        pl = [p for p in ctx["players"] if p["stats"] and not p["is_out"]]
        pl.sort(key=lambda p: p["stats"]["weighted_mpg"], reverse=True)
        for p in pl:
            pid = p["player"]["id"]
            checked = " checked" if pid in user_out else ""
            flag = ""
            if p["is_gtd"]:
                flag = ' <span class="gtd">GTD</span>'
            checks.append(
                f'<label class="pchk"><input type="checkbox" name="out" value="{_esc(pid)}"{checked}>'
                f'{_esc(p["player"]["name"])} '
                f'<span class="mut">{_esc(p["player"].get("position",""))} · '
                f'{p["stats"]["weighted_mpg"]:.0f} mpg</span>{flag}</label>')
        # actually-out players (info)
        outs = [p for p in ctx["players"] if p["is_out"]]
        out_note = ""
        if outs:
            names = ", ".join(f'{_esc(p["player"]["name"])} ({_esc(p["status"])})' for p in outs)
            out_note = f'<div class="note">Already out (excluded from baseline): {names}</div>'

        body += (f'<div class="panel"><div class="lbl">Sit players (check to simulate)</div>'
                 f'<div class="checks">{"".join(checks)}</div>{out_note}'
                 f'<button type="submit" class="btn">Recompute</button></div>')

        if sitting:
            sit_txt = ", ".join(
                f'<b>{_esc(s["name"])}</b> ({s["mpg"]:.0f} mpg' +
                (f', {s["ppg"]:.0f} ppg' if s["ppg"] else "") + ")"
                for s in sitting)
            body += (f'<div class="vac">Sitting: {sit_txt} &nbsp;·&nbsp; '
                     f'<b>{vacated:.0f}</b> minutes vacated → redistributed below.</div>')

            head = ("<tr><th>Player</th><th>Pos</th>"
                    "<th>Min</th><th>Δ</th><th>Usage</th>"
                    "<th>Pts</th><th>Δ</th><th>Reb</th><th>Δ</th></tr>")
            trs = []
            for r in rows:
                gtd = ' <span class="gtd">GTD</span>' if r["is_gtd"] else ""
                trs.append(
                    f'<tr class="{"hot" if r["d_min"] >= 2 else ""}">'
                    f'<td class="nm">{_esc(r["name"])}{gtd}</td>'
                    f'<td class="mut">{_esc(r["pos"])}</td>'
                    f'<td>{r["min"]:.1f}</td><td>{_delta(r["d_min"])}</td>'
                    f'<td class="mut">{("+%.1f%%" % r["usage"]) if r["usage"] > 0.05 else "·"}</td>'
                    f'<td>{r["pts"]:.1f}</td><td>{_delta(r["d_pts"])}</td>'
                    f'<td>{r["reb"]:.1f}</td><td>{_delta(r["d_reb"])}</td></tr>')
            body += (f'<div class="panel"><table>{head}{"".join(trs)}</table>'
                     f'<div class="note">Δ = change vs the baseline (before these players sit). '
                     f'Rows sorted by minutes gained; green rows gain ≥2 min. Projections are the '
                     f'props engine\'s expected values (same redistribution model as the nightly PDF).</div></div>')
        else:
            body += ('<div class="note">Check one or more players above and hit '
                     '<b>Recompute</b> to see who absorbs their minutes, usage &amp; rebounds.</div>')

    return PAGE.replace("{{LG_OPTS}}", lg_opts).replace("{{OPTS}}", opts) \
               .replace("{{DATE}}", _esc(date_str)) \
               .replace("{{DATE_PRETTY}}", _esc(_fmt_date(date_str))) \
               .replace("{{BODY}}", body).replace("{{ACCENT}}", ACCENT)


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0f1419">
<title>Usage Vacuum Explorer</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--mut:#8a95a5;
--accent:{{ACCENT}};--up:#4caf50;--dn:#e57373;--gtd:#f0b429;}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);margin:0;padding:22px}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:23px;margin:0 0 3px}h1 span{color:var(--accent)}
.sub{color:var(--mut);font-size:13.5px;margin-bottom:18px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;
padding:16px 18px;margin-bottom:16px}
.toprow{display:flex;gap:12px;flex-wrap:wrap;align-items:end;background:var(--panel);
border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:16px}
label.f{display:block;color:var(--mut);font-size:11px;text-transform:uppercase;
letter-spacing:.06em;margin-bottom:5px}
select,input[type=date]{padding:9px 11px;background:#0f1419;color:var(--text);
border:1px solid var(--border);border-radius:6px;font-size:14px;min-width:120px}
select:focus,input:focus{outline:none;border-color:var(--accent)}
.btn{padding:9px 18px;background:var(--accent);color:#fff;border:0;border-radius:6px;
font-size:14px;font-weight:600;cursor:pointer;margin-top:10px}
.btn:hover{filter:brightness(1.12)}
.lbl{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
.checks{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px 16px}
.pchk{display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer}
.pchk input{width:16px;height:16px;accent-color:var(--accent)}
.ctx{font-size:16px;margin-bottom:14px}
.vac{background:rgba(92,107,192,.13);border:1px solid rgba(92,107,192,.4);
border-radius:8px;padding:11px 14px;margin-bottom:14px;font-size:14px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 9px;text-align:right;border-bottom:1px solid var(--border)}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
td.nm{font-weight:600}
tr.hot td{background:rgba(76,175,80,.08)}
.up{color:var(--up);font-weight:600}.dn{color:var(--dn);font-weight:600}
.mut{color:var(--mut)}
.gtd{color:var(--gtd);font-size:10px;font-weight:700;border:1px solid var(--gtd);
border-radius:4px;padding:1px 4px;vertical-align:middle}
.note{color:var(--mut);font-size:12.5px;margin-top:10px;line-height:1.5}
.note.err{color:#ffb4a2}
</style></head><body><div class=wrap>
<h1>Usage <span>Vacuum</span> Explorer</h1>
<div class=sub>Sit a player, see who eats. Pick a team playing {{DATE_PRETTY}}, check who sits, and get the redistributed minutes / usage / points / rebounds — same engine as the nightly props.</div>
<form method=post>
<div class=toprow>
  <div><label class=f>League</label><select name=league onchange="this.form.submit()">{{LG_OPTS}}</select></div>
  <div><label class=f>Date (YYYYMMDD)</label><input name=date value="{{DATE}}" size=8></div>
  <div><label class=f>Team</label><select name=team onchange="this.form.submit()">{{OPTS}}</select></div>
  <button type=submit class=btn>Load</button>
</div>
{{BODY}}
</form>
<div class=note style="text-align:center;margin-top:20px">
Reuses the props engine's <code>project_player()</code> — no Odds API call.
First team load takes ~10-20s while game logs load, then it's cached.
</div>
</div></body></html>"""

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="6" fill="#0f1419"/>'
           '<path d="M9 8 h14 l-5 8 v7 l-4 2 v-9 z" fill="#5c6bc0"/></svg>')


def create_app():
    app = Flask(__name__)

    @app.route("/", methods=["GET", "POST"])
    def index():
        league = request.values.get("league", DEFAULT_LEAGUE)
        if league not in LEAGUES:
            league = DEFAULT_LEAGUE
        date_str = (request.values.get("date", "") or "").strip() or _today()
        team_id = request.values.get("team", "")

        err = None
        teams = []
        try:
            games, _ = get_slate(league, date_str)
            seen = {}
            for g in games:
                for side in ("home", "away"):
                    t = g.get(side) or {}
                    if t.get("id"):
                        seen[t["id"]] = t.get("display_name") or t.get("name") or t["id"]
            teams = sorted(seen.items(), key=lambda kv: kv[1])
            if not teams:
                err = f"No upcoming games found for {LEAGUES[league][0]} on {date_str}."
        except Exception as e:
            err = f"Could not load the slate: {e}"

        valid_ids = {tid for tid, _ in teams}
        if team_id not in valid_ids:
            team_id = teams[0][0] if teams else ""

        ctx = rows = sitting = None
        vacated = 0
        if team_id:
            try:
                ctx = team_context(league, date_str, team_id)
                if ctx:
                    user_out = set(request.values.getlist("out"))
                    rows, sitting, vacated = build_scenario(ctx, user_out)
            except Exception as e:
                err = f"Projection failed: {e}"

        return render_page(league, date_str, teams, team_id, ctx,
                           rows, sitting, vacated, err)

    @app.route("/favicon.svg")
    def favicon():
        return (FAVICON, 200, {"Content-Type": "image/svg+xml"})

    return app


app = create_app()


def main():
    ap = argparse.ArgumentParser(description="Usage Vacuum explorer")
    ap.add_argument("--port", type=int, default=5017)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"Usage Vacuum -> http://{args.host}:{args.port}")
    create_app().run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
