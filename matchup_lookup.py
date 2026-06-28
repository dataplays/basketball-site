"""
Matchup Lookup — recent form + head-to-head

Pick a league, enter the AWAY and HOME team, and get:
  * each team's last 10 games played (date, site, opponent, point spread,
    total, final score)
  * the last 3 head-to-head meetings between the two teams

All data from ESPN's free public APIs (no key). Spreads/totals are the closing
line ESPN stored (DraftKings/consensus via the summary `pickcenter`); shown as
"—" when ESPN has no line for that game.

Run (web):     py -3 matchup_lookup.py            ->  http://localhost:5009
Run (console): py -3 matchup_lookup.py --league wnba --away "New York" --home "Las Vegas"
"""

import argparse
import json
import sys
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher

from flask import Flask, render_template_string, request

app = Flask(__name__)

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) matchup-lookup"

LEAGUES = {
    "nba":  {"label": "NBA",                     "path": "nba"},
    "wnba": {"label": "WNBA",                    "path": "wnba"},
    "cbb":  {"label": "Men's College (NCAA)",    "path": "mens-college-basketball"},
    "wcbb": {"label": "Women's College (NCAA)",  "path": "womens-college-basketball"},
}
DEFAULT_LEAGUE = "nba"

LAST_N = 10        # games per team
H2H_N = 3          # head-to-head meetings
MAX_SEASONS = 6    # how far back to walk to find them

_teams_cache = {}      # league -> [team dicts]
_sched_cache = {}      # (path, team_id, season) -> raw json
_odds_cache = {}       # (path, event_id) -> {"details":.., "ou":..}
_lock = threading.Lock()


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# ── Teams + name resolution ──────────────────────────────────────────────────
def get_teams(league):
    with _lock:
        if league in _teams_cache:
            return _teams_cache[league]
    path = LEAGUES[league]["path"]
    data = _get(f"{BASE}/{path}/teams?limit=1000")
    teams = []
    for t in data["sports"][0]["leagues"][0]["teams"]:
        tm = t["team"]
        teams.append({
            "id": str(tm.get("id")),
            "abbr": (tm.get("abbreviation") or "").upper(),
            "display": tm.get("displayName") or "",
            "location": tm.get("location") or "",
            "name": tm.get("name") or "",
            "short": tm.get("shortDisplayName") or "",
            "nick": tm.get("nickname") or "",
        })
    with _lock:
        _teams_cache[league] = teams
    return teams


def resolve_team(league, query):
    """Best team match for a free-text query, or None."""
    q = (query or "").strip().lower()
    if not q:
        return None
    teams = get_teams(league)
    # exact abbreviation
    for t in teams:
        if t["abbr"].lower() == q:
            return t
    # exact name / location / nickname / display
    for t in teams:
        if q in (t["display"].lower(), t["location"].lower(),
                 t["name"].lower(), t["nick"].lower(), t["short"].lower()):
            return t
    # substring
    hits = [t for t in teams if q in t["display"].lower()
            or q in t["location"].lower() or q in t["nick"].lower()]
    if len(hits) == 1:
        return hits[0]
    pool = hits or teams
    best = max(pool, key=lambda t: SequenceMatcher(None, q, t["display"].lower()).ratio())
    if SequenceMatcher(None, q, best["display"].lower()).ratio() >= 0.5 or hits:
        return best
    return None


# ── Schedule + parsing ───────────────────────────────────────────────────────
def fetch_schedule(path, team_id, season=None):
    key = (path, team_id, season)
    with _lock:
        if key in _sched_cache:
            return _sched_cache[key]
    url = f"{BASE}/{path}/teams/{team_id}/schedule"
    if season:
        url += "?" + urllib.parse.urlencode({"season": season})
    try:
        data = _get(url)
    except Exception:
        data = {}
    with _lock:
        _sched_cache[key] = data
    return data


def _score(competitor):
    s = competitor.get("score")
    if isinstance(s, dict):
        v = s.get("value")
        if v is not None:
            return int(round(v))
        dv = s.get("displayValue")
        return int(dv) if (dv and str(dv).isdigit()) else None
    if isinstance(s, (int, float)):
        return int(s)
    if isinstance(s, str) and s.isdigit():
        return int(s)
    return None


