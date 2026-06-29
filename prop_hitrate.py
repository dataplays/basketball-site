#!/usr/bin/env python3
"""prop_hitrate.py — player prop hit-rate explorer (mounted at /hitrate).

Pick a league + player + stat + line and see how often they've actually cleared
it: hit rate over L5 / L10 / L20 / season, home vs away, and vs a chosen
opponent — plus the full game log marked over/under. Complements the Monte-Carlo
props model with raw "how often does he beat this number."

ESPN public APIs (no key): team rosters resolve the player; the athlete gamelog
feed supplies per-game stats. Standalone: `py -3 prop_hitrate.py` (web) or
`--league wnba --player "A'ja Wilson" --stat pts --line 24.5`.
"""

import argparse
import json
import sys
import threading
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher

from flask import Flask, Response, request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) prop-hitrate"
SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/basketball"
LEAGUES = {"nba": {"label": "NBA", "path": "nba", "season": 2026},
           "wnba": {"label": "WNBA", "path": "wnba", "season": 2026}}
DEFAULT_LEAGUE = "wnba"

# stat key -> (label, function over the per-game dict)
STATS = {
    "pts": ("Points", lambda g: g["pts"]),
    "reb": ("Rebounds", lambda g: g["reb"]),
    "ast": ("Assists", lambda g: g["ast"]),
    "fg3m": ("3-Pointers Made", lambda g: g["fg3m"]),
    "pra": ("Pts + Reb + Ast", lambda g: g["pts"] + g["reb"] + g["ast"]),
    "pr": ("Pts + Reb", lambda g: g["pts"] + g["reb"]),
    "pa": ("Pts + Ast", lambda g: g["pts"] + g["ast"]),
    "ra": ("Reb + Ast", lambda g: g["reb"] + g["ast"]),
    "stl": ("Steals", lambda g: g["stl"]),
    "blk": ("Blocks", lambda g: g["blk"]),
    "stocks": ("Steals + Blocks", lambda g: g["stl"] + g["blk"]),
    "to": ("Turnovers", lambda g: g["to"]),
}

