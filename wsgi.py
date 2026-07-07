"""
Combined basketball site — one server, one address, runs 24/7.

Serves a landing page at /, auto-mounts every *_live_projections.py Flask
dashboard under its own path (e.g. /nba, /wnba, /cbb, /wcbb, /nbl, /intl),
exposes a Tools page that runs the command-line props tools on demand, and
automatically regenerates the daily CBB report(s) once a day.

Local test:   python wsgi.py            (http://localhost:8000)
Production:   gunicorn wsgi:application  (host sets the port via $PORT)
"""

import html
import importlib
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (Flask, Response, render_template_string,
                   send_from_directory)
from werkzeug.middleware.dispatcher import DispatcherMiddleware

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BBALL_DATA_DIR", str(HERE / "data")))
DATA_DIR.mkdir(exist_ok=True)
REPORTS_DIR = HERE / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
ET = ZoneInfo("America/New_York")

# Hour (0-23, ET) to auto-refresh the daily CBB report(s). Override with env.
CBB_REFRESH_HOUR = int(os.environ.get("CBB_REFRESH_HOUR", "6"))

# ── Friendly labels ──────────────────────────────────────────────────────────
DASH_LABELS = {
    "nba": "NBA",
    "wnba": "WNBA",
    "cbb": "Men's College (CBB)",
    "wcbb": "Women's College (WCBB)",
    "nbl": "Australian NBL",
    "intl": "International",
    "summer": "NBA Summer League",
    "big3": "BIG3 (3-on-3)",
}
# Signature accent per dashboard (matches each dashboard's own header colour),
# used to colour-code the landing-page cards. Falls back to blue.
DASH_COLORS = {
    "nba": "#ff6d00",
    "wnba": "#c2185b",
    "cbb": "#ff4444",
    "wcbb": "#e040a0",
    "nbl": "#fdcb6e",
    "intl": "#00b894",
    "summer": "#f4a261",
    "big3": "#e8112d",
    "median": "#4fc3f7",
    "news": "#e8730c",
    "injuries": "#e03e3e",
    "prophetx": "#15c39a",
    "pricer": "#a78bfa",
    "matchup": "#26c6da",
    "calc": "#ffce54",
    "spots": "#7e57c2",
    "hitrate": "#26a69a",
    "refs": "#ef5350",
    "wins": "#66bb6a",
    "edges": "#22c55e",
    "vacuum": "#5c6bc0",
}
TOOL_LABELS = {
    "nba_props_projections": "NBA Player Props — Projections",
    "wnba_props_projections": "WNBA Player Props — Projections",
    "nba_props_projections_2": "NBA Player Props 2 — Projections (pre-Jul-1 model rollback)",
    "wnba_props_projections_2": "WNBA Player Props 2 — Projections (pre-Jul-1 model rollback)",
    "wnba_props_track": "WNBA Props — Tracker / Grading",
    "nba_props_track": "NBA Props — Tracker / Grading",
}
TOOL_REQUIRES = {
    "wnba_props_track": ["wnba_props_grade.py"],
    "wnba_props_projections": ["wnba_props_grade.py"],
    "nba_props_track": ["nba_props_grade.py"],
    "nba_props_projections": ["nba_props_grade.py"],
}
# Mounted pages that belong under "Tools & Reports" on the landing page (instead
# of the main Dashboards grid). prefix -> card description. They stay mounted and
# in the nav; this only changes which homepage section their card appears in.
LANDING_TOOLS = {
    "news": "Daily basketball & betting brief",
    "median": "Prop median → probability calculator",
    "injuries": "Injury reports — WNBA, NBA, NCAA Men (merged sources)",
    "prophetx": "Live exchange odds & money offered — ProphetX + Kalshi",
    "pricer": "Fair price for alternate spreads & totals",
    "matchup": "Last 10 games + head-to-head by team",
    "calc": "Odds, devig, Kelly, parlay, hedge & free-bet math",
    "spots": "Rest & schedule spots — B2B, 3-in-4, rest edge",
    "hitrate": "Player prop hit rates — L5/L10/L20, home/away, vs opp",
    "refs": "NBA referee tendencies + tonight's crews",
    "wins": "WNBA season win totals — projected final records",
    "edges": "Model vs market — ranked edges across every league",
    "vacuum": "Usage vacuum — who eats a sat player's minutes & usage",
}


