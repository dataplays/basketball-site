#!/usr/bin/env python3
"""situational_angles.py — schedule/rest spots for a night's slate (mounted /spots).

For each game on a chosen date, shows both teams' situational context from the
ESPN schedule: days rest, back-to-back, 3-in-4, 4-in-6, current road-trip /
home-stand length, and the rest edge between the two teams. These "spots" are a
classic angle for totals and sides (tired legs, scheduling losses).

Leagues: NBA, WNBA (rest matters most in the pro game). ESPN public APIs, no key.
Standalone: `py -3 situational_angles.py`  (web)  ·  `--league wnba --date YYYY-MM-DD`.
"""

import argparse
import json
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import Flask, Response, request

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    ET = None

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) situational-angles"
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
LEAGUES = {"nba": {"label": "NBA", "path": "nba"},
           "wnba": {"label": "WNBA", "path": "wnba"}}
DEFAULT_LEAGUE = "nba"

app = Flask(__name__)
_sched_cache: dict = {}
_lock = threading.Lock()


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _today():
    return (datetime.now(ET) if ET else datetime.now()).date()


def _team_dates(path, team_id):
    """Sorted list of (date, homeAway) for the team's season schedule."""
    key = (path, team_id)
    with _lock:
        if key in _sched_cache:
            return _sched_cache[key]
    out = []
    try:
        data = _get(f"{BASE}/{path}/teams/{team_id}/schedule")
        for ev in data.get("events", []):
            comps = ev.get("competitions") or []
            if not comps:
                continue
            d = (ev.get("date") or "")[:10]
            me = next((c for c in comps[0].get("competitors", [])
                       if str(c.get("team", {}).get("id")) == str(team_id)), None)
            if d and me:
                try:
                    out.append((datetime.fromisoformat(d).date(), me.get("homeAway", "")))
                except ValueError:
                    pass
    except Exception as e:  # noqa: BLE001
        print(f"[spots] schedule {team_id} failed: {e}", file=sys.stderr)
    out.sort()
    with _lock:
        _sched_cache[key] = out
    return out


def _rest_metrics(dates, target, home_away):
    """Compute rest context for a team playing on `target`."""
    all_d = [d for d, _ha in dates]
    prev = [d for d in all_d if d < target]
    days_off = (target - prev[-1]).days if prev else None
    in4 = sum(1 for d in all_d if target - timedelta(days=3) <= d <= target)
    in6 = sum(1 for d in all_d if target - timedelta(days=5) <= d <= target)
    # consecutive same-site streak ending tonight
    streak = 0
    idx = next((i for i, (d, _h) in enumerate(dates) if d == target), None)
    if idx is not None:
        i = idx
        while i >= 0 and dates[i][1] == home_away:
            streak += 1
            i -= 1
    return {
        "days_off": days_off,
        "rest": (days_off - 1) if days_off is not None else None,
        "b2b": days_off == 1,
        "three_in_four": in4 >= 3,
        "four_in_six": in6 >= 4,
        "streak": streak, "home_away": home_away,
    }