app = Flask(__name__)
_players_cache: dict = {}     # league -> (ts, {canon_name: {...}})
_lock = threading.Lock()


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _canon(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join("".join(ch for ch in s if ch.isalnum() or ch == " ").split())


# ── Player resolution (team rosters) ──

def _roster(path, tid):
    out = []
    try:
        d = _get(f"{SITE}/{path}/teams/{tid}/roster")
        ab = (d.get("team") or {}).get("abbreviation", "")
        for a in d.get("athletes", []):
            if a.get("id") and a.get("fullName"):
                out.append({"id": str(a["id"]), "name": a["fullName"],
                            "team": ab, "pos": (a.get("position") or {}).get("abbreviation", "")})
    except Exception:  # noqa: BLE001
        pass
    return out


def get_players(league):
    now = time.time()
    with _lock:
        hit = _players_cache.get(league)
        if hit and now - hit[0] < 21600:
            return hit[1]
    path = LEAGUES[league]["path"]
    try:
        td = _get(f"{SITE}/{path}/teams?limit=100")
        tids = [str(t["team"]["id"]) for t in td["sports"][0]["leagues"][0]["teams"]]
    except Exception:  # noqa: BLE001
        tids = []
    players: dict = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for roster in ex.map(lambda t: _roster(path, t), tids):
            for p in roster:
                players[_canon(p["name"])] = p
    with _lock:
        _players_cache[league] = (now, players)
    return players


def resolve_player(league, query):
    q = _canon(query)
    if not q:
        return None, []
    players = get_players(league)
    if q in players:
        return players[q], []
    subs = [p for k, p in players.items() if q in k]
    if len(subs) == 1:
        return subs[0], []
    pool = subs or list(players.values())
    ranked = sorted(pool, key=lambda p: SequenceMatcher(None, q, _canon(p["name"])).ratio(), reverse=True)
    if ranked and SequenceMatcher(None, q, _canon(ranked[0]["name"])).ratio() >= 0.6 and not subs:
        return ranked[0], []
    if len(subs) > 1:
        return None, subs[:8]
    return (ranked[0], []) if ranked else (None, [])


# ── Gamelog ──

def _parse_gamelog(data):
    labels = data.get("labels") or []
    idx = {lbl: i for i, lbl in enumerate(labels)}
    meta = data.get("events") or {}

    def num(stats, lbl):
        i = idx.get(lbl, -1)
        if i < 0 or i >= len(stats):
            return 0
        v = str(stats[i]).strip()
        return int(v) if v.lstrip("-").isdigit() else 0

    def made(stats, lbl):
        i = idx.get(lbl, -1)
        if i < 0 or i >= len(stats):
            return 0
        v = str(stats[i])
        return int(v.split("-")[0]) if "-" in v and v.split("-")[0].isdigit() else 0

    games = []
    for st in data.get("seasonTypes", []):
        if "regular" not in st.get("displayName", "").lower():
            continue
        for cat in st.get("categories", []):
            for evt in cat.get("events", []):
                stats = evt.get("stats") or []
                if not stats:
                    continue
                m = meta.get(str(evt.get("eventId")), {})
                if num(stats, "MIN") <= 0 and not m:
                    continue
                games.append({
                    "date": (m.get("gameDate") or "")[:10],
                    "opp": (m.get("opponent") or {}).get("abbreviation", "?"),
                    "ha": "A" if (m.get("atVs") or "").strip() == "@" else "H",
                    "res": m.get("gameResult", ""),
                    "min": num(stats, "MIN"),
                    "pts": num(stats, "PTS"), "reb": num(stats, "REB"), "ast": num(stats, "AST"),
                    "stl": num(stats, "STL"), "blk": num(stats, "BLK"), "to": num(stats, "TO"),
                    "fg3m": made(stats, "3PT"),
                })
    games.sort(key=lambda g: g["date"], reverse=True)
    return games


def fetch_gamelog(league, pid):
    path, season = LEAGUES[league]["path"], LEAGUES[league]["season"]
    for yr in (season, season - 1):
        try:
            data = _get(f"{WEB}/{path}/athletes/{pid}/gamelog?season={yr}&seasontype=2")
            games = _parse_gamelog(data)
            if games:
                return games
        except Exception:  # noqa: BLE001
            continue
    return []


# ── Hit-rate computation ──

def _window(games, fn, line):
    vals = [fn(g) for g in games]
    over = sum(1 for v in vals if v > line)
    under = sum(1 for v in vals if v < line)
    push = sum(1 for v in vals if v == line)
    n = len(vals)
    return {"n": n, "avg": (sum(vals) / n) if n else 0.0,
            "over": over, "under": under, "push": push,
            "rate": (over / (over + under) * 100) if (over + under) else 0.0}


def analyze(league, pid, stat, line, opp=""):
    games = fetch_gamelog(league, pid)
    fn = STATS[stat][1]
    rows = [("Last 5", games[:5]), ("Last 10", games[:10]), ("Last 20", games[:20]),
            ("Season", games),
            ("Home", [g for g in games if g["ha"] == "H"]),
            ("Away", [g for g in games if g["ha"] == "A"])]
    if opp:
        rows.append((f"vs {opp.upper()}", [g for g in games if g["opp"].upper() == opp.upper()]))
    summary = [(name, _window(gs, fn, line)) for name, gs in rows]
    log = [{**g, "val": fn(g)} for g in games]
    return games, summary, log


# ── Rendering ──

def _bar(rate):
    return (f'<span class="bar"><span class="fill" style="width:{rate:.0f}%"></span></span>'
            f'<span class="rt">{rate:.0f}%</span>')


def render(league, player, stat, line, opp, summary, log, error, suggestions):
    lopts = "".join(f'<option value="{k}"{" selected" if k==league else ""}>{v["label"]}</option>'
                    for k, v in LEAGUES.items())
    sopts = "".join(f'<option value="{k}"{" selected" if k==stat else ""}>{v[0]}</option>'
                    for k, v in STATS.items())
    head = ""
    if player:
        head = (f'<div class="phead"><b>{player["name"]}</b> '
                f'<span class="muted">{player.get("team","")} · {STATS[stat][0]} '
                f'{"o" if line else ""}{line if line else ""}</span></div>')
    err = ""
    if error:
        err = f'<div class="err">{error}'
        if suggestions:
            err += " — did you mean: " + ", ".join(s["name"] for s in suggestions)
        err += "</div>"

    srows = ""
    for name, w in summary:
        if not w["n"]:
            srows += f'<tr><td>{name}</td><td colspan="4" class="muted">no games</td></tr>'
            continue
        push = f' · {w["push"]}P' if w["push"] else ""
        srows += (f'<tr><td>{name}</td><td class="n">{w["n"]}</td>'
                  f'<td class="avg">{w["avg"]:.1f}</td>'
                  f'<td>{w["over"]}-{w["under"]}{push}</td>'
                  f'<td class="rate">{_bar(w["rate"])}</td></tr>')

    lrows = ""
    for g in log[:25]:
        v = g["val"]
        mark = ('<span class="ov">OVER</span>' if v > line else
                ('<span class="un">UNDER</span>' if v < line else '<span class="muted">push</span>'))
        lrows += (f'<tr><td>{g["date"][5:]}</td><td>{g["ha"]} {g["opp"]}</td>'
                  f'<td class="res">{g["res"]}</td><td class="val">{v}</td><td>{mark}</td></tr>')

    tables = ""
    if summary:
        tables = (f'<table class="sum"><thead><tr><th>Window</th><th>GP</th><th>Avg</th>'
                  f'<th>O-U</th><th>Over rate</th></tr></thead><tbody>{srows}</tbody></table>'
                  f'<h2>Game log <small>most recent 25</small></h2>'
                  f'<table class="logt"><thead><tr><th>Date</th><th>Opp</th><th>Res</th>'
                  f'<th>{STATS[stat][0].split(" ")[0]}</th><th>vs {line}</th></tr></thead>'
                  f'<tbody>{lrows}</tbody></table>')

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="Player prop hit-rate explorer: how often a player clears a line over L5/L10/L20, home/away, vs opponent.">
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><meta name="theme-color" content="#0f1419">
<title>Prop Hit-Rate</title><style>
:root{{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--muted:#8a95a5;--accent:#26a69a;
--ov:#4caf50;--un:#e57373}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);padding:22px 16px}}
.container{{max-width:760px;margin:0 auto}}
a.menu{{color:#7fb2ff;text-decoration:none;font-size:.85em;font-weight:600}}
h1{{font-size:23px;font-weight:700;margin:6px 0 3px}}
h2{{font-size:15px;font-weight:700;margin:20px 0 8px}}h2 small{{color:var(--muted);font-weight:400;font-size:.8em}}
.sub{{color:var(--muted);font-size:14px;margin-bottom:16px}}
form{{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-bottom:14px}}
label{{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}}
select,input{{padding:9px 11px;background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:7px;font-size:14px}}
input.pl{{min-width:180px}}input.ln{{width:80px}}input.op{{width:90px}}
button{{padding:9px 16px;background:var(--accent);color:#06231f;border:0;border-radius:7px;font-weight:800;font-size:14px;cursor:pointer}}
.phead{{font-size:18px;margin:6px 0 12px}}.phead .muted{{font-size:.7em}}
.muted{{color:var(--muted)}}.err{{color:var(--un);font-size:14px;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:6px}}
th,td{{padding:8px 11px;text-align:center;border-bottom:1px solid var(--border);font-size:14px}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;background:rgba(255,255,255,.03)}}
.sum td:first-child,.sum th:first-child{{text-align:left;font-weight:700}}
td.n,td.avg{{font-weight:700}}td.val{{font-weight:800}}
.bar{{display:inline-block;width:70px;height:7px;background:#0f1419;border-radius:4px;overflow:hidden;vertical-align:middle;margin-right:7px}}
.bar .fill{{display:block;height:100%;background:var(--accent)}}
.rt{{font-weight:800}}
.ov{{color:var(--ov);font-weight:800;font-size:.82em}}.un{{color:var(--un);font-weight:800;font-size:.82em}}
.res{{color:var(--muted)}}
.note{{color:var(--muted);font-size:12px;margin-top:16px;line-height:1.5}}
</style></head><body><div class="container">
<a class="menu" href="/">&#8962; Main Menu</a>
<h1>Prop Hit-Rate Explorer</h1>
<div class="sub">How often a player has actually cleared a prop line — by recency, home/away, and opponent.</div>
<form method="get">
  <div><label>League</label><select name="league">{lopts}</select></div>
  <div><label>Player</label><input class="pl" name="player" value="{player['name'] if player else ''}" placeholder="A'ja Wilson"></div>
  <div><label>Stat</label><select name="stat">{sopts}</select></div>
  <div><label>Line</label><input class="ln" name="line" inputmode="decimal" value="{line if line else ''}" placeholder="24.5"></div>
  <div><label>vs Opp (opt)</label><input class="op" name="opp" value="{opp}" placeholder="CHI"></div>
  <button type="submit">Go</button>
</form>{err}{head}{tables}
<div class="note">Over rate excludes pushes. Game log uses regular-season games (falls back to last season if the current one hasn&rsquo;t started). Data: ESPN. For information only.</div>
</div></body></html>"""


@app.route("/")
def index():
    league = request.args.get("league", DEFAULT_LEAGUE)
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE
    stat = request.args.get("stat", "pts")
    if stat not in STATS:
        stat = "pts"
    pquery = request.args.get("player", "").strip()
    opp = request.args.get("opp", "").strip()
    try:
        line = float(request.args.get("line", "") or 0)
    except ValueError:
        line = 0.0
    player, suggestions, summary, log, error = None, [], [], [], ""
    if pquery:
        player, suggestions = resolve_player(league, pquery)
        if not player:
            error = f"No player matching “{pquery}”."
        elif not line:
            error = "Enter a line to compute hit rates."
            _g, summary, log = analyze(league, player["id"], stat, line, opp)  # still show log
        else:
            _g, summary, log = analyze(league, player["id"], stat, line, opp)
    return Response(render(league, player, stat, line, opp, summary, log, error, suggestions),
                    mimetype="text/html")


def main():
    ap = argparse.ArgumentParser(description="Player prop hit-rate explorer")
    ap.add_argument("--league", default=DEFAULT_LEAGUE, choices=list(LEAGUES))
    ap.add_argument("--player")
    ap.add_argument("--stat", default="pts", choices=list(STATS))
    ap.add_argument("--line", type=float, default=0.0)
    ap.add_argument("--opp", default="")
    ap.add_argument("--port", type=int, default=5013)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if args.player:
        p, sug = resolve_player(args.league, args.player)
        if not p:
            print("Not found. Suggestions:", ", ".join(s["name"] for s in sug))
            return
        _g, summary, log = analyze(args.league, p["id"], args.stat, args.line, args.opp)
        print(f"{p['name']} ({p['team']}) — {STATS[args.stat][0]} o{args.line}")
        for name, w in summary:
            if w["n"]:
                print(f"  {name:10} {w['n']:2}gp  avg {w['avg']:5.1f}  "
                      f"{w['over']}-{w['under']}  {w['rate']:.0f}% over")
        return
    print(f"Serving at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
