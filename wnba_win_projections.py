#!/usr/bin/env python3
"""WNBA season win-totals projector  →  /wins

Projects each team's final regular-season win total: current record PLUS a
Monte-Carlo simulation of every remaining game, using the season ratings
(OE/DE/pace + home-court edge). No market lines — pure model output; compare
to the books yourself.

Method (per the win-totals strategy):
  - Win prob per game = logistic(projected margin / WINPROB_SCALE), where the
    margin comes from the SAME opponent-adjusted PPP construction as the live
    dashboard: away_ppp = (away_oe * home_de) / national_avg_oe / 100, so the
    two systems stay consistent.
  - Monte-Carlo with RATING UNCERTAINTY: each iteration perturbs every team's
    net rating by Gaussian noise whose sigma shrinks as games-played grows
    (sigma = RATING_NOISE_BASE / sqrt(games_played)). A fixed-rating Bernoulli
    sim is overconfident; this widens the distribution honestly — important when
    comparing to season win-total markets.
  - Outputs per team: mean projected wins, 10th/90th-percentile band, full
    distribution (sparkline), and a playoff-odds proxy (top-8 by simulated wins).

Data:
  - team ratings: wnba_ratings_2026.csv  (Team, Pace, OE, DE)  [same file the live/props boards use]
  - records + remaining schedule: ESPN WNBA per-team schedule feeds (no key)

Tunable top constants: WINPROB_SCALE, RATING_NOISE_BASE, HCA, SIMS, PLAYOFF_SPOTS.

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
from collections import Counter
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

# ── Tunable model constants ───────────────────────────────────────────────────
HCA = 2.7                 # WNBA home-court advantage (pts added to the home team's margin); 0 at neutral
WINPROB_SCALE = 6.8       # logistic scale: P(win) = 1/(1+exp(-margin/SCALE)); ~ margin SD 11-12
RATING_NOISE_BASE = 11.0  # per-iteration net-rating noise; sigma_team = BASE / sqrt(games_played)
SIMS = 10000              # Monte-Carlo iterations
PLAYOFF_SPOTS = 8         # WNBA playoff field (top-8 by wins, proxy — no formal tiebreakers)
CACHE_TTL = 900           # 15 min

app = Flask(__name__)
_cache: dict = {"ts": 0.0, "payload": None}
_lock = threading.Lock()


def _get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.load(r)


def load_ratings():
    """{location: {pace, oe, de}} + league-average OE."""
    R = {}
    with open(RATINGS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            R[row["Team"]] = {"pace": float(row["Pace"]), "oe": float(row["OE"]), "de": float(row["DE"])}
    lg = statistics.mean(v["oe"] for v in R.values()) if R else 100.0
    return R, lg


def base_margin(team: str, opp: str, hca_pts: float, R: dict, lg: float) -> float:
    """Projected margin (team − opp) from opponent-adjusted PPP + home edge."""
    a, b = R.get(team), R.get(opp)
    if not a or not b:
        return hca_pts
    pace = (a["pace"] + b["pace"]) / 2.0
    t_ppp = (a["oe"] * b["de"]) / lg / 100.0
    o_ppp = (b["oe"] * a["de"]) / lg / 100.0
    return pace * (t_ppp - o_ppp) + hca_pts


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


def _sparkline(sims: list, color: str = "#66bb6a") -> str:
    """Tiny inline-SVG histogram of a team's simulated win-total distribution."""
    if not sims:
        return ""
    lo, hi = sims[0], sims[-1]
    if hi <= lo:
        hi = lo + 1
    cnt = Counter(sims)
    vals = list(range(lo, hi + 1))
    mx = max((cnt.get(v, 0) for v in vals), default=1) or 1
    W, H = 90, 22
    bw = W / len(vals)
    bars = []
    for i, v in enumerate(vals):
        h = (cnt.get(v, 0) / mx) * (H - 2)
        bars.append(f'<rect x="{i*bw:.1f}" y="{H-h:.1f}" width="{max(bw-0.7,0.6):.1f}" '
                    f'height="{h:.1f}" fill="{color}" opacity="0.85"/>')
    return (f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
            f'preserveAspectRatio="none" role="img">{"".join(bars)}</svg>')


