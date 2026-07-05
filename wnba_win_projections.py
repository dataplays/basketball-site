#!/usr/bin/env python3
"""WNBA season win-totals projector  →  /wins

Projects each team's final regular-season win total: current record PLUS a
Monte-Carlo simulation of every remaining game, using the season ratings
(OE/DE/pace + home-court edge). No market lines — pure model output; compare
to the books yourself.

Data:
  - team ratings: wnba_ratings_2026.csv  (Team, Pace, OE, DE)  [same file the live/props boards use]
  - records + remaining schedule: ESPN WNBA per-team schedule feeds (no key)

Run standalone:  py -3 wnba_win_projections.py [--port 5016]
Mounted on the site at /wins.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from flask import Flask, Response

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

# ── Paths: Documents locally, BBALL_DATA_DIR/data on the server (one file, both places) ──
_ENV_DIR = os.environ.get("BBALL_DATA_DIR")
if _ENV_DIR:
    DATA_DIR = Path(_ENV_DIR)
elif Path(r"C:\Users\User\Documents\wnba_ratings_2026.csv").exists():
    DATA_DIR = Path(r"C:\Users\User\Documents")
else:
    DATA_DIR = Path(__file__).resolve().parent / "data"
RATINGS_CSV = DATA_DIR / "wnba_ratings_2026.csv"

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
UA = {"User-Agent": "Mozilla/5.0"}
HCA = 2.7            # WNBA home-court advantage (points added to the home team's margin)
MARGIN_SD = 10.5     # WNBA single-game margin SD (win prob = Phi(exp_margin / SD))
SIMS = 10000         # Monte-Carlo iterations per team
CACHE_TTL = 900      # 15 min

app = Flask(__name__)
_cache: dict = {"ts": 0.0, "payload": None}
_lock = threading.Lock()


def _get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.load(r)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def load_ratings():
    """{location: {pace, oe, de}} + league-average OE."""
    R = {}
    with open(RATINGS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            R[row["Team"]] = {"pace": float(row["Pace"]), "oe": float(row["OE"]), "de": float(row["DE"])}
    lg = statistics.mean(v["oe"] for v in R.values()) if R else 100.0
    return R, lg


def win_prob(team: str, opp: str, home: bool, R: dict, lg: float) -> float:
    a, b = R.get(team), R.get(opp)
    if not a or not b:
        return 0.5
    pace = (a["pace"] + b["pace"]) / 2.0
    t_ppp = (a["oe"] * b["de"]) / lg / 100.0
    o_ppp = (b["oe"] * a["de"]) / lg / 100.0
    margin = pace * (t_ppp - o_ppp) + (HCA if home else -HCA)
    return _phi(margin / MARGIN_SD)


def fetch_teams():
    """Return {team_id: {loc, abbr}} for WNBA teams."""
    d = _get(f"{ESPN}/teams?limit=50")
    out = {}
    for e in d.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
        t = e.get("team", {})
        out[str(t.get("id", ""))] = {"loc": t.get("location") or t.get("displayName", ""),
                                     "abbr": t.get("abbreviation", "")}
    return out


def _fetch_schedule(tid):
    try:
        return tid, _get(f"{ESPN}/teams/{tid}/schedule")
    except Exception:
        return tid, {"events": []}


def _won(me: dict, opp: dict):
    if me.get("winner") is True:
        return True
    if opp.get("winner") is True:
        return False
    try:
        def sc(c):
            s = c.get("score")
            return float(s.get("value") if isinstance(s, dict) else s)
        return sc(me) > sc(opp)
    except Exception:
        return None


def _parse_record(s: str):
    """'15-5' -> (15, 5); missing/malformed -> (None, None)."""
    import re
    m = re.match(r"\s*(\d+)\s*-\s*(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def build():
    R, lg = load_ratings()
    teams = fetch_teams()
    ids = list(teams)
    with ThreadPoolExecutor(max_workers=10) as ex:
        scheds = dict(ex.map(_fetch_schedule, ids))

    rows = []
    for tid, info in teams.items():
        loc = info["loc"]
        if loc not in R:                       # only rated WNBA teams
            continue
        sch = scheds.get(tid, {})
        # Authoritative current record from ESPN — correctly excludes the
        # Commissioner's Cup final, which appears as a regular-season game in the
        # schedule feed but does NOT count in the standings. Count as fallback.
        wins, losses = _parse_record(sch.get("team", {}).get("recordSummary", ""))
        cw = cl = 0
        remaining = []                          # (opp_loc, home)
        for ev in sch.get("events", []):
            st = ev.get("seasonType", {})
            stid = str(st.get("id") if isinstance(st, dict) else st)
            if stid != "2":                     # regular season only (skip pre/postseason)
                continue
            comp = ev.get("competitions", [{}])[0]
            done = comp.get("status", {}).get("type", {}).get("completed")
            cs = comp.get("competitors", [])
            me = next((c for c in cs if str(c.get("team", {}).get("id")) == tid), None)
            opp = next((c for c in cs if c is not me), None)
            if not me or not opp:
                continue
            opp_loc = teams.get(str(opp.get("team", {}).get("id", "")), {}).get("loc")
            if opp_loc not in R:                # skip non-WNBA (all-star, exhibition)
                continue
            if done:
                res = _won(me, opp)
                if res is True:
                    cw += 1
                elif res is False:
                    cl += 1
            else:
                remaining.append((opp_loc, me.get("homeAway") == "home"))
        if wins is None:                        # recordSummary missing/malformed
            wins, losses = cw, cl

        probs = [win_prob(loc, o, h, R, lg) for o, h in remaining]
        exp_wins = wins + sum(probs)
        # Monte-Carlo the remaining games (precomputed probs) for the distribution
        sims = []
        for _ in range(SIMS):
            sims.append(wins + sum(1 for p in probs if random.random() < p))
        sims.sort()
        rows.append({
            "team": loc, "abbr": info["abbr"], "w": wins, "l": losses,
            "left": len(remaining), "proj": exp_wins,
            "proj_l": (wins + losses + len(remaining)) - exp_wins,
            "p20": sims[int(0.20 * SIMS)], "p80": sims[int(0.80 * SIMS)],
            "lo": sims[int(0.05 * SIMS)], "hi": sims[int(0.95 * SIMS)],
        })
    rows.sort(key=lambda r: -r["proj"])
    ts = datetime.now(ET) if ET else datetime.now()
    return {"rows": rows, "updated": ts.strftime("%b %d, %I:%M %p ET"),
            "n_sims": SIMS, "games": sum(r["w"] + r["l"] for r in rows)}


def get_payload():
    with _lock:
        if _cache["payload"] and time.time() - _cache["ts"] < CACHE_TTL:
            return _cache["payload"]
    payload = build()
    with _lock:
        _cache.update(ts=time.time(), payload=payload)
    return payload


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><meta name="theme-color" content="#0f1419">
<title>WNBA Season Win Totals — Projections</title><style>
:root{{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--muted:#8a95a5;--accent:#c2185b;--good:#3fb950}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);padding:22px 16px}}
.container{{max-width:820px;margin:0 auto}}
a.menu{{color:#7fb2ff;text-decoration:none;font-size:.85em;font-weight:600}}
h1{{font-size:23px;font-weight:700;margin:6px 0 3px}}
.sub{{color:var(--muted);font-size:13.5px;margin-bottom:14px;line-height:1.5}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}}
th,td{{padding:8px 10px;text-align:center;border-bottom:1px solid var(--border);font-size:13.5px;font-variant-numeric:tabular-nums}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;background:rgba(255,255,255,.03)}}
td.tm,th.tm{{text-align:left;font-weight:700}}
td.proj{{font-weight:800;color:var(--good);font-size:15px}}
tr:hover td{{background:rgba(255,255,255,.02)}}
.rng{{color:var(--muted);font-size:12px}}
.note{{color:var(--muted);font-size:12px;margin-top:14px;line-height:1.5}}
.err{{color:#ff8a65;padding:40px 0;text-align:center}}
</style></head><body><div class="container">
<a class="menu" href="/">&#8962; Main Menu</a>
<h1>WNBA Season Win Totals <span style="font-size:.6em;color:var(--muted)">Projections</span></h1>
<div class="sub">Projected final regular-season wins = current record + a Monte-Carlo simulation
({sims:,} runs) of every remaining game, using each team's season <b>OE / DE / pace</b> ratings and
home-court edge. <b>No market lines</b> — compare to the books yourself.</div>
{body}
<div class="note">Win prob per game = &Phi;(expected margin / {sd}), expected margin from opponent-adjusted
ratings + {hca}-pt home edge. <b>Proj</b> is the mean; <b>likely range</b> is the 20th&ndash;80th percentile of the
simulated total (the 5&ndash;95% span is wider). Ratings from <code>wnba_ratings_2026.csv</code>; schedule/records
from ESPN. Updated {updated}. Informational only.</div>
</div></body></html>"""


