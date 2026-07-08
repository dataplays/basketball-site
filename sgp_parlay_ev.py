#!/usr/bin/env python3
"""
sgp_parlay_ev.py — SGP / Correlated Parlay EV Checker (/sgp).
=============================================================
Build a same-game parlay (spread / total / team total / moneyline / player
props), and see what it's actually worth: each leg's fair probability, the
naive independent-multiplication price, the **correlation-adjusted** joint
probability, and the EV% against the book's quoted SGP odds.

Why correlation is the whole game
---------------------------------
Books price SGPs off the multiplied legs and then apply a haircut. But the true
joint probability can sit far from the naive product in either direction:
"home -4.5 + home star Over points + game Over" legs all pull the same way
(joint prob HIGHER than the product — the book's haircut may still leave value),
while "player Over + game Under" fight each other (joint prob LOWER than the
product — a quoted price near the naive multiply is a trap).

The model (consistent with /pricer)
-----------------------------------
* Game margin M (home−away) ~ Normal(−home_spread, spread_sd) and total
  T ~ Normal(total, total_sd) — the SAME league SDs + total-aware scaling the
  /pricer uses (imported from it), with M ⟂ T (league-level margins and totals
  are ~uncorrelated).
* Every game leg is a deterministic function of (M, T): home score = (T+M)/2,
  covers, totals, ML — so game-leg joint probabilities are exact within the
  model, no hand-tuned pairwise correlations.
* Each player-prop leg gets a latent Normal stat anchored to its natural game
  quantity — points/assists/PRA to the player's TEAM SCORE, rebounds to the
  GAME TOTAL — at a documented default correlation (override per leg). Player
  legs therefore correlate with the game legs AND with each other (same-team
  players share the team-score anchor) through the game state, the way real
  SGP correlations arise.
* One Monte Carlo (~50k sims, deterministic seed per input) prices everything:
  leg marginals, the joint, pairwise leg correlations (phi), and EV vs quote.

Player leg means: if you enter the leg's book odds, the implied probability is
de-vigged (÷1.0476, the standard two-way hold) and the stat mean is backed out
from the line + SD; with no odds the line is treated as the median (50/50).

Usage
-----
    py -3 sgp_parlay_ev.py            # http://localhost:5018
Mounted at /sgp on the basketball-site (module-level app, relative form).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import random
from math import sqrt

from flask import Flask, request

# Reuse the pricer's league SDs and odds helpers so /sgp and /pricer agree.
from spread_total_calculator import (
    LEAGUES, normal_cdf, prob_to_american, team_total_sd,
)

DEFAULT_LEAGUE = "wnba"
N_SIMS = 50_000
MAX_LEGS = 6
PROP_TWO_WAY_HOLD = 1.0476     # implied-prob divisor when only one side's odds known

# Player-stat SDs (same shapes as the /median tool): (floor, fraction-of-mean)
STAT_SD = {
    "pts": (4.0, 0.30),
    "reb": (1.8, 0.38),
    "ast": (1.6, 0.42),
    "pra": (5.0, 0.32),
}
# Anchor + default correlation of each stat to its anchor.
#   team  = the player's own team score;  total = the game total
STAT_ANCHOR = {
    "pts": ("team", 0.50),
    "ast": ("team", 0.45),
    "reb": ("total", 0.20),
    "pra": ("team", 0.50),
}
STAT_LABEL = {"pts": "Points", "reb": "Rebounds", "ast": "Assists", "pra": "PRA"}

LEG_TYPES = [
    ("", "— none —"),
    ("spread", "Spread"),
    ("total", "Game Total"),
    ("team_total", "Team Total"),
    ("ml", "Moneyline"),
    ("prop", "Player Prop"),
]


# ── small numeric helpers ─────────────────────────────────────────────────────
def norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF via bisection (plenty fast for form input)."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    lo, hi = -6.0, 6.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if normal_cdf(mid, 0.0, 1.0) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def american_to_decimal(a: float) -> float:
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / abs(a))


def american_implied(a: float) -> float:
    d = american_to_decimal(a)
    return 1.0 / d


def parse_american(raw: str):
    raw = (raw or "").strip().replace("+", "")
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if abs(v) >= 100 else None


def stat_sd(stat: str, mean: float) -> float:
    floor, frac = STAT_SD.get(stat, (4.0, 0.30))
    return max(floor, frac * max(mean, 0.0))


# ── leg parsing ───────────────────────────────────────────────────────────────
def parse_legs(form) -> tuple[list[dict], list[str]]:
    legs, errors = [], []
    for i in range(MAX_LEGS):
        t = (form.get(f"type{i}") or "").strip()
        if not t:
            continue
        team = form.get(f"team{i}") or "home"
        side = form.get(f"side{i}") or "over"
        stat = form.get(f"stat{i}") or "pts"
        raw_line = (form.get(f"line{i}") or "").strip()
        odds = parse_american(form.get(f"odds{i}") or "")
        corr_raw = (form.get(f"corr{i}") or "").strip()
        pname = (form.get(f"pname{i}") or "").strip()

        line = None
        if raw_line:
            try:
                line = float(raw_line)
            except ValueError:
                errors.append(f"Leg {i+1}: bad line '{raw_line}'")
                continue

        if t in ("spread", "total", "team_total", "prop") and line is None:
            errors.append(f"Leg {i+1} ({t}): needs a line")
            continue

        corr = None
        if corr_raw:
            try:
                corr = min(0.9, max(-0.9, float(corr_raw)))
            except ValueError:
                pass

        legs.append({
            "slot": i, "type": t, "team": team, "side": side, "stat": stat,
            "line": line, "odds": odds, "corr": corr, "pname": pname,
        })
    return legs, errors


def leg_desc(leg: dict) -> str:
    tm = "Home" if leg["team"] == "home" else "Away"
    if leg["type"] == "spread":
        ln = leg["line"]
        return f'{tm} {"+" if ln >= 0 else ""}{ln:g}'
    if leg["type"] == "ml":
        return f"{tm} ML"
    if leg["type"] == "total":
        return f'Game {"Over" if leg["side"] == "over" else "Under"} {leg["line"]:g}'
    if leg["type"] == "team_total":
        return f'{tm} TT {"Over" if leg["side"] == "over" else "Under"} {leg["line"]:g}'
    nm = leg["pname"] or f"{tm} player"
    return (f'{nm} {"Over" if leg["side"] == "over" else "Under"} '
            f'{leg["line"]:g} {STAT_LABEL.get(leg["stat"], leg["stat"])}')


# ── the simulation ────────────────────────────────────────────────────────────
def simulate_parlay(league: str, home_spread: float, total: float,
                    legs: list[dict], sims: int = N_SIMS) -> dict:
    preset = LEAGUES.get(league, LEAGUES[DEFAULT_LEAGUE])
    # total-aware SD scaling, same default behaviour as /pricer
    scale = sqrt(total / preset["avg_total"]) if preset["avg_total"] > 0 and total > 0 else 1.0
    sd_m = preset["spread_sd"] * scale
    sd_t = preset["total_sd"] * scale

    mean_m = -home_spread                 # home -4.5 -> expected margin +4.5
    mean_t = total
    sd_team = team_total_sd(sd_m, sd_t)   # SD of a single team's score
    mean_h = (mean_t + mean_m) / 2.0
    mean_a = (mean_t - mean_m) / 2.0

    # Pre-compute player-leg parameters
    for leg in legs:
        if leg["type"] != "prop":
            continue
        anchor, def_r = STAT_ANCHOR.get(leg["stat"], ("team", 0.5))
        r = leg["corr"] if leg["corr"] is not None else def_r
        sd_guess = stat_sd(leg["stat"], leg["line"])
        if leg["odds"] is not None:
            implied = american_implied(leg["odds"])
            fair = min(0.98, max(0.02, implied / PROP_TWO_WAY_HOLD))
            z = norm_ppf(fair)
            mean = leg["line"] + sd_guess * z if leg["side"] == "over" \
                else leg["line"] - sd_guess * z
        else:
            fair = None
            mean = leg["line"]            # no odds -> line treated as the median
        sd = stat_sd(leg["stat"], mean)
        leg["_anchor"], leg["_r"] = anchor, r
        leg["_mean"], leg["_sd"] = mean, sd
        leg["_fair_in"] = fair

    # Deterministic seed per full input set
    key = f"{league}|{home_spread}|{total}|" + "|".join(
        f'{l["type"]},{l["team"]},{l["side"]},{l["stat"]},{l["line"]},{l["odds"]},{l["corr"]}'
        for l in legs)
    seed = int(hashlib.md5(key.encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    gauss = rng.gauss

    n = len(legs)
    hits = [0] * n
    joint_hits = 0
    pair_hits = [[0] * n for _ in range(n)]

    for _ in range(sims):
        m = gauss(mean_m, sd_m)
        t = gauss(mean_t, sd_t)
        h = (t + m) / 2.0
        a = (t - m) / 2.0
        z_h = (h - mean_h) / sd_team
        z_a = (a - mean_a) / sd_team
        z_t = (t - mean_t) / sd_t

        results = []
        for leg in legs:
            lt = leg["type"]
            if lt == "spread":
                margin = m if leg["team"] == "home" else -m
                hit = (margin + leg["line"]) > 0
            elif lt == "ml":
                hit = m > 0 if leg["team"] == "home" else m < 0
            elif lt == "total":
                hit = t > leg["line"] if leg["side"] == "over" else t < leg["line"]
            elif lt == "team_total":
                sc = h if leg["team"] == "home" else a
                hit = sc > leg["line"] if leg["side"] == "over" else sc < leg["line"]
            else:  # prop
                if leg["_anchor"] == "team":
                    z = z_h if leg["team"] == "home" else z_a
                else:
                    z = z_t
                r = leg["_r"]
                val = leg["_mean"] + leg["_sd"] * (r * z + sqrt(1 - r * r) * gauss(0, 1))
                hit = val > leg["line"] if leg["side"] == "over" else val < leg["line"]
            results.append(hit)

        all_hit = True
        for i, hit in enumerate(results):
            if hit:
                hits[i] += 1
                for j in range(i + 1, n):
                    if results[j]:
                        pair_hits[i][j] += 1
            else:
                all_hit = False
        if all_hit and n:
            joint_hits += 1

    p_legs = [hcount / sims for hcount in hits]
    p_joint = joint_hits / sims if n else 0.0
    p_naive = 1.0
    for p in p_legs:
        p_naive *= p

    # pairwise phi coefficients between leg outcomes
    phi = [[None] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            pi, pj, pij = p_legs[i], p_legs[j], pair_hits[i][j] / sims
            den = sqrt(pi * (1 - pi) * pj * (1 - pj))
            phi[i][j] = (pij - pi * pj) / den if den > 1e-9 else 0.0

    return {
        "p_legs": p_legs, "p_joint": p_joint, "p_naive": p_naive,
        "phi": phi, "sd_m": sd_m, "sd_t": sd_t,
        "lift": (p_joint / p_naive) if p_naive > 0 else None,
    }


# ── rendering ─────────────────────────────────────────────────────────────────
def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _leg_row(i: int, form) -> str:
    def sel(name, options, cur):
        o = "".join(f'<option value="{v}"{" selected" if v == cur else ""}>{lbl}</option>'
                    for v, lbl in options)
        return f'<select name="{name}{i}">{o}</select>'
    t = form.get(f"type{i}", "")
    return f"""<tr>
      <td class=idx>{i+1}</td>
      <td>{sel("type", LEG_TYPES, t)}</td>
      <td>{sel("team", [("home","Home"),("away","Away")], form.get(f"team{i}","home"))}</td>
      <td>{sel("side", [("over","Over"),("under","Under")], form.get(f"side{i}","over"))}</td>
      <td>{sel("stat", [(k, STAT_LABEL[k]) for k in ("pts","reb","ast","pra")], form.get(f"stat{i}","pts"))}</td>
      <td><input name="pname{i}" value="{_esc(form.get(f"pname{i}",""))}" size=10 placeholder="player"></td>
      <td><input name="line{i}" value="{_esc(form.get(f"line{i}",""))}" size=6 placeholder="line"></td>
      <td><input name="odds{i}" value="{_esc(form.get(f"odds{i}",""))}" size=6 placeholder="-115"></td>
      <td><input name="corr{i}" value="{_esc(form.get(f"corr{i}",""))}" size=4 placeholder="auto"></td>
    </tr>"""


def render(form, out) -> str:
    league = form.get("league", DEFAULT_LEAGUE)
    lg_opts = "".join(f'<option value="{k}"{" selected" if k == league else ""}>{v["label"]}</option>'
                      for k, v in LEAGUES.items())
    leg_rows = "".join(_leg_row(i, form) for i in range(MAX_LEGS))

    body = ""
    if out:
        if out.get("errors"):
            body += ('<div class="panel err">' +
                     "<br>".join(_esc(e) for e in out["errors"]) + "</div>")
        if out.get("res"):
            res, legs = out["res"], out["legs"]
            # legs table
            trs = []
            for i, leg in enumerate(legs):
                p = res["p_legs"][i]
                note = ""
                if leg["type"] == "prop":
                    src = ("mean backed out of odds" if leg["odds"] is not None
                           else "line = median (add odds to sharpen)")
                    note = (f'<span class=mut>μ {leg["_mean"]:.1f} σ {leg["_sd"]:.1f} '
                            f'r {leg["_r"]:+.2f} · {src}</span>')
                trs.append(f'<tr><td class=nm>{_esc(leg_desc(leg))}</td>'
                           f'<td>{p*100:.1f}%</td><td>{prob_to_american(p)}</td>'
                           f'<td class=note-td>{note}</td></tr>')
            body += (f'<div class=panel><h2>Legs</h2><table>'
                     f'<tr><th>Leg</th><th>Fair P</th><th>Fair Odds</th><th></th></tr>'
                     + "".join(trs) + "</table></div>")

            # parlay pricing
            lift = res["lift"]
            lift_txt = f"{lift:.2f}×" if lift else "—"
            lift_cls = "up" if (lift or 1) > 1.02 else ("dn" if (lift or 1) < 0.98 else "mut")
            body += f"""<div class=panel><h2>Parlay Price</h2>