def build():
    R, lg = load_ratings()
    teams = fetch_teams()
    ids = list(teams)
    with ThreadPoolExecutor(max_workers=10) as ex:
        scheds = dict(ex.map(_fetch_schedule, ids))

    # ── Gather per-team current record + remaining games (with fixed base margins) ──
    tdata = {}
    for tid, info in teams.items():
        loc = info["loc"]
        if loc not in R:                        # only rated WNBA teams
            continue
        sch = scheds.get(tid, {})
        # Authoritative record from ESPN — excludes the Commissioner's Cup final
        # (tagged regular-season in the feed but not counted in the standings).
        wins, losses = _parse_record(sch.get("team", {}).get("recordSummary", ""))
        cw = cl = 0
        remaining = []                          # (opp_loc, base_margin)
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
                if comp.get("neutralSite"):
                    hca = 0.0
                elif me.get("homeAway") == "home":
                    hca = HCA
                else:
                    hca = -HCA
                remaining.append((opp_loc, base_margin(loc, opp_loc, hca, R, lg)))
        if wins is None:                        # recordSummary missing/malformed
            wins, losses = cw, cl
        tdata[loc] = {"wins": wins, "losses": losses, "gp": wins + losses,
                      "abbr": info["abbr"], "remaining": remaining}

    # ── Monte Carlo with per-iteration rating noise ──────────────────────────────
    locs = list(tdata)
    sigma = {t: RATING_NOISE_BASE / math.sqrt(max(tdata[t]["gp"], 1)) for t in locs}
    sim_wins = {t: [] for t in locs}
    playoff = {t: 0 for t in locs}
    gauss, rnd, exp, scale = random.gauss, random.random, math.exp, WINPROB_SCALE
    for _ in range(SIMS):
        noise = {t: gauss(0.0, sigma[t]) for t in locs}
        totals = {}
        for t in locs:
            nt = noise[t]
            w = tdata[t]["wins"]
            for opp, bm in tdata[t]["remaining"]:
                m = bm + nt - noise[opp]
                if m >= 0:                       # numerically-stable logistic
                    p = 1.0 / (1.0 + exp(-m / scale))
                else:
                    e = exp(m / scale)
                    p = e / (1.0 + e)
                if rnd() < p:
                    w += 1
            totals[t] = w
            sim_wins[t].append(w)
        # playoff proxy: top-8 by simulated wins, random tiebreak (no formal WNBA tiebreakers)
        ranked = sorted(locs, key=lambda t: (totals[t], rnd()), reverse=True)
        for t in ranked[:PLAYOFF_SPOTS]:
            playoff[t] += 1

    rows = []
    for t in locs:
        sims = sorted(sim_wins[t])
        d = tdata[t]
        mean = sum(sims) / len(sims)
        total_games = d["gp"] + len(d["remaining"])
        rows.append({
            "team": t, "abbr": d["abbr"], "w": d["wins"], "l": d["losses"],
            "left": len(d["remaining"]), "proj": mean, "proj_l": total_games - mean,
            "p10": sims[int(0.10 * SIMS)], "p90": sims[int(0.90 * SIMS)],
            "playoff": 100.0 * playoff[t] / SIMS,
            "spark": _sparkline(sims),
        })
    rows.sort(key=lambda r: -r["proj"])
    ts = datetime.now(ET) if ET else datetime.now()
    return {"rows": rows, "updated": ts.strftime("%b %d, %I:%M %p ET"), "n_sims": SIMS}


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
:root{{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--muted:#8a95a5;--accent:#c2185b;--good:#66bb6a}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);padding:22px 16px}}
.container{{max-width:900px;margin:0 auto}}
a.menu{{color:#7fb2ff;text-decoration:none;font-size:.85em;font-weight:600}}
h1{{font-size:23px;font-weight:700;margin:6px 0 3px}}
.sub{{color:var(--muted);font-size:13.5px;margin-bottom:14px;line-height:1.5}}
.wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}}
th,td{{padding:8px 10px;text-align:center;border-bottom:1px solid var(--border);font-size:13.5px;font-variant-numeric:tabular-nums;white-space:nowrap}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;background:rgba(255,255,255,.03)}}
td.tm,th.tm{{text-align:left;font-weight:700}}
td.proj{{font-weight:800;color:var(--good);font-size:15px}}
td.po{{font-weight:700}}
tr:hover td{{background:rgba(255,255,255,.02)}}
.rng{{color:var(--muted);font-size:12px}}
.spark{{line-height:0}}
.note{{color:var(--muted);font-size:12px;margin-top:14px;line-height:1.55}}
.err{{color:#ff8a65;padding:40px 0;text-align:center}}
</style></head><body><div class="container">
<a class="menu" href="/">&#8962; Main Menu</a>
<h1>WNBA Season Win Totals <span style="font-size:.6em;color:var(--muted)">Projections</span></h1>
<div class="sub">Projected final regular-season wins = current record + a Monte-Carlo simulation
({sims:,} runs) of every remaining game. Win prob per game = <b>logistic(margin / {scale})</b> from the
same opponent-adjusted <b>OE / DE / pace</b> ratings as the live board, plus a {hca}-pt home edge. Each
run also <b>perturbs team ratings</b> (uncertainty shrinking with games played) so the bands are honest,
not overconfident. <b>No market lines</b> — compare to the books yourself.</div>
<div class="wrap">{body}</div>
<div class="note"><b>Proj</b> is the mean simulated win total; <b>80% range</b> is the 10th–90th percentile
of the distribution (shown as the sparkline). <b>Playoff%</b> = share of sims a team finishes top-{spots}
by wins (a proxy — no formal WNBA tiebreakers). Ratings from <code>wnba_ratings_2026.csv</code>;
schedule/records from ESPN (records via <code>recordSummary</code>, so the Commissioner's Cup final isn't
counted). Tune <code>WINPROB_SCALE</code> / <code>RATING_NOISE_BASE</code> against realized results.
Updated {updated}. Informational only.</div>
</div></body></html>"""


def render():
    try:
        p = get_payload()
    except Exception as e:  # noqa: BLE001
        return PAGE.format(sims=SIMS, scale=WINPROB_SCALE, hca=HCA, spots=PLAYOFF_SPOTS,
                           updated="—", body=f'<div class="err">Could not build projections: {e}</div>')
    if not p["rows"]:
        body = '<div class="err">No rated WNBA teams / schedule data available right now.</div>'
    else:
        head = ("<tr><th class='tm'>Team</th><th>Record</th><th>Left</th>"
                "<th>Proj Wins</th><th>Proj Record</th><th>80% Range</th>"
                "<th>Playoff%</th><th>Distribution</th></tr>")
        trs = ""
        for r in p["rows"]:
            po = r["playoff"]
            po_col = "#66bb6a" if po >= 80 else ("#c2185b" if po <= 20 else "#e8ecf1")
            trs += (f"<tr><td class='tm'>{r['team']}</td>"
                    f"<td>{r['w']}-{r['l']}</td><td>{r['left']}</td>"
                    f"<td class='proj'>{r['proj']:.1f}</td>"
                    f"<td>{r['proj']:.0f}-{r['proj_l']:.0f}</td>"
                    f"<td class='rng'>{r['p10']}&ndash;{r['p90']}</td>"
                    f"<td class='po' style='color:{po_col}'>{po:.0f}%</td>"
                    f"<td class='spark'>{r['spark']}</td></tr>")
        body = f"<table><thead>{head}</thead><tbody>{trs}</tbody></table>"
    return PAGE.format(sims=p.get("n_sims", SIMS), scale=WINPROB_SCALE, hca=HCA,
                       spots=PLAYOFF_SPOTS, updated=p.get("updated", "—"), body=body)


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
        print(f"{'Team':16} {'Rec':>7} {'Left':>4} {'Proj':>6} {'10-90':>7} {'Playoff':>8}")
        for r in p["rows"]:
            print(f"{r['team']:16} {r['w']:>3}-{r['l']:<3} {r['left']:>4} {r['proj']:>6.1f} "
                  f"{r['p10']:>3}-{r['p90']:<3} {r['playoff']:>7.0f}%")
        return
    print(f"WNBA win totals -> http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
