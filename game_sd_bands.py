"""
Game SD Bands -- given a side (spread) and total, show the 1.0 / 1.5 / 2.0
standard-deviation result ranges for the margin, the total, and each team's
score.

Model (same as spread_total_calculator.py, the /pricer tool):
  * Margin (favorite - underdog) ~ Normal(mean = |spread|, sd = spread_sd)
  * Total points ~ Normal(mean = total, sd = total_sd)
  * Team score = (total +/- margin) / 2, so team SD = sqrt(total_sd^2 + spread_sd^2) / 2
  * Total-aware SD (default ON): both SDs scale by sqrt(total / league_avg_total)

Web (default):
  py -3 game_sd_bands.py               ->  http://localhost:5020  (--port / --host)

Console (pass league, side, total):
  py -3 game_sd_bands.py wnba "WSH -6.5" 162.5
  py -3 game_sd_bands.py nba -4.5 228
  py -3 game_sd_bands.py cbb "Duke -7" 145.5 --no-scale
  py -3 game_sd_bands.py wnba -6.5 162.5 --spread-sd 11 --total-sd 15
"""

import argparse
import re
import sys
from math import erf, sqrt

from flask import Flask, render_template_string, request

app = Flask(__name__)

LEAGUES = {
    "nba":  {"label": "NBA",         "spread_sd": 11.0, "total_sd": 16.0, "avg_total": 225.0},
    "wnba": {"label": "WNBA",        "spread_sd": 10.5, "total_sd": 14.0, "avg_total": 162.0},
    "cbb":  {"label": "Men's CBB",   "spread_sd": 10.0, "total_sd": 12.5, "avg_total": 142.0},
    "wcbb": {"label": "Women's CBB", "spread_sd": 10.5, "total_sd": 13.0, "avg_total": 140.0},
}
DEFAULT_LEAGUE = "nba"
SD_LEVELS = [1.0, 1.5, 2.0]


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def coverage(k: float) -> float:
    """Two-sided probability of landing within +/- k SDs."""
    return erf(k / sqrt(2.0))


def prob_to_american(p: float) -> str:
    if p <= 0.0:
        return "+∞"
    if p >= 1.0:
        return "-∞"
    if p >= 0.5:
        return f"{int(round(-100.0 * p / (1.0 - p)))}"
    return f"+{int(round(100.0 * (1.0 - p) / p))}"


def parse_side(side: str) -> tuple[str, float]:
    """Parse a side like 'WSH -6.5', '-6.5', or 'Duke -7' into (team, margin).

    Returns the favorite's label ('Fav' if none given) and the favorite's
    margin as a positive number.
    """
    side = str(side).strip()
    m = re.match(r"^(.*?)\s*([+-]?\d+(?:\.\d+)?)$", side)
    if not m:
        raise ValueError(f"Could not parse side: {side!r}")
    team = m.group(1).strip() or "Fav"
    margin = abs(float(m.group(2)))
    return team, margin


def fmt(x: float) -> str:
    return f"{x:g}"


def fmt_margin(x: float, fav: str, dog: str) -> str:
    if x >= 0:
        return f"{fav} by {x:.1f}"
    return f"{dog} by {-x:.1f}"


def compute(league: str, side_text: str, total: float,
            spread_sd_in=None, total_sd_in=None, scale: bool = True) -> dict:
    """All the numbers -- shared by the console and web renderers."""
    preset = LEAGUES[league]
    fav, margin = parse_side(side_text)
    dog = "Opp"

    factor = 1.0
    scale_note = "league preset"
    if scale and total > 0:
        factor = sqrt(total / preset["avg_total"])
        scale_note = f"scaled x{factor:.3f} from league avg total {preset['avg_total']:.0f}"

    spread_sd = spread_sd_in if spread_sd_in else preset["spread_sd"] * factor
    total_sd = total_sd_in if total_sd_in else preset["total_sd"] * factor
    if spread_sd_in or total_sd_in:
        scale_note += "; manual override(s) flat"
    team_sd = sqrt(total_sd ** 2 + spread_sd ** 2) / 2.0

    fav_mean = (total + margin) / 2.0
    dog_mean = (total - margin) / 2.0
    p_fav = norm_cdf(margin / spread_sd)

    bands = []
    for k in SD_LEVELS:
        m_lo, m_hi = margin - k * spread_sd, margin + k * spread_sd
        bands.append({
            "k": k,
            "cov": coverage(k) * 100.0,
            "margin_lo": fmt_margin(m_lo, fav, dog),
            "margin_hi": fmt_margin(m_hi, fav, dog),
            "total_lo": total - k * total_sd,
            "total_hi": total + k * total_sd,
            "fav_lo": fav_mean - k * team_sd,
            "fav_hi": fav_mean + k * team_sd,
            "dog_lo": dog_mean - k * team_sd,
            "dog_hi": dog_mean + k * team_sd,
        })

    return {
        "league": league, "label": preset["label"], "avg_total": preset["avg_total"],
        "fav": fav, "dog": dog, "margin": margin, "total": total,
        "spread_sd": spread_sd, "total_sd": total_sd, "team_sd": team_sd,
        "scale_note": scale_note,
        "fav_mean": fav_mean, "dog_mean": dog_mean,
        "p_fav": p_fav * 100.0, "p_dog": (1.0 - p_fav) * 100.0,
        "ml_fav": prob_to_american(p_fav), "ml_dog": prob_to_american(1.0 - p_fav),
        "bands": bands,
    }


