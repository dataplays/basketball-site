"""
Basketball Median Probabilities Dashboard

Given a player's median points or rebounds for a game (rounded to nearest 0.5),
compute the probabilities of each half-integer line from median-5 to median+5.

Uses a normal distribution centered on the median. Default SDs scale with the
median and stat type, matching typical NBA game-to-game variance. The user can
override the SD directly.

Run:   py -3 median_probabilities.py
Then:  http://localhost:5003
"""

import argparse
from math import erf, sqrt

from flask import Flask, render_template_string, request

app = Flask(__name__)


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + erf((x - mu) / (sigma * sqrt(2.0))))


def default_sigma(stat: str, median: float) -> float:
    """Reasonable defaults matching typical NBA variance."""
    if stat == "points":
        # Points SD ~30% of median, floor 4.0 (low-scoring role players still have variance)
        return max(4.0, 0.30 * median)
    # Rebounds SD ~38% of median, floor 1.8
    return max(1.8, 0.38 * median)


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


TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Basketball Median Probabilities</title>
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
    form { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; align-items: end; }
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
    <h1>Basketball Median Probabilities</h1>
    <div class="sub">Set a player's game median (points or rebounds) and see the Over/Under probability at each half-point line around it.</div>

    <div class="panel">
      <form method="post">
        <div>
          <label for="stat">Stat</label>
          <select name="stat" id="stat">
            <option value="points" {% if stat == 'points' %}selected{% endif %}>Points</option>
            <option value="rebounds" {% if stat == 'rebounds' %}selected{% endif %}>Rebounds</option>
          </select>
        </div>
        <div>
          <label for="median">Median (nearest 0.5)</label>
          <input type="number" step="0.5" min="0.5" name="median" id="median" value="{{ '%g' % median }}">
        </div>
        <div>
          <label for="sigma">Std Dev (blank = auto)</label>
          <input type="number" step="0.1" min="0.1" name="sigma" id="sigma" value="{{ sigma_input }}" placeholder="{{ '%.2f' % sigma_default }}">
        </div>
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
          <div class="stat-label">Median</div>
          <div class="stat-value">{{ '%g' % median }}</div>
        </div>
        <div class="stat-box">
          <div class="stat-label">Std Dev</div>
          <div class="stat-value">{{ '%.2f' % sigma }}{% if not sigma_input %} <span style="font-size:13px;color:var(--muted);">(auto)</span>{% endif %}</div>
        </div>
      </div>
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
        Probabilities use a normal distribution N(median, σ²). Each line is a half-integer, so no pushes are possible. Auto-SD scales with the median: points ≈ max(4.0, 0.30×median), rebounds ≈ max(1.8, 0.38×median). Odds shown are <b>fair (no-vig) American odds</b> derived directly from the probabilities.
      </div>
    </div>
  </div>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    stat = request.values.get("stat", "points")
    if stat not in ("points", "rebounds"):
        stat = "points"

    try:
        median = float(request.values.get("median", 20.5))
    except ValueError:
        median = 20.5
    median = max(0.5, round_half(median))

    sigma_input = request.values.get("sigma", "").strip()
    sigma_default = default_sigma(stat, median)
    if sigma_input:
        try:
            sigma = float(sigma_input)
            if sigma <= 0:
                sigma = sigma_default
        except ValueError:
            sigma = sigma_default
    else:
        sigma = sigma_default

    rows = compute_rows(median, sigma)

    return render_template_string(
        TEMPLATE,
        stat=stat,
        median=median,
        sigma=sigma,
        sigma_input=sigma_input,
        sigma_default=sigma_default,
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
