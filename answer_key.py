#!/usr/bin/env python3
r"""
answer_key.py — Feustel-style "Answer Key" empirical derivative pricer.

Enter a game's full-game market (home spread + total). The tool finds the N
most similar historical games by CLOSING line (distance metric with the
total down-weighted, default /1.85 per Feustel), then prices derivatives
straight from how those games actually went: moneyline, alt spreads/totals
(with real push mass on integers), 1st half, 1st quarter, 2nd half, and team
totals — each next to the Normal-model fair number (the /pricer math) so
disagreements stand out. Data comes from answer_key_{league}.csv built by
answer_key_data.py.

Usage:
    py -3 answer_key.py                              # web UI on :5025
    py -3 answer_key.py --league wnba --spread -11.5 --total 176.5   # console
"""
import argparse
import csv
import math
import os
import sys
from statistics import mean, median, pstdev

DOCS = r"C:\Users\User\Documents"
DATA_DIR = os.environ.get("BBALL_DATA_DIR") or (
    DOCS if os.path.isdir(DOCS) else os.path.join(os.path.dirname(__file__), "data"))

DEFAULT_N = 200
DEFAULT_W = 1.85          # total distance divisor (Feustel first-pass weighting)

LEAGUE_CFG = {
    "wnba": {"label": "WNBA", "periods": 4,
             "spread_sd": 10.5, "total_sd": 14.0, "avg_total": 162.0},
    "nba":  {"label": "NBA", "periods": 4,
             "spread_sd": 11.0, "total_sd": 16.0, "avg_total": 225.0},
    "cbb":  {"label": "NCAA Men", "periods": 2,
             "spread_sd": 10.0, "total_sd": 12.5, "avg_total": 142.0},
    "wcbb": {"label": "NCAA Women", "periods": 4,
             "spread_sd": 10.5, "total_sd": 13.0, "avg_total": 140.0},
}

_CACHE = {}


def csv_path(lg):
    return os.path.join(DATA_DIR, f"answer_key_{lg}.csv")


def load_rows(lg):
    path = csv_path(lg)
    if not os.path.exists(path):
        return []
    mtime = os.path.getmtime(path)
    hit = _CACHE.get(lg)
    if hit and hit[0] == mtime:
        return hit[1]
    rows = []
    periods = LEAGUE_CFG[lg]["periods"]
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                row = {
                    "season": int(r["season"]),
                    "season_type": int(r["season_type"]),
                    "date": r["date"], "away": r["away"], "home": r["home"],
                    "neutral": r["neutral"] == "1",
                    "spread": float(r["spread_home"]),
                    "total": float(r["total"]),
                    "as": int(r["away_score"]), "hs": int(r["home_score"]),
                    "ot": int(r["ot_periods"] or 0),
                }
            except (ValueError, KeyError):
                continue
            # period scores (may be blank on games missing linescores)
            try:
                ap = [int(r[f"away_p{i}"]) for i in range(1, periods + 1)]
                hp = [int(r[f"home_p{i}"]) for i in range(1, periods + 1)]
                row["ap"], row["hp"] = ap, hp
            except (ValueError, KeyError):
                row["ap"] = row["hp"] = None
            rows.append(row)
    _CACHE[lg] = (mtime, rows)
    return rows


# ── sampling ──

def sample_games(rows, spread, total, n, w, neutral_mode, include_playoffs):
    cand = []
    for r in rows:
        if not include_playoffs and r["season_type"] == 3:
            continue
        if neutral_mode == "exclude" and r["neutral"]:
            continue
        if neutral_mode == "only" and not r["neutral"]:
            continue
        d = math.hypot(r["spread"] - spread, (r["total"] - total) / w)
        cand.append((d, r))
    cand.sort(key=lambda x: x[0])
    sel = cand[:n]
    return [r for _, r in sel], (sel[-1][0] if sel else 0.0)