# ── Console output ────────────────────────────────────────────────────────────
def print_console(r: dict) -> None:
    fav, dog = r["fav"], r["dog"]
    w = 72
    print("=" * w)
    print(f"  SD Bands -- {r['label']}  |  Side: {fav} -{r['margin']:g}  |  Total: {r['total']:g}")
    print("=" * w)
    print(f"  SDs: spread {r['spread_sd']:.2f}, total {r['total_sd']:.2f}, "
          f"team score {r['team_sd']:.2f}  ({r['scale_note']})")
    print(f"  Projected mean score: {fav} {r['fav_mean']:.1f}, {dog} {r['dog_mean']:.1f}")
    print(f"  Implied moneyline: {fav} wins {r['p_fav']:.1f}% ({r['ml_fav']}) / "
          f"{dog} {r['p_dog']:.1f}% ({r['ml_dog']})")

    print(f"\n  MARGIN ({fav} minus {dog})   mean +{r['margin']:.1f}, SD {r['spread_sd']:.2f}")
    print(f"    {'k':>6}  {'coverage':>8}  {'low':<18} {'high'}")
    for b in r["bands"]:
        print(f"    {b['k']:>4.1f}SD  {b['cov']:>7.1f}%  {b['margin_lo']:<18} {b['margin_hi']}")

    print(f"\n  TOTAL   mean {r['total']:.1f}, SD {r['total_sd']:.2f}")
    print(f"    {'k':>6}  {'coverage':>8}  {'range'}")
    for b in r["bands"]:
        print(f"    {b['k']:>4.1f}SD  {b['cov']:>7.1f}%  {b['total_lo']:.1f} - {b['total_hi']:.1f}")

    print(f"\n  TEAM SCORES   ({fav} mean {r['fav_mean']:.1f}, {dog} mean {r['dog_mean']:.1f}, "
          f"SD {r['team_sd']:.2f} each)")
    print(f"    {'k':>6}  {'coverage':>8}  {fav:<22} {dog}")
    for b in r["bands"]:
        f_rng = f"{b['fav_lo']:.1f} - {b['fav_hi']:.1f}"
        print(f"    {b['k']:>4.1f}SD  {b['cov']:>7.1f}%  {f_rng:<22} "
              f"{b['dog_lo']:.1f} - {b['dog_hi']:.1f}")
    print()