<div class=summary>
 <div class=sbox><div class=k>Naive (independent)</div>
   <div class=v>{res['p_naive']*100:.2f}%</div>
   <div class=v2>{prob_to_american(res['p_naive'])}</div></div>
 <div class=sbox><div class=k>Correlation-adjusted</div>
   <div class=v acc>{res['p_joint']*100:.2f}%</div>
   <div class=v2 acc>{prob_to_american(res['p_joint'])}</div></div>
 <div class=sbox><div class=k>Correlation lift</div>
   <div class="v {lift_cls}">{lift_txt}</div>
   <div class=v2 mut>joint ÷ naive</div></div>
</div></div>"""

            # verdict vs quote
            q = out.get("quote")
            if q is not None:
                d = american_to_decimal(q)
                p = res["p_joint"]
                ev = p * (d - 1) - (1 - p)
                stake = out.get("stake") or 100.0
                be = 1.0 / d
                cls = "up" if ev > 0.02 else ("dn" if ev < -0.02 else "mut")
                verdict = "+EV — the quote beats the correlated fair price" if ev > 0.02 else \
                          ("−EV — the quote is worse than the correlated fair price" if ev < -0.02
                           else "≈ fair")
                body += f"""<div class=panel><h2>Vs the Book's Quote</h2>