def metrics(sample, lg):
    """Home-perspective outcome arrays from the sample."""
    per = LEAGUE_CFG[lg]["periods"]
    m = {"margin": [], "total": [], "hpts": [], "apts": [],
         "h1m": [], "h1t": [], "q1m": [], "q1t": [], "h2m": [], "h2t": []}
    for r in sample:
        m["margin"].append(r["hs"] - r["as"])
        m["total"].append(r["hs"] + r["as"])
        m["hpts"].append(r["hs"])
        m["apts"].append(r["as"])
        if r["ap"] is None:
            continue
        if per == 2:                       # CBB: p1 IS the first half
            h1h, h1a = r["hp"][0], r["ap"][0]
        else:
            h1h, h1a = sum(r["hp"][:2]), sum(r["ap"][:2])
            m["q1m"].append(r["hp"][0] - r["ap"][0])
            m["q1t"].append(r["hp"][0] + r["ap"][0])
        m["h1m"].append(h1h - h1a)
        m["h1t"].append(h1h + h1a)
        m["h2m"].append((r["hs"] - r["as"]) - (h1h - h1a))   # incl. OT
        m["h2t"].append((r["hs"] + r["as"]) - (h1h + h1a))
    return m


# ── pricing ──

def american(p):
    p = min(max(p, 0.001), 0.999)
    return (f"-{round(100 * p / (1 - p))}" if p >= 0.5
            else f"+{round(100 * (1 - p) / p)}")


def emp_price(xs, line):
    """P(X > line) conditional on no push, plus push share."""
    over = sum(1 for x in xs if x > line)
    under = sum(1 for x in xs if x < line)
    push = len(xs) - over - under
    if over + under == 0:
        return None
    return over / (over + under), push / len(xs)


def half_line(x):
    return round(x * 2) / 2


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_price(x_mean, sd, line):
    """Same convention as /pricer: integer lines get a +/-0.5 push band,
    prob returned conditional on no push."""
    if sd <= 0:
        return None
    if abs(line - round(line)) < 1e-9:
        po = 1 - norm_cdf((line + 0.5 - x_mean) / sd)
        pu = norm_cdf((line - 0.5 - x_mean) / sd)
        return po / (po + pu) if po + pu else None
    return 1 - norm_cdf((line - x_mean) / sd)