def _parse_event(ev, team_id):
    """Parse one schedule event from team_id's perspective; None if not usable."""
    comps = ev.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    status = (comp.get("status") or {}).get("type") or {}
    if not status.get("completed"):
        return None
    competitors = comp.get("competitors") or []
    me = opp = None
    for c in competitors:
        if str(c.get("team", {}).get("id")) == str(team_id):
            me = c
        else:
            opp = c
    if not me or not opp:
        return None
    ms, os_ = _score(me), _score(opp)
    if ms is None or os_ is None:
        return None
    neutral = bool(comp.get("neutralSite"))
    site = "N" if neutral else ("H" if me.get("homeAway") == "home" else "A")
    otm = opp.get("team", {})
    return {
        "event_id": str(ev.get("id")),
        "date_iso": ev.get("date") or comp.get("date") or "",
        "site": site,
        "opp_id": str(otm.get("id")),
        "opp_abbr": (otm.get("abbreviation") or otm.get("shortDisplayName") or "?").upper(),
        "opp_name": otm.get("displayName") or otm.get("shortDisplayName") or "?",
        "team_score": ms,
        "opp_score": os_,
        "won": bool(me.get("winner")) if me.get("winner") is not None else ms > os_,
        "details": None,
        "ou": None,
    }


def collect_events(path, team_id, need=LAST_N, opp_id=None, need_h2h=H2H_N):
    """Walk back season-by-season until we have enough games (and, if opp_id is
    given, enough head-to-head meetings) or hit MAX_SEASONS. Returns parsed
    completed games, most recent first."""
    out, seen = [], set()
    data = fetch_schedule(path, team_id, None)
    year = (data.get("season") or {}).get("year")

    def absorb(d):
        for ev in d.get("events", []) or []:
            g = _parse_event(ev, team_id)
            if g and g["event_id"] not in seen:
                seen.add(g["event_id"])
                out.append(g)

    absorb(data)
    seasons = 1
    yr = year if isinstance(year, int) else datetime.now().year
    while seasons < MAX_SEASONS:
        h2h = sum(1 for g in out if opp_id and g["opp_id"] == str(opp_id))
        if len(out) >= need and (opp_id is None or h2h >= need_h2h):
            break
        yr -= 1
        absorb(fetch_schedule(path, team_id, yr))
        seasons += 1
    out.sort(key=lambda g: g["date_iso"], reverse=True)
    return out


# ── Odds (closing line via summary pickcenter) ───────────────────────────────
def fetch_odds(path, event_id):
    key = (path, event_id)
    with _lock:
        if key in _odds_cache:
            return _odds_cache[key]
    res = {"details": None, "ou": None}
    try:
        data = _get(f"{BASE}/{path}/summary?event={event_id}")
        for e in data.get("pickcenter") or []:
            if e.get("details") or e.get("overUnder") is not None:
                res = {"details": e.get("details"), "ou": e.get("overUnder")}
                break
    except Exception:
        pass
    with _lock:
        _odds_cache[key] = res
    return res


def attach_odds(path, games):
    """Fetch + attach closing odds for a list of game dicts, in parallel."""
    uniq = {g["event_id"] for g in games}
    if not uniq:
        return
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = dict(zip(uniq, ex.map(lambda eid: fetch_odds(path, eid), uniq)))
    for g in games:
        o = results.get(g["event_id"], {})
        g["details"], g["ou"] = o.get("details"), o.get("ou")


# ── Formatting ───────────────────────────────────────────────────────────────
def fmt_date(iso):
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return f"{d.month}/{d.day}/{str(d.year)[2:]}"
    except Exception:
        return (iso or "")[:10]


def spread_for(details, team_abbr):
    """Reorient a 'ABBR -x.x' details string to team_abbr's perspective."""
    if not details:
        return None
    d = details.strip()
    up = d.upper()
    if up in ("EVEN", "PK", "PICK", "PICK'EM"):
        return "PK"
    parts = d.split()
    if len(parts) >= 2:
        ab = parts[0].upper()
        try:
            num = float(parts[-1])
        except ValueError:
            return d
        val = num if ab == team_abbr.upper() else -num
        return f"{val:+g}"
    return d


def fmt_total(ou):
    if ou is None:
        return "—"
    try:
        return f"{float(ou):g}"
    except (TypeError, ValueError):
        return str(ou)


def build_team_table(league, team):
    path = LEAGUES[league]["path"]
    games = collect_events(path, team["id"], need=LAST_N)[:LAST_N]
    attach_odds(path, games)
    rows = []
    for g in games:
        sp = spread_for(g["details"], team["abbr"])
        rows.append({
            "date": fmt_date(g["date_iso"]),
            "site": g["site"],
            "opp": g["opp_abbr"],
            "spread": sp if sp is not None else "—",
            "total": fmt_total(g["ou"]),
            "final": f'{"W" if g["won"] else "L"} {g["team_score"]}-{g["opp_score"]}',
            "won": g["won"],
        })
    return rows