<div class=summary>
 <div class=sbox><div class=k>Quoted</div><div class=v>{'+' if q>0 else ''}{q:g}</div>
   <div class=v2 mut>implies {be*100:.2f}%</div></div>
 <div class=sbox><div class=k>Fair (correlated)</div><div class=v>{prob_to_american(p)}</div>
   <div class=v2 mut>true {p*100:.2f}%</div></div>
 <div class=sbox><div class=k>EV</div><div class="v {cls}">{ev*100:+.1f}%</div>
   <div class=v2 mut>{ev*stake:+.2f} on {stake:g} stake</div></div>
</div>
<div class="verdict {cls}">{verdict}</div></div>"""

            # phi matrix
            n = len(legs)
            if n >= 2:
                head = "<tr><th></th>" + "".join(f"<th>L{j+1}</th>" for j in range(n)) + "</tr>"
                rows = []
                for i in range(n):
                    cells = []
                    for j in range(n):
                        if j <= i:
                            cells.append("<td class=mut>·</td>")
                        else:
                            v = res["phi"][i][j]
                            cls = "up" if v > 0.05 else ("dn" if v < -0.05 else "mut")
                            cells.append(f'<td class="{cls}">{v:+.2f}</td>')
                    rows.append(f"<tr><th>L{i+1}</th>" + "".join(cells) + "</tr>")
                body += (f'<div class=panel><h2>Leg Correlations (φ)</h2><table class=phi>'
                         + head + "".join(rows) +
                         '</table><div class=note>φ = correlation between leg win/lose outcomes '
                         'in the simulation. Positive pairs raise the joint probability above the '
                         'naive product; negative pairs drag it below.</div></div>')

    return (PAGE
            .replace("{{LG_OPTS}}", lg_opts)
            .replace("{{SPREAD}}", _esc(form.get("spread", "-4.5")))
            .replace("{{TOTAL}}", _esc(form.get("total", "165.5")))
            .replace("{{QUOTE}}", _esc(form.get("quote", "")))
            .replace("{{STAKE}}", _esc(form.get("stake", "100")))
            .replace("{{LEG_ROWS}}", leg_rows)
            .replace("{{BODY}}", body))


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0f1419">
<title>SGP / Correlated Parlay EV</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--mut:#8a95a5;
--accent:#f06292;--up:#4caf50;--dn:#e57373}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);margin:0;padding:22px}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:23px;margin:0 0 3px}h1 span{color:var(--accent)}
h2{font-size:15px;margin:0 0 12px}
.sub{color:var(--mut);font-size:13.5px;margin-bottom:18px;line-height:1.5}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;
padding:16px 18px;margin-bottom:16px}
.panel.err{color:#ffb4a2}
.gamerow{display:flex;gap:14px;flex-wrap:wrap;align-items:end}
label.f{display:block;color:var(--mut);font-size:11px;text-transform:uppercase;
letter-spacing:.06em;margin-bottom:5px}
select,input{padding:8px 10px;background:#0f1419;color:var(--text);
border:1px solid var(--border);border-radius:6px;font-size:14px}
select:focus,input:focus{outline:none;border-color:var(--accent)}
.btn{padding:10px 20px;background:var(--accent);color:#fff;border:0;border-radius:6px;
font-size:14px;font-weight:700;cursor:pointer}
.btn:hover{filter:brightness(1.1)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:7px 8px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
td.idx{color:var(--mut);width:20px}
td.nm{font-weight:600}
td.note-td{font-size:12px}
.legs td{padding:5px 6px}
.legs select,.legs input{padding:6px 8px;font-size:13px;width:100%}
.summary{display:flex;gap:18px;flex-wrap:wrap}
.sbox{flex:1;min-width:150px}
.sbox .k{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.sbox .v{font-size:24px;font-weight:700}
.sbox .v2{font-size:13px;color:var(--mut);margin-top:2px}
.acc{color:var(--accent)}
.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut)}
.verdict{margin-top:12px;padding:10px 14px;border-radius:8px;font-weight:700;font-size:14px}
.verdict.up{background:rgba(76,175,80,.12);border:1px solid rgba(76,175,80,.4)}
.verdict.dn{background:rgba(229,115,115,.12);border:1px solid rgba(229,115,115,.4)}
.verdict.mut{background:rgba(138,149,165,.1);border:1px solid var(--border)}
table.phi{width:auto}table.phi td,table.phi th{text-align:center;padding:6px 12px}
.note{color:var(--mut);font-size:12.5px;margin-top:10px;line-height:1.55}
.foot{color:var(--mut);font-size:12px;text-align:center;margin:22px 0 8px;line-height:1.7}
@media(max-width:760px){.legs{display:block;overflow-x:auto}}
</style></head><body><div class=wrap>
<h1>SGP / <span>Correlated Parlay</span> EV</h1>
<div class=sub>Price a same-game parlay with the correlations the multiplied legs ignore.
Enter the game's spread &amp; total, add legs, paste the book's quoted SGP odds — get the fair
correlated price and the EV.</div>
<form method=post>
<div class=panel>
 <div class=gamerow>
  <div><label class=f>League</label><select name=league>{{LG_OPTS}}</select></div>
  <div><label class=f>Home spread</label><input name=spread value="{{SPREAD}}" size=6></div>
  <div><label class=f>Game total</label><input name=total value="{{TOTAL}}" size=6></div>
  <div><label class=f>Quoted SGP odds</label><input name=quote value="{{QUOTE}}" size=7 placeholder="+450"></div>
  <div><label class=f>Stake</label><input name=stake value="{{STAKE}}" size=5></div>
  <button type=submit class=btn>Price It</button>
 </div>
</div>
<div class=panel>
 <h2>Legs <span class=mut style="font-weight:400">— Team/Side/Stat/Player apply where relevant; Odds &amp; Corr are optional (props only)</span></h2>
 <table class=legs>
  <tr><th></th><th>Type</th><th>Team</th><th>O/U</th><th>Stat</th><th>Player</th><th>Line</th><th>Odds</th><th>Corr</th></tr>
  {{LEG_ROWS}}
 </table>
 <div class=note>Spread lines are the chosen team's handicap (Home −4.5 → enter team Home, line −4.5;
 the dog +4.5 → team Away, line +4.5). Player-prop <b>Odds</b> = the book's price on your side — the
 stat mean is backed out from it (de-vigged); leave blank to treat the line as the player's median.
 <b>Corr</b> overrides the default correlation to the anchor (pts/ast/PRA → own team score:
 0.50/0.45/0.50 · reb → game total: 0.20).</div>
</div>
{{BODY}}
</form>
<div class=foot>Model: margin &amp; total are the same Normal model as /pricer (league SDs,
total-aware scaling); game legs are exact functions of (margin, total); player stats are latent
Normals anchored to team score / game total. ~50k sims, deterministic per input. Integer-line
pushes aren't modeled — prefer .5 lines. Same-game only: legs from different games are
~independent, so plain multiplication (see /calc) already prices those.</div>
</div></body></html>"""

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="6" fill="#0f1419"/>'
           '<path d="M7 25 L13 13 L19 19 L25 7" fill="none" stroke="#f06292" '
           'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>'
           '<circle cx="13" cy="13" r="2" fill="#f06292"/>'
           '<circle cx="19" cy="19" r="2" fill="#f06292"/></svg>')