def build_report(lg, spread, total, n=DEFAULT_N, w=DEFAULT_W,
                 neutral_mode="exclude", include_playoffs=True):
    cfg = LEAGUE_CFG[lg]
    rows = load_rows(lg)
    if not rows:
        return {"error": f"No dataset for {cfg['label']} yet — run: "
                         f"py -3 answer_key_data.py --league {lg}"}
    sample, radius = sample_games(rows, spread, total, n, w,
                                  neutral_mode, include_playoffs)
    if len(sample) < 30:
        return {"error": f"Only {len(sample)} comparable games — dataset too "
                         f"thin for these filters."}
    m = metrics(sample, lg)
    scale = math.sqrt(max(total, 1) / cfg["avg_total"])
    sd_m = cfg["spread_sd"] * scale
    sd_t = cfg["total_sd"] * scale

    def ladder(xs, lines, model_mean, model_sd):
        out = []
        for L in lines:
            got = emp_price(xs, L)
            if not got:
                continue
            po, push = got
            mp = normal_price(model_mean, model_sd, L)
            out.append({"line": L, "p_over": po, "push": push,
                        "emp_o": american(po), "emp_u": american(1 - po),
                        "mod_o": american(mp) if mp is not None else "—",
                        "mod_u": american(1 - mp) if mp is not None else "—"})
        return out

    def steps(center, halfspan, step=1.0):
        k = int(round(halfspan / step))
        return [round(center + i * step, 1) for i in range(-k, k + 1)]

    seasons = {}
    for r in sample:
        seasons[r["season"]] = seasons.get(r["season"], 0) + 1

    rep = {
        "league": lg, "label": cfg["label"], "spread": spread, "total": total,
        "n": len(sample), "pool": len(rows), "radius": radius, "w": w,
        "sample_spread": mean(r["spread"] for r in sample),
        "sample_total": mean(r["total"] for r in sample),
        "seasons": dict(sorted(seasons.items())),
        "ot_pct": 100 * sum(1 for r in sample if r["ot"]) / len(sample),
        "playoff_pct": 100 * sum(1 for r in sample if r["season_type"] == 3) / len(sample),
        "linescore_n": len(m["h1m"]),
        # full game
        "home_win": sum(1 for x in m["margin"] if x > 0) / max(1, sum(1 for x in m["margin"] if x != 0)),
        "margin_mean": mean(m["margin"]), "margin_med": median(m["margin"]),
        "margin_sd": pstdev(m["margin"]),
        "total_mean": mean(m["total"]), "total_med": median(m["total"]),
        "total_sd_emp": pstdev(m["total"]),
        "model_margin_mean": -spread, "model_sd_m": sd_m, "model_sd_t": sd_t,
        "spread_ladder": ladder(m["margin"], [-x for x in steps(spread, 5)],
                                -spread, sd_m),
        "total_ladder": ladder(m["total"], steps(total, 8, 2), total, sd_t),
        "hist_margin": m["margin"], "hist_total": m["total"],
    }
    # NOTE: spread ladder lines are stored as MARGIN thresholds (-line);
    # rendering converts back to the home-handicap convention.

    if m["h1m"]:
        f = 0.5
        rep["h1"] = {
            "fair_spread": half_line(-median(m["h1m"])),
            "fair_total": half_line(median(m["h1t"])),
            "model_spread": half_line(-(-spread * f)),
            "model_total": half_line(total * f),
            "spread_ladder": ladder(m["h1m"],
                                    steps(median(m["h1m"]), 3),
                                    -spread * f, sd_m * math.sqrt(f)),
            "total_ladder": ladder(m["h1t"], steps(median(m["h1t"]), 4),
                                   total * f, sd_t * math.sqrt(f)),
        }
        rep["h2"] = {
            "fair_spread": half_line(-median(m["h2m"])),
            "fair_total": half_line(median(m["h2t"])),
            "h2m_mean": mean(m["h2m"]), "h2t_mean": mean(m["h2t"]),
        }
    if m["q1m"]:
        f = 0.25
        rep["q1"] = {
            "fair_spread": half_line(-median(m["q1m"])),
            "fair_total": half_line(median(m["q1t"])),
            "spread_ladder": ladder(m["q1m"], steps(median(m["q1m"]), 2),
                                    -spread * f, sd_m * math.sqrt(f)),
            "total_ladder": ladder(m["q1t"], steps(median(m["q1t"]), 3),
                                   total * f, sd_t * math.sqrt(f)),
        }
    # team totals (model: score = (T -/+ M)/2, sd = sqrt(sd_t^2+sd_m^2)/2)
    sd_team = math.sqrt(sd_t ** 2 + sd_m ** 2) / 2
    rep["teams"] = []
    for side, xs, mmean in (("Home", m["hpts"], (total - spread) / 2),
                            ("Away", m["apts"], (total + spread) / 2)):
        fair = half_line(median(xs))
        rep["teams"].append({
            "side": side, "fair": fair, "mean": mean(xs),
            "model_fair": half_line(mmean),
            "ladder": ladder(xs, steps(fair, 3), mmean, sd_team),
        })
    return rep


# ── rendering ──

def svg_hist(xs, width=260, height=56, bins=17, accent="#d4af37"):
    if not xs:
        return ""
    lo, hi = min(xs), max(xs)
    if hi == lo:
        hi = lo + 1
    step = (hi - lo) / bins
    counts = [0] * bins
    for x in xs:
        counts[min(bins - 1, int((x - lo) / step))] += 1
    mx = max(counts)
    bw = width / bins
    bars = "".join(
        f'<rect x="{i * bw:.1f}" y="{height - c / mx * (height - 8):.1f}" '
        f'width="{bw - 1:.1f}" height="{c / mx * (height - 8):.1f}" '
        f'fill="{accent}" opacity="0.75"/>' for i, c in enumerate(counts))
    return (f'<svg width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">{bars}'
            f'<text x="2" y="{height - 1}" font-size="9" fill="#8aa">{lo:g}</text>'
            f'<text x="{width - 2}" y="{height - 1}" font-size="9" fill="#8aa" '
            f'text-anchor="end">{hi:g}</text></svg>')


def fmt_hcap(x):
    return f"{x:+g}".replace("+", "+").replace("-0", "0") if x else "PK"


def render_ladder_rows(ladder, kind):
    out = []
    for r in ladder:
        if kind == "spread":         # r['line'] is a MARGIN threshold
            label = f"Home {fmt_hcap(-r['line'])}"
            a, b = "Home", "Away"
        else:
            label = f"{r['line']:g}"
            a, b = "Over", "Under"
        out.append(
            f"<tr><td>{label}</td>"
            f"<td>{100 * r['p_over']:.1f}%</td>"
            f"<td>{100 * r['push']:.1f}%</td>"
            f"<td class='emp'>{a} {r['emp_o']} / {b} {r['emp_u']}</td>"
            f"<td class='mod'>{a} {r['mod_o']} / {b} {r['mod_u']}</td></tr>")
    return "".join(out)


