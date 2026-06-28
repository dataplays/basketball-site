"""
Spread & Total Fair-Price Calculator

Given a game's CURRENT spread and total, price every alternate spread and
alternate total -- plus the no-vig moneyline implied by the spread.

Model (the standard sportsbook approach):
  * Margin (favorite - underdog) ~ Normal(mean = spread, sd = spread_sd).
    The current spread IS the market's expected margin, so a bet on the
    favorite -spread is a coin flip (50%) by construction.
  * Total points ~ Normal(mean = total, sd = total_sd).
  * Moneyline: P(favorite wins) = P(margin > 0) = Phi(spread / spread_sd).

Total-aware SD (default ON): a game's variance scales with its pace, and the
total is a direct proxy for possessions. So both SDs are scaled by
sqrt(total / league_avg_total) -- a high-total game gets a wider margin
distribution (alt lines move price slower), a low-total game a tighter one.
Turn it off to use flat league SDs, or type a manual SD to override entirely.

Standard deviations default per league (typical historical variance around the
closing number). Whole-number lines get a push band (continuity correction);
half-point lines cannot push. Odds are fair, no-vig American.

Run:   py -3 spread_total_calculator.py
Then:  http://localhost:5008
"""

import argparse
from math import erf, sqrt

from flask import Flask, render_template_string, request

app = Flask(__name__)


# Per league: sd of the game margin around the spread, sd of the total around
# the total line, and the league-average total (the anchor for total-aware
# scaling). All tunable -- these are typical historical values.
LEAGUES = {
    "nba":  {"label": "NBA",         "spread_sd": 11.0, "total_sd": 16.0, "avg_total": 225.0},
    "wnba": {"label": "WNBA",        "spread_sd": 10.5, "total_sd": 14.0, "avg_total": 162.0},
    "cbb":  {"label": "Men's CBB",   "spread_sd": 10.0, "total_sd": 12.5, "avg_total": 142.0},
    "wcbb": {"label": "Women's CBB", "spread_sd": 10.5, "total_sd": 13.0, "avg_total": 140.0},
}
DEFAULT_LEAGUE = "nba"
SPREAD_SPAN = 6     # alt spreads from current -6 .. +6 (1-pt steps)
TOTAL_SPAN = 8      # alt totals  from current -8 .. +8 (1-pt steps)


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + erf((x - mu) / (sigma * sqrt(2.0))))


def round_half(x: float) -> float:
    return round(x * 2) / 2


def fmt(x: float) -> str:
    return f"{x:g}"


def prob_to_american(p: float) -> str:
    """Fair American odds (no vig) for a probability in (0,1)."""
    if p <= 0.0:
        return "+∞"
    if p >= 1.0:
        return "-∞"
    if p >= 0.5:
        return f"{int(round(-100.0 * p / (1.0 - p)))}"
    return f"+{int(round(100.0 * (1.0 - p) / p))}"


def over_under_probs(mu: float, sigma: float, line: float):
    """Return (p_over, p_push, p_under) for a line.

    A whole-number line can push: model the discrete result with a +/-0.5 band
    (continuity correction). A half-point line never pushes.
    """
    if abs(line - round(line)) < 1e-9:          # integer line -> push possible
        lo = normal_cdf(line - 0.5, mu, sigma)
        hi = normal_cdf(line + 0.5, mu, sigma)
        return 1.0 - hi, hi - lo, lo
    p_over = 1.0 - normal_cdf(line, mu, sigma)
    return p_over, 0.0, 1.0 - p_over


def two_way(mu: float, sigma: float, line: float) -> dict:
    """Probabilities + fair (no-push-conditional) odds for both sides of a line."""
    p_over, p_push, p_under = over_under_probs(mu, sigma, line)
    denom = p_over + p_under
    cond_over = p_over / denom if denom > 0 else 0.5
    return {
        "p_over": p_over * 100.0,
        "p_under": p_under * 100.0,
        "p_push": p_push * 100.0,
        "odds_over": prob_to_american(cond_over),
        "odds_under": prob_to_american(1.0 - cond_over),
    }


def fav_label(L: float) -> str:
    if abs(L) < 1e-9:
        return "PK"
    return f"-{fmt(L)}" if L > 0 else f"+{fmt(-L)}"