# ── Web ───────────────────────────────────────────────────────────────────────
TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Game SD Bands</title>
  <style>
    :root {
      --bg:#0f1419; --panel:#1a2029; --border:#2a3340; --text:#e8ecf1;
      --muted:#8a95a5; --accent:#ff8a65; --highlight:#2d3846; --gold:#f0b429;
    }
    * { box-sizing:border-box; }
    body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
           background:var(--bg); color:var(--text); margin:0; padding:24px; }
    .container { max-width:980px; margin:0 auto; }
    h1 { margin:0 0 4px; font-size:24px; font-weight:600; }
    h2 { font-size:16px; font-weight:600; margin:0 0 14px; }
    h2 small { color:var(--muted); font-weight:400; }
    .sub { color:var(--muted); margin-bottom:24px; font-size:14px; }
    .panel { background:var(--panel); border:1px solid var(--border);
             border-radius:10px; padding:20px; margin-bottom:20px; }
    form { display:grid; grid-template-columns:repeat(6,1fr); gap:14px; align-items:end; }
    label { display:block; color:var(--muted); font-size:12px; margin-bottom:6px;
            text-transform:uppercase; letter-spacing:.06em; }
    input, select { width:100%; padding:10px 12px; background:#0f1419; color:var(--text);
                    border:1px solid var(--border); border-radius:6px; font-size:15px; }
    input:focus, select:focus { outline:none; border-color:var(--accent); }
    .check { display:flex; align-items:center; gap:8px; }
    .check input { width:16px; height:16px; accent-color:var(--accent); }
    .check label { margin:0; text-transform:none; letter-spacing:0; font-size:13px; color:var(--text); }
    button { padding:10px 18px; background:var(--accent); color:#0f1419; border:0;
             border-radius:6px; font-size:15px; font-weight:600; cursor:pointer; width:100%; }
    button:hover { background:#ffab91; }
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
    td:first-child { color:var(--accent); font-weight:600; }
    th { color:var(--muted); font-weight:500; font-size:11px;
         text-transform:uppercase; letter-spacing:.05em; }
    .cov { color:var(--muted); }
    .note { color:var(--muted); font-size:13px; margin-top:8px; line-height:1.55; }
    .err { color:#e57373; font-size:14px; margin-bottom:16px; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Game SD Bands</h1>
    <div class="sub">Enter a side (spread) and total. See where the margin, the total and each
      team's score land at &plusmn;1.0, &plusmn;1.5 and &plusmn;2.0 standard deviations.</div>

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
          <label for="side">Side (fav, e.g. WSH -6.5)</label>
          <input type="text" name="side" id="side" value="{{ side_input }}" placeholder="WSH -6.5">
        </div>
        <div>
          <label for="total">Total</label>
          <input type="number" step="0.5" name="total" id="total" value="{{ total_input }}">
        </div>
        <div>
          <label for="spread_sd">Spread SD (blank=auto)</label>
          <input type="number" step="0.1" name="spread_sd" id="spread_sd" value="{{ spread_sd_input }}"
                 placeholder="{{ '%.1f' % leagues[league].spread_sd }}">
        </div>
        <div>
          <label for="total_sd">Total SD (blank=auto)</label>
          <input type="number" step="0.1" name="total_sd" id="total_sd" value="{{ total_sd_input }}"
                 placeholder="{{ '%.1f' % leagues[league].total_sd }}">
        </div>
        <button type="submit">Calculate</button>
        <div class="check" style="grid-column:1 / -1; margin-top:2px;">
          <input type="checkbox" name="scale_sd" id="scale_sd" value="on" {% if scale_sd %}checked{% endif %}>
          <label for="scale_sd">Scale SD by this game's total (pace-adjust the variance) &mdash;
            anchor: league avg {{ fmt(leagues[league].avg_total) }}</label>
        </div>
      </form>
    </div>

    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    {% if r %}
    <div class="panel">
      <div class="summary">
        <div class="stat-box"><div class="stat-label">Side</div>
          <div class="stat-value">{{ r.fav }} -{{ fmt(r.margin) }}</div></div>
        <div class="stat-box"><div class="stat-label">Total</div>
          <div class="stat-value">{{ fmt(r.total) }}</div></div>
        <div class="stat-box"><div class="stat-label">Projected Score</div>
          <div class="stat-value">{{ '%.1f' % r.fav_mean }} &ndash; {{ '%.1f' % r.dog_mean }}</div></div>
        <div class="stat-box"><div class="stat-label">Fair Moneyline</div>
          <div class="stat-value ml">{{ r.ml_fav }} / {{ r.ml_dog }}</div></div>
        <div class="stat-box"><div class="stat-label">Win Prob</div>
          <div class="stat-value">{{ '%.1f' % r.p_fav }}% / {{ '%.1f' % r.p_dog }}%</div></div>
        <div class="stat-box"><div class="stat-label">SDs (Sprd / Tot / Team)</div>
          <div class="stat-value">{{ '%.1f' % r.spread_sd }} / {{ '%.1f' % r.total_sd }} / {{ '%.1f' % r.team_sd }}
            <small>({{ r.scale_note }})</small></div></div>
      </div>
    </div>

    <div class="cols">
      <div class="panel">
        <h2>Margin <small>&mdash; {{ r.fav }} minus {{ r.dog }}, mean +{{ '%.1f' % r.margin }}, SD {{ '%.1f' % r.spread_sd }}</small></h2>
        <table>
          <thead><tr><th>Band</th><th>Coverage</th><th>Low</th><th>High</th></tr></thead>
          <tbody>
            {% for b in r.bands %}
            <tr>
              <td>&plusmn;{{ '%.1f' % b.k }} SD</td>
              <td class="cov">{{ '%.1f' % b.cov }}%</td>
              <td>{{ b.margin_lo }}</td>
              <td>{{ b.margin_hi }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h2>Total <small>&mdash; mean {{ fmt(r.total) }}, SD {{ '%.1f' % r.total_sd }}</small></h2>
        <table>
          <thead><tr><th>Band</th><th>Coverage</th><th>Range</th></tr></thead>
          <tbody>
            {% for b in r.bands %}
            <tr>
              <td>&plusmn;{{ '%.1f' % b.k }} SD</td>
              <td class="cov">{{ '%.1f' % b.cov }}%</td>
              <td>{{ '%.1f' % b.total_lo }} &ndash; {{ '%.1f' % b.total_hi }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <h2>Team Scores <small>&mdash; {{ r.fav }} mean {{ '%.1f' % r.fav_mean }}, {{ r.dog }} mean
        {{ '%.1f' % r.dog_mean }}, SD {{ '%.1f' % r.team_sd }} each</small></h2>
      <table>
        <thead><tr><th>Band</th><th>Coverage</th><th>{{ r.fav }}</th><th>{{ r.dog }}</th></tr></thead>
        <tbody>
          {% for b in r.bands %}
          <tr>
            <td>&plusmn;{{ '%.1f' % b.k }} SD</td>
            <td class="cov">{{ '%.1f' % b.cov }}%</td>
            <td>{{ '%.1f' % b.fav_lo }} &ndash; {{ '%.1f' % b.fav_hi }}</td>
            <td>{{ '%.1f' % b.dog_lo }} &ndash; {{ '%.1f' % b.dog_hi }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <div class="panel note">
      <b>Model.</b> Margin (favorite&minus;underdog) &sim; Normal(mean = spread, SD = Spread&nbsp;SD);
      total &sim; Normal(mean = total, SD = Total&nbsp;SD); team score = (total &plusmn; margin)/2, so
      team SD = &radic;(totalSD&sup2; + spreadSD&sup2;)/2. Same model and league SDs as the
      Spread &amp; Total Pricer.
      <br><b>Coverage</b> is the share of outcomes inside the band: &plusmn;1.0 SD &asymp; 68.3%,
      &plusmn;1.5 SD &asymp; 86.6%, &plusmn;2.0 SD &asymp; 95.4%.
      <br><b>Total-aware SD</b> (checkbox, on by default): both SDs are multiplied by
      &radic;(total / league&nbsp;avg). A typed SD overrides everything (used flat).
      <br><b>League SD / avg-total anchors:</b> NBA 11.0 / 16.0 @ 225 &middot; WNBA 10.5 / 14.0 @ 162
      &middot; Men's CBB 10.0 / 12.5 @ 142 &middot; Women's CBB 10.5 / 13.0 @ 140.
    </div>
    {% endif %}
  </div>
</body>
</html>
"""


def _parse_opt(field):
    raw = request.values.get(field, "").strip()
    if raw == "":
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except ValueError:
        return None


@app.route("/", methods=["GET", "POST"])
def index():
    league = request.values.get("league", DEFAULT_LEAGUE)
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE

    side_input = request.values.get("side", "").strip() or "-5.5"
    total_input = request.values.get("total", "").strip() or "225.5"
    spread_sd_in = _parse_opt("spread_sd")
    total_sd_in = _parse_opt("total_sd")

    if request.values.get("submitted"):
        scale_sd = request.values.get("scale_sd") == "on"
    else:
        scale_sd = True

    r, error = None, None
    try:
        total = float(total_input)
        if total <= 0:
            raise ValueError("Total must be positive.")
        r = compute(league, side_input, total, spread_sd_in, total_sd_in, scale_sd)
    except ValueError as e:
        error = str(e)

    return render_template_string(
        TEMPLATE,
        leagues=LEAGUES, league=league, fmt=fmt,
        side_input=side_input, total_input=total_input,
        spread_sd_input=(request.values.get("spread_sd", "").strip()),
        total_sd_input=(request.values.get("total_sd", "").strip()),
        scale_sd=scale_sd, r=r, error=error,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="1.0/1.5/2.0 SD result bands for a game's side + total. "
                    "With league/side/total: print to console; without: serve the web page.")
    ap.add_argument("league", nargs="?", choices=sorted(LEAGUES), help="league for SD presets")
    ap.add_argument("side", nargs="?", help="the side, e.g. 'WSH -6.5' or -6.5 (favorite margin)")
    ap.add_argument("total", nargs="?", type=float, help="the game total, e.g. 162.5")
    ap.add_argument("--spread-sd", type=float, default=None, help="override spread SD (flat, no scaling)")
    ap.add_argument("--total-sd", type=float, default=None, help="override total SD (flat, no scaling)")
    ap.add_argument("--no-scale", action="store_true",
                    help="don't scale league SDs by sqrt(total / league avg total)")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.league and args.side is not None and args.total is not None:
        r = compute(args.league, args.side, args.total,
                    args.spread_sd, args.total_sd, not args.no_scale)
        print_console(r)
    else:
        print(f"Serving at http://{args.host}:{args.port}")
        app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