CSS = """
body{background:#10151c;color:#dfe7ef;font-family:Segoe UI,Arial,sans-serif;
     margin:0;padding:14px 18px}
h1{color:#d4af37;font-size:20px;margin:4px 0 10px}
h2{color:#d4af37;font-size:15px;border-bottom:1px solid #2a3542;
   padding-bottom:3px;margin:20px 0 8px}
form{background:#171f29;border:1px solid #2a3542;border-radius:8px;
     padding:10px 12px;display:flex;flex-wrap:wrap;gap:10px;align-items:end}
label{font-size:11px;color:#9ab;display:block}
input,select{background:#0e131a;color:#dfe7ef;border:1px solid #33414f;
             border-radius:4px;padding:5px 7px;font-size:13px}
input[type=number]{width:90px}
button{background:#d4af37;color:#10151c;font-weight:700;border:none;
       border-radius:5px;padding:7px 16px;font-size:13px;cursor:pointer}
table{border-collapse:collapse;font-size:12.5px;margin:6px 0}
th,td{padding:3px 10px;border-bottom:1px solid #222c37;text-align:left}
th{color:#9ab;font-weight:600}
.emp{color:#ffd76b}.mod{color:#7fb4d8}
.pill{display:inline-block;background:#1d2836;border:1px solid #33414f;
      border-radius:12px;padding:2px 10px;margin:2px 4px 2px 0;font-size:12px}
.note{color:#8aa;font-size:11.5px;margin:4px 0}
.err{color:#ff7b6b;font-size:14px;margin:14px 0}
.grid{display:flex;flex-wrap:wrap;gap:26px}
.big{font-size:17px;color:#ffd76b;font-weight:700}
"""