def build_h2h(league, away, home):
    """Last H2H_N meetings, from the AWAY team's schedule, filtered to HOME."""
    path = LEAGUES[league]["path"]
    ev = collect_events(path, away["id"], need=LAST_N, opp_id=home["id"])
    meetings = [g for g in ev if g["opp_id"] == home["id"]][:H2H_N]
    attach_odds(path, meetings)
    rows = []
    for g in meetings:
        # g is from AWAY's perspective: team_score = away score, opp = home
        host = away["abbr"] if g["site"] == "H" else (home["abbr"] if g["site"] == "A" else "Neutral")
        sp = spread_for(g["details"], home["abbr"])     # show vs the (current) home team
        rows.append({
            "date": fmt_date(g["date_iso"]),
            "host": ("@ " + host) if host != "Neutral" else "Neutral",
            "spread": (home["abbr"] + " " + sp) if sp not in (None, "PK") else (sp or "—"),
            "total": fmt_total(g["ou"]),
            "final": f'{away["abbr"]} {g["team_score"]} - {g["opp_score"]} {home["abbr"]}',
        })
    return rows


# ── Web UI ───────────────────────────────────────────────────────────────────
TEMPLATE = """
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Matchup Lookup</title>
<style>
  :root{--bg:#0f1419;--panel:#1a2029;--border:#2a3340;--text:#e8ecf1;--muted:#8a95a5;
        --accent:#4fc3f7;--win:#4caf50;--loss:#e57373;--hl:#2d3846;}
  *{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       background:var(--bg);color:var(--text);margin:0;padding:24px}
  .container{max-width:1000px;margin:0 auto}
  h1{margin:0 0 4px;font-size:24px;font-weight:600}
  h2{font-size:17px;font-weight:600;margin:0 0 12px}
  h2 .rec{color:var(--muted);font-size:13px;font-weight:400}
  .sub{color:var(--muted);margin-bottom:22px;font-size:14px}
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;
         padding:18px 20px;margin-bottom:20px}
  form{display:grid;grid-template-columns:1.2fr 1fr 1fr auto;gap:14px;align-items:end}
  @media(max-width:760px){form{grid-template-columns:1fr 1fr}}
  label{display:block;color:var(--muted);font-size:12px;margin-bottom:6px;
        text-transform:uppercase;letter-spacing:.06em}
  input,select{width:100%;padding:10px 12px;background:#0f1419;color:var(--text);
               border:1px solid var(--border);border-radius:6px;font-size:15px}
  input:focus,select:focus{outline:none;border-color:var(--accent)}
  button{padding:10px 18px;background:var(--accent);color:#0f1419;border:0;border-radius:6px;
         font-size:15px;font-weight:600;cursor:pointer;width:100%}
  button:hover{background:#81d4fa}
  .err{color:var(--loss);font-size:14px;margin-top:10px}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:20px}
  @media(max-width:760px){.cols{grid-template-columns:1fr}}
  table{width:100%;border-collapse:collapse;font-size:13.5px}
  th,td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--border);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
  .win{color:var(--win)} .loss{color:var(--loss)}
  .site{color:var(--muted)}
  .note{color:var(--muted);font-size:12.5px;margin-top:6px;line-height:1.5}
</style></head>
<body><div class="container">
  <h1>Matchup Lookup</h1>
  <div class="sub">Last 10 games (date · site · opponent · spread · total · final) for each team, plus the last {{ h2h_n }} head-to-head meetings.</div>

  <div class="panel">
    <form method="post">
      <div>
        <label for="league">League</label>
        <select name="league" id="league">
          {% for k,v in leagues.items() %}
          <option value="{{k}}" {% if league==k %}selected{% endif %}>{{v.label}}</option>
          {% endfor %}
        </select>
      </div>
      <div><label for="away">Away team</label>
        <input name="away" id="away" value="{{ away_in }}" placeholder="e.g. New York or NY"></div>
      <div><label for="home">Home team</label>
        <input name="home" id="home" value="{{ home_in }}" placeholder="e.g. Las Vegas or LV"></div>
      <button type="submit">Look up</button>
    </form>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
  </div>

  {% if away_team %}
  <div class="panel">
    <div style="font-size:18px;font-weight:700;margin-bottom:4px;">
      {{ away_team.display }} <span style="color:var(--muted);font-weight:400;">@</span> {{ home_team.display }}</div>
    <div class="note">Site column is from that team's perspective: <b>H</b> home · <b>A</b> away · <b>N</b> neutral. Spread is from the listed team's view (− = favored).</div>
  </div>

  <div class="cols">
    {% for blk in [away_blk, home_blk] %}
    <div class="panel">
      <h2>{{ blk.team.display }} <span class="rec">— last {{ blk.rows|length }}</span></h2>
      <table>
        <thead><tr><th>Date</th><th>Site</th><th>Opp</th><th>Spread</th><th>Total</th><th>Final</th></tr></thead>
        <tbody>
          {% for r in blk.rows %}
          <tr>
            <td>{{ r.date }}</td>
            <td class="site">{{ r.site }}</td>
            <td>{{ r.opp }}</td>
            <td>{{ r.spread }}</td>
            <td>{{ r.total }}</td>
            <td class="{{ 'win' if r.won else 'loss' }}">{{ r.final }}</td>
          </tr>
          {% endfor %}
          {% if not blk.rows %}<tr><td colspan="6" class="site">No completed games found.</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
    {% endfor %}
  </div>

  <div class="panel">
    <h2>Head-to-head <span class="rec">— last {{ h2h|length }} meeting(s)</span></h2>
    <table>
      <thead><tr><th>Date</th><th>Host</th><th>Spread</th><th>Total</th><th>Final ({{ away_team.abbr }} – {{ home_team.abbr }})</th></tr></thead>
      <tbody>
        {% for r in h2h %}
        <tr><td>{{ r.date }}</td><td class="site">{{ r.host }}</td><td>{{ r.spread }}</td>
            <td>{{ r.total }}</td><td>{{ r.final }}</td></tr>
        {% endfor %}
        {% if not h2h %}<tr><td colspan="5" class="site">No recent head-to-head meetings found (last {{ max_seasons }} seasons).</td></tr>{% endif %}
      </tbody>
    </table>
  </div>

  <div class="panel note">
    Data: ESPN public APIs. Spreads/totals are the closing line ESPN stored (DraftKings / consensus); shown as &ldquo;&mdash;&rdquo; when no line is on file for that game. Last 10 and head-to-head walk back up to {{ max_seasons }} seasons to fill out.
  </div>
  {% endif %}
</div></body></html>
"""


