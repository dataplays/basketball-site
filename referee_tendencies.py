#!/usr/bin/env python3
"""referee_tendencies.py — NBA referee tendencies + tonight's crews (mounted /refs).

Refs measurably affect totals and foul/free-throw volume. This tool:
  - shows each official's tendencies from a season scan (games, avg combined
    points, avg fouls called, avg FTA) with over/under and whistle-heavy flags;
  - overlays the night's assigned crews (from the NBA's official feed) with the
    crew's blended lean vs league average.

Tendencies are precomputed by scanning ESPN game summaries (officials + final
score + box fouls/FTA) into `nba_ref_stats.csv`:
    py -3 referee_tendencies.py --build [--season 2026]
Then serve: `py -3 referee_tendencies.py`  (reads the CSV + live assignments).
"""

import argparse
import csv
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from flask import Flask, Response, request

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    ET = None

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"
SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
NBA_OFFICIALS = "https://official.nba.com/wp-json/api/v1/get-game-officials?gamedate={date}"
CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nba_ref_stats.csv")
MIN_GAMES = 8        # refs below this are hidden from the main table

app = Flask(__name__)


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ── Season scan → CSV ──

def _all_game_ids(season):
    ids = set()
    try:
        td = _get(f"{SITE}/teams?limit=40")
        tids = [str(t["team"]["id"]) for t in td["sports"][0]["leagues"][0]["teams"]]
    except Exception as e:  # noqa: BLE001
        print("teams fetch failed:", e); return ids
    for tid in tids:
        try:
            d = _get(f"{SITE}/teams/{tid}/schedule?season={season}")
            for ev in d.get("events", []):
                if ev["competitions"][0].get("status", {}).get("type", {}).get("completed"):
                    ids.add(str(ev["id"]))
        except Exception:  # noqa: BLE001
            continue
    return ids


def _game_officiating(gid):
    try:
        d = _get(f"{SITE}/summary?event={gid}", timeout=12)
    except Exception:  # noqa: BLE001
        return None
    offs = [o.get("displayName") for o in d.get("gameInfo", {}).get("officials", []) if o.get("displayName")]
    comps = d.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
    try:
        total = sum(int(c.get("score", 0)) for c in comps)
    except (TypeError, ValueError):
        total = 0
    if not offs or total <= 0:
        return None
    fouls = fta = 0

    def stat(t, name):
        return next((s.get("displayValue") for s in t.get("statistics", []) if s.get("name") == name), None)

    for t in d.get("boxscore", {}).get("teams", []):
        f = stat(t, "fouls")
        ft = stat(t, "freeThrowsMade-freeThrowsAttempted")
        if f and str(f).isdigit():
            fouls += int(f)
        if ft and "-" in str(ft) and str(ft).split("-")[1].isdigit():
            fta += int(str(ft).split("-")[1])
    return offs, total, fouls, fta


def build_ref_stats(season):
    print(f"Collecting {season} game ids …")
    ids = _all_game_ids(season)
    print(f"  {len(ids)} completed games. Scanning summaries …")
    agg: dict = {}
    done = ok = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_game_officiating, g): g for g in ids}
        for fut in as_completed(futs):
            done += 1
            res = fut.result()
            if done % 150 == 0:
                print(f"   {done}/{len(ids)} …", file=sys.stderr)
            if not res:
                continue
            ok += 1
            offs, total, fouls, fta = res
            for ref in offs:
                a = agg.setdefault(ref, [0, 0, 0, 0])
                a[0] += 1; a[1] += total; a[2] += fouls; a[3] += fta
    rows = []
    for ref, (g, t, f, ft) in agg.items():
        if g <= 0:
            continue
        rows.append({"ref": ref, "games": g, "avg_total": round(t / g, 1),
                     "avg_fouls": round(f / g, 1), "avg_fta": round(ft / g, 1)})
    rows.sort(key=lambda r: -r["avg_total"])
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=["ref", "games", "avg_total", "avg_fouls", "avg_fta"])
        w.writeheader(); w.writerows(rows)
    print(f"Wrote {len(rows)} refs ({ok}/{len(ids)} games parsed) -> {CSV_PATH}")


# ── Load + cache ──

_stats_cache: dict = {"ts": 0.0, "rows": None, "lg": None}