# ── Discover dashboards & tools sitting next to this file ─────────────────────
def discover_dashboards():
    found = {}
    for p in sorted(HERE.glob("*_live_projections.py")):
        prefix = p.name[: -len("_live_projections.py")]
        try:
            mod = importlib.import_module(p.stem)
            app = getattr(mod, "app", None)
            if app is not None:
                found[prefix] = app
        except Exception as e:
            print(f"[warn] could not load dashboard {p.name}: {e}")
    return found


def discover_tools():
    HELPERS = ("_grade",)  # imported by other tools; not runnable on their own
    tools = []
    for p in sorted(HERE.glob("*_props_*.py")):
        if p.stem.endswith(HELPERS):
            continue
        tools.append(p.stem)
    return tools


sys.path.insert(0, str(HERE))
DASHBOARDS = discover_dashboards()
TOOLS = discover_tools()

# Extra Flask dashboards that don't follow the *_live_projections.py naming.
# Map: module name -> (url path, homepage label)
EXTRA_DASHBOARDS = {
    "median_probabilities": ("median", "Median Props"),
    "news_page": ("news", "Court & Cover — Daily Brief"),
    "wnba_injuries": ("injuries", "WNBA Injury Report"),
    "prophetx_live": ("prophetx", "Current Lines and Odds"),
    "spread_total_calculator": ("pricer", "Spread & Total Pricer"),
    "matchup_lookup": ("matchup", "Matchup Lookup"),
    "betting_calculators": ("calc", "Betting Calculators"),
    "situational_angles": ("spots", "Situational Spots"),
    "prop_hitrate": ("hitrate", "Prop Hit-Rate"),
    "referee_tendencies": ("refs", "Referee Tendencies"),
    "wnba_win_projections": ("wins", "WNBA Win Totals"),
    "edges_dashboard": ("edges", "Today's Edges"),
    "usage_vacuum": ("vacuum", "Usage Vacuum"),
}
for _modname, (_prefix, _label) in EXTRA_DASHBOARDS.items():
    try:
        _mod = importlib.import_module(_modname)
        _app = getattr(_mod, "app", None)
        if _app is not None:
            DASHBOARDS[_prefix] = _app
            DASH_LABELS[_prefix] = _label
    except Exception as e:
        print(f"[warn] could not load extra dashboard {_modname}: {e}")

# Extra command-line tools (not matching *_props_*.py) to expose on the Tools page.
EXTRA_TOOLS = {
    "update_all_stats": "Update All Stats \u2014 refresh NBA/WNBA/CBB/WCBB ratings",
}
for _tname, _tlabel in EXTRA_TOOLS.items():
    if (HERE / f"{_tname}.py").exists() and _tname not in TOOLS:
        TOOLS.append(_tname)
        TOOL_LABELS[_tname] = _tlabel

# ── Landing + tools pages ────────────────────────────────────────────────────
landing = Flask(__name__)

# Inline basketball favicon, served at /favicon.svg (and /favicon.ico) so every
# page — including the mounted dashboards — gets a real icon instead of the
# browser's generic globe.
FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<circle cx='16' cy='16' r='15' fill='#ff6d00'/>"
    "<g fill='none' stroke='#1a1008' stroke-width='1.4'>"
    "<circle cx='16' cy='16' r='15'/>"
    "<path d='M1 16h30M16 1v30M5.2 5.2C11 10 11 22 5.2 26.8"
    "M26.8 5.2C21 10 21 22 26.8 26.8'/></g></svg>"
)
# Shared <head> tags injected into every page: favicon + mobile theme colour.
HEAD_EXTRA = (
    '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'
    '<meta name="theme-color" content="#0f1923">'
)