def dog_label(L: float) -> str:
    if abs(L) < 1e-9:
        return "PK"
    return f"+{fmt(L)}" if L > 0 else f"-{fmt(-L)}"


def spread_rows(spread: float, sigma: float, span: int = SPREAD_SPAN) -> list:
    """Alternate spreads around the current number. p_over == favorite covers."""
    rows = []
    for off in range(-span, span + 1):
        L = round_half(spread + off)
        t = two_way(spread, sigma, L)            # mean margin == spread
        rows.append({
            "off": off,
            "fav_line": fav_label(L),
            "dog_line": dog_label(L),
            "p_fav": t["p_over"], "odds_fav": t["odds_over"],
            "p_dog": t["p_under"], "odds_dog": t["odds_under"],
            "p_push": t["p_push"],
            "is_current": off == 0,
        })
    return rows


def total_rows(total: float, sigma: float, span: int = TOTAL_SPAN) -> list:
    """Alternate totals around the current number."""
    rows = []
    for off in range(-span, span + 1):
        L = round_half(total + off)
        t = two_way(total, sigma, L)
        rows.append({
            "off": off,
            "line": L,
            "p_over": t["p_over"], "odds_over": t["odds_over"],
            "p_under": t["p_under"], "odds_under": t["odds_under"],
            "p_push": t["p_push"],
            "is_current": off == 0,
        })
    return rows


def moneyline(spread: float, sigma: float) -> dict:
    """No-vig moneyline implied by the spread (ties impossible -> continuous split)."""
    p_fav = 1.0 - normal_cdf(0.0, spread, sigma)   # P(margin > 0) = Phi(spread/sigma)
    return {
        "p_fav": p_fav * 100.0, "p_dog": (1.0 - p_fav) * 100.0,
        "ml_fav": prob_to_american(p_fav), "ml_dog": prob_to_american(1.0 - p_fav),
    }


TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spread &amp; Total Fair-Price Calculator</title>
  <style>
    :root {
      --bg:#0f1419; --panel:#1a2029; --border:#2a3340; --text:#e8ecf1;
      --muted:#8a95a5; --accent:#4fc3f7; --over:#4caf50; --under:#e57373;
      --highlight:#2d3846; --gold:#f0b429;
    }
    * { box-sizing:border-box; }
    body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
           background:var(--bg); color:var(--text); margin:0; padding:24px; }
    .container { max-width:980px; margin:0 auto; }
    h1 { margin:0 0 4px; font-size:24px; font-weight:600; }
    h2 { font-size:16px; font-weight:600; margin:0 0 14px; }
    .sub { color:var(--muted); margin-bottom:24px; font-size:14px; }
    .panel { background:var(--panel); border:1px solid var(--border);
             border-radius:10px; padding:20px; margin-bottom:20px; }
    form { display:grid; grid-template-columns:repeat(6,1fr); gap:14px; align-items:end; }
    label { display:block; color:var(--muted); font-size:12px; margin-bottom:6px;
            text-transform:uppercase; letter-spacing:.06em; }
    input[type=number], select { width:100%; padding:10px 12px; background:#0f1419; color:var(--text);
                    border:1px solid var(--border); border-radius:6px; font-size:15px; }
    input:focus, select:focus { outline:none; border-color:var(--accent); }
    .check { display:flex; align-items:center; gap:8px; }
    .check input { width:16px; height:16px; accent-color:var(--accent); }
    .check label { margin:0; text-transform:none; letter-spacing:0; font-size:13px; color:var(--text); }
    button { padding:10px 18px; background:var(--accent); color:#0f1419; border:0;
             border-radius:6px; font-size:15px; font-weight:600; cursor:pointer; width:100%; }
    button:hover { background:#81d4fa; }
    .summary { display:flex; gap:20px; flex-wrap:wrap; }
    .stat-box { flex:1; min-width:120px; }
    .stat-label { color:var(--muted); font-size:12px; text-transform:uppercase;
                  letter-spacing:.06em; margin-bottom:4px; }
    .stat-value { font-size:22px; font-weight:600; }
    .stat-value small { font-size:12px; color:var(--muted); font-weight:400; }
    .ml { color:var(--gold); }
    .cols { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
    @media (max-width:820px){ .cols{ grid-template-columns:1fr; } form{ grid-template-columns:repeat(2,1fr);} }
    table { width:100%; border-collapse:collapse; font-size:14px; }
    th, td { padding:8px 10px; text-align:right; border-bottom:1px solid var(--border); }
    th:first-child, td:first-child { text-align:left; }
    th { color:var(--muted); font-weight:500; font-size:11px;
         text-transform:uppercase; letter-spacing:.05em; }
    tr.current-row { background:var(--highlight); font-weight:600; }
    tr.current-row td:first-child::before { content:"\\2605 "; color:var(--accent); }
    .over { color:var(--over); } .under { color:var(--under); }
    .push { color:var(--muted); }
    .note { color:var(--muted); font-size:13px; margin-top:8px; line-height:1.55; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Spread &amp; Total Fair-Price Calculator</h1>
    <div class="sub">Enter the current spread &amp; total. See the fair (no-vig) price for every alternate spread and total, plus the moneyline implied by the spread.</div>

    <div class="panel">
      <form method="post">
        <input type="hidden" name="submitted" value="1">
        <div>
          <label for="league">League</label>
          <select name="league" id="league">
            {% for k, v in leagues.items() %}
            <option value="{{ k }}" {% if league == k %}selected{% endif %}>{{ v.label }}</option>
            {% endfor %}
          </select>
        </div>
        <div>
          <label for="spread">Spread (fav by)</label>
          <input type="number" step="0.5" name="spread" id="spread" value="{{ fmt(spread) }}">
        </div>
        <div>
          <label for="total">Total</label>
          <input type="number" step="0.5" name="total" id="total" value="{{ fmt(total) }}">
        </div>
        <div>
          <label for="spread_sd">Spread SD (blank=auto)</label>
          <input type="number" step="0.1" name="spread_sd" id="spread_sd"
                 value="{{ spread_sd_input }}" placeholder="{{ '%.1f' % spread_sd }}">
        </div>
        <div>
          <label for="total_sd">Total SD (blank=auto)</label>
          <input type="number" step="0.1" name="total_sd" id="total_sd"
                 value="{{ total_sd_input }}" placeholder="{{ '%.1f' % total_sd }}">
        </div>
        <button type="submit">Calculate</button>
        <div class="check" style="grid-column:1 / -1; margin-top:2px;">
          <input type="checkbox" name="scale_sd" id="scale_sd" value="on" {% if scale_sd %}checked{% endif %}>
          <label for="scale_sd">Scale SD by this game's total (pace-adjust the spread &amp; total variance) — anchor: league avg {{ fmt(avg_total) }}</label>
        </div>
      </form>
    </div>

    <div class="panel">
      <div class="summary">
        <div class="stat-box"><div class="stat-label">Spread</div>
          <div class="stat-value">Fav -{{ fmt(spread) }}</div></div>
        <div class="stat-box"><div class="stat-label">Total</div>
          <div class="stat-value">{{ fmt(total) }}</div></div>
        <div class="stat-box"><div class="stat-label">Fair Moneyline</div>
          <div class="stat-value ml">{{ ml.ml_fav }} / {{ ml.ml_dog }}</div></div>
        <div class="stat-box"><div class="stat-label">Win Prob (Fav / Dog)</div>
          <div class="stat-value">{{ '%.1f' % ml.p_fav }}% / {{ '%.1f' % ml.p_dog }}%</div></div>
        <div class="stat-box"><div class="stat-label">Spread SD</div>
          <div class="stat-value">{{ '%.1f' % spread_sd }} <small>{{ spread_sd_tag }}</small></div></div>
        <div class="stat-box"><div class="stat-label">Total SD</div>
          <div class="stat-value">{{ '%.1f' % total_sd }} <small>{{ total_sd_tag }}</small></div></div>
      </div>
    </div>

    <div class="cols">
      <div class="panel">
        <h2>Alternate Spreads</h2>
        <table>
          <thead><tr>
            <th>Fav</th><th>P(Cover)</th><th>Odds</th><th>Push</th>
            <th>Dog</th><th>P(Cover)</th><th>Odds</th>
          </tr></thead>
          <tbody>
            {% for r in spreads %}
            <tr class="{% if r.is_current %}current-row{% endif %}">
              <td>{{ r.fav_line }}</td>
              <td class="over">{{ '%.1f' % r.p_fav }}%</td>
              <td class="over">{{ r.odds_fav }}</td>
              <td class="push">{% if r.p_push > 0.05 %}{{ '%.1f' % r.p_push }}%{% else %}—{% endif %}</td>
              <td>{{ r.dog_line }}</td>
              <td class="under">{{ '%.1f' % r.p_dog }}%</td>
              <td class="under">{{ r.odds_dog }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h2>Alternate Totals</h2>
        <table>
          <thead><tr>
            <th>Line</th><th>P(Over)</th><th>Odds</th><th>Push</th>
            <th>P(Under)</th><th>Odds</th>
          </tr></thead>
          <tbody>
            {% for r in totals %}
            <tr class="{% if r.is_current %}current-row{% endif %}">
              <td>{{ fmt(r.line) }}</td>
              <td class="over">{{ '%.1f' % r.p_over }}%</td>
              <td class="over">{{ r.odds_over }}</td>
              <td class="push">{% if r.p_push > 0.05 %}{{ '%.1f' % r.p_push }}%{% else %}—{% endif %}</td>
              <td class="under">{{ '%.1f' % r.p_under }}%</td>
              <td class="under">{{ r.odds_under }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel note">
      <b>Model.</b> Margin (favorite&minus;underdog) &sim; Normal(mean = spread, SD = Spread&nbsp;SD); total points &sim; Normal(mean = total, SD = Total&nbsp;SD). The current spread/total price at exactly 50% by construction. Moneyline = P(margin &gt; 0).
      <br><b>Total-aware SD</b> (checkbox, on by default): both SDs are multiplied by &radic;(total / league&nbsp;avg), so a high-total game widens the spread distribution and a low-total game tightens it — the spread lines DO react to this game's total. Uncheck for flat league SDs. A typed SD overrides everything (used flat).
      <br><b>League SD / avg-total anchors:</b>
      NBA 11.0 / 16.0 @ 225 &middot; WNBA 10.5 / 14.0 @ 162 &middot; Men's CBB 10.0 / 12.5 @ 142 &middot; Women's CBB 10.5 / 13.0 @ 140.
      <br><b>Odds</b> are fair, no-vig American. Whole-number lines include a push band (continuity correction); half-point lines can't push (&ldquo;&mdash;&rdquo;).
    </div>
  </div>
</body>
</html>
"""


def _resolve_sd(field, base, scale, total, avg_total):
    """Return (effective_sd, raw_input, tag). Manual input wins (flat); else
    optionally scale the base league SD by sqrt(total / league_avg_total)."""
    raw = request.values.get(field, "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v, raw, "(manual)"
        except ValueError:
            pass
    if scale and avg_total > 0 and total > 0:
        return base * sqrt(total / avg_total), "", f"(scaled from {base:.1f})"
    return base, "", "(league avg)"


@app.route("/", methods=["GET", "POST"])
def index():
    league = request.values.get("league", DEFAULT_LEAGUE)
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE
    preset = LEAGUES[league]

    try:
        spread = abs(round_half(float(request.values.get("spread", 5.5))))
    except ValueError:
        spread = 5.5

    try:
        total = max(0.5, round_half(float(request.values.get("total", 225.5))))
    except ValueError:
        total = 225.5

    # Total-aware scaling defaults ON; on submit an unchecked box sends nothing.
    if request.values.get("submitted"):
        scale_sd = request.values.get("scale_sd") == "on"
    else:
        scale_sd = True

    spread_sd, spread_sd_input, spread_sd_tag = _resolve_sd(
        "spread_sd", preset["spread_sd"], scale_sd, total, preset["avg_total"])
    total_sd, total_sd_input, total_sd_tag = _resolve_sd(
        "total_sd", preset["total_sd"], scale_sd, total, preset["avg_total"])

    return render_template_string(
        TEMPLATE,
        leagues=LEAGUES, league=league, fmt=fmt,
        spread=spread, total=total, avg_total=preset["avg_total"],
        scale_sd=scale_sd,
        spread_sd=spread_sd, total_sd=total_sd,
        spread_sd_input=spread_sd_input, total_sd_input=total_sd_input,
        spread_sd_tag=spread_sd_tag, total_sd_tag=total_sd_tag,
        ml=moneyline(spread, spread_sd),
        spreads=spread_rows(spread, spread_sd),
        totals=total_rows(total, total_sd),
    )


def main():
    parser = argparse.ArgumentParser(description="Spread & total fair-price calculator")
    parser.add_argument("--port", type=int, default=5008)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"Serving at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
