"""
Basketball Median Probabilities Dashboard

Given a player's median points, rebounds, or assists for a game (rounded to
nearest 0.5), compute the probabilities of each half-integer line from
median-5 to median+5.

Uses a normal distribution centered on the median. Default SDs scale with the
median and stat type, matching typical NBA game-to-game variance. The user can
override the SD directly.

Run:   py -3 median_probabilities.py
Then:  http://localhost:5003
"""

import argparse
from math import erf, exp, floor, lgamma, log, sqrt

from flask import Flask, render_template_string, request

app = Flask(__name__)


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + erf((x - mu) / (sigma * sqrt(2.0))))


def default_sigma(stat: str, median: float) -> float:
    """Reasonable defaults matching typical NBA variance."""
    if stat == "points":
        # Points SD ~30% of median, floor 4.0 (low-scoring role players still have variance)
        return max(4.0, 0.30 * median)
    if stat == "assists":
        # Assists are the streakiest of the three (teammate/matchup dependent):
        # SD ~42% of median, floor 1.6
        return max(1.6, 0.42 * median)
    # Rebounds SD ~38% of median, floor 1.8
    return max(1.8, 0.38 * median)


def default_phi(stat: str) -> float:
    """Default variance/mean ratio (overdispersion) for the count model.

    φ = 1 would be Poisson (variance = mean); real box-score counts are
    mildly overdispersed. Assists run a touch streakier than rebounds.
    """
    if stat == "assists":
        return 1.35
    return 1.30  # rebounds


def round_half(x: float) -> float:
    return round(x * 2) / 2


def prob_to_american(p: float) -> str:
    """Convert a probability (0-1) to fair American odds (no vig)."""
    if p <= 0.0:
        return "+∞"
    if p >= 1.0:
        return "-∞"
    if p >= 0.5:
        odds = -100.0 * p / (1.0 - p)
        return f"{int(round(odds))}"
    odds = 100.0 * (1.0 - p) / p
    return f"+{int(round(odds))}"


def compute_rows(median: float, sigma: float) -> list[dict]:
    """Build 11 rows (offsets -5..+5) with P(Over) and P(Under) for each line."""
    rows = []
    for offset in range(-5, 6):
        line = median + offset
        # Line is a half-integer → no ties possible. P(X > line) = P(X >= ceil(line)).
        # Using a continuous normal approx, P(X > line) = 1 - Phi((line - mu)/sigma).
        p_over = 1.0 - normal_cdf(line, median, sigma)
        p_under = 1.0 - p_over
        rows.append({
            "offset": offset,
            "line": line,
            "p_over": p_over * 100.0,
            "p_under": p_under * 100.0,
            "odds_over": prob_to_american(p_over),
            "odds_under": prob_to_american(p_under),
            "is_median": offset == 0,
        })
    return rows


# ── Negative-binomial count model (rebounds / assists) ──
#
# A right-skewed discrete model for count stats. Parameterized by mean μ and
# size r with variance = μ + μ²/r = φ·μ, so r = μ/(φ-1) for a chosen
# variance/mean ratio φ. We solve μ so the input median lands at the 50/50
# line, which reproduces the count structure's skew: symmetric offsets get
# asymmetric Over/Under probabilities (a bit more Over on high lines, less
# Under on low lines) — the correction a symmetric normal misses.

def _nb_logpmf(k: int, mu: float, r: float) -> float:
    return (lgamma(k + r) - lgamma(r) - lgamma(k + 1)
            + r * log(r / (r + mu)) + k * log(mu / (r + mu)))


def _nb_cdf(k: int, mu: float, r: float) -> float:
    """P(X <= k) for integer k >= 0."""
    if k < 0:
        return 0.0
    s = 0.0
    for i in range(int(k) + 1):
        s += exp(_nb_logpmf(i, mu, r))
    return min(1.0, s)


