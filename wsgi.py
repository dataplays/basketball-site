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
}
TOOL_LABELS = {
    "nba_props_projections": "NBA Player Props — Projections",
    "wnba_props_projections": "WNBA Player Props — Projections",
    "wnba_props_track": "WNBA Props — Tracker / Grading",
}
TOOL_REQUIRES = {
    "wnba_props_track": ["wnba_props_grade.py"],
    "wnba_props_projections": ["wnba_props_grade.py"],
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
    "median_probabilities": ("median", "Median Probabilities"),
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

PAGE_CSS = """
:root{--bg:#0f1923;--card:#1a2634;--border:#2a3a4a;--text:#e8edf2;
--muted:#8899aa;--accent:#ff4444;--blue:#2196f3;--green:#4caf50;}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:var(--bg);color:var(--text);min-height:100vh}
header{background:#0a1218;border-bottom:2px solid var(--accent);padding:18px 24px}
header h1{font-size:1.5em}header h1 span{color:var(--accent)}
.sub{color:var(--muted);font-size:.85em;margin-top:4px}
.container{max-width:980px;margin:0 auto;padding:24px 16px}
.section{font-size:1.1em;font-weight:700;margin:8px 0 14px;padding-bottom:8px;
border-bottom:1px solid var(--border)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
a.card,div.card{display:block;background:var(--card);border:1px solid var(--border);
border-left:4px solid var(--blue);border-radius:8px;padding:18px;color:var(--text);
text-decoration:none;transition:border-color .2s,transform .1s}
a.card:hover{border-color:var(--blue);transform:translateY(-2px)}
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
<title>Basketball Dashboards</title><style>{{ css }}</style></head><body>
<header><h1><span>&#9679;</span> Basketball Dashboards</h1>
<div class=sub>Live projections &amp; tools &middot; updates automatically</div></header>
<div class=container>
  <div class=section>Dashboards &amp; Calculators</div>
  {% if dashboards %}
  <div class=grid>
    {% for prefix,label in dashboards %}
    <a class=card href="/{{ prefix }}/">
      <div class=t>{{ label }}</div>
      <div class=d>/{{ prefix }} &middot; live scores &amp; projections</div>
    </a>
    {% endfor %}
  </div>
  {% else %}<div class=empty>No dashboards found.</div>{% endif %}

  <div class=section style="margin-top:32px">Tools &amp; Reports</div>
  <div class=grid>
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
</div></body></html>"""

TOOLS_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Props Tools</title><style>{{ css }}</style></head><body>
<header><h1><span>&#9679;</span> Props Tools</h1>
<div class=sub><a class=back href="/">&larr; Back to dashboards</a></div></header>
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


@landing.route("/")
def home():
    dash = sorted([(p, DASH_LABELS.get(p, p.upper())) for p in DASHBOARDS],
                  key=lambda x: x[1])
    return render_template_string(
        LANDING_HTML, css=PAGE_CSS, dashboards=dash,
        reports=_reports(), refresh_hour=f"{CBB_REFRESH_HOUR:02d}",
    )


@landing.route("/tools")
def tools_page():
    items = []
    for name in TOOLS:
        missing = [f for f in TOOL_REQUIRES.get(name, []) if not (HERE / f).exists()]
        items.append((name, TOOL_LABELS.get(name, name), missing))
    return render_template_string(TOOLS_HTML, css=PAGE_CSS, tools=items)


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

# ── Mount everything under one WSGI application ───────────────────────────────
application = DispatcherMiddleware(
    landing, {f"/{prefix}": app for prefix, app in DASHBOARDS.items()}
)

if __name__ == "__main__":
    from werkzeug.serving import run_simple
    port = int(os.environ.get("PORT", 8000))
    print(f"Dashboards: {', '.join(sorted(DASHBOARDS)) or 'none'}")
    print(f"Tools:      {', '.join(sorted(TOOLS)) or 'none'}")
    print(f"Serving on http://localhost:{port}")
    run_simple("0.0.0.0", port, application, threaded=True)