def run_lookup(league, away_in, home_in):
    """Resolve teams + build all tables. Returns a context dict for the template."""
    ctx = {"leagues": LEAGUES, "league": league, "away_in": away_in, "home_in": home_in,
           "h2h_n": H2H_N, "max_seasons": MAX_SEASONS, "error": None,
           "away_team": None, "home_team": None}
    if not (away_in and home_in):
        return ctx
    away = resolve_team(league, away_in)
    home = resolve_team(league, home_in)
    if not away or not home:
        miss = [n for n, t in (("away", away), ("home", home)) if not t]
        ctx["error"] = "Could not resolve the " + " and ".join(miss) + " team — check spelling or use the abbreviation."
        return ctx
    if away["id"] == home["id"]:
        ctx["error"] = "Away and home are the same team."
        return ctx
    ctx["away_team"], ctx["home_team"] = away, home
    ctx["away_blk"] = {"team": away, "rows": build_team_table(league, away)}
    ctx["home_blk"] = {"team": home, "rows": build_team_table(league, home)}
    ctx["h2h"] = build_h2h(league, away, home)
    return ctx


@app.route("/", methods=["GET", "POST"])
def index():
    league = request.values.get("league", DEFAULT_LEAGUE)
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE
    ctx = run_lookup(league, request.values.get("away", "").strip(),
                     request.values.get("home", "").strip())
    return render_template_string(TEMPLATE, **ctx)


# ── Console mode ─────────────────────────────────────────────────────────────
def _print_table(title, rows, h2h=False):
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    if h2h:
        print(f"  {'Date':<9}{'Host':<10}{'Spread':<12}{'Total':<7}Final")
        for r in rows:
            print(f"  {r['date']:<9}{r['host']:<10}{r['spread']:<12}{r['total']:<7}{r['final']}")
    else:
        print(f"  {'Date':<9}{'Site':<5}{'Opp':<6}{'Spread':<8}{'Total':<7}Final")
        for r in rows:
            print(f"  {r['date']:<9}{r['site']:<5}{r['opp']:<6}{str(r['spread']):<8}{r['total']:<7}{r['final']}")


def cli(league, away_in, home_in):
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # render em-dashes on Windows
    except Exception:
        pass
    ctx = run_lookup(league, away_in, home_in)
    if ctx["error"]:
        print("Error:", ctx["error"])
        return 1
    a, h = ctx["away_team"], ctx["home_team"]
    print("=" * 70)
    print(f"  {a['display']} (away)  @  {h['display']} (home)   [{LEAGUES[league]['label']}]")
    print("=" * 70)
    _print_table(f"{a['display']} — last {len(ctx['away_blk']['rows'])}", ctx["away_blk"]["rows"])
    _print_table(f"{h['display']} — last {len(ctx['home_blk']['rows'])}", ctx["home_blk"]["rows"])
    _print_table(f"Head-to-head — last {len(ctx['h2h'])} meeting(s)", ctx["h2h"], h2h=True)
    return 0


def main():
    p = argparse.ArgumentParser(description="Matchup lookup — recent form + head-to-head")
    p.add_argument("--league", default=DEFAULT_LEAGUE, choices=list(LEAGUES))
    p.add_argument("--away")
    p.add_argument("--home")
    p.add_argument("--port", type=int, default=5009)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    if args.away and args.home:
        sys.exit(cli(args.league, args.away, args.home))
    print(f"Serving at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