def build_slate(league, date_str):
    path = LEAGUES[league]["path"]
    yyyymmdd = date_str.replace("-", "")
    try:
        sb = _get(f"{BASE}/{path}/scoreboard?dates={yyyymmdd}")
    except Exception as e:  # noqa: BLE001
        return [], f"Could not load scoreboard: {e}"
    target = datetime.fromisoformat(date_str).date()
    games = []
    teams_needed = set()
    raw = []
    for ev in sb.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors", [])
        home = next((c for c in cs if c.get("homeAway") == "home"), None)
        away = next((c for c in cs if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        ht, at = home["team"], away["team"]
        raw.append((ev, at, ht))
        teams_needed.add(str(at.get("id")))
        teams_needed.add(str(ht.get("id")))
    # fetch all needed schedules in parallel
    with ThreadPoolExecutor(max_workers=min(len(teams_needed) or 1, 10)) as ex:
        futs = {ex.submit(_team_dates, path, tid): tid for tid in teams_needed}
        sched = {futs[f]: f.result() for f in futs}
    for ev, at, ht in raw:
        a_dates, h_dates = sched.get(str(at["id"]), []), sched.get(str(ht["id"]), [])
        am = _rest_metrics(a_dates, target, "away")
        hm = _rest_metrics(h_dates, target, "home")
        edge = None
        if am["rest"] is not None and hm["rest"] is not None:
            edge = hm["rest"] - am["rest"]
        tip = ""
        try:
            dt = datetime.fromisoformat((ev.get("date") or "").replace("Z", "+00:00"))
            if ET:
                dt = dt.astimezone(ET)
            h = dt.hour % 12 or 12
            tip = f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'} ET"
        except Exception:  # noqa: BLE001
            pass
        games.append({
            "away": at.get("abbreviation") or at.get("displayName"),
            "home": ht.get("abbreviation") or ht.get("displayName"),
            "away_name": at.get("displayName"), "home_name": ht.get("displayName"),
            "tip": tip, "a": am, "h": hm, "edge": edge,
        })
    return games, None


# ── Rendering ──

def _flags(m):
    out = []
    if m["b2b"]:
        out.append('<span class="flag b2b">B2B</span>')
    if m["three_in_four"]:
        out.append('<span class="flag warn">3-in-4</span>')
    if m["four_in_six"]:
        out.append('<span class="flag warn">4-in-6</span>')
    if m["streak"] >= 4:
        side = "road trip" if m["home_away"] == "away" else "home stand"
        out.append(f'<span class="flag trip">{m["streak"]}-game {side}</span>')
    return " ".join(out)


def _rest_cell(m):
    if m["rest"] is None:
        return '<span class="muted">—</span>'
    cls = "rest-tired" if m["rest"] == 0 else ("rest-fresh" if m["rest"] >= 2 else "")
    return f'<span class="{cls}">{m["rest"]}d</span>'


def render(league, date_str, games, error):
    opts = "".join(f'<option value="{k}"{" selected" if k==league else ""}>{v["label"]}</option>'
                   for k, v in LEAGUES.items())
    rows = []
    for g in games:
        edge = g["edge"]
        if edge is None:
            edge_html = '<span class="muted">—</span>'
        elif edge == 0:
            edge_html = '<span class="muted">even</span>'
        else:
            who = g["home"] if edge > 0 else g["away"]
            edge_html = f'<span class="edge">{who} +{abs(edge)}d</span>'
        rows.append(
            f'<tr><td class="g">{g["away"]} @ {g["home"]}<div class="tip">{g["tip"]}</div></td>'
            f'<td>{_rest_cell(g["a"])}</td><td class="fl">{_flags(g["a"]) or "&mdash;"}</td>'
            f'<td>{_rest_cell(g["h"])}</td><td class="fl">{_flags(g["h"]) or "&mdash;"}</td>'
            f'<td>{edge_html}</td></tr>')
    body = ("".join(rows) if rows else
            '<tr><td colspan="6" class="muted" style="padding:18px">No games on this date.</td></tr>')
    err = f'<div class="err">{error}</div>' if error else ""
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="Basketball schedule/rest spots: back-to-backs, 3-in-4, rest edge by game.">
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><meta name="theme-color" content="#0f1419">
<title>Situational Spots</title><style>
:root{{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--muted:#8a95a5;--accent:#7e57c2;
--tired:#e57373;--fresh:#4caf50}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);padding:22px 16px}}
.container{{max-width:920px;margin:0 auto}}
a.menu{{color:#7fb2ff;text-decoration:none;font-size:.85em;font-weight:600}}
h1{{font-size:23px;font-weight:700;margin:6px 0 3px}}
.sub{{color:var(--muted);font-size:14px;margin-bottom:16px}}
form{{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:16px}}
label{{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}}
select,input{{padding:9px 11px;background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:7px;font-size:14px}}
button{{padding:9px 16px;background:var(--accent);color:#fff;border:0;border-radius:7px;font-weight:700;font-size:14px;cursor:pointer}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:11px;overflow:hidden}}
th,td{{padding:9px 12px;text-align:center;border-bottom:1px solid var(--border);font-size:14px}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;background:rgba(255,255,255,.03)}}
td.g,th.g{{text-align:left;font-weight:700}}
.tip{{color:var(--muted);font-size:11px;font-weight:400}}
td.fl{{text-align:left}}
.muted{{color:var(--muted)}}
.rest-tired{{color:var(--tired);font-weight:800}}.rest-fresh{{color:var(--fresh);font-weight:700}}
.flag{{display:inline-block;font-size:.68em;font-weight:800;padding:2px 6px;border-radius:5px;letter-spacing:.3px;margin:1px 0}}
.flag.b2b{{background:rgba(229,115,115,.2);color:#ff8a80}}
.flag.warn{{background:rgba(255,206,84,.18);color:#ffd479}}
.flag.trip{{background:rgba(126,87,194,.22);color:#b39ddb}}
.edge{{color:#9ccc65;font-weight:800}}
.err{{color:var(--tired);font-size:14px;margin-bottom:12px}}
.legend{{color:var(--muted);font-size:12px;margin-top:14px;line-height:1.6}}
.legend b{{color:var(--text)}}
</style></head><body><div class="container">
<a class="menu" href="/">&#8962; Main Menu</a>
<h1>Situational Spots</h1>
<div class="sub">Rest &amp; scheduling context for each game — tired legs and schedule losses are a classic totals/side angle.</div>
<form method="get">
  <div><label>League</label><select name="league">{opts}</select></div>
  <div><label>Date</label><input type="date" name="date" value="{date_str}"></div>
  <button type="submit">Show slate</button>
</form>{err}
<table><thead><tr><th class="g">Game</th><th>Away Rest</th><th>Away Spot</th>
<th>Home Rest</th><th>Home Spot</th><th>Rest Edge</th></tr></thead><tbody>{body}</tbody></table>
<div class="legend"><b>Rest</b> = days off since last game (<span class="rest-tired">0</span> = back-to-back,
<span class="rest-fresh">2+</span> = fresh). <b>3-in-4 / 4-in-6</b> = games in a 4- / 6-night window (fatigue).
<b>Rest edge</b> = the better-rested side and by how many days. Data: ESPN. For information only.</div>
</div></body></html>"""


@app.route("/")
def index():
    league = request.args.get("league", DEFAULT_LEAGUE)
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE
    date_str = request.args.get("date", "") or _today().isoformat()
    try:
        datetime.fromisoformat(date_str)
    except ValueError:
        date_str = _today().isoformat()
    games, error = build_slate(league, date_str)
    return Response(render(league, date_str, games, error), mimetype="text/html")


def main():
    ap = argparse.ArgumentParser(description="Situational schedule/rest spots")
    ap.add_argument("--league", default=DEFAULT_LEAGUE, choices=list(LEAGUES))
    ap.add_argument("--date", default="")
    ap.add_argument("--port", type=int, default=5012)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if args.date:
        games, err = build_slate(args.league, args.date)
        print(f"{args.league} {args.date}: {len(games)} games" + (f" ({err})" if err else ""))
        for g in games:
            print(f"  {g['away']}@{g['home']:4} away rest={g['a']['rest']} b2b={g['a']['b2b']} "
                  f"| home rest={g['h']['rest']} b2b={g['h']['b2b']} | edge={g['edge']}")
        return
    print(f"Serving at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