def load_ref_stats():
    if not os.path.exists(CSV_PATH):
        return [], {}
    if _stats_cache["rows"] is not None and time.time() - _stats_cache["ts"] < 3600:
        return _stats_cache["rows"], _stats_cache["lg"]
    rows = []
    with open(CSV_PATH, encoding="utf-8") as fp:
        for r in csv.DictReader(fp):
            rows.append({"ref": r["ref"], "games": int(r["games"]),
                         "avg_total": float(r["avg_total"]), "avg_fouls": float(r["avg_fouls"]),
                         "avg_fta": float(r["avg_fta"])})
    qual = [r for r in rows if r["games"] >= MIN_GAMES]
    n = len(qual) or 1
    lg = {"avg_total": sum(r["avg_total"] for r in qual) / n,
          "avg_fouls": sum(r["avg_fouls"] for r in qual) / n,
          "avg_fta": sum(r["avg_fta"] for r in qual) / n}
    _stats_cache.update(ts=time.time(), rows=rows, lg=lg)
    return rows, lg


def ref_index(rows):
    return {r["ref"]: r for r in rows}


# ── Tonight's assignments ──

_assign_cache: dict = {}


def fetch_assignments(date_str):
    if date_str in _assign_cache and time.time() - _assign_cache[date_str][0] < 900:
        return _assign_cache[date_str][1]
    games = []
    try:
        d = _get(NBA_OFFICIALS.format(date=date_str), timeout=12)
        tbl = (d.get("nba") or {}).get("Table") or {}
        cols = [c["Name"] for c in tbl.get("columns", [])]
        for row in tbl.get("rows", []):
            rec = dict(zip(cols, row))
            crew = [rec.get(f"official{i}") for i in (1, 2, 3) if rec.get(f"official{i}")]
            games.append({"away": rec.get("away_team_abbr") or rec.get("away_team"),
                          "home": rec.get("home_team_abbr") or rec.get("home_team"),
                          "crew": [c for c in crew if c]})
    except Exception as e:  # noqa: BLE001
        print("[refs] assignments failed:", e, file=sys.stderr)
    _assign_cache[date_str] = (time.time(), games)
    return games


# ── Rendering ──

def _lean(val, lg, hi="OVER", lo="UNDER", th=1.5):
    diff = val - lg
    if diff >= th:
        return f'<span class="lean hi">{hi} +{diff:.1f}</span>'
    if diff <= -th:
        return f'<span class="lean lo">{lo} {diff:.1f}</span>'
    return '<span class="lean ev">neutral</span>'