PAGE_CSS = """
:root{--bg:#0f1923;--card:#1a2634;--border:#2a3a4a;--text:#e8edf2;
--muted:#8899aa;--accent:#ff4444;--blue:#2196f3;--green:#4caf50;}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:var(--bg);color:var(--text);min-height:100vh}
header{background:linear-gradient(180deg,#101d28,#0a1218);
border-bottom:2px solid var(--accent);padding:20px 24px}
header h1{font-size:1.5em;letter-spacing:-.01em}header h1 span{color:var(--accent)}
.sub{color:var(--muted);font-size:.85em;margin-top:4px}
.container{max-width:980px;margin:0 auto;padding:24px 16px}
.footer{max-width:980px;margin:8px auto 0;padding:18px 16px 28px;color:var(--muted);
font-size:.8em;border-top:1px solid var(--border);text-align:center}
.footer a{color:var(--blue);text-decoration:none}.footer a:hover{text-decoration:underline}
.section{font-size:1.1em;font-weight:700;margin:8px 0 14px;padding-bottom:8px;
border-bottom:1px solid var(--border)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
a.card,div.card{display:block;background:var(--card);border:1px solid var(--border);
border-left:4px solid var(--blue);border-radius:8px;padding:18px;color:var(--text);
text-decoration:none;transition:border-color .2s,transform .1s}
a.card:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(0,0,0,.38)}
.card .t{font-size:1.15em;font-weight:700}
.card .d{color:var(--muted);font-size:.82em;margin-top:6px}
.tool{background:var(--card);border:1px solid var(--border);border-radius:8px;
padding:16px 18px;margin-bottom:12px;display:flex;justify-content:space-between;
align-items:center;gap:12px;flex-wrap:wrap}
.tool .t{font-weight:700}
.tool .warn{color:var(--accent);font-size:.8em;margin-top:4px}
button{background:var(--blue);color:#fff;border:none;border-radius:6px;
padding:9px 18px;font-size:.95em;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.1)}
button:disabled{background:var(--muted);cursor:wait}
pre{background:#0a1218;border:1px solid var(--border);border-radius:8px;
padding:14px;margin-top:16px;overflow:auto;font-size:.82em;white-space:pre-wrap;
max-height:480px}
.back{color:var(--blue);text-decoration:none;font-size:.9em}
.empty{color:var(--muted);font-style:italic}
"""

LANDING_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=description content="Live basketball projections and betting tools for the NBA, WNBA, college and international leagues — updated automatically.">
{{ head_extra|safe }}
<title>Basketball Dashboards</title><style>{{ css }}</style></head><body>
<header><h1><span>&#9679;</span> Basketball Dashboards</h1>
<div class=sub>Live projections &amp; tools &middot; updates automatically</div></header>
<div class=container>
  <div class=section>Live Projection Sets</div>
  {% if dashboards %}
  <div class=grid>
    {% for prefix,label,color in dashboards %}
    <a class=card style="border-left-color:{{ color }}" href="/{{ prefix }}/">
      <div class=t>{{ label }}</div>
      <div class=d>/{{ prefix }} &middot; live scores &amp; projections</div>
    </a>
    {% endfor %}
  </div>
  {% else %}<div class=empty>No dashboards found.</div>{% endif %}

  <div class=section style="margin-top:32px">Tools &amp; Reports</div>
  <div class=grid>
    {% for prefix,label,color,desc in extras %}
    <a class=card style="border-left-color:{{ color }}" href="/{{ prefix }}/">
      <div class=t>{{ label }}</div>
      <div class=d>{{ desc }}</div>
    </a>
    {% endfor %}
    <a class=card style="border-left-color:var(--green)" href="/tools">
      <div class=t>Props Tools &rarr;</div>
      <div class=d>Run projections &amp; grading on demand</div>
    </a>
    {% if reports %}
      {% for r in reports %}
      <a class=card style="border-left-color:var(--accent)" href="/reports/{{ r }}" target=_blank>
        <div class=t>Daily Report (PDF)</div>
        <div class=d>{{ r }}</div>
      </a>
      {% endfor %}
    {% else %}
      <div class=card style="border-left-color:var(--accent)">
        <div class=t>Daily CBB Reports</div>
        <div class=d>Generate automatically at {{ refresh_hour }}:00 ET</div>
      </div>
    {% endif %}
  </div>