def _nb_solve_mean(m_floor: int, phi: float) -> float:
    """Find the NB mean μ so that CDF(m_floor) = 0.5 (median at the 50/50 line)."""
    lo, hi = 1e-4, max(10.0, (m_floor + 1) * 3.0)
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        r = mid / (phi - 1.0)
        # Higher mean shifts mass right -> less mass at/below m_floor -> lower CDF.
        if _nb_cdf(m_floor, mid, r) > 0.5:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def compute_rows_nb(median: float, phi: float):
    """Build the 11 offset rows under the negative-binomial model.

    Returns (rows, mean, sd). The mean is solved so the median line sits at
    50/50; sd is the implied standard deviation sqrt(φ·mean).
    """
    m_floor = floor(median)
    mu = _nb_solve_mean(m_floor, phi)
    r = mu / (phi - 1.0)
    sd = sqrt(phi * mu)

    rows = []
    for offset in range(-5, 6):
        line = median + offset
        lf = floor(line)
        # P(X > line): for a half-integer line this is P(X >= lf+1) exactly (no
        # push); for an integer line the push mass folds into Under, matching
        # the normal side's "Under = not Over" convention.
        p_over = 1.0 if lf < 0 else 1.0 - _nb_cdf(lf, mu, r)
        p_over = min(1.0, max(0.0, p_over))
        p_under = 1.0 - p_over
        rows.append({
            "offset": offset,
            "line": line,
            "p_over": p_over * 100.0,
            "p_under": p_under * 100.0,
            "odds_over": prob_to_american(p_over),
            "odds_under": prob_to_american(p_under),
            "is_median": offset == 0,
        })
    return rows, mu, sd


TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Basketball Median Probabilities</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <meta name="theme-color" content="#0f1419">
  <style>
    :root {
      --bg: #0f1419;
      --panel: #1a2029;
      --border: #2a3340;
      --text: #e8ecf1;
      --muted: #8a95a5;
      --accent: #4fc3f7;
      --over: #4caf50;
      --under: #e57373;
      --highlight: #2d3846;
    }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); margin: 0; padding: 24px;
    }
    .container { max-width: 880px; margin: 0 auto; }
    h1 { margin: 0 0 4px; font-size: 24px; font-weight: 600; }
    .sub { color: var(--muted); margin-bottom: 24px; font-size: 14px; }
    .panel {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 10px; padding: 20px; margin-bottom: 20px;
    }
    form { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; align-items: end; }
    label { display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px;
            text-transform: uppercase; letter-spacing: 0.06em; }
    input, select {
      width: 100%; padding: 10px 12px; background: #0f1419; color: var(--text);
      border: 1px solid var(--border); border-radius: 6px; font-size: 15px;
    }
    input:focus, select:focus { outline: none; border-color: var(--accent); }
    button {
      padding: 10px 18px; background: var(--accent); color: #0f1419;
      border: 0; border-radius: 6px; font-size: 15px; font-weight: 600; cursor: pointer;
    }
    button:hover { background: #81d4fa; }
    .summary { display: flex; gap: 24px; flex-wrap: wrap; margin-top: 4px; }
    .stat-box { flex: 1; min-width: 140px; }
    .stat-label { color: var(--muted); font-size: 12px; text-transform: uppercase;
                  letter-spacing: 0.06em; margin-bottom: 4px; }
    .stat-value { font-size: 22px; font-weight: 600; }
    table { width: 100%; border-collapse: collapse; font-size: 15px; }
    th, td { padding: 10px 12px; text-align: right; border-bottom: 1px solid var(--border); }
    th:first-child, td:first-child { text-align: left; }
    th { color: var(--muted); font-weight: 500; font-size: 12px;
         text-transform: uppercase; letter-spacing: 0.06em; }
    tr.median-row { background: var(--highlight); font-weight: 600; }
    tr.median-row td:first-child::before { content: "★ "; color: var(--accent); }
    .over { color: var(--over); }
    .under { color: var(--under); }
    .offset-neg { color: var(--under); }
    .offset-pos { color: var(--over); }
    .offset-zero { color: var(--accent); font-weight: 600; }
    .note { color: var(--muted); font-size: 13px; margin-top: 12px; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="container">
    <a href="/" style="color:var(--accent);text-decoration:none;font-size:14px;font-weight:600;display:inline-block;margin-bottom:12px">&larr; Main Menu</a>
    <h1>Basketball Median Probabilities</h1>
    <div class="sub">Set a player's game median (points, rebounds, or assists) and see the Over/Under probability at each half-point line around it.</div>

    <div class="panel">
      <form method="post">
        <div>
          <label for="stat">Stat</label>
          <select name="stat" id="stat">
            <option value="points" {% if stat == 'points' %}selected{% endif %}>Points</option>
            <option value="rebounds" {% if stat == 'rebounds' %}selected{% endif %}>Rebounds</option>
            <option value="assists" {% if stat == 'assists' %}selected{% endif %}>Assists</option>
          </select>
        </div>
        <div>
          <label for="model">Model</label>
          <select name="model" id="model">
            <option value="normal" {% if model == 'normal' %}selected{% endif %}>Normal</option>
            <option value="negbinom" {% if model == 'negbinom' %}selected{% endif %}>Negative binomial</option>
          </select>
        </div>
        <div>
          <label for="median">Median (nearest 0.5)</label>
          <input type="number" step="0.5" min="0.5" name="median" id="median" value="{{ '%g' % median }}">
        </div>
        {% if model == 'negbinom' %}
        <div>
          <label for="phi">Var/Mean &phi; (blank = auto)</label>
          <input type="number" step="0.05" min="1.05" max="3" name="phi" id="phi" value="{{ phi_input }}" placeholder="{{ '%.2f' % phi_default }}">
        </div>
        {% else %}
        <div>
          <label for="sigma">Std Dev (blank = auto)</label>
          <input type="number" step="0.1" min="0.1" name="sigma" id="sigma" value="{{ sigma_input }}" placeholder="{{ '%.2f' % sigma_default }}">
        </div>
        {% endif %}
        <button type="submit">Calculate</button>
      </form>
    </div>

    <div class="panel">
      <div class="summary">
        <div class="stat-box">
          <div class="stat-label">Stat</div>
          <div class="stat-value">{{ stat|capitalize }}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Model</div>
          <div class="stat-value">{{ 'Neg. binomial' if eff_model == 'negbinom' else 'Normal' }}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Median</div>
          <div class="stat-value">{{ '%g' % median }}</div>
        </div>
        {% if eff_model == 'negbinom' %}
        <div class="stat-box">
          <div class="stat-label">Var/Mean &phi;</div>
          <div class="stat-value">{{ '%.2f' % phi }}{% if not phi_input %} <span style="font-size:13px;color:var(--muted);">(auto)</span>{% endif %}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Implied Mean</div>
          <div class="stat-value">{{ '%.2f' % nb_mean }}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Implied SD</div>
          <div class="stat-value">{{ '%.2f' % nb_sd }}</div>
        </div>
        {% else %}
        <div class="stat-box">
          <div class="stat-label">Std Dev</div>
          <div class="stat-value">{{ '%.2f' % sigma }}{% if not sigma_input %} <span style="font-size:13px;color:var(--muted);">(auto)</span>{% endif %}</div>
        </div>
        {% endif %}
      </div>
      {% if points_nb_fallback %}
      <div class="note" style="color:var(--under); margin-top:14px;">Negative binomial is a count model and isn't a good fit for <b>points</b> (a weighted sum of makes, not a count of unit events) &mdash; showing the <b>Normal</b> model instead.</div>
      {% endif %}
    </div>

    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>Offset</th>
            <th>Line</th>
            <th>P(Over)</th>
            <th>Over Odds</th>
            <th>P(Under)</th>
            <th>Under Odds</th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
          <tr class="{% if r.is_median %}median-row{% endif %}">
            <td class="{% if r.offset < 0 %}offset-neg{% elif r.offset > 0 %}offset-pos{% else %}offset-zero{% endif %}">
              {% if r.offset == 0 %}median{% elif r.offset > 0 %}+{{ r.offset }}{% else %}{{ r.offset }}{% endif %}
            </td>
            <td>{{ '%g' % r.line }}</td>
            <td class="over">{{ '%.2f' % r.p_over }}%</td>
            <td class="over">{{ r.odds_over }}</td>
            <td class="under">{{ '%.2f' % r.p_under }}%</td>
            <td class="under">{{ r.odds_under }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      <div class="note">
        <b>Normal</b> N(median, σ²): symmetric, best for <b>points</b> (a sum over many possessions is ≈ normal) and fine for higher-count rebounds. Auto-SD scales with the median: points ≈ max(4.0, 0.30×median), rebounds ≈ max(1.8, 0.38×median), assists ≈ max(1.6, 0.42×median).
        <br><br>
        <b>Negative binomial</b>: a right-skewed <i>count</i> model for <b>rebounds / assists</b>. The mean is solved so the median line sits at 50/50, and the spread comes from the variance/mean ratio φ (φ = 1 would be Poisson; defaults ≈ 1.30 rebounds, 1.35 assists). The skew mainly shifts lines far from the median &mdash; a bit more Over on high lines, less Under on low lines &mdash; which a symmetric normal misses. Its effect is largest for low counts and shrinks as the count grows (assists &gt; rebounds &gt; points ≈ none), so it's offered only for rebounds and assists.
        <br><br>
        Lines are half-integers, so no pushes. Odds shown are <b>fair (no-vig) American odds</b> derived directly from the probabilities.
      </div>
    </div>
  </div>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    stat = request.values.get("stat", "points")
    if stat not in ("points", "rebounds", "assists"):
        stat = "points"

    model = request.values.get("model", "normal")
    if model not in ("normal", "negbinom"):
        model = "normal"

    try:
        median = float(request.values.get("median", 20.5))
    except ValueError:
        median = 20.5
    median = max(0.5, round_half(median))

    sigma_input = request.values.get("sigma", "").strip()
    phi_input = request.values.get("phi", "").strip()
    sigma_default = default_sigma(stat, median)
    phi_default = default_phi(stat)

    # Negative binomial is a count model — inappropriate for points (a weighted
    # sum of makes, not a count of unit events). Fall back to Normal for points.
    points_nb_fallback = (model == "negbinom" and stat == "points")
    eff_model = "normal" if points_nb_fallback else model

    sigma = sigma_default
    phi = phi_default
    nb_mean = nb_sd = None

    if eff_model == "negbinom":
        if phi_input:
            try:
                phi = float(phi_input)
            except ValueError:
                phi = phi_default
        phi = min(3.0, max(1.05, phi))
        rows, nb_mean, nb_sd = compute_rows_nb(median, phi)
    else:
        if sigma_input:
            try:
                sigma = float(sigma_input)
                if sigma <= 0:
                    sigma = sigma_default
            except ValueError:
                sigma = sigma_default
        rows = compute_rows(median, sigma)

    return render_template_string(
        TEMPLATE,
        stat=stat,
        model=model,
        eff_model=eff_model,
        points_nb_fallback=points_nb_fallback,
        median=median,
        sigma=sigma,
        sigma_input=sigma_input,
        sigma_default=sigma_default,
        phi=phi,
        phi_input=phi_input,
        phi_default=phi_default,
        nb_mean=nb_mean,
        nb_sd=nb_sd,
        rows=rows,
    )


def main():
    parser = argparse.ArgumentParser(description="Basketball median probabilities dashboard")
    parser.add_argument("--port", type=int, default=5003)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print(f"Serving at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