def create_app():
    app = Flask(__name__)

    @app.route("/", methods=["GET", "POST"])
    def index():
        form = request.form if request.method == "POST" else {}
        out = None
        if request.method == "POST":
            legs, errors = parse_legs(form)
            out = {"errors": errors}
            try:
                home_spread = float(form.get("spread", "-4.5"))
            except ValueError:
                home_spread = -4.5
                errors.append("Bad home spread; using -4.5")
            try:
                total = float(form.get("total", "165.5"))
            except ValueError:
                total = 165.5
                errors.append("Bad total; using 165.5")
            league = form.get("league", DEFAULT_LEAGUE)
            if league not in LEAGUES:
                league = DEFAULT_LEAGUE
            if legs:
                res = simulate_parlay(league, home_spread, total, legs)
                out["res"] = res
                out["legs"] = legs
                out["quote"] = parse_american(form.get("quote", ""))
                try:
                    out["stake"] = float(form.get("stake", "100") or 100)
                except ValueError:
                    out["stake"] = 100.0
            elif not errors:
                out["errors"] = ["Add at least one leg."]
        return render(form, out)

    @app.route("/favicon.svg")
    def favicon():
        return (FAVICON, 200, {"Content-Type": "image/svg+xml"})

    return app


app = create_app()


def main():
    ap = argparse.ArgumentParser(description="SGP / correlated parlay EV checker")
    ap.add_argument("--port", type=int, default=5018)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"SGP EV checker -> http://{args.host}:{args.port}")
    create_app().run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