</div>
<footer class=footer>Live data from ESPN, WarrenNolan &amp; Basketball-Reference &middot;
projections update automatically &middot; <a href="/tools">Props Tools</a></footer>
</body></html>"""

TOOLS_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
{{ head_extra|safe }}
<title>Props Tools</title><style>{{ css }}</style></head><body>
<header><h1><span>&#9679;</span> Props Tools</h1>
<div class=sub><a class=back href="/">&larr; Main Menu</a></div></header>
<div class=container>
  {% if tools %}
  {% for name,label,missing in tools %}
  <div class=tool>
    <div>
      <div class=t>{{ label }}</div>
      {% if missing %}<div class=warn>Needs file(s): {{ missing|join(', ') }} &mdash; upload to the server to enable.</div>{% endif %}
    </div>
    <button onclick="run('{{ name }}',this)" {{ 'disabled' if missing else '' }}>Run</button>
  </div>
  {% endfor %}
  <pre id=out>Output will appear here.</pre>
  {% else %}<div class=empty>No tools found.</div>{% endif %}
</div>
<script>
async function run(name,btn){
  const out=document.getElementById('out');
  btn.disabled=true;const old=btn.textContent;btn.textContent='Running...';
  out.textContent='Running '+name+' ...';
  try{
    const r=await fetch('run/'+name,{method:'POST'});
    const t=await r.text();out.textContent=t;
  }catch(e){out.textContent='Error: '+e;}
  btn.disabled=false;btn.textContent=old;
}
</script></body></html>"""


def _reports(limit=6):
    pdfs = sorted(REPORTS_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in pdfs[:limit]]


@landing.route("/favicon.svg")
@landing.route("/favicon.ico")
def favicon():
    return Response(FAVICON_SVG, mimetype="image/svg+xml")


@landing.route("/")
def home():
    dash = sorted([(p, DASH_LABELS.get(p, p.upper()), DASH_COLORS.get(p, "#2196f3"))
                   for p in DASHBOARDS if p not in LANDING_TOOLS], key=lambda x: x[1])
    extras = sorted([(p, DASH_LABELS.get(p, p.upper()), DASH_COLORS.get(p, "#2196f3"),
                      LANDING_TOOLS[p]) for p in DASHBOARDS if p in LANDING_TOOLS],
                    key=lambda x: x[1])
    return render_template_string(
        LANDING_HTML, css=PAGE_CSS, head_extra=HEAD_EXTRA, dashboards=dash,
        extras=extras, reports=_reports(), refresh_hour=f"{CBB_REFRESH_HOUR:02d}",
    )


@landing.route("/tools")
def tools_page():
    items = []
    for name in TOOLS:
        missing = [f for f in TOOL_REQUIRES.get(name, []) if not (HERE / f).exists()]
        items.append((name, TOOL_LABELS.get(name, name), missing))
    return render_template_string(TOOLS_HTML, css=PAGE_CSS, head_extra=HEAD_EXTRA,
                                  tools=items)