def render():
    try:
        p = get_payload()
    except Exception as e:  # noqa: BLE001
        return PAGE.format(sims=SIMS, sd=MARGIN_SD, hca=HCA, updated="—",
                           body=f'<div class="err">Could not build projections: {e}</div>')
    if not p["rows"]:
        body = '<div class="err">No rated WNBA teams / schedule data available right now.</div>'
    else:
        head = ("<tr><th class='tm'>Team</th><th>Record</th><th>Left</th>"
                "<th>Proj Wins</th><th>Proj Record</th><th>Likely Range</th></tr>")
        trs = ""
        for r in p["rows"]:
            trs += (f"<tr><td class='tm'>{r['team']}</td>"
                    f"<td>{r['w']}-{r['l']}</td><td>{r['left']}</td>"
                    f"<td class='proj'>{r['proj']:.1f}</td>"
                    f"<td>{r['proj']:.0f}-{r['proj_l']:.0f}</td>"
                    f"<td class='rng'>{r['p20']}&ndash;{r['p80']} <span style='opacity:.6'>(5–95%: {r['lo']}–{r['hi']})</span></td></tr>")
        body = f"<table><thead>{head}</thead><tbody>{trs}</tbody></table>"
    return PAGE.format(sims=p.get("n_sims", SIMS), sd=MARGIN_SD, hca=HCA,
                       updated=p.get("updated", "—"), body=body)


@app.route("/")
def index():
    return Response(render(), mimetype="text/html")


@app.route("/refresh")
def refresh():
    with _lock:
        _cache["payload"] = None
    return render()


def main():
    ap = argparse.ArgumentParser(description="WNBA season win-totals projector")
    ap.add_argument("--port", type=int, default=5016)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--once", action="store_true", help="print the projection table to console and exit")
    args = ap.parse_args()
    if args.once:
        p = build()
        print(f"WNBA projected win totals  (updated {p['updated']}, {p['n_sims']:,} sims)")
        print(f"{'Team':16} {'Rec':>7} {'Left':>4} {'Proj':>6}  Range(20-80)")
        for r in p["rows"]:
            print(f"{r['team']:16} {r['w']:>3}-{r['l']:<3} {r['left']:>4} {r['proj']:>6.1f}  {r['p20']}-{r['p80']}")
        return
    print(f"WNBA win totals -> http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