def render_page(rep, form):
    body = [f"<style>{CSS}</style><title>Answer Key</title>",
            "<h1>&#128273; Answer Key &mdash; empirical derivative pricer</h1>",
            "<div class='note'>Feustel-style: prices read from historical games "
            "whose closing spread/total were closest to yours. "
            "<span class='emp'>Gold = empirical</span> &middot; "
            "<span class='mod'>blue = Normal model (/pricer math)</span>. "
            "Spread is the HOME line (negative = home favored).</div>",
            form]
    if not rep:
        body.append("<p class='note'>Enter a market above.</p>")
        return "".join(body)
    if "error" in rep:
        body.append(f"<p class='err'>{rep['error']}</p>")
        return "".join(body)

    seasons = " ".join(f"<span class='pill'>{y}: {c}</span>"
                       for y, c in rep["seasons"].items())
    body.append(
        f"<h2>Sample</h2>"
        f"<div class='note'>{rep['n']} games (of {rep['pool']} in pool), "
        f"distance radius {rep['radius']:.2f} "
        f"(total weighted 1/{rep['w']:g}) &middot; sample avg line "
        f"{rep['sample_spread']:+.1f} / {rep['sample_total']:.1f} vs yours "
        f"{rep['spread']:+.1f} / {rep['total']:.1f} &middot; "
        f"OT {rep['ot_pct']:.0f}% &middot; playoffs {rep['playoff_pct']:.0f}% "
        f"&middot; linescores {rep['linescore_n']}/{rep['n']}</div>{seasons}")

    body.append(
        f"<h2>Full game</h2><div class='grid'><div>"
        f"<div>Moneyline (home): <span class='big'>{american(rep['home_win'])}"
        f"</span> <span class='note'>({100 * rep['home_win']:.1f}% win)</span></div>"
        f"<div class='note'>margin mean {rep['margin_mean']:+.1f} / median "
        f"{rep['margin_med']:+.1f} / sd {rep['margin_sd']:.1f} "
        f"(model sd {rep['model_sd_m']:.1f}) &middot; total mean "
        f"{rep['total_mean']:.1f} / sd {rep['total_sd_emp']:.1f} "
        f"(model sd {rep['model_sd_t']:.1f})</div>"
        f"<div>Margin {svg_hist(rep['hist_margin'])}</div>"
        f"<div>Total {svg_hist(rep['hist_total'], accent='#7fb4d8')}</div></div>"
        f"<div><table><tr><th>Alt spread</th><th>Home cover</th><th>Push</th>"
        f"<th>Empirical fair</th><th>Normal model</th></tr>"
        f"{render_ladder_rows(rep['spread_ladder'], 'spread')}</table></div>"
        f"<div><table><tr><th>Alt total</th><th>Over</th><th>Push</th>"
        f"<th>Empirical fair</th><th>Normal model</th></tr>"
        f"{render_ladder_rows(rep['total_ladder'], 'total')}</table></div></div>")

    if "h1" in rep:
        h1 = rep["h1"]
        body.append(
            f"<h2>First half</h2>"
            f"<div>Fair 1H line: <span class='big'>Home {fmt_hcap(h1['fair_spread'])}"
            f" / {h1['fair_total']:g}</span> <span class='note'>(model: Home "
            f"{fmt_hcap(h1['model_spread'])} / {h1['model_total']:g})</span></div>"
            f"<div class='grid'><div><table><tr><th>1H spread</th><th>Home cover</th>"
            f"<th>Push</th><th>Empirical fair</th><th>Normal model</th></tr>"
            f"{render_ladder_rows(h1['spread_ladder'], 'spread')}</table></div>"
            f"<div><table><tr><th>1H total</th><th>Over</th><th>Push</th>"
            f"<th>Empirical fair</th><th>Normal model</th></tr>"
            f"{render_ladder_rows(h1['total_ladder'], 'total')}</table></div></div>")
    if "q1" in rep:
        q1 = rep["q1"]
        body.append(
            f"<h2>First quarter</h2>"
            f"<div>Fair 1Q line: <span class='big'>Home {fmt_hcap(q1['fair_spread'])}"
            f" / {q1['fair_total']:g}</span></div>"
            f"<div class='grid'><div><table><tr><th>1Q spread</th><th>Home cover</th>"
            f"<th>Push</th><th>Empirical fair</th><th>Normal model</th></tr>"
            f"{render_ladder_rows(q1['spread_ladder'], 'spread')}</table></div>"
            f"<div><table><tr><th>1Q total</th><th>Over</th><th>Push</th>"
            f"<th>Empirical fair</th><th>Normal model</th></tr>"
            f"{render_ladder_rows(q1['total_ladder'], 'total')}</table></div></div>")
    if "h2" in rep:
        h2 = rep["h2"]
        body.append(
            f"<h2>Second half (incl. OT)</h2>"
            f"<div>Fair 2H line: <span class='big'>Home {fmt_hcap(h2['fair_spread'])}"
            f" / {h2['fair_total']:g}</span> <span class='note'>2H margin mean "
            f"{h2['h2m_mean']:+.1f}, total mean {h2['h2t_mean']:.1f}</span></div>")

    tt = "<h2>Team totals</h2><div class='grid'>"
    for t in rep["teams"]:
        tt += (f"<div><div>{t['side']}: <span class='big'>{t['fair']:g}</span> "
               f"<span class='note'>(mean {t['mean']:.1f}, model "
               f"{t['model_fair']:g})</span></div>"
               f"<table><tr><th>Line</th><th>Over</th><th>Push</th>"
               f"<th>Empirical fair</th><th>Normal model</th></tr>"
               f"{render_ladder_rows(t['ladder'], 'total')}</table></div>")
    body.append(tt + "</div>")
    body.append("<div class='note'>Alt-spread rows show real push mass on "
                "integer lines — something the Normal band only approximates. "
                "Empirical and model agreeing = market-shaped info only; "
                "big gaps = where the Normal assumption bends.</div>")
    return "".join(body)