NOTFOUND_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
{{ head_extra|safe }}<title>Page not found</title><style>{{ css }}</style></head><body>
<header><h1><span>&#9679;</span> Basketball Dashboards</h1></header>
<div class=container style="text-align:center;padding-top:48px">
  <div style="font-size:3.4em;font-weight:800;color:var(--accent);line-height:1">404</div>
  <p style="color:var(--muted);margin:14px 0 26px">That page doesn't exist.</p>
  <a class=card style="display:inline-block;border-left-color:var(--blue);text-align:left;min-width:220px"
     href="/"><div class=t>&larr; Main Menu</div>
     <div class=d>Back to all dashboards</div></a>
</div></body></html>"""


@landing.errorhandler(404)
def not_found(_e):
    body = render_template_string(NOTFOUND_HTML, css=PAGE_CSS, head_extra=HEAD_EXTRA)
    return body, 404


def _run_script(path, timeout=600):
    env = dict(os.environ, BBALL_DATA_DIR=str(DATA_DIR), CBB_REPORT_DIR=str(REPORTS_DIR))
    try:
        proc = subprocess.run([sys.executable, str(path)], capture_output=True,
                              text=True, timeout=timeout, cwd=str(HERE), env=env)
        return (proc.stdout or ""), (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return "", f"{path.name}: timed out"
    except Exception as e:
        return "", f"{path.name}: {e}"


@landing.route("/run/<name>", methods=["POST"])
def run_tool(name):
    if name not in TOOLS:  # whitelist guard — only known tools may run
        return Response("Unknown tool.", status=404, mimetype="text/plain")
    missing = [f for f in TOOL_REQUIRES.get(name, []) if not (HERE / f).exists()]
    if missing:
        return Response("Cannot run — missing file(s): " + ", ".join(missing),
                        mimetype="text/plain")
    out, err = _run_script(HERE / f"{name}.py", timeout=300)
    body = (out or "") + ("\n--- errors ---\n" + err if err.strip() else "")
    return Response(html.unescape(body) or "(no output)", mimetype="text/plain")


@landing.route("/reports/<path:fname>")
def get_report(fname):
    return send_from_directory(REPORTS_DIR, fname)


# ── Automatic daily refresh: update ratings, then build CBB reports ───────
_refresh_lock = threading.Lock()


def _run_scripts(paths, timeout=600):
    logs = []
    for sc in paths:
        out, err = _run_script(sc, timeout=timeout)
        logs.append(f"=== {sc.name} ===\n{out}{('[stderr] ' + err) if err.strip() else ''}")
    return "\n".join(logs) if logs else "(nothing to run)"


def run_cbb_refresh():
    """Build the daily CBB PDF report(s) only."""
    scripts = sorted(HERE.glob("cbb_*_daily.py"))
    if not scripts:
        return "no cbb_*_daily.py scripts found"
    with _refresh_lock:
        return _run_scripts(scripts)


def run_daily_refresh():
    """Full daily job: refresh all ratings CSVs, then build the CBB reports."""
    scripts = []
    if (HERE / "update_all_stats.py").exists():
        scripts.append(HERE / "update_all_stats.py")
    scripts += sorted(HERE.glob("cbb_*_daily.py"))
    with _refresh_lock:
        return _run_scripts(scripts)


def _seconds_until_next_run():
    now = datetime.now(ET)
    nxt = now.replace(hour=CBB_REFRESH_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _scheduler_loop():
    # On first boot, build a CBB report if none exists yet (light; no full scrape).
    if not _reports():
        print("[daily-refresh] no report yet — generating now...")
        try:
            run_cbb_refresh()
        except Exception as e:
            print("[daily-refresh] initial run error:", e)
    while True:
        wait = _seconds_until_next_run()
        print(f"[daily-refresh] next run in {wait/3600:.1f}h (daily {CBB_REFRESH_HOUR:02d}:00 ET)")
        time.sleep(wait)
        print("[daily-refresh] updating ratings + building reports...")
        try:
            run_daily_refresh()
        except Exception as e:
            print("[daily-refresh] error:", e)


def start_scheduler():
    if os.environ.get("CBB_SCHEDULER_DISABLED") == "1":
        return
    threading.Thread(target=_scheduler_loop, name="daily-refresh", daemon=True).start()


@landing.route("/refresh-cbb", methods=["POST"])
def refresh_cbb_now():
    return Response(run_cbb_refresh() or "(no output)", mimetype="text/plain")


start_scheduler()

# ── Shared chrome injected into every dashboard page ──────────────────────────
# A slim cross-dashboard nav bar, a "last updated" footer, and a loading
# indicator are injected at the WSGI layer so all dashboards stay in sync
# without editing each one. The nav's active tab is derived from the mount path.
# Top nav bar: just the live game dashboards. News / Median / Injuries are reached
# from the landing page's "Tools & Reports" section (and the Tools link below).
NAV_SHORT = {"nba": "NBA", "wnba": "WNBA", "cbb": "CBB", "wcbb": "WCBB",
             "nbl": "NBL", "intl": "Intl", "big3": "BIG3", "prophetx": "Lines"}
NAV_ORDER = ["nba", "wnba", "cbb", "wcbb", "nbl", "intl", "big3", "prophetx"]

INJECT_CSS = (
    ".bb-nav{display:flex;gap:4px;overflow-x:auto;background:#0a1218;"
    "border-bottom:1px solid #2a3a4a;padding:7px 12px;white-space:nowrap;"
    "scrollbar-width:none;-ms-overflow-style:none}"
    ".bb-nav::-webkit-scrollbar{height:0}"
    ".bb-nav a{color:#8899aa;text-decoration:none;font-size:13px;font-weight:600;"
    "padding:5px 12px;border-radius:6px;flex:0 0 auto;transition:background .15s,color .15s}"
    ".bb-nav a:hover{background:rgba(255,255,255,.07);color:#e8edf2}"
    ".bb-nav a.active{box-shadow:0 1px 6px rgba(0,0,0,.35)}"
    ".bb-foot{max-width:1100px;margin:22px auto 0;padding:16px 16px 30px;"
    "border-top:1px solid #2a3a4a;color:#8899aa;font-size:12px;text-align:center;line-height:1.8}"
    ".bb-foot a{color:#2196f3;text-decoration:none}.bb-foot a:hover{text-decoration:underline}"
    ".bb-dot{color:#4caf50}"
    "@keyframes bb-spin{to{transform:rotate(360deg)}}"
    ".bb-ind{position:fixed;right:14px;bottom:14px;display:flex;align-items:center;gap:8px;"
    "background:#1a2634;border:1px solid #2a3a4a;color:#e8edf2;padding:8px 14px;border-radius:22px;"
    "font-size:12px;font-weight:600;box-shadow:0 6px 18px rgba(0,0,0,.45);opacity:0;"
    "transform:translateY(10px);transition:opacity .2s,transform .2s;pointer-events:none;z-index:60}"
    "body.bb-loading .bb-ind{opacity:1;transform:translateY(0)}"
    ".bb-spin{width:12px;height:12px;border:2px solid rgba(255,255,255,.25);"
    "border-top-color:#fff;border-radius:50%;animation:bb-spin .7s linear infinite}"
    # Polished, consistent empty state (overrides each dashboard's .no-games).
    ".no-games{border:1px dashed #2a3a4a;border-radius:10px;"
    "background:rgba(255,255,255,.015);padding:30px 20px;color:#8899aa;"
    "text-align:center;font-style:normal}"
    ".no-games::before{content:'\\1F3C0';display:block;font-size:26px;"
    "margin-bottom:8px;opacity:.75}"
)

INJECT_FOOT = (
    '<div class="bb-ind"><span class="bb-spin"></span>Updating…</div>'
    '<footer class="bb-foot"><span id="bb-updated">&nbsp;</span> &middot; '
    '<a href="/">Main Menu</a><br>Live data from ESPN, WarrenNolan &amp; Basketball-Reference</footer>'
    "<script>(function(){"
    "var fmt=function(){try{return new Date().toLocaleTimeString([],"
    "{hour:'2-digit',minute:'2-digit',second:'2-digit'});}catch(e){return new Date().toLocaleTimeString();}};"
    "var setU=function(t){var e=document.getElementById('bb-updated');"
    "if(e)e.innerHTML='<span class=\"bb-dot\">&#9679;</span> Last updated '+t;};"
    "setU(fmt());"
    "var of=window.fetch;"
    "if(of){window.fetch=function(){var a=arguments,"
    "u=(a[0]&&a[0].url)?a[0].url:(''+a[0]),g=u.indexOf('api/games')>=0;"
    "if(g)document.body.classList.add('bb-loading');"
    "return of.apply(this,a).then(function(r){if(g){document.body.classList.remove('bb-loading');setU(fmt());}return r;},"
    "function(e){if(g)document.body.classList.remove('bb-loading');throw e;});};}"
    "})();</script>"
)


def _nav_html(active):
    links = ['<a href="/">&#8962; Menu</a>']
    for pfx in NAV_ORDER:
        if pfx not in DASHBOARDS:
            continue
        label = NAV_SHORT.get(pfx, pfx.upper())
        if pfx == active:
            color = DASH_COLORS.get(pfx, "#2196f3")
            links.append(f'<a href="/{pfx}/" class="active" '
                         f'style="background:{color};color:#fff">{label}</a>')
        else:
            links.append(f'<a href="/{pfx}/">{label}</a>')
    links.append('<a href="/tools">Tools</a>')
    return '<nav class="bb-nav">' + "".join(links) + "</nav>"


class _Injector:
    """Buffers a mounted app's HTML response and injects the shared nav bar,
    footer, and loading indicator. Non-HTML responses (the JSON refresh
    endpoint, redirects) pass through untouched."""

    def __init__(self, app, prefix):
        self.app = app
        self._css = "<style>" + INJECT_CSS + "</style>"
        self._nav = _nav_html(prefix)

    def __call__(self, environ, start_response):
        buf, cap = [], {}

        def _capture(status, headers, exc_info=None):
            cap["status"], cap["headers"], cap["exc"] = status, headers, exc_info
            return buf.append

        result = self.app(environ, _capture)
        try:
            for chunk in result:
                buf.append(chunk)
        finally:
            if hasattr(result, "close"):
                result.close()

        status = cap.get("status", "500 Internal Server Error")
        headers = cap.get("headers", [])
        ctype = next((v for k, v in headers if k.lower() == "content-type"), "")
        body = b"".join(buf)

        if status.startswith("200") and "text/html" in ctype.lower() and b"</body>" in body:
            doc = body.decode("utf-8", "replace")
            if "</head>" in doc:
                doc = doc.replace("</head>", self._css + "</head>", 1)
            if "</header>" in doc:
                doc = doc.replace("</header>", "</header>" + self._nav, 1)
            elif "<body>" in doc:
                doc = doc.replace("<body>", "<body>" + self._nav, 1)
            doc = doc.replace("</body>", INJECT_FOOT + "</body>", 1)
            body = doc.encode("utf-8")

        out = [(k, v) for k, v in headers if k.lower() != "content-length"]
        out.append(("Content-Length", str(len(body))))
        start_response(status, out, cap.get("exc"))
        return [body]


# ── Mount everything under one WSGI application ───────────────────────────────
application = DispatcherMiddleware(
    landing,
    {f"/{prefix}": _Injector(app, prefix) for prefix, app in DASHBOARDS.items()},
)

if __name__ == "__main__":
    from werkzeug.serving import run_simple
    port = int(os.environ.get("PORT", 8000))
    print(f"Dashboards: {', '.join(sorted(DASHBOARDS)) or 'none'}")
    print(f"Tools:      {', '.join(sorted(TOOLS)) or 'none'}")
    print(f"Serving on http://localhost:{port}")
    run_simple("0.0.0.0", port, application, threaded=True)