def render(date_str, error=""):
    rows, lg = load_ref_stats()
    idx = ref_index(rows)
    assigns = fetch_assignments(date_str)
    have = bool(rows)

    crew_html = ""
    if assigns:
        cards = []
        for g in assigns:
            known = [idx[r] for r in g["crew"] if r in idx]
            if known:
                ct = sum(r["avg_total"] for r in known) / len(known)
                cf = sum(r["avg_fouls"] for r in known) / len(known)
                lean = _lean(ct, lg["avg_total"]) + " " + _lean(cf, lg["avg_fouls"], "WHISTLE", "LET-PLAY", 1.0)
                stat = f'<div class="cstat">crew avg total <b>{ct:.1f}</b> · fouls <b>{cf:.1f}</b> {lean}</div>'
            else:
                stat = '<div class="cstat muted">no tendency data for this crew</div>'
            names = " · ".join(f'{r}{(" ("+str(idx[r]["games"])+"g, "+str(idx[r]["avg_total"])+")" ) if r in idx else ""}'
                               for r in g["crew"]) or "TBD"
            cards.append(f'<div class="crewcard"><div class="cmatch">{g["away"]} @ {g["home"]}</div>'
                         f'<div class="cnames">{names}</div>{stat}</div>')
        crew_html = "".join(cards)
    else:
        crew_html = ('<div class="muted" style="padding:10px 0">No crew assignments posted for this date '
                     '(the NBA feed only lists them around game day — empty in the off-season).</div>')

    trows = ""
    for r in [x for x in rows if x["games"] >= MIN_GAMES]:
        trows += (f'<tr><td class="rf">{r["ref"]}</td><td>{r["games"]}</td>'
                  f'<td class="num">{r["avg_total"]:.1f}</td><td>{_lean(r["avg_total"], lg["avg_total"])}</td>'
                  f'<td class="num">{r["avg_fouls"]:.1f}</td><td class="num">{r["avg_fta"]:.1f}</td>'
                  f'<td>{_lean(r["avg_fouls"], lg["avg_fouls"], "WHISTLE", "LET-PLAY", 1.0)}</td></tr>')
    table = (f'<table><thead><tr><th class="rf">Referee</th><th>G</th><th>Avg Total</th><th>Total lean</th>'
             f'<th>Fouls</th><th>FTA</th><th>Foul lean</th></tr></thead><tbody>{trows}</tbody></table>'
             if have else
             '<div class="err">No ref stats yet — run <code>py -3 referee_tendencies.py --build</code> to '
             'generate <code>nba_ref_stats.csv</code>.</div>')

    lgline = (f'League avg (qualified refs): total <b>{lg.get("avg_total",0):.1f}</b> · '
              f'fouls <b>{lg.get("avg_fouls",0):.1f}</b> · FTA <b>{lg.get("avg_fta",0):.1f}</b>'
              if have else "")

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="NBA referee tendencies: avg total points, fouls and FTA per official, plus tonight's assigned crews and their over/under lean.">
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><meta name="theme-color" content="#0f1419">
<title>Referee Tendencies</title><style>
:root{{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--muted:#8a95a5;--accent:#ef5350;
--hi:#ff8a65;--lo:#4fc3f7;--ev:#8a95a5}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);padding:22px 16px}}
.container{{max-width:880px;margin:0 auto}}
a.menu{{color:#7fb2ff;text-decoration:none;font-size:.85em;font-weight:600}}
h1{{font-size:23px;font-weight:700;margin:6px 0 3px}}
h2{{font-size:15px;font-weight:700;margin:20px 0 9px}}
.sub{{color:var(--muted);font-size:14px;margin-bottom:14px}}
form{{display:flex;gap:10px;align-items:end;margin-bottom:14px}}
label{{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}}
input{{padding:9px 11px;background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:7px;font-size:14px}}
button{{padding:9px 16px;background:var(--accent);color:#fff;border:0;border-radius:7px;font-weight:800;font-size:14px;cursor:pointer}}
.lgline{{color:var(--muted);font-size:12px;margin-bottom:8px}}.lgline b{{color:var(--text)}}
.crewcard{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:11px 14px;margin-bottom:9px}}
.cmatch{{font-weight:800;font-size:15px}}.cnames{{color:var(--muted);font-size:13px;margin:3px 0 5px}}
.cstat{{font-size:13px}}.cstat b{{color:var(--text)}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}}
th,td{{padding:7px 10px;text-align:center;border-bottom:1px solid var(--border);font-size:13.5px}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;background:rgba(255,255,255,.03)}}
td.rf,th.rf{{text-align:left;font-weight:700}}td.num{{font-weight:700}}
.lean{{font-size:.72em;font-weight:800;padding:1px 6px;border-radius:5px;white-space:nowrap}}
.lean.hi{{background:rgba(239,83,80,.18);color:var(--hi)}}
.lean.lo{{background:rgba(79,195,247,.16);color:var(--lo)}}
.lean.ev{{background:rgba(138,149,165,.14);color:var(--ev)}}
.muted{{color:var(--muted)}}.err{{color:#ff8a65;font-size:14px}}.err code{{background:#0f1419;padding:1px 5px;border-radius:4px}}
.note{{color:var(--muted);font-size:12px;margin-top:16px;line-height:1.5}}
</style></head><body><div class="container">
<a class="menu" href="/">&#8962; Main Menu</a>
<h1>Referee Tendencies <span style="font-size:.6em;color:var(--muted)">NBA</span></h1>
<div class="sub">Officials shift totals and free-throw volume. High-total / whistle-heavy crews lean Over; let-play crews lean Under.</div>
<form method="get"><div><label>Game date</label><input type="date" name="date" value="{date_str}"></div>
<button type="submit">Load crews</button></form>
<h2>Crews for {date_str}</h2>{crew_html}
<h2>Referee tendencies <small class="muted" style="font-weight:400">(≥{MIN_GAMES} games)</small></h2>
<div class="lgline">{lgline}</div>{table}
<div class="note">&ldquo;Lean&rdquo; compares each ref/crew to the league average (±1.5 pts total, ±1.0 fouls).
Tendencies reflect the scanned season; crews from the NBA&rsquo;s official assignment feed. Correlation, not causation —
use as one input. For information only.</div>
</div></body></html>"""


@app.route("/")
def index():
    date_str = request.args.get("date", "") or (datetime.now(ET) if ET else datetime.now()).date().isoformat()
    try:
        datetime.fromisoformat(date_str)
    except ValueError:
        date_str = (datetime.now(ET) if ET else datetime.now()).date().isoformat()
    return Response(render(date_str), mimetype="text/html")


def main():
    ap = argparse.ArgumentParser(description="NBA referee tendencies")
    ap.add_argument("--build", action="store_true", help="scan the season and write the CSV")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--port", type=int, default=5014)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if args.build:
        build_ref_stats(args.season)
        return
    print(f"Serving at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