def build_form(lg="wnba", spread="", total="", n=DEFAULT_N, w=DEFAULT_W,
               neutral="exclude", playoffs="on"):
    opts = "".join(
        f"<option value='{k}'{' selected' if k == lg else ''}>"
        f"{v['label']}{'' if os.path.exists(csv_path(k)) else ' (no data yet)'}"
        f"</option>" for k, v in LEAGUE_CFG.items())
    nsel = "".join(
        f"<option value='{v}'{' selected' if v == neutral else ''}>{t}</option>"
        for v, t in (("exclude", "Home-court games"),
                     ("include", "Include neutral"), ("only", "Neutral only")))
    return (
        "<form method='post' action=''>"
        f"<div><label>League</label><select name='league'>{opts}</select></div>"
        f"<div><label>Home spread</label><input type='number' step='0.5' "
        f"name='spread' value='{spread}' placeholder='-6.5' required></div>"
        f"<div><label>Total</label><input type='number' step='0.5' "
        f"name='total' value='{total}' placeholder='162.5' required></div>"
        f"<div><label>Sample N</label><input type='number' name='n' "
        f"value='{n}' min='30' max='2000'></div>"
        f"<div><label>Total weight (1/W)</label><input type='number' "
        f"step='0.05' name='w' value='{w}'></div>"
        f"<div><label>Site filter</label><select name='neutral'>{nsel}</select></div>"
        f"<div><label>Playoffs</label><input type='checkbox' name='playoffs'"
        f"{' checked' if playoffs else ''}></div>"
        "<button type='submit'>Price it</button></form>")


# ── flask app ──

try:
    from flask import Flask, request
    app = Flask(__name__)

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "POST":
            lg = request.form.get("league", "wnba")
            try:
                spread = float(request.form["spread"])
                total = float(request.form["total"])
            except (KeyError, ValueError):
                return render_page({"error": "Enter a numeric spread and total."},
                                   build_form(lg))
            n = int(request.form.get("n") or DEFAULT_N)
            w = float(request.form.get("w") or DEFAULT_W)
            neutral = request.form.get("neutral", "exclude")
            playoffs = bool(request.form.get("playoffs"))
            rep = build_report(lg, spread, total, n, w, neutral, playoffs)
            form = build_form(lg, f"{spread:g}", f"{total:g}", n, w, neutral,
                              "on" if playoffs else "")
            return render_page(rep, form)
        return render_page(None, build_form())
except ImportError:                       # console mode still works sans flask
    app = None


def console(a):
    rep = build_report(a.league, a.spread, a.total, a.n, a.w,
                       a.neutral, not a.no_playoffs)
    if "error" in rep:
        print(rep["error"])
        return 1
    print(f"\nANSWER KEY — {rep['label']}  target Home {rep['spread']:+g} / "
          f"{rep['total']:g}   sample {rep['n']} games (radius {rep['radius']:.2f})")
    print(f"  Moneyline (home): {american(rep['home_win'])} "
          f"({100 * rep['home_win']:.1f}%)")
    print(f"  FG margin med {rep['margin_med']:+.1f} sd {rep['margin_sd']:.1f} "
          f"(model {rep['model_sd_m']:.1f}) | total med {rep['total_med']:.1f} "
          f"sd {rep['total_sd_emp']:.1f} (model {rep['model_sd_t']:.1f})")
    if "h1" in rep:
        print(f"  1H fair: Home {rep['h1']['fair_spread']:+g} / "
              f"{rep['h1']['fair_total']:g}   (model Home "
              f"{rep['h1']['model_spread']:+g} / {rep['h1']['model_total']:g})")
    if "q1" in rep:
        print(f"  1Q fair: Home {rep['q1']['fair_spread']:+g} / "
              f"{rep['q1']['fair_total']:g}")
    if "h2" in rep:
        print(f"  2H fair: Home {rep['h2']['fair_spread']:+g} / "
              f"{rep['h2']['fair_total']:g}")
    for t in rep["teams"]:
        print(f"  {t['side']} team total fair: {t['fair']:g} "
              f"(model {t['model_fair']:g})")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Answer Key derivative pricer")
    ap.add_argument("--league", choices=sorted(LEAGUE_CFG), default="wnba")
    ap.add_argument("--spread", type=float, help="home spread, neg = home fav")
    ap.add_argument("--total", type=float)
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--w", type=float, default=DEFAULT_W)
    ap.add_argument("--neutral", choices=["exclude", "include", "only"],
                    default="exclude")
    ap.add_argument("--no-playoffs", action="store_true")
    ap.add_argument("--port", type=int, default=5025)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    if a.spread is not None and a.total is not None:
        sys.exit(console(a))
    if app is None:
        print("flask not installed and no --spread/--total given")
        sys.exit(1)
    print(f"Answer Key at http://{a.host}:{a.port}")
    app.run(host=a.host, port=a.port, threaded=True)


if __name__ == "__main__":
    main()
